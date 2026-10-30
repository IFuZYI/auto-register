"""仓储的两种后端要跑出同样的结果。

用户要求「增加数据存储方式（json、sqlite）」。**换后端不能换行为** ——
否则「切存储」会变成「换一套语义」，用户的账号会以难以察觉的方式变样。

## 手法：同一套契约跑两遍

`_backend_cases()` 产出两个 fixture（sqlite / json），每个测试用同一个
`subTest` 跑两遍。任何一边行为不一致就红。

## 为什么不是「各自写一套测试」

各写一套时，两边会逐渐漂移：sqlite 版加了新断言、json 版忘了加。同一套
断言跑两遍，漂移当场暴露。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class _JsonBackendFixture:
    """把仓储切到 JSON 后端。

    做法：`core.storage.resolve_backend_name` 返回 `json`，并把 JSON 文件
    指到临时目录。**不改 os.environ** —— 那会污染后续测试（实测踩过：
    12 个无关测试因此红）。
    """

    name = "json"

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "accounts.json"
        self._patches: list = []

    def __enter__(self):
        from core.storage.account_store_json import AccountStoreJson

        # 设**真实门槛环境变量**（`STORAGE_BACKEND_ACCOUNTS=json`），而不是
        # 只 mock `_json_store` —— 那样会绕过门槛，测不到「显式开启才生效」
        # 这条规则。
        self._env = mock.patch.dict(
            "os.environ", {"STORAGE_BACKEND_ACCOUNTS": "json"}, clear=False
        )
        self._env.start()
        self._patches = [
            mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "json"),
            # `_json_store` 是 **staticmethod**，调用时不传 self —— 桩也不接。
            # 第一版写成 `lambda self=None: ...self.path`，于是 self 恒为 None
            # （实测 `'NoneType' object has no attribute 'path'`）。
            mock.patch(
                "core.db.repository.AccountRepository._json_store",
                staticmethod(lambda: AccountStoreJson(self.path)),
                create=True,
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        self._env.stop()
        return False


class _SqliteBackendFixture:
    name = "sqlite"

    def __init__(self, root: Path):
        self.root = root

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RepositoryContractTests(unittest.TestCase):
    """两种后端跑同一套断言。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _cases(self):
        return (_SqliteBackendFixture(self.root), _JsonBackendFixture(self.root))

    def _cleanup_sqlite(self):
        """清掉本测试写进真实库的行（sqlite 后端落在 conftest 的临时库上）。"""
        from core.db import platform_session
        from core.db.models_account import AccountModel
        from sqlmodel import select

        with platform_session("chatgpt") as s:
            rows = s.exec(
                select(AccountModel).where(AccountModel.email.like("contract-%"))
            ).all()
            for row in rows:
                s.delete(row)
            s.commit()

    # ------------------------------------------------------------ 契约

    def test_outlook_pool_summary_works_on_both_backends(self):
        """号池汇总不能因为后端切换而 500。

        原先直接对 `outlook_accounts` 发 `GROUP BY`，JSON 部署下那张表不存在
        （`no such table: outlook_accounts`）—— 接口 500，而单测全绿。
        改成走仓储层后两条后端都能算。
        """
        for fixture in self._cases():
            with self.subTest(backend=fixture.name), fixture:
                from core.mailboxes.channels.outlook import OutlookMailbox

                summary = OutlookMailbox.pool_status_summary()

                self.assertIn("total", summary)
                for key in ("unpooled", "available", "in_use", "used", "failed"):
                    self.assertIn(key, summary, f"号池汇总缺 {key}")

    def test_get_many_and_delete_many_work_on_both_backends(self):
        """批量取/批量删在两条后端上都要工作。

        这条是补的**既有缺口**：`get_many` / `delete_many` 原先只有 sqlite 分支，
        JSON 部署下导出与批量删除会去查一张不存在的表
        （`no such table: accounts`，base commit 上就复现）。
        """
        for fixture in self._cases():
            with self.subTest(backend=fixture.name), fixture:
                from core.base_platform import Account
                from core.db import account_repository

                first = account_repository.upsert(
                    Account(platform="chatgpt", email="contract-many-a@example.com", password="p")
                )
                second = account_repository.upsert(
                    Account(platform="chatgpt", email="contract-many-b@example.com", password="p")
                )

                rows = account_repository.get_many([str(second.id), str(first.id)])
                self.assertEqual(
                    [str(r.id) for r in rows], [str(second.id), str(first.id)],
                    "批量取要保持调用方给的顺序",
                )

                deleted, missing = account_repository.delete_many(
                    [str(first.id), "00000000-0000-4000-8000-000000000000"]
                )
                self.assertEqual(deleted, [str(first.id)])
                self.assertEqual(missing, ["00000000-0000-4000-8000-000000000000"])

                account_repository.delete_many([str(second.id)])
        self._cleanup_sqlite()

    def test_upsert_then_find(self):
        for fixture in self._cases():
            with self.subTest(backend=fixture.name), fixture:
                from core.base_platform import Account, AccountStatus
                from core.db.repository import account_repository

                email = f"contract-a-{fixture.name}@x.c"
                acct = Account(
                    platform="chatgpt", email=email, password="pw",
                    token="AT", status=AccountStatus.REGISTERED, extra={"k": "v"},
                )
                row = account_repository.upsert(acct)
                self.assertTrue(row.id)
                found = account_repository.find_by_email("chatgpt", email)
                self.assertIsNotNone(found, f"{fixture.name}: upsert 后查不到")
                self.assertEqual(found.password, "pw")
                self.assertEqual(found.token, "AT")
        self._cleanup_sqlite()

    def test_is_registered(self):
        for fixture in self._cases():
            with self.subTest(backend=fixture.name), fixture:
                from core.base_platform import Account, AccountStatus
                from core.db.repository import account_repository

                email = f"contract-b-{fixture.name}@x.c"
                self.assertFalse(
                    account_repository.is_registered("chatgpt", email)
                )
                account_repository.upsert(Account(
                    platform="chatgpt", email=email, password="pw",
                    status=AccountStatus.REGISTERED, extra={},
                ))
                self.assertTrue(
                    account_repository.is_registered("chatgpt", email)
                )
        self._cleanup_sqlite()

    def test_upsert_is_idempotent_on_the_unique_key(self):
        for fixture in self._cases():
            with self.subTest(backend=fixture.name), fixture:
                from core.base_platform import Account, AccountStatus
                from core.db.repository import account_repository

                email = f"contract-c-{fixture.name}@x.c"
                acct = Account(
                    platform="chatgpt", email=email, password="p1",
                    status=AccountStatus.REGISTERED, extra={},
                )
                first = account_repository.upsert(acct)
                acct.password = "p2"
                second = account_repository.upsert(acct)
                self.assertEqual(
                    first.id, second.id,
                    f"{fixture.name}: 重复 upsert 建了新行（唯一键失效）",
                )
                self.assertEqual(
                    account_repository.find_by_email("chatgpt", email).password,
                    "p2",
                )
        self._cleanup_sqlite()

    def test_delete(self):
        for fixture in self._cases():
            with self.subTest(backend=fixture.name), fixture:
                from core.base_platform import Account, AccountStatus
                from core.db.repository import account_repository

                email = f"contract-d-{fixture.name}@x.c"
                row = account_repository.upsert(Account(
                    platform="chatgpt", email=email, password="pw",
                    status=AccountStatus.REGISTERED, extra={},
                ))
                self.assertTrue(account_repository.delete(row.id, "chatgpt"))
                self.assertIsNone(
                    account_repository.find_by_email("chatgpt", email)
                )
        self._cleanup_sqlite()

    def test_get_by_id(self):
        for fixture in self._cases():
            with self.subTest(backend=fixture.name), fixture:
                from core.base_platform import Account, AccountStatus
                from core.db.repository import account_repository

                email = f"contract-e-{fixture.name}@x.c"
                row = account_repository.upsert(Account(
                    platform="chatgpt", email=email, password="pw",
                    status=AccountStatus.REGISTERED, extra={},
                ))
                got = account_repository.get(row.id, "chatgpt")
                self.assertIsNotNone(got, f"{fixture.name}: 按 id 取不到")
                self.assertEqual(got.email, email)
        self._cleanup_sqlite()

    def test_platforms_by_email(self):
        """**每个后端用不同的邮箱**。

        第一版两个子测试共用 `contract-f@x.c`：SQLite 子测试先写入真实库，
        JSON 子测试虽然写进了 JSON，但 `platforms_by_email` 回退去扫 SQLite
        时**找到了上一轮那行** —— 于是假通过（它根本没验证 JSON 路径）。
        用不同邮箱后，回退就会找不到，测试如实变红。
        """
        for fixture in self._cases():
            email = f"contract-f-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.base_platform import Account, AccountStatus
                from core.db.repository import account_repository

                account_repository.upsert(Account(
                    platform="chatgpt", email=email, password="pw",
                    status=AccountStatus.REGISTERED, extra={},
                ))
                # 签名是 `platforms_by_email(emails) -> {地址: {平台}}`
                mapping = account_repository.platforms_by_email([email])
                self.assertIn(
                    "chatgpt", mapping.get(email, set()),
                    f"{fixture.name}: platforms_by_email 漏了 chatgpt（返回 {mapping}）",
                )
        self._cleanup_sqlite()


if __name__ == "__main__":
    unittest.main()


class RepositorySaveContractTests(unittest.TestCase):
    """`save()` 是**写路径的统一入口** —— 两种后端都要正确落库。

    加它的理由：账号写路径散落在 8 个文件、约 18 处，都长这样
    `session.add(model); session.commit()`。JSON 后端下这些原生写会落到
    SQLite，而读取走 JSON —— **新建的账号查不到**（静默的数据不一致）。

    把写收敛到一个方法后，后端分派只有一处；调用方不再需要知道后端存在。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _cases(self):
        return (_SqliteBackendFixture(self.root), _JsonBackendFixture(self.root))

    def _cleanup_sqlite(self):
        from core.db import platform_session
        from core.db.models_account import AccountModel
        from sqlmodel import select

        with platform_session("chatgpt") as s:
            rows = s.exec(
                select(AccountModel).where(AccountModel.email.like("save-%"))
            ).all()
            for row in rows:
                s.delete(row)
            s.commit()

    def test_save_persists_modifications(self):
        """改字段后 `save()`，再读回来要是新值。"""
        for fixture in self._cases():
            email = f"save-a-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.base_platform import Account, AccountStatus
                from core.db.repository import account_repository

                row = account_repository.upsert(Account(
                    platform="chatgpt", email=email, password="p1",
                    status=AccountStatus.REGISTERED, extra={},
                ))
                row.password = "p2"
                account_repository.save(row, session=None)

                again = account_repository.find_by_email("chatgpt", email)
                self.assertEqual(
                    again.password, "p2",
                    f"{fixture.name}: save 后读回来还是旧值",
                )
        self._cleanup_sqlite()

    def test_save_works_inside_an_explicit_session(self):
        """带 session 调用时也要按后端分派（调用方有自己的事务）。"""
        for fixture in self._cases():
            email = f"save-b-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.base_platform import Account, AccountStatus
                from core.db import platform_session
                from core.db.repository import account_repository

                row = account_repository.upsert(Account(
                    platform="chatgpt", email=email, password="p1",
                    status=AccountStatus.REGISTERED, extra={},
                ))
                # 带 session 时**由调用方提交** —— 本仓既有约定（`api/actions.py`
                # 等都是 `s.add(...); s.commit()`）。`save` 不替调用方做这件事：
                # 它可能正在一个更大的事务里。
                with platform_session("chatgpt") as s:
                    row.password = "p3"
                    account_repository.save(row, session=s)
                    s.commit()

                again = account_repository.find_by_email("chatgpt", email)
                self.assertEqual(
                    again.password, "p3",
                    f"{fixture.name}: 带 session 的 save 没落库",
                )
        self._cleanup_sqlite()

    def test_save_returns_the_row(self):
        for fixture in self._cases():
            email = f"save-c-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.base_platform import Account, AccountStatus
                from core.db.repository import account_repository

                row = account_repository.upsert(Account(
                    platform="chatgpt", email=email, password="p1",
                    status=AccountStatus.REGISTERED, extra={},
                ))
                saved = account_repository.save(row, session=None)
                self.assertIsNotNone(saved)
                self.assertEqual(saved.email, email)
        self._cleanup_sqlite()
