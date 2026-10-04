"""内置邮箱渠道实现。

每个模块：
1. 定义渠道类（继承 `..base.BaseMailbox`）；
2. 末尾用 `@register_mailbox_provider(...)` 注册一个 `build()` builder。

新增渠道只需加一个文件（+ 在 `core/mailboxes/__init__.py` 里 import 一次），
不必再改任何工厂分支——见 docs/EXTENDING.md §3。

现存渠道只有两条，都是「本地号池」型：注册前先在面板里维护一份号，注册时
从池里取。一次性临时邮箱渠道（Laoudo / Freemail / MoeMail / SkyMail /
CloudMail / MaliAPI / GPTMail / OpenTrashMail / TempMail.lol / Mail.tm /
DuckMail / CF Worker / Aitre）与远程 icloud-hme 已按用户要求整体删除，
所以这里不再有「现开现用」的实现。
"""

from __future__ import annotations

__all__ = [
    "outlook",
]
