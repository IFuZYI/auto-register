"""Grok token 刷新与有效性检测。

三条实测事实（2026-10-04 逐项验证）：

1. **RT 会轮换**：x.ai 的 OAuth refresh grant 每次成功都返回**新的
   refresh_token**，旧值随即失效。实测：浏览器换到新 RT 后，新 RT 走
   refresh grant 返回 200 并再次轮换；本地存的旧 RT 全部
   `invalid_grant`（因为上游已经轮换过一轮，本地没跟上）。
2. **协议 device flow 被 CF 挡**：device/verify 与 device/approve 纯 HTTP
   返回 403，只有真实浏览器里完成授权才走得通（浏览器 flow 实测成功，
   拿到全新 AT + 轮换 RT）。
3. **错误码要分类**（口径对齐参考实现 grok2api 的
   `IsPermanentCredentialRefreshErrorCode`）：
   - `permanent`：`invalid_grant` 等 —— 账号需要重新授权（走登录协议刷新）；
   - `configuration`：`invalid_client` 等 —— 网关配置问题，**不该标账号失效**；
   - `retryable`：`rate_limited` / 5xx / 网络异常 —— 瞬时，应重试；
   - `unknown`：无法归类，保守处理（不误杀）。

两条刷新路径：
- `refresh_via_grant`：RT → 新 token（纯协议快路径，用于日常检测/刷新）；
- `refresh_via_device_flow`：SSO → 登录协议重新获取（浏览器完成授权，
  被 CF 挡住协议路径时的兜底）。
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .constants import CLIENT_ID, DEFAULT_UA, TOKEN_URL

LogFn = Optional[Callable[[str], None]]

#: 永久失效的错误码（对齐 grok2api 的 IsPermanentCredentialRefreshErrorCode）。
_PERMANENT_CODES = frozenset({
    "invalid_grant",
    "invalid_refresh_token",
    "refresh_token_invalid",
    "refresh_token_expired",
    "refresh_token_revoked",
    "refresh_token_reused",
    "refresh_token_reuse",
    "token_reused",
    "token_reuse_detected",
    "expired_token",
    "revoked_token",
    "token_revoked",
    "missing_refresh_token",
})

#: 网关配置类错误（不该标账号失效 —— grok2api 同款口径）。
_CONFIGURATION_CODES = frozenset({
    "invalid_client",
    "unauthorized_client",
    "invalid_request",
    "invalid_scope",
    "unsupported_grant_type",
})

#: 瞬时错误（应重试，不该误杀账号）。
_RETRYABLE_CODES = frozenset({
    "rate_limited",
    "rate_limit_exceeded",
    "too_many_requests",
    "temporarily_unavailable",
    "server_error",
    "oauth_timeout",
    "oauth_transport_error",
    "oauth_unavailable",
})


def _normalize_code(code: str) -> str:
    return str(code or "").strip().lower().replace("-", "_")


def classify_refresh_error(status: int, code: str) -> str:
    """把刷新失败归类：permanent / configuration / retryable / unknown。

    对齐 grok2api 的判定顺序：先看错误码（精确），再看 HTTP 状态码（粗判）。
    状态码单独看是不够的 —— OAuth 网关对临时策略/客户端/基础设施问题
    同样用 400/401。
    """
    normalized = _normalize_code(code)
    if normalized in _PERMANENT_CODES:
        return "permanent"
    if normalized in _CONFIGURATION_CODES:
        return "configuration"
    if normalized in _RETRYABLE_CODES:
        return "retryable"
    if status >= 500:
        return "retryable"
    return "unknown"


@dataclass
class RefreshResult:
    """一次刷新尝试的结果。"""

    ok: bool
    tokens: dict[str, Any] = field(default_factory=dict)
    #: 实际使用的路径：grant（RT 换新）/ device（登录协议重取）
    method: str = ""
    #: 失败时的分类：permanent / configuration / retryable / unknown
    kind: str = ""
    #: 失败时的错误码（如 invalid_grant）
    error_code: str = ""
    #: 失败时的可读原因
    error: str = ""
    #: 失败时的 HTTP 状态码（有的话）
    status: int = 0


def _post_form(url: str, form: dict[str, str], *, proxy: str = "", timeout: int = 30):
    """POST 表单（可带代理），返回响应对象。

    独立成函数是为了测试可以整体 mock 掉网络层。
    """
    from curl_cffi import requests as curl_requests

    headers = {
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    kwargs: dict[str, Any] = {
        "headers": headers,
        "impersonate": "chrome",
        "timeout": timeout,
    }
    if proxy:
        from core.proxy_utils import build_requests_proxy_config

        kwargs["proxies"] = build_requests_proxy_config(proxy)
    return curl_requests.post(url, data=urllib.parse.urlencode(form), **kwargs)


def refresh_via_grant(
    refresh_token: str,
    *,
    proxy: str = "",
    log: LogFn = None,
) -> RefreshResult:
    """用 refresh_token grant 换新 token（纯协议快路径）。

    成功时返回轮换后的完整凭证（x.ai 会轮换 RT —— 必须把新值存回去，
    否则下一次刷新就会 invalid_grant）。
    """
    token = str(refresh_token or "").strip()
    if not token:
        return RefreshResult(
            ok=False, method="grant", kind="permanent",
            error_code="missing_refresh_token", error="账号没有 refresh_token",
        )

    try:
        resp = _post_form(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "refresh_token": token,
            },
            proxy=proxy,
        )
    except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一归瞬时
        if log:
            log(f"[Grok] refresh grant 网络异常: {type(exc).__name__}: {str(exc)[:120]}")
        return RefreshResult(
            ok=False, method="grant", kind="retryable",
            error=f"{type(exc).__name__}: {str(exc)[:150]}",
        )

    status = int(getattr(resp, "status_code", 0) or 0)
    try:
        doc = resp.json()
    except Exception:
        doc = {}
    doc = doc if isinstance(doc, dict) else {}

    if 200 <= status < 300 and doc.get("access_token"):
        tokens = dict(doc)
        tokens.setdefault("token_type", "Bearer")
        # x.ai 未轮换时保留原值（有些部署/网关不轮换）
        tokens.setdefault("refresh_token", token)
        if log:
            log(
                f"[Grok] refresh grant 成功（expires_in={tokens.get('expires_in')}，"
                f"RT {'已轮换' if tokens.get('refresh_token') != token else '未变'}）"
            )
        return RefreshResult(ok=True, tokens=tokens, method="grant")

    code = str(doc.get("error") or "")
    description = str(doc.get("error_description") or doc.get("error_message") or "")
    kind = classify_refresh_error(status, code)
    error = f"HTTP {status}: {code or 'unknown'}" + (f"（{description[:120]}）" if description else "")
    if log:
        log(f"[Grok] refresh grant 失败 [{kind}]: {error}")
    return RefreshResult(
        ok=False, method="grant", kind=kind,
        error_code=code or f"http_{status}", error=error, status=status,
    )


def refresh_via_device_flow(
    sso: str,
    *,
    proxy: str = "",
    timeout: int = 240,
    log: LogFn = None,
) -> RefreshResult:
    """SSO → 登录协议重新获取 token（浏览器完成 Device Flow 授权）。

    为什么必须走浏览器：device/verify 与 device/approve 被 CF 保护，
    纯 HTTP 返回 403（实测）。浏览器里点授权按钮才能完成。
    """
    cookie = str(sso or "").strip()
    if not cookie:
        return RefreshResult(
            ok=False, method="device", kind="permanent",
            error_code="missing_sso", error="账号没有 SSO，无法走登录协议",
        )

    try:
        from .register_browser import exchange_oauth_via_browser

        tokens = exchange_oauth_via_browser(cookie, proxy=proxy, timeout=timeout, log=log)
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"[Grok] device flow 异常: {type(exc).__name__}: {str(exc)[:120]}")
        return RefreshResult(
            ok=False, method="device", kind="retryable",
            error=f"{type(exc).__name__}: {str(exc)[:150]}",
        )

    if tokens and tokens.get("access_token"):
        tokens = dict(tokens)
        tokens.setdefault("token_type", "Bearer")
        return RefreshResult(ok=True, tokens=tokens, method="device")
    if tokens and tokens.get("sso_rejected"):
        # SSO 被上游明确拒绝（重定向到登录页）—— 对齐 grok2api 的
        # 「SSO credential rejected」：账号需要重新登录，标失效而不是
        # 瞬时失败（瞬时失败不该动账号状态）。
        return RefreshResult(
            ok=False, method="device", kind="permanent",
            error_code="sso_rejected",
            error="SSO 已被上游拒绝（登录页重定向），需要重新登录",
        )
    return RefreshResult(
        ok=False, method="device", kind="retryable",
        error="浏览器登录协议未换到 token（SSO 可能已失效或被风控）",
    )


__all__ = [
    "RefreshResult",
    "classify_refresh_error",
    "refresh_via_device_flow",
    "refresh_via_grant",
]
