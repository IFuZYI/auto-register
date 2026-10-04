"""TraceMixin：HTTP 追踪、cookie 头构造、导航/公共头、client_auth_session_dump。

从 3541 行的 auth_flow.py 拆出（纯搬家，方法体逐字节不变）。
这些方法只依赖 `self.session` / `self._fingerprint` / `logger` 等实例状态，
不引入新的 __init__ 假设。
"""
from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class TraceMixin:
    def _build_chatgpt_cookie_header(self) -> str:
        """
        导出当前会话中的 chatgpt.com 相关 cookie。

        说明：
        - `/backend-api/payments/checkout` 的 modern/custom 入口不仅依赖
          `__Secure-next-auth.session-token`，还会校验若干同域 cookie
          （如 csrf / oai-sc / Cloudflare 相关 cookie 等）。
        - 因此这里不能只回传 session_token，需要尽量保留当前会话里已经拿到的
          `chatgpt.com` 域 cookie 集合。
        """
        cookie_pairs: list[tuple[str, str]] = []
        seen: set[str] = set()

        try:
            jar_iter = list(self.session.cookies)
        except Exception:
            jar_iter = []

        for cookie in jar_iter:
            try:
                name = (getattr(cookie, "name", "") or "").strip()
                value = getattr(cookie, "value", "") or ""
                domain = (getattr(cookie, "domain", "") or "").strip().lower()
            except Exception:
                continue
            if not name or not value:
                continue
            if domain and "chatgpt.com" not in domain:
                continue
            if name in seen:
                continue
            seen.add(name)
            cookie_pairs.append((name, value))

        # 兜底补齐关键 cookie，避免某些 cookiejar 迭代行为差异导致遗漏
        critical_names = [
            "__Secure-next-auth.session-token",
            "__Host-next-auth.csrf-token",
            "__Secure-next-auth.callback-url",
            "oai-did",
            "oai-sc",
            "cf_clearance",
            "__cf_bm",
            "_cfuvid",
            "__cflb",
            "__stripe_mid",
            "__stripe_sid",
            "oai-client-auth-info",
            "oai-gn",
            "oai-nav-state",
            "oai-hlib",
            "_account_is_fedramp",
            "oai_consent_analytics",
            "oai_consent_marketing",
            "oai-allow-ne",
            "_ga",
            "_ga_9SHBSK2D9J",
            "_gcl_au",
            "_fbp",
            "_puid",
            "_dd_s",
            "g_state",
        ]
        for name in critical_names:
            if name in seen:
                continue
            value = self._get_cookie_value_by_name(name)
            if value:
                seen.add(name)
                cookie_pairs.append((name, value))

        return "; ".join(f"{name}={value}" for name, value in cookie_pairs if name and value)
        if self._trace_dump_enabled:
            try:
                os.makedirs("outputs", exist_ok=True)
                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
                self._trace_dump_path = os.path.join("outputs", f"auth_trace_{ts}_{os.getpid()}.jsonl")
                logger.info(f"HTTP 明文抓包已启用: {self._trace_dump_path}")
            except Exception as e:
                logger.warning(f"初始化 HTTP 抓包文件失败: {e}")
                self._trace_dump_enabled = False


    def _trace_http(self, step: str, resp, extra_request: dict | None = None):
        """可选 HTTP 细粒度追踪（用于协议调试）"""
        if (not self._http_trace_enabled and not self._trace_dump_enabled) or resp is None:
            return
        try:
            req = getattr(resp, "request", None)
            method = getattr(req, "method", "") if req else ""
            req_url = getattr(req, "url", "") if req else ""
            req_body = ""
            req_headers = {}
            if req is not None:
                raw_req_body = getattr(req, "body", None)
                if raw_req_body is None:
                    raw_req_body = getattr(req, "content", None)
                if raw_req_body is None:
                    raw_req_body = getattr(req, "data", None)
                if isinstance(raw_req_body, bytes):
                    req_body = raw_req_body.decode("utf-8", errors="replace")
                elif raw_req_body is not None:
                    req_body = str(raw_req_body)
                try:
                    req_headers = dict(getattr(req, "headers", {}) or {})
                except Exception:
                    req_headers = {}

            # 手动补充请求信息（curl_cffi 某些场景 request.body/headers 为空）
            if isinstance(extra_request, dict):
                if not method:
                    method = str(extra_request.get("method", "") or "")
                if not req_url:
                    req_url = str(extra_request.get("url", "") or "")
                if not req_body:
                    maybe_body = extra_request.get("body", "")
                    if isinstance(maybe_body, bytes):
                        req_body = maybe_body.decode("utf-8", errors="replace")
                    else:
                        req_body = str(maybe_body or "")
                extra_headers = extra_request.get("headers", {})
                if isinstance(extra_headers, dict):
                    merged = dict(req_headers or {})
                    merged.update(extra_headers)
                    req_headers = merged

            status = getattr(resp, "status_code", "N/A")
            final_url = str(getattr(resp, "url", "") or "")
            req_cookie = (req_headers.get("Cookie", "") or "")
            location = (resp.headers.get("Location", "") or "")[:180]
            req_id = (resp.headers.get("x-request-id", "") or "")[:120]
            ctype = (resp.headers.get("Content-Type", "") or "")[:120]
            # 尽量保留完整 Set-Cookie（某些关键 cookie 可能在后续片段）
            set_cookie_list: list[str] = []
            try:
                get_list = getattr(resp.headers, "get_list", None) or getattr(resp.headers, "getlist", None)
                if callable(get_list):
                    vals = get_list("Set-Cookie")
                    if isinstance(vals, list):
                        set_cookie_list = [str(x) for x in vals if x]
            except Exception:
                set_cookie_list = []
            if not set_cookie_list:
                one = (resp.headers.get("Set-Cookie", "") or "")
                if one:
                    set_cookie_list = [one]
            set_cookie_raw = " || ".join(set_cookie_list)
            set_cookie = set_cookie_raw[:260]
            req_headers_lc = {(str(k).lower()): v for k, v in (req_headers or {}).items()}

            if self._http_trace_enabled:
                # 控制台这行只放路由信息：响应体原文一律走 AUTH_TRACE_DUMP 的 jsonl，
                # 倒进日志既刷屏又会把 cookie/验证码之类的东西一起摊开。
                logger.info(
                    "[HTTP TRACE] %s | %s %s -> %s | url=%s | location=%s | req_id=%s | ctype=%s | set_cookie=%s",
                    step,
                    method,
                    req_url[:180],
                    status,
                    final_url[:180],
                    location,
                    req_id,
                    ctype,
                    set_cookie,
                )
                if self._trace_include_cookie and req_cookie:
                    logger.info("[HTTP TRACE] %s | req_cookie=%s", step, req_cookie[:360])

            raw_text = resp.text or ""

            # 明文 HTTP 抓包落盘（jsonl）
            if self._trace_dump_enabled and self._trace_dump_path:
                try:
                    include_req_cookie = self._env_flag("AUTH_TRACE_INCLUDE_REQ_COOKIE", "0")
                    record = {
                        "ts": datetime.utcnow().isoformat() + "Z",
                        "step": step,
                        "request": {
                            "method": method,
                            "url": req_url,
                            "body": req_body[:120000],
                            "headers": {
                                "Content-Type": (req_headers_lc.get("content-type", "") or "")[:240],
                                "Accept": (req_headers_lc.get("accept", "") or "")[:240],
                                "Referer": (req_headers_lc.get("referer", "") or "")[:500],
                                "Origin": (req_headers_lc.get("origin", "") or "")[:120],
                                **(
                                    {
                                        "Cookie": (req_headers_lc.get("cookie", "") or "")[:6000],
                                    }
                                    if include_req_cookie
                                    else {}
                                ),
                            },
                        },
                        "response": {
                            "status_code": status,
                            "url": final_url,
                            "location": resp.headers.get("Location", ""),
                            "x_request_id": resp.headers.get("x-request-id", ""),
                            "content_type": resp.headers.get("Content-Type", ""),
                            "set_cookie": set_cookie_raw,
                            "set_cookie_list": set_cookie_list,
                            "body": raw_text[:120000],
                        },
                    }
                    if self._trace_include_cookie and req_cookie:
                        record["request"]["headers"]["Cookie"] = req_cookie[:8000]
                    with open(self._trace_dump_path, "a", encoding="utf-8") as fw:
                        fw.write(json.dumps(record, ensure_ascii=False) + "\n")
                except Exception as e:
                    logger.debug(f"HTTP 抓包写入失败: {e}")
        except Exception as e:
            logger.debug(f"HTTP trace 输出失败: {e}")


    @staticmethod
    def _walk_collect_str_fields(obj: Any, wanted_keys: set[str], out: dict[str, str], depth: int = 0, max_depth: int = 6):
        """递归收集目标字段（仅字符串值）。"""
        if depth > max_depth or obj is None:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                kk = (str(k) or "").strip().lower()
                if kk in wanted_keys and isinstance(v, str) and v.strip():
                    out[kk] = v.strip()
                TraceMixin._walk_collect_str_fields(v, wanted_keys, out, depth + 1, max_depth)
        elif isinstance(obj, list):
            for it in obj:
                TraceMixin._walk_collect_str_fields(it, wanted_keys, out, depth + 1, max_depth)


    def fetch_client_auth_session_dump(self, stage: str = "") -> dict:
        """
        尝试读取 auth.openai 的 client_auth_session_dump：
        - 可能包含 session_id / client_auth_session 的额外状态
        - 若出现 verifier/refresh 相关字段，自动注入当前流程
        """
        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Accept"] = "application/json"
        try:
            resp = self.session.get(
                "https://auth.openai.com/api/accounts/client_auth_session_dump",
                headers=headers,
                timeout=30,
            )
            self._trace_http(f"client_auth_session_dump_{stage or 'default'}", resp)
        except Exception as e:
            logger.debug(f"client_auth_session_dump 请求异常({stage}): {e}")
            return {}

        if resp.status_code != 200:
            logger.info(
                "client_auth_session_dump(%s) 非 200: %s",
                stage or "default",
                resp.status_code,
            )
            return {}

        try:
            data = resp.json()
        except Exception:
            logger.warning(f"client_auth_session_dump({stage}) JSON 解析失败")
            return {}

        if not isinstance(data, dict):
            return {}

        self._client_auth_session_dump = data
        cas = data.get("client_auth_session", {}) if isinstance(data.get("client_auth_session"), dict) else {}

        sid = (data.get("session_id", "") or "").strip() or (cas.get("session_id", "") or "").strip()
        if sid:
            self._client_auth_session_id = sid

        # 同步 OAuth client_id（若 dump 给出更准确值）
        dump_client_id = (cas.get("openai_client_id", "") or data.get("openai_client_id", "") or "").strip()
        if dump_client_id:
            self._oauth_client_id = dump_client_id

        wanted = {"refresh_token", "oauth_refresh_token", "access_token", "id_token"}
        found: dict[str, str] = {}
        self._walk_collect_str_fields(data, wanted, found)

        # token 候选（极少见，但若有直接收下）
        refresh = (found.get("refresh_token", "") or found.get("oauth_refresh_token", "")).strip()
        if refresh:
            self.result.refresh_token = refresh
        acc = (found.get("access_token", "") or "").strip()
        if acc:
            self.result.access_token = acc
        idt = (found.get("id_token", "") or "").strip()
        if idt:
            self.result.id_token = idt

        logger.debug(
            "client_auth_session_dump(%s) 成功: top_keys=%s cas_keys=%s session_id=%s refresh=%s",
            stage or "default",
            list(data.keys())[:12],
            list(cas.keys())[:18] if isinstance(cas, dict) else [],
            (self._client_auth_session_id[:24] if self._client_auth_session_id else ""),
            "有" if self.result.refresh_token else "无",
        )
        return data


    def _get_cookie_value_by_name(self, name: str) -> str:
        """按名称取 cookie，优先 chatgpt.com，避免跨域同名冲突。"""
        try:
            target = (name or "").strip().lower()
            cookies = list(self.session.cookies)
            matches = []
            for c in cookies:
                cname = (getattr(c, "name", "") or "").strip().lower()
                if cname != target:
                    continue
                value = (getattr(c, "value", "") or "").strip()
                domain = (getattr(c, "domain", "") or "").strip().lower()
                if value:
                    matches.append((domain, value))
            for domain, value in matches:
                if "chatgpt.com" in domain:
                    return value
            if matches:
                return matches[0][1]
        except Exception:
            pass
        try:
            return (self.session.cookies.get(name, domain=".chatgpt.com") or "").strip()
        except Exception:
            return ""
        return ""


    @staticmethod
    def _datadog_trace_headers() -> dict:
        """生成 Datadog RUM 追踪头（对齐 gptfree-register 格式）。"""
        tid = f"{random.getrandbits(64):016x}"
        sid = str(random.getrandbits(63))
        pid = str(random.getrandbits(63))
        ts_hex = f"{int(time.time()):08x}"
        return {
            "traceparent": f"00-0000000000000000{tid}-{random.getrandbits(64):016x}-01",
            "x-datadog-trace-id": sid,
            "x-datadog-parent-id": pid,
            "x-datadog-sampling-priority": "1",
            "x-datadog-origin": "rum",
            "x-datadog-tags": f"_dd.p.id={tid},_dd.p.tid={ts_hex}00000000,_dd.b.sr=1",
        }


    def _common_headers(self, referer: str = "https://chatgpt.com/") -> dict:
        """
        构造通用请求头。

        关键点：
        - Origin 必须与 Referer 同源（尤其 auth.openai.com 的状态机接口），
          否则容易触发 invalid_state / 风控分支。
        - auth.openai.com 侧不带 oai-device-id：抓包里浏览器在该域一次都没发过
          这个头，设备标识走 oai-did cookie 和 sentinel body 里的 id。
        - 全请求注入 Datadog trace 头，避免 OTP silent-drop。
        """
        origin = "https://chatgpt.com"
        try:
            parsed = urlparse(referer or "")
            if parsed.scheme and parsed.netloc:
                origin = f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            pass

        fp = self._fingerprint
        headers = {
            "Accept": "application/json",
            "Referer": referer,
            "Origin": origin,
            "User-Agent": self._ua,
            "Accept-Language": fp["lang_full"],
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "priority": "u=1, i",
        }
        if fp.get("sec_ch_ua"):
            headers["sec-ch-ua"] = fp["sec_ch_ua"]
            headers["sec-ch-ua-mobile"] = fp.get("sec_ch_ua_mobile") or "?0"
            headers["sec-ch-ua-platform"] = fp["sec_ch_ua_platform"]
            # Client Hints 全套（仅 Chromium 有值，其他浏览器为空串不下发）
            if fp.get("sec_ch_ua_full_version_list"):
                headers["sec-ch-ua-full-version-list"] = fp["sec_ch_ua_full_version_list"]
            if fp.get("sec_ch_ua_arch"):
                headers["sec-ch-ua-arch"] = fp["sec_ch_ua_arch"]
            if fp.get("sec_ch_ua_bitness"):
                headers["sec-ch-ua-bitness"] = fp["sec_ch_ua_bitness"]
            if fp.get("sec_ch_ua_model"):
                headers["sec-ch-ua-model"] = fp["sec_ch_ua_model"]
            if fp.get("sec_ch_ua_platform_version"):
                headers["sec-ch-ua-platform-version"] = fp["sec_ch_ua_platform_version"]

        headers.update(self._datadog_trace_headers())
        return headers


    def _navigation_headers(self) -> dict:
        """文档导航请求（地址栏直达那种）的头，含 client hints。

        和 _common_headers 的区别只在 Sec-Fetch-* 那组：那边是 XHR（empty/cors/
        same-origin），这里是整页导航（document/navigate/none + user + UIR）。
        **client hints 两边必须一致**，都从 self._fingerprint 取：Chrome 指纹发
        全套，Safari/Firefox 指纹 sec_ch_ua 为空串、一个都不发——这正是真实浏览器
        的行为。旧 warmup 手搓头漏了这段，导致 Chrome UA 裸奔，实测 403 率 4/5，
        补齐后 5/5 通过（详见 warmup docstring）。
        """
        fp = self._fingerprint
        headers = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "image/avif,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": fp["lang_full"],
            "Accept-Encoding": "gzip, deflate, br, zstd",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "none",
            "sec-fetch-user": "?1",
            "upgrade-insecure-requests": "1",
            "priority": "u=0, i",
            "User-Agent": self._ua,
        }
        if fp.get("sec_ch_ua"):
            headers["sec-ch-ua"] = fp["sec_ch_ua"]
            headers["sec-ch-ua-mobile"] = fp.get("sec_ch_ua_mobile") or "?0"
            headers["sec-ch-ua-platform"] = fp["sec_ch_ua_platform"]
            for key, name in (
                ("sec_ch_ua_full_version_list", "sec-ch-ua-full-version-list"),
                ("sec_ch_ua_arch", "sec-ch-ua-arch"),
                ("sec_ch_ua_bitness", "sec-ch-ua-bitness"),
                ("sec_ch_ua_model", "sec-ch-ua-model"),
                ("sec_ch_ua_platform_version", "sec-ch-ua-platform-version"),
            ):
                if fp.get(key):
                    headers[name] = fp[key]
        return headers
