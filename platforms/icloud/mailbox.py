"""通过 iCloud IMAP TLS 实时收件。

所有收件都走 `imap.mail.me.com:993`；Web Session 只负责 Hide My Email 管理。
邮件标识由 `<邮箱目录>:<UIDVALIDITY>:<UID>` 组成，避免目录不同或
UIDVALIDITY 变化时发生冲突。
"""

from __future__ import annotations

import imaplib
import re
from datetime import datetime, timezone
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from typing import Iterator, Optional

from .constants import (
    DEFAULT_IMAP_HOST,
    DEFAULT_IMAP_PORT,
    DEFAULT_SYNC_LIMIT,
    DEFAULT_TIMEOUT_SECONDS,
    DELIVERY_HEADER_NAMES,
    MAX_SYNC_LIMIT,
)
from .credentials import ICloudCredentials
from .delivery import delivered_to, delivery_addresses
from .errors import ICloudError, invalid_config, invalid_response, upstream_unavailable
from .models import MailAddress, MailMessage, utcnow
from .utils import normalize_email_address, parse_address_list, strip_html, truncate_text

_SNIPPET_LENGTH = 240
_UID_PATTERN = re.compile(rb"UID\s+(\d+)")


class IMAPTarget:
    """已校验的 IMAP 连接参数。"""

    def __init__(self, host: str, port: int, username: str, password: str) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password


def resolve_imap_target(credentials: ICloudCredentials, account_email: str) -> IMAPTarget:
    host = credentials.imap_host.strip() or DEFAULT_IMAP_HOST
    port = int(credentials.imap_port or 0)
    if "://" in host or any(character in host for character in "/?#\r\n\t "):
        raise invalid_config("imap_host 无效")
    if host.count(":") == 1:
        host, _, raw_port = host.partition(":")
        if not raw_port.isdigit():
            raise invalid_config("imap_host 中的端口无效")
        port = port or int(raw_port)
    host = host.strip("[]")
    port = port or DEFAULT_IMAP_PORT
    if not host:
        raise invalid_config("imap_host 无效")
    if not 1 <= port <= 65535:
        raise invalid_config("imap_port 必须在 1 到 65535 之间")

    username = credentials.imap_username.strip() or str(account_email or "").strip()
    if not username:
        raise invalid_config("iCloud IMAP 用户名不能为空")
    if not credentials.imap_password.strip():
        raise invalid_config("iCloud IMAP App 专用密码不能为空")
    return IMAPTarget(host, port, username, credentials.imap_password)


def fetch_inbox(
    credentials: ICloudCredentials,
    account_email: str,
    *,
    mailbox: str = "INBOX",
    limit: int = 0,
    recipient: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[MailMessage]:
    """读取邮箱目录中最近的邮件，可按隐私邮箱地址过滤。"""
    target = resolve_imap_target(credentials, account_email)
    mailbox = (mailbox or "INBOX").strip() or "INBOX"
    limit = min(max(int(limit or credentials.sync_limit or DEFAULT_SYNC_LIMIT), 1), MAX_SYNC_LIMIT)
    recipient = normalize_email_address(recipient) if recipient else ""

    with _connect(target, timeout) as client:
        uid_validity, message_count = _select(client, mailbox)
        if message_count == 0:
            return []
        first = max(message_count - limit + 1, 1)
        raw_messages = _fetch_range(client, first, message_count)

    messages = []
    for uid, flags, internal_date, raw in raw_messages:
        parsed = _parse_message(
            raw,
            uid=uid,
            flags=flags,
            internal_date=internal_date,
            mailbox=mailbox,
            uid_validity=uid_validity,
        )
        if parsed is None:
            continue
        if recipient:
            candidates = delivery_addresses(
                alias_address=parsed.alias_address,
                headers=parsed.headers,
                to=[item.email for item in parsed.to],
            )
            if not delivered_to(candidates, recipient):
                continue
            parsed.alias_address = recipient
        messages.append((uid, parsed))

    messages.sort(key=lambda item: (item[1].received_at, item[0]), reverse=True)
    return [message for _uid, message in messages]


class _Connection:
    def __init__(self, client: imaplib.IMAP4_SSL) -> None:
        self.client = client

    def __enter__(self) -> imaplib.IMAP4_SSL:
        return self.client

    def __exit__(self, *_exc) -> None:
        try:
            self.client.logout()
        except Exception:
            pass


def _connect(target: IMAPTarget, timeout: float) -> _Connection:
    try:
        client = imaplib.IMAP4_SSL(target.host, target.port, timeout=timeout)
    except (OSError, imaplib.IMAP4.error) as exc:
        raise upstream_unavailable("连接 iCloud IMAP 服务失败", exc) from exc
    try:
        client.login(target.username, target.password)
    except imaplib.IMAP4.error as exc:
        try:
            client.logout()
        except Exception:
            pass
        raise ICloudError(
            "invalid_credentials", "iCloud IMAP 用户名或 App 专用密码错误", cause=exc
        ) from exc
    except OSError as exc:
        raise upstream_unavailable("连接 iCloud IMAP 服务失败", exc) from exc
    return _Connection(client)


def _select(client: imaplib.IMAP4_SSL, mailbox: str) -> tuple[int, int]:
    try:
        status, payload = client.select(f'"{mailbox}"', readonly=True)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise ICloudError("upstream_rejected", "iCloud IMAP 拒绝打开邮箱", cause=exc) from exc
    if status != "OK":
        raise ICloudError("upstream_rejected", f"iCloud IMAP 拒绝打开邮箱 {mailbox}")

    uid_validity = 0
    raw_validity = client.response("UIDVALIDITY")[1]
    if raw_validity and raw_validity[0]:
        uid_validity = int(raw_validity[0])
    if uid_validity == 0:
        raise invalid_response("iCloud IMAP SELECT 响应缺少 UIDVALIDITY")
    return uid_validity, int(payload[0] or 0)


def _fetch_range(
    client: imaplib.IMAP4_SSL, first: int, last: int
) -> list[tuple[int, str, Optional[datetime], bytes]]:
    try:
        status, payload = client.fetch(
            f"{first}:{last}", "(UID FLAGS INTERNALDATE BODY.PEEK[])"
        )
    except (imaplib.IMAP4.error, OSError) as exc:
        raise ICloudError("upstream_rejected", "iCloud IMAP 拒绝读取邮件", cause=exc) from exc
    if status != "OK":
        raise ICloudError("upstream_rejected", "iCloud IMAP 拒绝读取邮件")
    return list(_iter_fetch_items(payload))


def _iter_fetch_items(payload) -> Iterator[tuple[int, str, Optional[datetime], bytes]]:
    for item in payload or []:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        descriptor, raw = item[0], item[1]
        if not isinstance(raw, (bytes, bytearray)) or not raw:
            continue
        match = _UID_PATTERN.search(descriptor or b"")
        if not match:
            continue
        text = (descriptor or b"").decode("utf-8", "ignore")
        yield int(match.group(1)), text, _parse_internal_date(text), bytes(raw)


def _parse_internal_date(descriptor: str) -> Optional[datetime]:
    match = re.search(r'INTERNALDATE "([^"]+)"', descriptor)
    if not match:
        return None
    parsed = imaplib.Internaldate2tuple(f'INTERNALDATE "{match.group(1)}"'.encode())
    if parsed is None:
        return None
    import calendar

    return datetime.fromtimestamp(calendar.timegm(parsed), tz=timezone.utc)


def _parse_message(
    raw: bytes,
    *,
    uid: int,
    flags: str,
    internal_date: Optional[datetime],
    mailbox: str,
    uid_validity: int,
) -> Optional[MailMessage]:
    try:
        parsed = message_from_bytes(raw)
    except Exception:
        return None

    text_parts, html_parts, has_attachments = _walk_parts(parsed)
    text_body = "\n\n".join(text_parts)
    html_body = "\n\n".join(html_parts)
    snippet = text_body if text_body.strip() else strip_html(html_body)

    headers = {
        name: parsed.get(name)
        for name in (*DELIVERY_HEADER_NAMES, "Message-Id")
        if parsed.get(name)
    }
    sent_at = _parse_date(parsed.get("Date"))
    received_at = internal_date or sent_at or utcnow()
    to = [MailAddress(email=address, name=name) for name, address in parse_address_list(parsed.get("To", ""))]
    sender_list = parse_address_list(parsed.get("From", ""))
    candidates = delivery_addresses(headers=headers, to=[item.email for item in to])

    return MailMessage(
        provider_message_id=f"{mailbox}:{uid_validity}:{uid}",
        mailbox=mailbox,
        subject=_decode_header(parsed.get("Subject", "")),
        snippet=truncate_text(snippet, _SNIPPET_LENGTH),
        text_body=text_body,
        html_body=html_body,
        sender=MailAddress(email=sender_list[0][1], name=sender_list[0][0]) if sender_list else MailAddress(email=""),
        to=to,
        cc=[MailAddress(email=address, name=name) for name, address in parse_address_list(parsed.get("Cc", ""))],
        received_at=received_at,
        sent_at=sent_at,
        is_read="\\Seen" in flags,
        has_attachments=has_attachments,
        alias_address=candidates[0] if candidates else "",
        headers=headers,
    )


def _walk_parts(message: Message) -> tuple[list[str], list[str], bool]:
    text_parts: list[str] = []
    html_parts: list[str] = []
    has_attachments = False

    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        filename = part.get_filename()
        if filename or (part.get_content_disposition() or "").lower() == "attachment":
            has_attachments = True
            continue
        if content_type not in ("text/plain", "text/html"):
            continue
        decoded = _decode_part(part)
        if not decoded:
            continue
        (html_parts if content_type == "text/html" else text_parts).append(decoded)
    return text_parts, html_parts, has_attachments


def _decode_part(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except Exception:
        return ""
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace").strip()
    except LookupError:
        return payload.decode("utf-8", "replace").strip()


def _decode_header(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _parse_date(value: str | None) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
