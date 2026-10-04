"""Mailbox provider construction independent of task/API layers.

`create_mailbox` 转发到 `core.mailboxes` 的注册表实现（单一来源）。
`MailboxFactory` 保留为显式注入 builder 的轻量工厂（测试/自定义场景用），
应用默认渠道走 `create_mailbox`。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

MailboxBuilder = Callable[..., Any]


class MailboxFactory:
    """Build mailboxes from an explicit provider-to-builder registry."""

    def __init__(self, builders: Mapping[str, MailboxBuilder]):
        self._builders = {
            str(name).strip().lower(): builder
            for name, builder in builders.items()
            if str(name).strip()
        }

    def create(self, provider: str, extra: dict | None = None, proxy: str | None = None) -> Any:
        normalized = str(provider or "").strip().lower()
        try:
            builder = self._builders[normalized]
        except KeyError as exc:
            available = ", ".join(sorted(self._builders)) or "(none)"
            raise ValueError(f"未知邮箱提供商: {provider!r}；可用: {available}") from exc
        return builder(extra=dict(extra or {}), proxy=proxy)


def create_mailbox(provider: str, extra: dict | None = None, proxy: str | None = None) -> Any:
    """Compatibility facade over the application's registered mailbox providers.

    走 `core.base_mailbox`（公开门面）而不是直接 `core.mailboxes`：调用方与
    测试都以 `core.base_mailbox.create_mailbox` 为打桩点，经它转发才能保持
    `patch("core.base_mailbox.create_mailbox")` 继续生效。
    """
    from core.base_mailbox import create_mailbox as application_create_mailbox

    return application_create_mailbox(provider, extra=extra, proxy=proxy)
