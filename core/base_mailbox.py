"""邮箱渠道兼容门面（历史导入路径）。

真正的实现已拆到 `core/mailboxes/` 包：

```
core/mailboxes/
├── base.py       MailboxAccount / BaseMailbox
├── registry.py   渠道注册表（新增渠道 = 加文件 + 注册一行）
└── channels/     每个渠道一个文件
```

为什么保留这个模块
------------------
`core.base_mailbox` 是历史导入路径，全仓（含 tests/、scripts/）有多处引用，
且部分代码用 `from core.base_mailbox import X` 的**延迟导入**方式取类，
直接删掉会让这些引用失效。这里重导出全部公开名字，让旧路径继续可用。

新增渠道请改 `core/mailboxes/channels/`，不要往这里加东西。
"""

from __future__ import annotations

from .mailboxes import (  # noqa: F401  (重导出，兼容旧路径)
    BaseMailbox,
    MailboxAccount,
    available_providers,
    build_mailbox,
    create_mailbox,
    is_registered,
    provider_display_name,
    register_mailbox_provider,
)
from .mailboxes.channels.outlook import (  # noqa: F401
    MailApiUrlOtpBackend,
    OutlookGraphMailboxBackend,
    OutlookImapMailboxBackend,
    OutlookMailbox,
    OutlookMailboxBackend,
)

__all__ = [
    "BaseMailbox",
    "MailApiUrlOtpBackend",
    "MailboxAccount",
    "OutlookGraphMailboxBackend",
    "OutlookImapMailboxBackend",
    "OutlookMailbox",
    "OutlookMailboxBackend",
    "available_providers",
    "build_mailbox",
    "create_mailbox",
    "is_registered",
    "provider_display_name",
    "register_mailbox_provider",
]
