"""接码 provider 抽象与 sms-activate 协议系实现（SmsBower / HeroSMS 共用）。

⚠️ 三个模块级可变状态（`_SMS_CACHE` / `_SMS_CACHE_LOCK` / `_SMS_VERIFY_LOCK`）
**刻意不在此文件定义**，而是从门面 `services.sms_service` 读取 —— 见门面 docstring。
本文件通过 `_facade` 访问它们，保证「测试重置的那一份」与「实现使用的那一份」是同一个。
"""
from __future__ import annotations

import json
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from types import ModuleType
from typing import Optional

import requests

from services.sms.constants import (
    OPENAI_SMS_COUNTRIES,
    SMS_DEFAULT_COUNTRY,
    SMS_DEFAULT_SERVICE,
    SMS_PHONE_LIFETIME,
    SMS_PROVIDERS,
    SMS_STATUS_CANCELED,  # noqa: F401  （供同包内其它模块/审计引用）
)
from services.sms.parsing import (
    _hash_secret,
    _make_sms_candidate,
    _parse_sms_status_text,
    _safe_float,
    _safe_int,
    _status_token,
    country_label,
)


class _FacadeProxy:
    """延迟解析门面模块 `services.sms_service`。

    门面 import 本包、本包又需要读门面的三个状态名（见门面 docstring），
    构成循环 import。模块级 `from services import sms_service` 会在包 `__init__`
    加载期炸掉，所以改用「运行时经 sys.modules 取门面」的代理 ——
    所有 provider 方法都在门面加载完成后才被调用，取到的一定是同一个模块对象。
    """

    @property
    def _module(self) -> ModuleType:
        return sys.modules["services.sms_service"]

    def __getattr__(self, name: str):
        return getattr(self._module, name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._module, name, value)


_facade = _FacadeProxy()

# 这些名字刻意不在本文件持有独立副本：
#   * `_SMS_CACHE` 会被重新赋值 → 经 `_FacadeProxy` 动态读写门面那份。
#   * `_cache_file` 会被测试 patch 到门面上 → 方法里改为 `_facade._cache_file()`。
# 模块级 `__getattr__`（PEP 562）让 `providers._cache_file` / `providers._SMS_CACHE`
# 在**读取**时也转发到门面，满足「同一对象」断言。
_FORWARDED_TO_FACADE = frozenset({"_cache_file", "_SMS_CACHE"})


def __getattr__(name: str):
    if name in _FORWARDED_TO_FACADE:
        return getattr(_facade, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# 两个锁永不重新赋值 → 直接别名到门面那份即可（满足「同一对象」的测试）。
_SMS_CACHE_LOCK = _facade._SMS_CACHE_LOCK
_SMS_VERIFY_LOCK = _facade._SMS_VERIFY_LOCK


@dataclass
class SmsActivation:
    """一次手机号租用的句柄。"""

    activation_id: str
    phone_number: str  # E.164 格式，带 + 前缀
    country: str = ""
    metadata: dict = field(default_factory=dict)


class BaseSmsProvider(ABC):
    """接码 provider 抽象基类。"""

    auto_report_success_on_code = True  # True = 收到码即报成功；False = 等业务侧确认

    @abstractmethod
    def get_number(
        self,
        *,
        service: str,
        country: str = "",
        country_candidates: Optional[list[str]] = None,
    ) -> SmsActivation:
        ...

    @abstractmethod
    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        ...

    @abstractmethod
    def cancel(self, activation_id: str) -> bool:
        ...

    def get_balance(self) -> float:
        """查询余额（货币随平台）。"""
        raise NotImplementedError

    def report_success(self, activation_id: str) -> bool:
        """业务侧验证通过后调用，平台据此结算并允许复用。"""
        return True

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧收到码但 validate 失败 → 记下这个码，别再拿它去验。"""

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        """业务侧拒绝该手机号（add-phone/send 返错）→ 停止复用并退款。"""

    def stop_reuse(self, activation_id: str, reason: str = "") -> None:
        """号已经被业务侧占用（注册出账号了）→ 不再复用，但也不退款。"""

    def mark_send_succeeded(self, activation_id: str) -> None:
        """业务侧已成功触发短信发送（add-phone/send 200）。"""


class SmsActivateProvider(BaseSmsProvider):
    """sms-activate 协议系 provider（SmsBower / HeroSMS 共用）。"""

    DEFAULT_BASE_URL = SMS_PROVIDERS["smsbower"]["base_url"]
    auto_report_success_on_code = False  # 等业务侧确认才报成功，便于号码复用

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "",
        default_service: str = SMS_DEFAULT_SERVICE,
        default_country: str = SMS_DEFAULT_COUNTRY,
        max_price: float = -1,
        fixed_price: float = -1,
        proxy: Optional[str] = None,
        reuse_phone_to_max: bool = True,
        phone_success_max: int = 3,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "").strip() or self.DEFAULT_BASE_URL
        self.default_service = str(default_service or SMS_DEFAULT_SERVICE).strip()
        self.default_country = str(default_country or SMS_DEFAULT_COUNTRY).strip()
        self.max_price = float(max_price or -1)
        self.fixed_price = float(fixed_price or -1)
        self._proxy = (proxy or "").strip() or None
        self._proxies = {"http": self._proxy, "https": self._proxy} if self._proxy else None
        self.reuse_phone_to_max = bool(reuse_phone_to_max)
        self.phone_success_max = max(0, int(phone_success_max or 0))
        self.last_code_result: Optional[dict] = None
        self.current_activation: Optional[SmsActivation] = None
        self._warned_low_bid = False

    # ── HTTP ──

    def _request(self, params: dict, *, needs_key: bool = True, timeout: int = 30):
        payload = dict(params)
        if needs_key:
            payload["api_key"] = self.api_key
        resp = requests.get(self.base_url, params=payload, timeout=timeout, proxies=self._proxies)
        resp.raise_for_status()
        return resp

    # ── 余额 / 价格 / 国家 ──

    def get_balance(self) -> float:
        text = self._request({"action": "getBalance"}).text.strip()
        if text.startswith("ACCESS_BALANCE:"):
            return float(text.split(":", 1)[1])
        raise RuntimeError(f"查询余额失败: {text}")

    def get_prices(self, service: Optional[str] = None, country=None) -> dict:
        params = {"action": "getPrices"}
        if service:
            params["service"] = service
        if country not in (None, ""):
            params["country"] = country
        data = self._request(params).json()
        if isinstance(data, dict):
            return data
        raise RuntimeError("getPrices 返回结构异常")

    def get_top_countries(self, service: Optional[str] = None) -> list[dict]:
        """按价格升序、库存降序返回国家列表。"""
        service_code = str(service or self.default_service or SMS_DEFAULT_SERVICE).strip()
        for action in ("getTopCountriesByServiceRank", "getTopCountriesByService"):
            try:
                data = self._request({"action": action, "service": service_code}).json()
                rows = self._parse_top_countries(data)
                if rows:
                    rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
                    return rows
            except Exception:
                continue

        # 排名 API 不可用时退回 getPrices 自己算
        try:
            prices = self.get_prices(service=service_code)
        except Exception:
            return []
        rows = []
        for country_id, services in prices.items():
            if not isinstance(services, dict):
                continue
            entry = services.get(service_code)
            if not isinstance(entry, dict):
                continue
            price = _safe_float(entry.get("cost") or entry.get("price"), -1)
            count = _safe_int(entry.get("count") or entry.get("qty") or entry.get("available"), 0)
            if price >= 0 and count > 0:
                rows.append({"country": str(country_id), "price": price, "count": count})
        rows.sort(key=lambda r: (r.get("price") or 999, -(r.get("count") or 0)))
        return rows

    @staticmethod
    def _parse_top_countries(data) -> list[dict]:
        items = data
        if isinstance(data, dict):
            items = data.get("data") or data.get("result") or data.get("response") or data

        rows: list[dict] = []
        if isinstance(items, dict):
            for key, value in items.items():
                if not isinstance(value, dict):
                    continue
                try:
                    country_id = str(int(key))
                except (TypeError, ValueError):
                    continue
                price = _safe_float(
                    value.get("price") or value.get("cost") or value.get("retail_price"), -1
                )
                count = _safe_int(value.get("count") or value.get("qty") or value.get("available"), 0)
                if price >= 0:
                    rows.append({"country": country_id, "price": price, "count": count})
        elif isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                country_id = (
                    item.get("country")
                    or item.get("countryId")
                    or item.get("country_id")
                    or item.get("id")
                )
                if country_id is None:
                    continue
                price = _safe_float(item.get("price") or item.get("cost"), -1)
                count = _safe_int(item.get("count") or item.get("qty") or item.get("available"), 0)
                if price >= 0:
                    rows.append({"country": str(country_id), "price": price, "count": count})
        return rows

    def get_best_country(
        self,
        service: Optional[str] = None,
        *,
        min_stock: int = 20,
        max_price: float = 0,
        strict_whitelist: bool = False,
        allowed_countries: Optional[list[str]] = None,
    ) -> Optional[str]:
        """自动选最优国家。

        ``allowed_countries`` 优先级最高（从这些国家里挑最便宜且库存足的）；
        其次 ``strict_whitelist`` 只从 ``OPENAI_SMS_COUNTRIES`` 里选；都没设
        就全平台自由选，由调用方承担"OpenAI 让用 WhatsApp"的风险。
        """
        try:
            rows = self.get_top_countries(service=service)
        except Exception as exc:
            _facade.logger.warning("查询国家排名失败: %s", exc)
            return None
        if not rows:
            return None

        allowed_set: Optional[set[str]] = None
        if allowed_countries:
            allowed_set = {str(c).strip() for c in allowed_countries if str(c).strip()}

        def _pick(stock_threshold: int) -> Optional[str]:
            for row in rows:
                cid = str(row.get("country") or "")
                if allowed_set is not None:
                    if cid not in allowed_set:
                        continue
                elif strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    continue
                price = row.get("price") or 0
                count = row.get("count") or 0
                if count < stock_threshold:
                    continue
                if max_price > 0 and price > max_price:
                    continue
                if not strict_whitelist and cid not in OPENAI_SMS_COUNTRIES:
                    _facade.logger.warning(
                        "自动选中非 OpenAI-SMS 白名单国家 country=%s price=%s"
                        "（OpenAI 可能让此号走 WhatsApp 验证，收不到短信）",
                        cid,
                        price,
                    )
                return cid
            return None

        return _pick(min_stock) or _pick(1)

    # ── 号码复用缓存 ──

    def _cache_identity(self, service: str, country: str) -> dict:
        return {
            "api_key_hash": _hash_secret(self.api_key),
            "service": str(service),
            "country": str(country),
        }

    def _load_cache(self, service: str, country: str) -> Optional[dict]:
        cache = _facade._SMS_CACHE
        if cache is None:
            path = _facade._cache_file()
            if not path.exists():
                return None
            try:
                cache = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None

        identity = self._cache_identity(service, country)
        if any(str(cache.get(k) or "") != str(v) for k, v in identity.items()):
            return None

        elapsed = time.time() - float(cache.get("acquired_at") or 0)
        if elapsed >= SMS_PHONE_LIFETIME or cache.get("reuse_stopped"):
            self._clear_cache()
            return None
        if self.phone_success_max > 0 and int(cache.get("use_count") or 0) >= self.phone_success_max:
            cache["reuse_stopped"] = True
            cache["stop_reason"] = f"已达单号复用上限 ({self.phone_success_max})"
            self._save_cache(cache)
            return None

        cache["used_codes"] = set(cache.get("used_codes") or [])
        _facade._SMS_CACHE = cache
        return cache

    def _save_cache(self, cache: Optional[dict]) -> None:
        _facade._SMS_CACHE = cache
        path = _facade._cache_file()
        if cache is None:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            return
        serializable = dict(cache)
        serializable["used_codes"] = sorted(serializable.get("used_codes") or [])
        path.write_text(json.dumps(serializable, ensure_ascii=False), encoding="utf-8")

    def _clear_cache(self) -> None:
        self._save_cache(None)

    # ── 租号 ──

    def _request_number(self, action: str, service: str, country: str) -> dict:
        """单次调用 getNumberV2 或 getNumber，失败原样抛给调用方的双重循环。"""
        params = {"action": action, "service": service, "country": country}
        if self.fixed_price > 0:
            # HeroSMS 用 fixedPrice 开关；SmsBower 要求 minPrice == maxPrice 才算固定价
            if "hero-sms.com" in self.base_url:
                params["maxPrice"] = self.fixed_price
                params["fixedPrice"] = "true"
            else:
                params["minPrice"] = self.fixed_price
                params["maxPrice"] = self.fixed_price
        elif self.max_price > 0:
            params["maxPrice"] = self.max_price

        _facade.logger.info(
            "接码 %s: service=%s country=%s maxPrice=%s",
            action,
            service,
            country,
            params.get("maxPrice", "未设置"),
        )
        resp = self._request(params)
        resp_text = resp.text.strip()

        if action == "getNumberV2":
            try:
                data = resp.json()
            except ValueError:
                data = None
            if isinstance(data, dict) and data.get("activationId"):
                return data
            raise RuntimeError(resp_text[:200] or "空响应")

        if resp_text.startswith("ACCESS_NUMBER:"):
            parts = resp_text.split(":", 2)
            if len(parts) == 3:
                return {
                    "activationId": parts[1],
                    "phoneNumber": parts[2],
                    "countryPhoneCode": "",
                }
        raise RuntimeError(resp_text[:200] or "空响应")

    @staticmethod
    def _format_phone(info: dict) -> str:
        raw = str(info.get("phoneNumber") or "").strip()
        code = str(info.get("countryPhoneCode") or "").strip()
        if raw.startswith("+"):
            return raw
        if code and raw.startswith(code):
            return f"+{raw}"
        if code:
            return f"+{code}{raw}"
        return f"+{raw}"

    def get_number(
        self,
        *,
        service: str,
        country: str = "",
        country_candidates: Optional[list[str]] = None,
    ) -> SmsActivation:
        """租号，按候选国家顺序依次尝试，每个国家先试 V2 再退 V1。"""
        service_code = str(self.default_service or service or SMS_DEFAULT_SERVICE).strip()
        if not country_candidates:
            country_candidates = [
                str(country or self.default_country or SMS_DEFAULT_COUNTRY).strip()
            ]

        with _facade._SMS_VERIFY_LOCK, _facade._SMS_CACHE_LOCK:
            cache = (
                self._load_cache(service_code, country_candidates[0])
                if self.reuse_phone_to_max
                else None
            )
            if cache and str(cache.get("country") or "") in country_candidates:
                activation = SmsActivation(
                    activation_id=str(cache["activation_id"]),
                    phone_number=str(cache["phone_number"]),
                    country=str(cache.get("country") or country_candidates[0]),
                    metadata={"reused": True, "use_count": int(cache.get("use_count") or 0)},
                )
                self.current_activation = activation
                return activation

            failures: list[str] = []
            last_exc: Optional[Exception] = None
            for cid in country_candidates:
                cid = str(cid).strip()
                if not cid:
                    continue
                for action in ("getNumberV2", "getNumber"):
                    try:
                        info = self._request_number(action, service_code, cid)
                    except Exception as exc:
                        failures.append(f"{cid}: {action}={str(exc)[:120]}")
                        last_exc = exc
                        continue

                    activation_id = str(info.get("activationId") or "")
                    phone = self._format_phone(info)
                    if not activation_id or not phone.strip("+"):
                        failures.append(f"{cid}: {action} 返回信息不完整")
                        continue

                    self._save_cache(
                        {
                            **self._cache_identity(service_code, cid),
                            "country": cid,
                            "activation_id": activation_id,
                            "phone_number": phone,
                            "acquired_at": time.time(),
                            "use_count": 0,
                            "used_codes": set(),
                            "reuse_stopped": False,
                            "stop_reason": "",
                        }
                    )
                    activation = SmsActivation(
                        activation_id=activation_id,
                        phone_number=phone,
                        country=cid,
                        metadata={"reused": False},
                    )
                    self.current_activation = activation
                    if len(country_candidates) > 1:
                        _facade.logger.info("在国家 %s 租到号 %s (action=%s)", cid, phone, action)
                    return activation

            detail = " | ".join(failures) if failures else "未知"
            if "NO_NUMBERS" in detail:
                self._explain_no_numbers(service_code, country_candidates)
            raise RuntimeError(
                f"依次尝试 {len(country_candidates)} 个候选国家全部失败: {detail}"
            ) from last_exc

    def _explain_no_numbers(self, service: str, countries: list[str]) -> None:
        """NO_NUMBERS 多半不是没货，是出价太低 —— 把挂牌价查出来摆在日志里。

        平台按出价撮合，出价压在挂牌价以下时既租不到号，偶尔撮合成功的也是被反复
        回收的号段。只查一次价，别让每轮换号都多打一次接口。
        """
        bid = self.fixed_price if self.fixed_price > 0 else self.max_price
        if bid <= 0 or self._warned_low_bid:
            return
        self._warned_low_bid = True
        for country in countries:
            try:
                prices = self.get_prices(service, country) or {}
                market = _safe_float(
                    ((prices.get(str(country)) or {}).get(service) or {}).get("cost"), 0
                )
            except Exception:
                continue
            if market > 0 and bid < market:
                _facade.logger.warning(
                    "租不到号多半是出价太低: %s 的 %s 挂牌价 %.3f，当前出价只有 %.3f"
                    "（约挂牌价的 %d%%）；把接码设置里的固定价/最高价提到 %.2f 以上再试",
                    country_label(str(country)),
                    service,
                    market,
                    bid,
                    round(bid / market * 100),
                    market,
                )
                return

    # ── 等码 / 状态查询 ──

    def get_status(self, activation_id: str) -> dict:
        return _parse_sms_status_text(
            self._request({"action": "getStatus", "id": activation_id}).text
        )

    def get_status_v2(self, activation_id: str) -> dict:
        # 少了 type=sms 平台只回 {"error":"Bad type parameter"}，而这个错解析下来
        # 长得和"还在等码"一模一样 —— 等于白等一整个窗口还以为一切正常。
        resp = self._request({"action": "getStatusV2", "id": activation_id, "type": "sms"})
        raw_text = (resp.text or "").strip()
        try:
            data = resp.json()
        except ValueError:
            return _parse_sms_status_text(raw_text)

        if isinstance(data, str):
            return _parse_sms_status_text(data)
        # raw 只放平台自己写在字段里的那句话（error / status_description），
        # 响应体原文不进这个 dict —— 它最后是要被打进任务日志的。
        if not isinstance(data, dict):
            return {"status": "unknown", "raw": ""}

        if data.get("error"):
            return {"status": "unknown", "raw": _status_token(data.get("error"))}

        raw_status = data.get("status")
        if isinstance(raw_status, str):
            parsed = _parse_sms_status_text(raw_status)
            if parsed.get("status") != "unknown":
                return parsed

        candidate = _make_sms_candidate(activation_id, "getStatusV2.code", data.get("code"))
        if candidate:
            return candidate
        for channel in ("sms", "call"):
            item = data.get(channel)
            if isinstance(item, dict):
                candidate = _make_sms_candidate(
                    activation_id, f"getStatusV2.{channel}", item.get("code")
                )
                if candidate:
                    return candidate

        description = str(data.get("status_description") or "").strip()
        if _safe_int(raw_status, -1) == SMS_STATUS_CANCELED:
            return {"status": "cancel", "raw": description}
        return {"status": "wait_code", "raw": description}

    def wait_for_code(
        self,
        activation_id: str,
        *,
        timeout: int = 80,
        poll: int = 3,
    ) -> Optional[dict]:
        """等短信验证码：只轮询接码平台，不去催发。

        码是 OpenAI 那边一次性发出来的，催发既换不来第二条短信，还会把当前这条
        challenge 弄失效。超时返回 None，由上层 cancel 换号。
        """
        start = time.time()
        deadline = start + timeout
        with _facade._SMS_CACHE_LOCK:
            used_codes = set((_facade._SMS_CACHE or {}).get("used_codes") or [])

        last_seen = ""
        next_report = start + 30

        while time.time() < deadline:
            seen_this_round = []
            for source in ("v2", "v1"):
                try:
                    result = (
                        self.get_status_v2(activation_id)
                        if source == "v2"
                        else self.get_status(activation_id)
                    )
                except Exception as exc:
                    _facade.logger.debug("查询接码状态 %s 失败: %s", source, exc)
                    continue
                seen_this_round.append(self._describe_status(source, result))
                last_seen = " | ".join(seen_this_round)
                if result.get("status") == "cancel":
                    _facade.logger.info("平台报告号码已取消: %s", last_seen)
                    return None
                if result.get("status") == "ok":
                    code = str(result.get("code") or "")
                    if code and code not in used_codes:
                        return {
                            "status": "ok",
                            "code": code,
                            "sms_key": result.get("sms_key") or "",
                        }

            # 「一直没码」到底是平台没收到短信、还是号被取消了，光看超时看不出来
            if time.time() >= next_report:
                _facade.logger.info(
                    "等码中 (已等 %ds/%ds)：平台状态 %s",
                    int(time.time() - start),
                    timeout,
                    last_seen or "(未取到)",
                )
                next_report = time.time() + 30

            time.sleep(poll)

        _facade.logger.warning(
            "等码 %ds 结束，平台始终没收到短信：最后状态 %s", timeout, last_seen or "(未取到)"
        )
        return None

    @staticmethod
    def _describe_status(source: str, result: dict) -> str:
        """只报状态和平台给的那句说明；短信正文不进日志。"""
        status = str(result.get("status") or "")
        detail = str(result.get("raw") or "").strip()
        parts = [f"{source}={status}"]
        if detail:
            parts.append(detail[:80])
        return " ".join(parts)

    def get_code(self, activation_id: str, *, timeout: int = 180) -> str:
        # 传进来的 timeout 就是真 timeout：号码有 20 分钟生命周期，但 OpenAI 那边的
        # phone-otp challenge 等不了那么久，超时就该让上层换号。
        candidate = self.wait_for_code(activation_id, timeout=timeout)
        self.last_code_result = candidate
        return str((candidate or {}).get("code") or "")

    # ── 状态报告 ──

    def cancel(self, activation_id: str) -> bool:
        ok = False
        try:
            resp = self._request({"action": "cancelActivation", "id": activation_id})
            ok = resp.status_code == 204 or "ACCESS_CANCEL" in resp.text
        except Exception:
            ok = False
        if not ok:
            try:
                resp = self._request({"action": "setStatus", "id": activation_id, "status": 8})
                ok = "ACCESS_CANCEL" in resp.text
            except Exception:
                ok = False
        with _facade._SMS_CACHE_LOCK:
            cache = _facade._SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                self._clear_cache()
        return ok

    def report_success(self, activation_id: str) -> bool:
        with _facade._SMS_CACHE_LOCK:
            cache = _facade._SMS_CACHE
            should_finish = False
            should_clear = False
            if cache and str(cache.get("activation_id")) == str(activation_id):
                cache["use_count"] = int(cache.get("use_count") or 0) + 1
                if self.last_code_result and self.last_code_result.get("code"):
                    used = set(cache.get("used_codes") or [])
                    used.add(self.last_code_result["code"])
                    cache["used_codes"] = used
                remaining = SMS_PHONE_LIFETIME - (time.time() - float(cache.get("acquired_at") or 0))
                if not self.reuse_phone_to_max:
                    should_finish = should_clear = True
                    cache["reuse_stopped"] = True
                elif self.phone_success_max > 0 and int(cache["use_count"]) >= self.phone_success_max:
                    should_finish = True
                    cache["reuse_stopped"] = True
                elif remaining <= 30:
                    should_finish = should_clear = True
                    cache["reuse_stopped"] = True
                self._save_cache(cache)
                if should_clear:
                    self._clear_cache()
            holds_cache = bool(cache and str(cache.get("activation_id")) == str(activation_id))

        if not (should_finish or not holds_cache):
            return True
        try:
            resp = self._request({"action": "finishActivation", "id": activation_id})
            return resp.status_code in (200, 204) or "ACCESS" in resp.text
        except Exception:
            try:
                resp = self._request({"action": "setStatus", "id": activation_id, "status": 6})
                return "ACCESS" in resp.text
            except Exception:
                return False

    def mark_code_failed(self, activation_id: str, reason: str = "") -> None:
        with _facade._SMS_CACHE_LOCK:
            cache = _facade._SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                if self.last_code_result and self.last_code_result.get("code"):
                    used = set(cache.get("used_codes") or [])
                    used.add(self.last_code_result["code"])
                    cache["used_codes"] = used
                self._save_cache(cache)

    def mark_send_succeeded(self, activation_id: str) -> None:
        try:
            self._request({"action": "setStatus", "id": activation_id, "status": 1})
        except Exception:
            pass

    def stop_reuse(self, activation_id: str, reason: str = "") -> None:
        with _facade._SMS_CACHE_LOCK:
            cache = _facade._SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                cache["reuse_stopped"] = True
                cache["stop_reason"] = reason or "号码已被业务侧占用"
                self._save_cache(cache)
                self._clear_cache()

    def mark_send_failed(self, activation_id: str, reason: str = "") -> None:
        # 业务侧拒了这个号 → cancel 退款，号根本没用上，不能白花钱
        cancelled = False
        try:
            resp = self._request({"action": "setStatus", "id": activation_id, "status": 8})
            cancelled = "ACCESS_CANCEL" in resp.text or resp.status_code in (200, 204)
        except Exception:
            cancelled = False
        _facade.logger.info(
            "号 activation_id=%s 退款%s（原因: %s）",
            activation_id,
            "成功" if cancelled else "失败",
            (reason or "未知原因")[:80],
        )
        with _facade._SMS_CACHE_LOCK:
            cache = _facade._SMS_CACHE
            if cache and str(cache.get("activation_id")) == str(activation_id):
                cache["reuse_stopped"] = True
                cache["stop_reason"] = reason or "号码被业务侧拒绝"
                self._save_cache(cache)
                self._clear_cache()
