"""微软邮箱渠道（历史导入路径 core.mailboxes.channels.outlook）。实现在同目录各模块。

`build` 与 `@register_mailbox_provider` 刻意留在门面：`core/mailboxes/__init__.py`
按 `from .channels import outlook` 触发自注册。
"""
from __future__ import annotations

import threading  # noqa: F401  （保留旧模块泄漏的导入名，维持导入面逐字节一致）
import time  # noqa: F401
from abc import ABC, abstractmethod  # noqa: F401
from typing import Any, Optional  # noqa: F401

from ...base import BaseMailbox, MailboxAccount  # noqa: F401
from ....proxy_utils import build_requests_proxy_config  # noqa: F401
from ...registry import register_mailbox_provider
from .backends import (
    MailApiUrlOtpBackend,
    OutlookGraphMailboxBackend,
    OutlookImapMailboxBackend,
    OutlookMailboxBackend,
)
from .mailbox import OutlookMailbox

__all__ = [
    "MailApiUrlOtpBackend",
    "OutlookGraphMailboxBackend",
    "OutlookImapMailboxBackend",
    "OutlookMailbox",
    "OutlookMailboxBackend",
    "build",
]


@register_mailbox_provider(
    'outlook',
    'microsoft',
    # `mail_import` 是前端「邮箱导入」下拉项的取值 —— 界面上它是一个 UI 值，
    # 后端真正跑的就是这条渠道（同一个微软号池，按 mail_import_source 选账号
    # 类型）。注册页会先用 resolveEffectiveMailProvider 收敛成 microsoft，但
    # 其它入口（Accounts 页快速注册、周期任务、otp 回填）直接把配置里的原值
    # 透传进来，不认它就会在建邮箱这一步抛
    # `ValueError: 未知邮箱提供商: 'mail_import'`。
    'mail_import',
    display_name='微软邮箱（Outlook / Hotmail）',
)
def build(*, extra: dict, proxy: str = None) -> BaseMailbox:
    """构建 微软邮箱（Outlook / Hotmail） 渠道。"""
    return OutlookMailbox(
        imap_server=extra.get("outlook_imap_server", ""),
        imap_port=extra.get("outlook_imap_port", ""),
        token_endpoint=extra.get("outlook_token_endpoint", ""),
        backend=extra.get("outlook_backend", ""),
        graph_api_base=extra.get("outlook_graph_api_base", ""),
        mail_import_source=extra.get("mail_import_source", ""),
        proxy=proxy,
    )


# 子模块导入会在包对象上留下 `backends` / `mailbox` 两个属性；
# 旧单文件没有它们，为保持「导入面逐字节一致」的闸门，这里显式抹掉。
for _submodule in ("backends", "mailbox"):
    globals().pop(_submodule, None)
del _submodule
