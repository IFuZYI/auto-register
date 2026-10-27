"""iCloud 别名表的仓储：两种后端跑同一套契约。

用上一轮建的**查询层**（`core/storage/query.py`）—— 取号的筛选条件含
`OR` 与 `lower()` 归一，一份定义两个执行器，不重写规则。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class _ApiFixture:
    """JSON 后端：把仓储的 store 指到临时文件。"""

    name = "json"

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "icloud_aliases.json"
        self._env = None
        self._patches: list = []

    def __enter__(self):
        from core.storage.json_table import JsonTableStore

        self._env = mock.patch.dict("os.environ", {"STORAGE_BACKEND": "json"}, clear=False)
        self._env.start()
        self._patches = [
            mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "json"),
            mock.patch(
                "core.db.icloud_alias_repository.ICloudAliasRepository._store",
                staticmethod(lambda: JsonTableStore(self.path, table="icloud_aliases")),
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


class ICloudAliasRepositoryContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _cases(self):
        return (_SqliteFixture(self.root), _ApiFixture(self.root))

    def _cleanup_sqlite(self):
        from core.db import platform_session
        from core.db.models_platform import ICloudAliasModel
        from sqlmodel import select

        with platform_session("icloud") as s:
            for row in s.exec(
                select(ICloudAliasModel).where(
                    ICloudAliasModel.address.like("contract-alias-%")
                )
            ).all():
                s.delete(row)
            s.commit()

    def test_create_then_list(self):
        for fixture in self._cases():
            address = f"contract-alias-a-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.icloud_alias_repository import icloud_alias_repository as repo

                repo.upsert(account_id=999, address=address)
                rows = repo.list_aliases(account_id=999)
                self.assertIn(address, [r["address"] for r in rows],
                              f"{fixture.name}: 建完查不到")
        self._cleanup_sqlite()

    def test_upsert_is_idempotent_by_address(self):
        for fixture in self._cases():
            address = f"contract-alias-b-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.icloud_alias_repository import icloud_alias_repository as repo

                first = repo.upsert(account_id=999, address=address, label="l1")
                second = repo.upsert(account_id=999, address=address, label="l2")
                self.assertEqual(first["id"], second["id"],
                                 f"{fixture.name}: 同地址建了两行")
                rows = [r for r in repo.list_aliases(account_id=999) if r["address"] == address]
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["label"], "l2")
        self._cleanup_sqlite()

    def test_set_pool_status(self):
        for fixture in self._cases():
            address = f"contract-alias-c-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.icloud_alias_repository import icloud_alias_repository as repo

                row = repo.upsert(account_id=999, address=address)
                repo.set_pool_status(row["id"], "available")
                got = repo.get(row["id"])
                self.assertEqual(got["pool_status"], "available")
        self._cleanup_sqlite()

    def test_find_available_uses_the_query_layer(self):
        """取号用的筛选：`pool_status IN (available) AND account_id = N`。

        走查询层（一份谓词定义），不是后端各写一遍。
        """
        for fixture in self._cases():
            address = f"contract-alias-d-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.icloud_alias_repository import icloud_alias_repository as repo

                repo.upsert(account_id=999, address=address)
                repo.set_pool_status(
                    repo.list_aliases(account_id=999)[0]["id"], "available"
                )
                found = repo.find_available(account_id=999)
                self.assertIn(address, [r["address"] for r in found],
                              f"{fixture.name}: 可用别名没被筛出来")
        self._cleanup_sqlite()

    def test_delete(self):
        for fixture in self._cases():
            address = f"contract-alias-e-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.icloud_alias_repository import icloud_alias_repository as repo

                row = repo.upsert(account_id=999, address=address)
                self.assertTrue(repo.delete(row["id"]))
                self.assertIsNone(repo.get(row["id"]))
        self._cleanup_sqlite()


if __name__ == "__main__":
    unittest.main()
