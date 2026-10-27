"""代理表的仓储：两种后端跑同一套契约。

接 JSON 后端的完整步骤（写在 `core/storage/json_table.py` 的注释里）：
1. 给该表加仓储，**读也走它**；
2. 把散落的 `session.exec(select(...))` 改成仓储调用；
3. 把 `session.add(...)` 改成 `save_model(...)`；
4. 最后把表名加进 `JSON_TABLES`。

这条测试覆盖 1-3，第 4 步由 `test_json_table_store.py` 的白名单守卫兜底。
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
        self.path = root / "proxies.json"
        self._env = None
        self._patches: list = []

    def __enter__(self):
        from core.storage.json_table import JsonTableStore

        self._env = mock.patch.dict(
            "os.environ", {"STORAGE_BACKEND": "json"}, clear=False
        )
        self._env.start()
        self._patches = [
            mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "json"),
            mock.patch(
                "core.db.proxy_repository.ProxyRepository._store",
                staticmethod(lambda: JsonTableStore(self.path, table="proxies")),
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


class ProxyRepositoryContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _cases(self):
        return (_SqliteFixture(self.root), _JsonFixture(self.root))

    def _cleanup_sqlite(self):
        from core.db import current_engine
        from core.db.models_infra import ProxyModel
        from sqlmodel import Session, select

        with Session(current_engine()) as s:
            for row in s.exec(select(ProxyModel).where(ProxyModel.url.like("contract-%"))).all():
                s.delete(row)
            s.commit()

    def test_add_then_list(self):
        for fixture in self._cases():
            url = f"contract-a-{fixture.name}"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.proxy_repository import proxy_repository

                proxy_repository.add(url=url, region="US")
                urls = [p.url for p in proxy_repository.list_all()]
                self.assertIn(url, urls, f"{fixture.name}: 加完查不到")
        self._cleanup_sqlite()

    def test_add_is_unique_by_url(self):
        for fixture in self._cases():
            url = f"contract-b-{fixture.name}"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.proxy_repository import proxy_repository

                proxy_repository.add(url=url, region="US")
                found = proxy_repository.find_by_url(url)
                self.assertIsNotNone(found, f"{fixture.name}: 按 url 查不到")
                self.assertEqual(found.region, "US")
        self._cleanup_sqlite()

    def test_set_active_persists(self):
        for fixture in self._cases():
            url = f"contract-c-{fixture.name}"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.proxy_repository import proxy_repository

                row = proxy_repository.add(url=url, region="")
                row.is_active = False
                proxy_repository.save(row)
                again = proxy_repository.find_by_url(url)
                self.assertFalse(again.is_active, f"{fixture.name}: 改动没落库")
        self._cleanup_sqlite()

    def test_delete(self):
        for fixture in self._cases():
            url = f"contract-d-{fixture.name}"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.proxy_repository import proxy_repository

                row = proxy_repository.add(url=url, region="")
                proxy_repository.delete(row)
                self.assertIsNone(
                    proxy_repository.find_by_url(url),
                    f"{fixture.name}: 删完还能查到",
                )
        self._cleanup_sqlite()

    def test_get_many(self):
        for fixture in self._cases():
            url = f"contract-e-{fixture.name}"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.proxy_repository import proxy_repository

                row = proxy_repository.add(url=url, region="")
                found = proxy_repository.get_many([row.id])
                self.assertEqual(len(found), 1, f"{fixture.name}: 批量取不到")
        self._cleanup_sqlite()


if __name__ == "__main__":
    unittest.main()
