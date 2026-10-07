"""接码控制器与配置装配：把 provider 包装成 AuthFlow 的 add-phone 回调。

无模块级可变状态；共享的 `_SMS_VERIFY_LOCK` 从门面读取（见 providers.py docstring）。
"""
from __future__ import annotations

from typing import Callable, Optional

from services.sms.constants import (
    OPENAI_SMS_COUNTRIES,
    SMS_COUNTRY_NAMES_CN,
    SMS_DEFAULT_COUNTRY,
    SMS_DEFAULT_SERVICE,
    SMS_PROVIDERS,
)
from services.sms.parsing import _safe_bool, _safe_float, _safe_int, country_label
from services.sms.providers import (
    BaseSmsProvider,
    SmsActivateProvider,
    SmsActivation,
    _FacadeProxy,
)

_facade = _FacadeProxy()


def create_sms_provider(provider_key: str, config: dict) -> BaseSmsProvider:
    """从配置创建 provider 实例。

    ``provider_key`` 取 ``smsbower`` / ``herosms``；配置字段见
    ``api/config.py`` 里的 ``sms_*`` 系列。
    """
    key = (provider_key or "").lower().strip().replace("_", "")
    meta = SMS_PROVIDERS.get(key)
    if meta is None:
        raise RuntimeError(f"未知接码服务: {provider_key}")

    api_key = str(config.get("sms_api_key") or "").strip()
    if not api_key:
        raise RuntimeError(f"{meta['label']} 未配置 API Key")

    return SmsActivateProvider(
        api_key=api_key,
        base_url=meta["base_url"],
        default_service=str(config.get("sms_service") or "").strip() or SMS_DEFAULT_SERVICE,
        default_country=str(config.get("sms_country") or "").strip() or SMS_DEFAULT_COUNTRY,
        max_price=_safe_float(config.get("sms_max_price"), -1),
        fixed_price=_safe_float(config.get("sms_fixed_price"), -1),
        proxy=(str(config.get("sms_proxy") or config.get("proxy") or "")).strip() or None,
        reuse_phone_to_max=_safe_bool(config.get("sms_reuse_phone"), False),
        phone_success_max=max(0, _safe_int(config.get("sms_phone_success_max"), 3)),
    )


class PhoneCallbackController:
    """把接码 provider 包装成两阶段回调，注入 ``AuthFlow`` 的 add-phone 流程。"""

    def __init__(
        self,
        provider_key: str,
        config: dict,
        *,
        service: str = "openai",
        country: str = "",
        log_fn: Optional[Callable[[str], None]] = None,
        auto_select_country: bool = False,
    ):
        self.provider_key = provider_key
        self.config = dict(config or {})
        self.service = service
        self.country = country
        self.log = log_fn or _facade.logger.info
        self.auto_select_country = bool(auto_select_country)
        self.provider: Optional[BaseSmsProvider] = None
        self.activation: Optional[SmsActivation] = None
        self.completed = False
        self._verify_lock_acquired = False
        self._warned_off_whitelist = False

    def _ensure_provider(self) -> BaseSmsProvider:
        if self.provider is None:
            self.provider = create_sms_provider(self.provider_key, self.config)
        return self.provider

    def get_phone(self) -> str:
        """阶段 1：租手机号（返回带 + 的 E.164）。"""
        provider = self._ensure_provider()
        # 同号复用锁，防止两个注册任务并发抢同一份缓存
        if isinstance(provider, SmsActivateProvider) and not self._verify_lock_acquired:
            _facade._SMS_VERIFY_LOCK.acquire()
            self._verify_lock_acquired = True

        candidates = self._resolve_country_candidates(provider)
        preview = ",".join(
            f"{c}({SMS_COUNTRY_NAMES_CN.get(c, '?')})" for c in candidates[:5]
        )
        self.log(
            f"准备租号: provider={self.provider_key} service={self.service} "
            f"候选={preview}{' …' if len(candidates) > 5 else ''}"
        )

        try:
            self.activation = provider.get_number(
                service=self.service,
                country=candidates[0],
                country_candidates=candidates,
            )
        except Exception:
            self._release_lock()
            raise

        reused = bool((self.activation.metadata or {}).get("reused"))
        used_country = self.activation.country or candidates[0]
        self.log(
            f"已租到号码{'（复用）' if reused else ''}: {self.activation.phone_number} "
            f"国家={country_label(used_country)} "
            f"(activation_id={self.activation.activation_id})"
        )
        # 白名单外的号段，OpenAI 常把验证改走 WhatsApp，接码平台就永远等不到短信。
        # 这时"发送成功但收不到码"看起来像平台的问题，其实是选错了国家。
        if used_country not in OPENAI_SMS_COUNTRIES and not self._warned_off_whitelist:
            self._warned_off_whitelist = True
            whitelist = "、".join(country_label(c) for c in sorted(OPENAI_SMS_COUNTRIES))
            self.log(
                f"提醒: {country_label(used_country)} 不在 OpenAI 纯短信白名单（{whitelist}）；"
                "这些号段 OpenAI 可能改用 WhatsApp 发码，会出现"
                "「发送成功但一直等不到短信」（白名单内的号也只是概率更高，不保证收得到）"
            )
        return self.activation.phone_number

    def _resolve_country_candidates(self, provider: BaseSmsProvider) -> list[str]:
        allowed_raw = str(self.config.get("sms_allowed_countries") or "").strip()
        allowed = [c.strip() for c in allowed_raw.replace(";", ",").split(",") if c.strip()]

        if not (self.auto_select_country and isinstance(provider, SmsActivateProvider)):
            return [self.country] if self.country else [SMS_DEFAULT_COUNTRY]

        if allowed:
            self.log(f"自动选号: 从勾选的 {len(allowed)} 个国家按价格升序依次尝试")
            try:
                rows = provider.get_top_countries(service=self.service)
            except Exception as exc:
                self.log(f"排名查询失败（{exc}），按勾选的原始顺序尝试")
                return list(allowed)
            ranked = [str(r["country"]) for r in rows if str(r.get("country") or "") in allowed]
            candidates = ranked + [c for c in allowed if c not in ranked]
            self.log(f"候选顺序: {','.join(candidates)}")
            return candidates or [SMS_DEFAULT_COUNTRY]

        self.log("自动选号: 未指定允许国家，按全平台价格 + 库存挑最优")
        try:
            best = provider.get_best_country(
                service=self.service,
                min_stock=_safe_int(self.config.get("sms_auto_min_stock"), 20),
                max_price=_safe_float(self.config.get("sms_auto_max_price"), 0),
                strict_whitelist=_safe_bool(self.config.get("sms_strict_whitelist"), False),
            )
        except Exception as exc:
            self.log(f"国家智能选择失败（{exc}），使用默认国家")
            best = ""
        if best:
            in_whitelist = best in OPENAI_SMS_COUNTRIES
            self.log(
                f"自动选择国家: {country_label(best)} "
                f"[{'OpenAI SMS 白名单' if in_whitelist else '非白名单，可能走 WhatsApp'}]"
            )
            return [best]

        self.log("未找到满足条件的国家，使用默认国家")
        return [self.country] if self.country else [SMS_DEFAULT_COUNTRY]

    def get_code(self, timeout: int = 180) -> str:
        """阶段 2：等待短信验证码。"""
        if not self.activation:
            raise RuntimeError("尚未租号，无法等待验证码")
        provider = self._ensure_provider()
        self.log(
            f"等待短信验证码…(activation_id={self.activation.activation_id} timeout={timeout}s)"
        )
        code = provider.get_code(self.activation.activation_id, timeout=timeout)
        if code:
            self.log(f"收到短信验证码: {code}")
            if getattr(provider, "auto_report_success_on_code", True):
                self.report_success()
        else:
            self.log(f"未收到短信验证码: activation_id={self.activation.activation_id}")
        return code

    def report_success(self) -> None:
        if self.activation and self.provider and not self.completed:
            try:
                self.provider.report_success(self.activation.activation_id)
            except Exception as exc:
                _facade.logger.warning("上报号码成功失败: %s", exc)
            self.completed = True
            self.log(f"号码已标记完成: activation_id={self.activation.activation_id}")
        self._release_lock()

    def mark_code_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_code_failed(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def mark_send_succeeded(self) -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_send_succeeded(self.activation.activation_id)
            except Exception:
                pass

    def mark_send_failed(self, reason: str = "") -> None:
        if self.activation and self.provider:
            try:
                self.provider.mark_send_failed(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def stop_reuse(self, reason: str = "") -> None:
        if self.activation and self.provider:
            try:
                self.provider.stop_reuse(self.activation.activation_id, reason=reason)
            except Exception:
                pass

    def cleanup(self) -> None:
        """流程结束（成功或失败）调用：释放未完成的号并解锁。"""
        if self.activation and not self.completed and self.provider:
            try:
                self.provider.cancel(self.activation.activation_id)
                self.log(f"已释放未使用号码: activation_id={self.activation.activation_id}")
            except Exception:
                pass
            self.activation = None
        self._release_lock()

    def _release_lock(self) -> None:
        if self._verify_lock_acquired:
            try:
                _facade._SMS_VERIFY_LOCK.release()
            except RuntimeError:
                pass
            self._verify_lock_acquired = False


def resolve_sms_settings(extra_config: Optional[dict] = None) -> dict:
    """把全局配置里的 ``sms_*`` 项和本次任务的覆盖合并成一份接码配置。"""
    from core.config_store import config_store

    settings = {
        key: value
        for key, value in (config_store.get_all() or {}).items()
        if key.startswith("sms_")
    }
    for key, value in (extra_config or {}).items():
        if key.startswith("sms_") and value not in (None, ""):
            settings[key] = value
    return settings


def build_phone_callback(
    settings: dict,
    *,
    log_fn: Optional[Callable[[str], None]] = None,
    proxy: Optional[str] = None,
) -> Optional[PhoneCallbackController]:
    """按配置构造接码控制器；未启用或缺 API Key 时返回 None。

    返回 None 意味着注册链路命中 add-phone 时会回退到手工号码路径
    （``OPENAI_PHONE_NUMBER`` 系列），行为跟没接接码时一致。
    """
    settings = dict(settings or {})
    if not _safe_bool(settings.get("sms_enabled"), False):
        return None

    if not str(settings.get("sms_api_key") or "").strip():
        _facade.logger.warning("已启用接码但未配置 sms_api_key，跳过接码")
        return None

    if proxy and not str(settings.get("sms_proxy") or "").strip():
        settings["sms_proxy"] = proxy

    # 服务码在租号和查国家排名两处都要用，必须同一个值：平台按服务码分库存，
    # 拿 "openai" 这种人类可读名去查排名只会得到空表。OpenAI 对应的是 dr。
    try:
        return PhoneCallbackController(
            provider_key=str(settings.get("sms_provider") or "smsbower"),
            config=settings,
            service=str(settings.get("sms_service") or "").strip() or SMS_DEFAULT_SERVICE,
            country=str(settings.get("sms_country") or "").strip() or SMS_DEFAULT_COUNTRY,
            log_fn=log_fn,
            auto_select_country=_safe_bool(settings.get("sms_auto_country"), False),
        )
    except Exception as exc:
        _facade.logger.warning("创建接码控制器失败: %s", exc)
        return None
