"""iCloud Web 请求的传输层：会话构造、请求头与上游错误归一化。"""

from __future__ import annotations

import email.utils
from typing import Any, Mapping, Optional

import requests

from .constants import DEFAULT_TIMEOUT_SECONDS, USER_AGENT, endpoints_for
from .errors import ICloudError
from core.proxy_utils import build_requests_proxy_config

def build_session(proxy: str | None = None) -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    proxies = build_requests_proxy_config(proxy)
    if proxies:
        session.proxies.update(proxies)
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return session


def web_origin(region: str | None, service_url: str = "") -> str:
    if str(region or "").strip():
        return endpoints_for(region).origin
    if "icloud.com.cn" in str(service_url or "").lower():
        return "https://www.icloud.com.cn"
    return "https://www.icloud.com"


def web_headers(
    region: str | None,
    service_url: str,
    *,
    accept: str = "*/*",
    content_type: str = "",
) -> dict[str, str]:
    origin = web_origin(region, service_url)
    headers = {
        "Accept": accept,
        "Origin": origin,
        "Referer": origin + "/",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "User-Agent": USER_AGENT,
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


class WebTransport:
    """带超时与代理的 iCloud Web 请求执行器。"""

    def __init__(
        self,
        *,
        proxy: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.timeout = float(timeout or DEFAULT_TIMEOUT_SECONDS)
        self.session = session or build_session(proxy)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("allow_redirects", True)
        return self.session.request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "WebTransport":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def parse_retry_after(value: str | None) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.isdigit():
        return float(text)
    parsed = email.utils.parsedate_to_datetime(text) if text else None
    if parsed is None:
        return 0.0
    import datetime as _datetime

    delta = parsed - _datetime.datetime.now(parsed.tzinfo or _datetime.timezone.utc)
    return max(delta.total_seconds(), 0.0)


def http_error(response: requests.Response, service_name: str) -> ICloudError:
    """把 iCloud 的 HTTP 状态码映射为稳定的业务错误码。"""
    status = response.status_code
    is_mail = "mail" in service_name.lower()
    if status in (401, 403):
        if is_mail:
            code = "mail_access_denied"
            message = (
                f"{service_name} 返回 HTTP {status}：Mail 未开通或 Mail 子会话未获授权，"
                "这不表示 Hide My Email 会话失效"
            )
        else:
            code = "session_expired"
            message = (
                f"{service_name} 返回 HTTP {status}：iCloud Web 会话已失效，"
                "请重新登录并确认没有待接受的服务条款"
            )
    elif status == 429:
        code, message = "provider_rate_limited", f"{service_name} 触发上游速率限制"
    elif status == 408 or status >= 500:
        code, message = "upstream_unavailable", f"{service_name} 暂时不可用（HTTP {status}）"
    elif status in (400, 422):
        code, message = "upstream_rejected", f"{service_name} 拒绝了请求（HTTP {status}）"
    else:
        code, message = "upstream_error", f"{service_name} 返回 HTTP {status}"
    return ICloudError(
        code,
        f"{message}；{_diagnostic(response)}",
        retry_after=parse_retry_after(response.headers.get("Retry-After")),
    )


def _diagnostic(response: requests.Response) -> str:
    details = [f"Host: {requests.utils.urlparse(response.url).hostname or '-'}"]
    content_type = response.headers.get("Content-Type", "").strip()
    if content_type:
        details.append(f"Content-Type: {content_type}")
    request_id = next(
        (
            response.headers[name]
            for name in (
                "X-Apple-Request-UUID",
                "X-Apple-Request-ID",
                "X-Apple-I-Request-ID",
                "X-Apple-Jingle-Correlation-Key",
            )
            if response.headers.get(name)
        ),
        "",
    )
    if request_id:
        details.append(f"Apple Request ID: {request_id}")
    details.append(f"上游响应正文 {len(response.content)} 字节")
    return "；".join(details)


def envelope_error(envelope: Mapping[str, Any], fallback_message: str) -> ICloudError:
    """把 iCloud JSON 信封里的业务错误映射为稳定的业务错误码。"""
    error_object = envelope.get("error") if isinstance(envelope.get("error"), Mapping) else envelope
    upstream_code = str(
        _first_scalar(error_object, "code", "errorCode") or _first_scalar(envelope, "code", "errorCode")
    ).lower()
    upstream_message = str(
        _first_scalar(error_object, "message", "errorMessage", "reason")
        or _first_scalar(envelope, "message", "errorMessage", "reason")
    ).lower()

    rate_limited_markers = ("reached the limit of addresses", "too many requests", "rate limit")
    expired_markers = ("session expired", "invalid session", "invalid global session", "authentication failed")
    if (
        upstream_code in {"429", "provider_rate_limited"}
        or "rate_limit" in upstream_code
        or any(marker in upstream_message for marker in rate_limited_markers)
    ):
        return ICloudError(
            "provider_rate_limited",
            "iCloud 已达到当前隐私邮箱创建上限，请稍后再试",
            retry_after=_body_retry_after(error_object),
        )
    if (
        upstream_code in {"401", "403", "session_expired", "invalid_credentials"}
        or "invalid_session" in upstream_code
        or any(marker in upstream_message for marker in expired_markers)
    ):
        return ICloudError("session_expired", f"{fallback_message}：iCloud Web Session 已失效，请重新登录")
    if "cannot connect to icloud" in upstream_message:
        return ICloudError(
            "upstream_rejected",
            "iCloud 拒绝了请求，请确认账号区域正确、已开通 iCloud+，并重新登录主号",
        )
    if upstream_code == "408" or (upstream_code.isdigit() and 500 <= int(upstream_code) <= 599):
        return ICloudError("upstream_unavailable", f"{fallback_message}：iCloud 上游暂时不可用，请稍后重试")
    return ICloudError("upstream_rejected", fallback_message)


def _first_scalar(payload: Any, *keys: str) -> str:
    if not isinstance(payload, Mapping):
        return ""
    lowered = {str(key).lower(): value for key, value in payload.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def _body_retry_after(payload: Any) -> float:
    try:
        return max(float(_first_scalar(payload, "retryAfter", "retry_after") or 0), 0.0)
    except ValueError:
        return 0.0


def find_string(payload: Any, *keys: str) -> str:
    """在任意嵌套 JSON 中深度优先查找首个非空字符串字段。"""
    wanted = {key.lower() for key in keys}
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            if str(key).lower() in wanted and isinstance(value, str) and value:
                return value
        for value in payload.values():
            found = find_string(value, *keys)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_string(value, *keys)
            if found:
                return found
    return ""


def nested_value(payload: Any, *path: str) -> Any:
    current = payload
    for key in path:
        if not isinstance(current, Mapping) or key not in current:
            return None
        current = current[key]
    return current


def nested_string(payload: Any, *path: str) -> str:
    value = nested_value(payload, *path)
    if isinstance(value, str):
        return value
    if isinstance(value, bool) or value is None:
        return ""
    if isinstance(value, (int, float)):
        return f"{value:.0f}" if isinstance(value, float) else str(value)
    return ""
