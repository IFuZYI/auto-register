"""iCloud 主号凭据模型。

Web Session（Cookie / DSID / HME 服务地址）用于 Hide My Email 管理，
IMAP 应用专用密码用于收件；两者都以密文形式持久化。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Mapping

from .constants import DEFAULT_SYNC_LIMIT, REGION_GLOBAL


@dataclass
class ICloudCredentials:
    region: str = REGION_GLOBAL
    dsid: str = ""
    cookies: str = ""
    hme_service_url: str = ""
    mail_gateway_url: str = ""
    client_id: str = ""
    client_build_number: str = ""
    client_mastering_number: str = ""
    mail_client_build_number: str = ""
    mail_client_mastering_number: str = ""
    ckjs_build_version: str = ""
    web_auth_token: str = ""
    web_auth_token_header: str = ""
    imap_host: str = ""
    imap_port: int = 0
    imap_username: str = ""
    imap_password: str = ""
    sync_limit: int = DEFAULT_SYNC_LIMIT
    extra_headers: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ICloudCredentials":
        payload = payload or {}
        known = {item.name for item in fields(cls)}
        values = {key: value for key, value in payload.items() if key in known}
        headers = values.get("extra_headers")
        values["extra_headers"] = dict(headers) if isinstance(headers, Mapping) else {}
        values["imap_port"] = int(values.get("imap_port") or 0)
        values["sync_limit"] = int(values.get("sync_limit") or DEFAULT_SYNC_LIMIT)
        return cls(**values)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def merged_with(self, other: "ICloudCredentials") -> "ICloudCredentials":
        """用新会话覆盖旧凭据，但保留新会话未显式提交的 IMAP 配置。"""
        merged = ICloudCredentials.from_dict(other.to_dict())
        if not other.imap_password:
            merged.imap_host = self.imap_host
            merged.imap_port = self.imap_port
            merged.imap_username = self.imap_username
            merged.imap_password = self.imap_password
        return merged

    @property
    def has_web_session(self) -> bool:
        return bool(self.cookies and self.dsid and self.hme_service_url)

    @property
    def has_imap(self) -> bool:
        return bool(self.imap_password.strip())

    def public_state(self) -> dict[str, bool]:
        """只暴露“是否已配置”，绝不回传任何凭据原文。"""
        return {
            "has_session_cookies": bool(self.cookies),
            "has_dsid": bool(self.dsid),
            "has_hme_service_url": bool(self.hme_service_url),
            "has_mail_gateway": bool(self.mail_gateway_url),
            "has_web_auth_token": bool(self.web_auth_token),
            "has_imap_credentials": self.has_imap,
        }
