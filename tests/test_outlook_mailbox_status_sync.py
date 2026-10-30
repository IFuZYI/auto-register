"""Outlook 邮箱池的状态同步不能一进去就 `NameError`。

`requeue_account` 与 `set_account_status` 里一路调用 `outlook_repository.*`，
但那个名字在方法体内从来没绑过（模块级也没有）—— 真跑注册时，Outlook 渠道
第一次回写状态就会炸。base commit 上就有，`pyflakes` 一跑就现形：

    core/mailboxes/channels/outlook/mailbox.py:328: undefined name 'outlook_repository'

补的是两行局部 import（和同文件 `fetch_account` 里已有的写法一致，
避免 `core.db` ↔ `core.mailboxes` 的模块级环）。
"""

from __future__ import annotations

import unittest

from core.db.outlook_repository import outlook_repository
from core.mailboxes.base import MailboxAccount
from core.mailboxes.channels.outlook.mailbox import OutlookMailbox

EMAIL = "status-sync@example.com"


class OutlookStatusSyncTests(unittest.TestCase):
    def setUp(self):
        # 全量跑的时候，前面某些测试会把共享库的表整个删掉（迁移/建库类测试），
        # 这条文件单独跑是绿的、全量跑就 `no such table: outlook_accounts`。
        # `create_all` 幂等，自己把表兜住，别依赖执行顺序。
        from sqlmodel import SQLModel

        from core.db import OutlookAccountModel, current_engine  # noqa: F401 - 注册元数据

        SQLModel.metadata.create_all(current_engine())
        self._drop()

    def tearDown(self):
        self._drop()

    @staticmethod
    def _drop() -> None:
        row = outlook_repository.find_by_email(EMAIL)
        if row:
            outlook_repository.delete(row["id"])

    def _seed(self) -> dict:
        return outlook_repository.add(
            email=EMAIL, password="pw", client_id="cid", refresh_token="rt",
            status="available",
        )

    def _account(self, row: dict) -> MailboxAccount:
        return MailboxAccount(email=EMAIL, account_id=str(row["id"]), extra={})

    def test_set_account_status_writes_the_pool_row(self):
        row = self._seed()
        box = OutlookMailbox()

        box.set_account_status(self._account(row), "used")

        self.assertEqual(outlook_repository.get(row["id"])["status"], "used")

    def test_requeue_account_puts_it_back_to_available(self):
        row = self._seed()
        box = OutlookMailbox()
        box.set_account_status(self._account(row), "used")

        box.requeue_account(self._account(row))

        self.assertEqual(outlook_repository.get(row["id"])["status"], "available")

    def test_unknown_status_still_raises_value_error(self):
        """修 import 不能顺手把参数校验吞掉。"""
        row = self._seed()
        with self.assertRaises(ValueError):
            OutlookMailbox().set_account_status(self._account(row), "不存在的状态")


if __name__ == "__main__":
    unittest.main()
