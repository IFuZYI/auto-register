"""邮箱池（`outlook_accounts`）仓储：两种后端跑同一套契约。

这张表是最后一张接 JSON 的。筛选最复杂：`enabled` + 状态 OR +
`lower(email) NOT IN (...)` + 账号类型匹配（含「NULL/空串算 OAuth」）——
全部用查询层（`core/storage/query.py`）的中立 clause 表达。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class _JsonFixture:
    name = "json"

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "outlook_accounts.json"
        self._env = None
        self._patches: list = []

    def __enter__(self):
        from core.storage.json_table import JsonTableStore

        self._env = mock.patch.dict("os.environ", {"STORAGE_BACKEND": "json"}, clear=False)
        self._env.start()
        self._patches = [
            mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "json"),
            mock.patch(
                "core.db.outlook_repository.OutlookRepository._store",
                staticmethod(lambda: JsonTableStore(self.path, table="outlook_accounts")),
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


class _SqliteFixture:
    name = "sqlite"

    def __init__(self, root: Path):
        self.root = root

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class OutlookRepositoryContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _cases(self):
        return (_SqliteFixture(self.root), _JsonFixture(self.root))

    def _cleanup_sqlite(self):
        from core.db import mailbox_pool_session
        from core.db.models_platform import OutlookAccountModel
        from sqlmodel import select

        with mailbox_pool_session("outlook") as s:
            for row in s.exec(
                select(OutlookAccountModel).where(
                    OutlookAccountModel.email.like("contract-outlook-%")
                )
            ).all():
                s.delete(row)
            s.commit()

    def test_add_then_list(self):
        for fixture in self._cases():
            email = f"contract-outlook-a-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.outlook_repository import outlook_repository as repo

                repo.add(email=email, password="pw", account_type="microsoft_oauth")
                rows = repo.list_all()
                self.assertIn(email, [r["email"] for r in rows],
                              f"{fixture.name}: 建完查不到")
        self._cleanup_sqlite()

    def test_set_status_persists(self):
        for fixture in self._cases():
            email = f"contract-outlook-b-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.outlook_repository import outlook_repository as repo

                row = repo.add(email=email, password="pw")
                repo.set_fields(row["id"], status="available")
                self.assertEqual(repo.get(row["id"])["status"], "available")
        self._cleanup_sqlite()

    def test_find_claimable_excludes_unpooled(self):
        """取号只认 `available` / `used` / 空 —— `unpooled` 必须排除。

        用户明确要求「选择导入才能导入邮箱池，不是全部导入」。
        """
        for fixture in self._cases():
            ok_email = f"contract-outlook-c1-{fixture.name}@x.c"
            no_email = f"contract-outlook-c2-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.outlook_repository import outlook_repository as repo

                a = repo.add(email=ok_email, password="pw")
                b = repo.add(email=no_email, password="pw")
                repo.set_fields(a["id"], status="available", enabled=True)
                repo.set_fields(b["id"], status="unpooled", enabled=True)

                found = [r["email"] for r in repo.find_claimable()]
                self.assertIn(ok_email, found, f"{fixture.name}: 可用号没被选中")
                self.assertNotIn(no_email, found,
                                 f"{fixture.name}: 未入池的号被选中了")
        self._cleanup_sqlite()

    def test_find_claimable_excludes_disabled(self):
        for fixture in self._cases():
            email = f"contract-outlook-d-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.outlook_repository import outlook_repository as repo

                row = repo.add(email=email, password="pw")
                repo.set_fields(row["id"], status="available", enabled=False)
                found = [r["email"] for r in repo.find_claimable()]
                self.assertNotIn(email, found, f"{fixture.name}: 停用的号被选中")
        self._cleanup_sqlite()

    def test_find_claimable_respects_registered_emails(self):
        """已注册的地址要能排除（`lower(email) NOT IN (...)`，大小写无关）。"""
        for fixture in self._cases():
            email = f"contract-outlook-e-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.outlook_repository import outlook_repository as repo

                row = repo.add(email=email, password="pw")
                repo.set_fields(row["id"], status="available", enabled=True)
                found = [
                    r["email"] for r in repo.find_claimable(
                        registered_emails={email.upper()}
                    )
                ]
                self.assertNotIn(email, found,
                                 f"{fixture.name}: 已注册地址没被排除（大小写？）")
        self._cleanup_sqlite()

    def test_count_by_status(self):
        for fixture in self._cases():
            email = f"contract-outlook-f-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.outlook_repository import outlook_repository as repo

                row = repo.add(email=email, password="pw")
                repo.set_fields(row["id"], status="available", enabled=True)
                self.assertGreaterEqual(repo.count_by_status(["available"]), 1)
        self._cleanup_sqlite()


if __name__ == "__main__":
    unittest.main()
