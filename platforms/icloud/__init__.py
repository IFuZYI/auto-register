"""iCloud 主号与 Hide My Email 隐私邮箱注册。"""

from .client import build_cache, fetch_inbox, login_manager, web_client
from .constants import (
    ALIAS_STATUS_ACTIVE,
    ALIAS_STATUS_DISABLED,
    DEFAULT_ALIAS_LABEL,
    DEFAULT_IMAP_HOST,
    DEFAULT_IMAP_PORT,
    LOGIN_STATUS_COMPLETED,
    LOGIN_STATUS_VERIFICATION_REQUIRED,
    POOL_STATUSES,
    POOL_STATUS_AVAILABLE,
    POOL_STATUS_IN_USE,
    POOL_STATUS_UNPOOLED,
    POOL_STATUS_USED,
    normalize_region,
)
from .credentials import ICloudCredentials
from .errors import ICloudError
from .login import LoginRequest, LoginState
from .models import ImportedSession, MailMessage, PrivateEmail, SessionImportRequest, TrustedPhone

__all__ = [
    "ALIAS_STATUS_ACTIVE",
    "ALIAS_STATUS_DISABLED",
    "POOL_STATUSES",
    "POOL_STATUS_AVAILABLE",
    "POOL_STATUS_IN_USE",
    "POOL_STATUS_UNPOOLED",
    "POOL_STATUS_USED",
    "DEFAULT_ALIAS_LABEL",
    "DEFAULT_IMAP_HOST",
    "DEFAULT_IMAP_PORT",
    "ICloudCredentials",
    "ICloudError",
    "ImportedSession",
    "LOGIN_STATUS_COMPLETED",
    "LOGIN_STATUS_VERIFICATION_REQUIRED",
    "LoginRequest",
    "LoginState",
    "MailMessage",
    "PrivateEmail",
    "SessionImportRequest",
    "TrustedPhone",
    "build_cache",
    "fetch_inbox",
    "login_manager",
    "normalize_region",
    "web_client",
]
