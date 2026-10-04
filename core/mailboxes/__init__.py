"""邮箱渠道包：基类 + 注册表 + 各渠道实现。

结构
----
```
core/mailboxes/
├── base.py            MailboxAccount / BaseMailbox
├── registry.py        渠道注册表（新增渠道 = 加文件 + 注册一行）
├── __init__.py        create_mailbox 工厂（兼容旧导入路径）
└── channels/
    ├── outlook/       微软邮箱（Graph / IMAP / MailAPI URL）
    └── ...
```

`icloud_local` 在 `modules/mail/icloud_local.py`（依赖 services，不进 core）。

依赖方向
--------
本包只依赖 `core/` 内部（`proxy_utils`、`mail_import_sources`、`db`）。
`modules.mail` 里的渠道通过 `core.mailboxes.registry` 的
`_OPTIONAL_PROVIDER_MODULES` 延迟加载进来，这是 core 唯一放行的反向依赖
（加载器例外，与 `core/registry.py` 同性质）。
"""

from __future__ import annotations

from .base import BaseMailbox, MailboxAccount
from .registry import (
    available_providers,
    build_mailbox,
    is_registered,
    provider_display_name,
    register_mailbox_provider,
)

# 导入内置渠道以触发自注册。顺序即展示顺序。
from .channels import (  # noqa: F401  (导入副作用：注册)
    outlook,
)


def create_mailbox(provider: str, extra: dict = None, proxy: str = None) -> BaseMailbox:
    """按 provider 名创建邮箱渠道实例（兼容旧签名）。

    未知 provider 直接抛 `ValueError` 并列出可用渠道——旧版工厂末尾用
    `else: # laoudo` 兜底，名字拼错会静默返回 LaoudoMailbox，故障表现为
    「一直收不到验证码」，极难排查。
    """
    return build_mailbox(provider, extra=extra, proxy=proxy)


__all__ = [
    "BaseMailbox",
    "MailboxAccount",
    "available_providers",
    "build_mailbox",
    "create_mailbox",
    "is_registered",
    "provider_display_name",
    "register_mailbox_provider",
]
