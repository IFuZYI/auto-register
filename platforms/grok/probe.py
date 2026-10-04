"""账号测活：直连 cli-chat-proxy 发一次最小请求。

出处：reference/grok/grokRegister-cpa/sso_to_auth_json.py:648-728

要点：
- 新 token 常有瞬时 403（permission-denied），需 warmup + 重试
- 403 但含 denied 关键字 → 可重试；其他状态码直接返回
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from core.proxy_utils import build_requests_proxy_config

from .constants import (
    CPA_GROK_HEADERS,
    CPA_PROBE_MODEL,
    CPA_PROBE_URL,
    probe_retries,
    probe_warmup_seconds,
)

LogFn = Optional[Callable[[str], None]]


def probe_token(
    access_token: str,
    email: str = "",
    sub: str = "",
    executor: Any = None,
    model: str = "",
    warmup: Optional[bool] = None,
    retries: Optional[int] = None,
    proxy: str = "",
    log: LogFn = None,
) -> tuple[Optional[int], str]:
    """测活，返回 (HTTP 状态码, 响应摘要)。

    executor: 已配置代理的 ProtocolExecutor；为 None 时用 curl_cffi 直连。
    proxy: executor 为空时用它的代理（curl_cffi 支持）。

    注：`proxy` 参数曾经缺失 —— 而 `GrokPlatform.check_valid` 一直以
    `probe_token(..., proxy=...)` 调用它，于是每次都抛 TypeError，
    又被 `except Exception: return False` 吞掉：**所有 Grok 账号都被判成无效**。
    实测：一个 SSO/OAuth 都正常的新账号，测活报「无效」。
    """
    access = str(access_token or "").strip()
    if not access:
        return None, "missing access_token"

    model = model or CPA_PROBE_MODEL
    if warmup is None:
        warmup = probe_warmup_seconds() > 0
    if retries is None:
        retries = probe_retries()

    if warmup:
        wait = probe_warmup_seconds()
        if wait > 0:
            time.sleep(wait)

    sub = str(sub or "").strip() or f"probe-{int(time.time() * 1000)}"
    email = str(email or "").strip()
    last_code: Optional[int] = None
    last_summary = ""

    for attempt in range(max(1, int(retries or 1))):
        if attempt > 0:
            time.sleep(4)

        headers = dict(CPA_GROK_HEADERS)
        headers["Authorization"] = f"Bearer {access}"
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        rid = f"{int(time.time() * 1_000_000)}"
        headers["x-grok-session-id"] = f"probe-{sub}"
        headers["x-grok-conv-id"] = f"probe-{sub}"
        headers["x-grok-req-id"] = rid
        headers["x-grok-turn-idx"] = "1"
        headers["x-grok-agent-id"] = f"agent-{rid[:8]}"
        headers["x-grok-model-override"] = model
        if email:
            headers["x-email"] = email
        if sub:
            headers["x-userid"] = sub

        payload = {
            "model": model,
            "store": False,
            "stream": False,
            "max_output_tokens": 16,
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "ok"}],
                }
            ],
        }

        try:
            if executor is not None:
                resp = executor.post(CPA_PROBE_URL, headers=headers, json=payload)
                code = int(resp.status_code or 0)
                summary = str(resp.text or "").replace("\n", " ").strip()[:300]
            else:
                from curl_cffi import requests as curl_requests

                # 走代理：x.ai 会按 IP 判风险，直连（本机出口）常被 CF 拦。
                # curl_cffi 需要显式传 proxies；空串表示直连。
                req_kwargs: dict[str, Any] = {
                    "headers": headers,
                    "json": payload,
                    "impersonate": "chrome",
                    "timeout": 30,
                }
                if proxy:
                    # 走共享 helper 而不是手搓 dict：它会做 normalize_proxy_url
                    # （socks5→socks5h，避免本地 DNS 泄漏；兼容老式
                    # host:port:user:pass 写法）。手搓会让 executor 分支与
                    # 直连分支对同一个代理串给出不同解释（评审发现）。
                    req_kwargs["proxies"] = build_requests_proxy_config(proxy)
                resp = curl_requests.post(CPA_PROBE_URL, **req_kwargs)
                code = int(resp.status_code)
                summary = str(resp.text or "").replace("\n", " ").strip()[:300]
        except Exception as exc:
            last_code, last_summary = None, str(exc)[:300]
            continue

        last_code, last_summary = code, summary
        if code == 200:
            if log:
                log(f"[Grok] 测活成功 (HTTP 200)")
            return code, summary

        low = summary.lower()
        if code == 403 and (
            "permission-denied" in low or "chat endpoint is denied" in low or "denied" in low
        ):
            if log:
                log(f"[Grok] 测活瞬时 403，重试 {attempt + 1}/{retries}")
            continue
        return code, summary

    return last_code, last_summary


__all__ = ["probe_token"]
