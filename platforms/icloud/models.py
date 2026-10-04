"""iCloud 领域模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .constants import ALIAS_STATUS_ACTIVE
from .credentials import ICloudCredentials


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class PrivateEmail:
    """一个 Hide My Email 隐私邮箱地址。"""

    address: str
    label: str = ""
    note: str = ""
    status: str = ALIAS_STATUS_ACTIVE
    provider_id: str = ""
    created_at: datetime = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "label": self.label,
            "note": self.note,
            "status": self.status,
            "provider_id": self.provider_id,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class MailAddress:
    email: str
    name: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"email": self.email, "name": self.name}


@dataclass
class MailMessage:
    """一封通过 IMAP 实时读取的邮件。"""

    provider_message_id: str
    mailbox: str
    subject: str = ""
    snippet: str = ""
    text_body: str = ""
    html_body: str = ""
    sender: MailAddress = field(default_factory=lambda: MailAddress(email=""))
    to: list[MailAddress] = field(default_factory=list)
    cc: list[MailAddress] = field(default_factory=list)
    received_at: datetime = field(default_factory=utcnow)
    sent_at: Optional[datetime] = None
    is_read: bool = False
    has_attachments: bool = False
    alias_address: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.provider_message_id,
            "provider_message_id": self.provider_message_id,
            "mailbox": self.mailbox,
            "subject": self.subject,
            "snippet": self.snippet,
            "text_body": self.text_body,
            "html_body": self.html_body,
            "from": self.sender.to_dict(),
            "to": [item.to_dict() for item in self.to],
            "cc": [item.to_dict() for item in self.cc],
            "received_at": self.received_at.isoformat(),
            "sent_at": self.sent_at.isoformat() if self.sent_at else None,
            "is_read": self.is_read,
            "has_attachments": self.has_attachments,
            "alias_address": self.alias_address,
            "headers": self.headers,
        }


@dataclass
class SessionImportRequest:
    """导入一份 iCloud Web Session，可选同时提交 IMAP 配置。"""

    region: str = ""
    cookie_header: str = ""
    validate_cookie_header: str = ""
    cookies_json: Any = None
    web_auth_token: str = ""
    web_auth_token_header: str = ""
    client_id: str = ""
    imap_host: str = ""
    imap_port: int = 0
    imap_username: str = ""
    imap_password: str = ""


@dataclass
class ImportedSession:
    credentials: ICloudCredentials
    account_email: str = ""
    masked_dsid: str = ""
    service_host: str = ""
    cookie_count: int = 0

    def public_summary(self) -> dict[str, Any]:
        return {
            "masked_dsid": self.masked_dsid,
            "service_host": self.service_host,
            "cookie_count": self.cookie_count,
        }


@dataclass
class TrustedPhone:
    id: int
    number: str
    push_mode: str = "sms"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "number": self.number, "push_mode": self.push_mode}


def mask_secret(value: str) -> str:
    value = str(value or "")
    if len(value) <= 4:
        return "*" * len(value)
    return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"
