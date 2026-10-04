"""通过 iCloud Web Mail API（mccgateway）收件。

为什么需要
----------
IMAP 收件要用户去 appleid.apple.com 生成 App 专用密码，门槛高；而导入 Cookie
登录的主号天然带着 Web 会话。参考项目（icloud-hme）两条路都走：IMAP 优先，
Web API 兜底（`internal/mail/web_client.go`），并在响应里用 `method` 字段标明
实际用了哪条路。

链路
----
1. `setup.icloud.com/setup/ws/1/validate` 拿 `webservices.mccgateway.url`
   （导入会话时已经拿到并存进凭据，见 web_client.py 的 import_session）
2. POST `{mccgateway}/mailws2/v1/thread/search` 拉邮件摘要

已知坑（参考项目 API.md 明确记录）
----------------------------------
validate 返回的 mccgateway URL 可能带 `:443` 端口。requests 的 Cookie 按
不带端口的 host 存，带端口请求时 Cookie 附加不上，Apple 直接回 403。
`_normalize_service_url`（web_client.py:457）已经在存凭据时剥掉 443 端口，
这里再兜一层，避免历史凭据里存着带端口的 URL。

限制
----
Web API **不支持按收件人搜索**，只能在本地对 To/主题/发件人做子串匹配，
所以必须传完整别名地址（传 `abc` 会误命中 `abcd@icloud.com`）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from .constants import DEFAULT_TIMEOUT_SECONDS, FALLBACK_MAIL_BUILD, FALLBACK_MAIL_MASTERING
from .credentials import ICloudCredentials
from .errors import ICloudError, invalid_config, invalid_response, upstream_unavailable
from .models import MailAddress, MailMessage, utcnow
from .transport import WebTransport, http_error, web_headers
from .utils import normalize_email_address, strip_html, truncate_text

logger = logging.getLogger(__name__)

THREAD_SEARCH_PATH = "/mailws2/v1/thread/search"

# 摘要截断长度。Web API 只给 preview，没有正文，所以这个值比 IMAP 路径小。
_PREVIEW_LENGTH = 240
# 单次最多取多少封。Apple 对 maxResults 没有明确上限，但拉太多没意义。
_MAX_RESULTS = 100


def fetch_inbox_web(
    credentials: ICloudCredentials,
    *,
    limit: int = 20,
    recipient: str = "",
    proxy: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    transport: Optional[WebTransport] = None,
) -> list[MailMessage]:
    """用 Web 会话读收件箱。

    `recipient` 非空时在本地按别名过滤（Web API 不支持服务端收件人搜索）。
    """
    if not credentials.has_web_session:
        raise invalid_config("缺少 iCloud Web 会话，无法用 Web API 收件")
    if not credentials.mail_gateway_url:
        raise invalid_config(
            "iCloud 未返回 Mail 服务地址（mccgateway），该主号可能未开通 Mail"
        )

    limit = max(min(int(limit or 20), _MAX_RESULTS), 1)
    recipient = normalize_email_address(recipient) if recipient else ""

    own_transport = transport is None
    active = transport or WebTransport(proxy=proxy, timeout=timeout)
    try:
        threads = _thread_search(active, credentials, limit=limit, recipient=recipient)
    finally:
        if own_transport:
            active.close()

    messages = [_thread_to_message(item, recipient) for item in threads]
    if recipient:
        messages = [item for item in messages if item.alias_address]
    return messages


def _thread_search(
    transport: WebTransport,
    credentials: ICloudCredentials,
    *,
    limit: int,
    recipient: str,
) -> list[Mapping[str, Any]]:
    url = _search_url(credentials, limit=limit, recipient=recipient)
    headers = web_headers(
        credentials.region,
        credentials.mail_gateway_url,
        accept="application/json",
        content_type="application/json",
    )
    headers["Cookie"] = credentials.cookies
    if credentials.web_auth_token and credentials.web_auth_token_header.strip():
        headers[credentials.web_auth_token_header.strip()] = credentials.web_auth_token
    for key, value in credentials.extra_headers.items():
        if key.lower() not in {"host", "content-length", "cookie"}:
            headers[key] = value

    # Web API 不支持收件人搜索，要过滤就得把范围拉大，否则别名邮件可能
    # 落在 limit 之外。多取一倍留余量。
    max_results = limit * 2 if recipient else limit
    payload = {
        "responseType": "THREAD_DIGEST",
        "includeFolderStatus": True,
        "maxResults": min(max_results, _MAX_RESULTS),
        "sessionHeaders": {
            "folder": "INBOX",
            "modseq": None,
            "threadmodseq": None,
            "condstore": 1,
            "qresync": 1,
            "threadmode": 1,
        },
    }

    try:
        response = transport.request("POST", url, json=payload, headers=headers)
    except requests.RequestException as exc:
        raise upstream_unavailable("连接 iCloud Mail 服务失败", exc) from exc
    if not response.ok:
        raise http_error(response, "iCloud Mail")

    try:
        body = response.json() or {}
    except ValueError as exc:
        raise invalid_response("iCloud Mail 返回了无效 JSON", exc) from exc
    if body.get("success") is False:
        logger.error(
            "iCloud Mail thread/search 被拒绝，原始响应: %s", str(body)[:600]
        )
        raise ICloudError("upstream_rejected", "iCloud Mail 拒绝了收件请求")

    threads = body.get("threadList")
    if not isinstance(threads, list):
        return []
    return [item for item in threads if isinstance(item, Mapping)]


def _search_url(credentials: ICloudCredentials, *, limit: int, recipient: str) -> str:
    base = _strip_default_port(credentials.mail_gateway_url).rstrip("/")
    parsed = urlparse(base + THREAD_SEARCH_PATH)
    merged = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query = {
        "clientBuildNumber": credentials.mail_client_build_number.strip()
        or FALLBACK_MAIL_BUILD,
        "clientMasteringNumber": credentials.mail_client_mastering_number.strip()
        or FALLBACK_MAIL_MASTERING,
        "clientId": credentials.client_id or "",
        "dsid": str(credentials.dsid or "").strip('"'),
    }
    merged.update({key: value for key, value in query.items() if value})
    return urlunparse(parsed._replace(query=urlencode(sorted(merged.items()))))


def _strip_default_port(url: str) -> str:
    """剥掉 443 端口：带端口的 host 附加不上 Cookie，Apple 会回 403。"""
    parsed = urlparse(str(url or "").strip())
    if not parsed.hostname:
        return str(url or "").strip()
    if parsed.port in (None, 443):
        return urlunparse(parsed._replace(netloc=parsed.hostname))
    return str(url or "").strip()


def _thread_to_message(thread: Mapping[str, Any], recipient: str) -> MailMessage:
    senders = thread.get("senders")
    sender = ""
    if isinstance(senders, list) and senders:
        sender = str(senders[0] or "")
    preview = truncate_text(
        strip_html(str(thread.get("preview") or "")).strip(), _PREVIEW_LENGTH
    )
    received_at = _timestamp(thread.get("timestamp"))
    subject = str(thread.get("subject") or "")

    # Web API 不给 To 头，别名只能靠「主题/发件人/preview 里出现该地址」判定。
    # 这正是 API.md 强调必须传完整地址的原因。
    alias_address = ""
    if recipient:
        haystack = f"{subject}\n{sender}\n{preview}".lower()
        if recipient.lower() in haystack:
            alias_address = recipient

    return MailMessage(
        provider_message_id=f"web:{thread.get('threadId') or ''}",
        mailbox="INBOX",
        subject=subject,
        snippet=preview,
        text_body=preview,
        sender=MailAddress(email=sender),
        received_at=received_at,
        alias_address=alias_address,
        headers={"web_api": "1"},
    )


def _timestamp(value: Any) -> datetime:
    """threadList 的 timestamp 是毫秒。"""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return utcnow()
    if millis <= 0:
        return utcnow()
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return utcnow()


__all__ = ["fetch_inbox_web"]
