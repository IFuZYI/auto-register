"""Mailbox provider construction and compatibility entry points.

这里 import 各渠道模块**是为了触发自注册**（每个渠道文件末尾调用
`core.mailboxes.registry.register_mailbox_provider`）。少了这一步，渠道会
「文件存在但查不到」，`create_mailbox` 直接报未知 provider。

`icloud_local` 依赖 services/，不能下沉进 core，所以由这里注册。
"""

from .factory import MailboxFactory, create_mailbox

# 导入副作用：注册 core 侧够不着的渠道（依赖 services/，不能下沉进 core）
from . import icloud_local  # noqa: F401

__all__ = ["MailboxFactory", "create_mailbox", "icloud_local"]
