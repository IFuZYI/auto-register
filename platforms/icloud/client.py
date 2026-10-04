"""iCloud 提供方门面。

对外暴露三类能力：Apple ID 应用内登录、Hide My Email 管理、IMAP 实时收件。
构建号缓存与登录会话是进程级共享状态，其余请求级对象按需创建。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from .build_info import BuildInfoCache
from .constants import DEFAULT_TIMEOUT_SECONDS
from .credentials import ICloudCredentials
from .login import LoginSessionManager
from .mailbox import fetch_inbox as _fetch_inbox
from .models import MailMessage
from .transport import WebTransport
from .web_client import ICloudWebClient

_BUILD_CACHE = BuildInfoCache()
_LOGIN_MANAGER = LoginSessionManager(_BUILD_CACHE)


def build_cache() -> BuildInfoCache:
    return _BUILD_CACHE


def login_manager() -> LoginSessionManager:
    return _LOGIN_MANAGER


@contextmanager
def web_client(
    *, proxy: str | None = None, timeout: float = DEFAULT_TIMEOUT_SECONDS
) -> Iterator[ICloudWebClient]:
    """按请求创建一个 iCloud Web 客户端，退出时释放连接。"""
    with WebTransport(proxy=proxy, timeout=timeout) as transport:
        yield ICloudWebClient(transport, _BUILD_CACHE)


def fetch_inbox(
    credentials: ICloudCredentials,
    account_email: str,
    *,
    mailbox: str = "INBOX",
    limit: int = 0,
    recipient: str = "",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[MailMessage]:
    return _fetch_inbox(
        credentials,
        account_email,
        mailbox=mailbox,
        limit=limit,
        recipient=recipient,
        timeout=timeout,
    )
