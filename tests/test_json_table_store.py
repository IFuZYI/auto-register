"""通用 JSON 表存储：一套代码覆盖所有表。

## 为什么不做「每张表一个 store」

账号那轮写了 `AccountStoreJson`，再给代理、任务、iCloud 各写一个就是四份
形状相同、只在字段列表上不同的代码 —— 加一张表要再抄一遍，字段改了要改
四处（本仓已经因为「同一规则两份定义」踩过多次）。

通用版按 **SQLModel 的字段定义**做序列化与反序列化：模型加字段时 JSON
存储自动跟上，没有第二份清单。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


class _Row:
    """测试用的最小模型（不建表，只提供字段定义）。"""

    def __init__(self, **kw):
        for key, value in kw.items():
            setattr(self, key, value)


class GenericTableStoreTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "store.json"

    def _store(self):
        from core.storage.json_table import JsonTableStore

        return JsonTableStore(self.path, table="proxies")

    def test_insert_assigns_id(self):
        store = self._store()
        row = store.insert({"url": "socks5h://a"})
        self.assertIsInstance(row["id"], int)
        self.assertGreater(row["id"], 0)

    def test_ids_are_not_reused(self):
        store = self._store()
        a = store.insert({"url": "a"})
        store.delete(a["id"])
        b = store.insert({"url": "b"})
        self.assertNotEqual(b["id"], a["id"], "复用了已删除的 id")

    def test_get_and_update(self):
        store = self._store()
        row = store.insert({"url": "a", "region": "US"})
        store.update(row["id"], {"region": "JP"})
        got = store.get(row["id"])
        self.assertEqual(got["region"], "JP")
        self.assertEqual(got["url"], "a", "局部更新清空了其他字段")

    def test_delete_many(self):
        store = self._store()
        ids = [store.insert({"url": str(i)})["id"] for i in range(3)]
        self.assertEqual(store.delete_many(ids[:2]), 2)
        self.assertEqual(store.count(), 1)

    def test_datetime_roundtrip(self):
        """datetime 要能原样回来 —— 调用方会读 `.year` / 做比较。"""
        store = self._store()
        moment = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
        row = store.insert({"created_at": moment})
        got = store.get(row["id"])
        self.assertIsInstance(got["created_at"], datetime)
        self.assertEqual(got["created_at"].year, 2026)

    def test_find_by_field(self):
        store = self._store()
        store.insert({"url": "a", "region": "US"})
        store.insert({"url": "b", "region": "JP"})
        found = store.find(region="JP")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["url"], "b")

    def test_list_all_sorted_by_id(self):
        store = self._store()
        for i in range(5):
            store.insert({"url": str(i)})
        ids = [r["id"] for r in store.all()]
        self.assertEqual(ids, sorted(ids))

    def test_separate_tables_do_not_collide(self):
        from core.storage.json_table import JsonTableStore

        a = JsonTableStore(self.path, table="proxies")
        b = JsonTableStore(self.path, table="task_logs")
        a.insert({"url": "p"})
        b.insert({"message": "t"})
        self.assertEqual(a.count(), 1)
        self.assertEqual(b.count(), 1)
        # 各自的自增游标独立
        self.assertEqual(a.all()[0]["id"], 1)
        self.assertEqual(b.all()[0]["id"], 1)


if __name__ == "__main__":
    unittest.main()


class GenericModelSaveDispatchTests(unittest.TestCase):
    """通用 `save_model()`：按 `__tablename__` 自动路由，不逐表写分派。

    账号那轮给每张表都要加一个 `save()` 的话，5 张表就是 5 份形状相同的
    代码。这里按模型的表名自动选 store —— 加一张表**不需要改这里**。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_sqlite_backend_uses_the_session(self):
        """SQLite 后端下仍走原生 session（不改变既有行为）。"""
        from unittest import mock

        from core.storage.json_table import save_model

        class _M:
            __tablename__ = "proxies"
            id = 1

        session = mock.Mock()
        with mock.patch("core.storage.json_table.table_wants_json", return_value=False):
            save_model(_M(), session=session, commit=False)
        session.add.assert_called_once()

    def test_json_backend_writes_through_the_store(self):
        from unittest import mock

        from core.storage.json_table import save_model

        class _M:
            __tablename__ = "proxies"
            model_fields = {"id": None, "url": None}

            def __init__(self):
                self.id = 0
                self.url = "socks5h://a"

        store_path = self.root / "t.json"
        with mock.patch("core.storage.json_table.table_wants_json", return_value=True), \
             mock.patch("core.storage.json_table.table_path", return_value=store_path):
            saved = save_model(_M(), session=mock.Mock(), commit=True)
        self.assertEqual(saved["url"], "socks5h://a")

    def test_unknown_table_falls_back_to_session(self):
        """没有 JSON 支持的表名 → 走 SQLite（不静默丢写）。"""
        from unittest import mock

        from core.storage.json_table import save_model

        class _M:
            __tablename__ = "some_future_table"
            model_fields = {"id": None}

            def __init__(self):
                self.id = 1

        session = mock.Mock()
        with mock.patch("core.storage.json_table.table_path", return_value=None):
            save_model(_M(), session=session, commit=False)
        session.add.assert_called_once()


class TableWantsJsonTests(unittest.TestCase):
    def test_only_whitelisted_tables_switch(self):
        """**白名单**控制哪些表走 JSON —— 没接线的表不能悄悄切过去。

        这正是账号那轮踩过的坑：部分接线时跟着总开关走会得到「读 JSON、
        写 SQLite」的静默不一致。
        """
        from core.storage.json_table import table_wants_json

        import os
        from unittest import mock

        from core.storage.json_table import JSON_TABLES

        with mock.patch.dict(os.environ, {"STORAGE_BACKEND": "json"}, clear=False):
            with mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "json"):
                # 已完整接线（读写都走 store）的表
                for table in ("proxies", "task_runs", "task_logs"):
                    self.assertIn(table, JSON_TABLES)
                    self.assertTrue(table_wants_json(table))
                # **未接线**的表不能切 —— 只有写接了的话会「写 JSON、读 SQLite」。
                # `icloud_aliases` 仓储已就绪，但 `services/icloud/aliases.py`
                # 里还有原生会话调用点，链路未全通，所以仍在外面。
                for table in ("icloud_aliases", "icloud_accounts",
                              "outlook_accounts", "some_future_table"):
                    self.assertNotIn(
                        table, JSON_TABLES,
                        f"{table} 进了白名单 —— 请确认它的**读路径**也走 store",
                    )
                    self.assertFalse(
                        table_wants_json(table), f"{table} 未完整接线却切到了 JSON"
                    )


class BackendJudgmentUsesTheEngineTests(unittest.TestCase):
    """「有没有 SQLite 库」必须从 **engine 的 URL** 解析，不能用固定路径。

    实测踩过两次（第一次在 `core/config_store`，第二次在这里）：用固定的
    `DATA_DIR/account_manager.db` 判断时，测试环境（库在临时目录）看不到它 →
    判成「全新部署」→ 表静默切到 JSON → 3 个无关测试变红。

    所以判据抽到 `core.storage.sqlite_db_exists()` 共用，这条测试钉住它。
    """

    def test_uses_the_engine_not_a_fixed_path(self):
        import tempfile
        from pathlib import Path
        from unittest import mock

        from sqlmodel import SQLModel, create_engine

        from core.storage import sqlite_db_exists

        tmp = Path(tempfile.mkdtemp())
        db = tmp / "custom-name.db"
        engine = create_engine(f"sqlite:///{db}")

        with mock.patch("core.db.engine", engine):
            # 库还不存在 → False
            self.assertFalse(sqlite_db_exists())
            # 建了之后 → True（固定路径的判据在这里会一直是 False）
            SQLModel.metadata.create_all(engine)
            self.assertTrue(
                sqlite_db_exists(),
                "换了 engine 后判据没跟上 —— 会静默切到 JSON 后端",
            )

    def test_memory_db_counts_as_absent(self):
        from unittest import mock

        from sqlmodel import create_engine

        from core.storage import sqlite_db_exists

        with mock.patch("core.db.engine", create_engine("sqlite://")):
            self.assertFalse(sqlite_db_exists(), "内存库不该算「已有数据」")


class ExplicitKeyAndFilterCapabilitiesTests(unittest.TestCase):
    """任务表需要两个通用存储还没有的能力。

    - **显式主键**：任务 id 是字符串（`task_1793...` / `backfill_rt_...`），
      不是自增整数。通用存储原来只支持自增 id。
    - **集合过滤**：任务列表要按 `status IN (...)` 筛（`_LIVE_STATUSES`
      这类），等值过滤不够。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "tasks.json"

    def _store(self):
        from core.storage.json_table import JsonTableStore

        return JsonTableStore(self.path, table="task_runs")

    def test_put_with_explicit_string_key(self):
        store = self._store()
        store.put("task_1793000000000", {"status": "running", "total": 5})
        got = store.get("task_1793000000000")
        self.assertIsNotNone(got, "显式字符串主键取不到")
        self.assertEqual(got["status"], "running")

    def test_put_keeps_the_caller_supplied_timestamps(self):
        """`put` **不覆盖**调用方给的时间。

        任务快照自带 `created_at` / `updated_at`（描述任务本身的时间线）——
        覆盖会丢掉真实时间（任务页显示的「已运行时长」就靠它）。
        """
        store = self._store()
        store.put("k", {
            "status": "done",
            "created_at": "2026-01-02T03:04:05+00:00",
            "updated_at": "2026-01-02T04:05:06+00:00",
        })
        got = store.get("k")
        self.assertEqual(got["updated_at"].isoformat(), "2026-01-02T04:05:06+00:00")
        self.assertEqual(got["created_at"].isoformat(), "2026-01-02T03:04:05+00:00")

    def test_put_fills_missing_timestamps(self):
        store = self._store()
        got = store.put("k2", {"status": "done"})
        self.assertIsNotNone(got.get("created_at"))
        self.assertIsNotNone(got.get("updated_at"))

    def test_put_is_idempotent(self):
        store = self._store()
        store.put("k1", {"status": "running"})
        store.put("k1", {"status": "done"})
        self.assertEqual(store.get("k1")["status"], "done")
        self.assertEqual(store.count(), 1, "put 建了重复行")

    def test_explicit_key_does_not_bump_the_auto_cursor(self):
        """显式主键与自增游标互不干扰。"""
        store = self._store()
        store.put("task_x", {"status": "done"})
        auto = store.insert({"status": "auto"})
        self.assertEqual(auto["id"], 1, "显式主键把自增游标顶走了")

    def test_find_in(self):
        store = self._store()
        store.put("a", {"status": "running"})
        store.put("b", {"status": "done"})
        store.put("c", {"status": "pending"})
        found = store.find_in("status", ["running", "pending"])
        self.assertEqual(sorted(r["status"] for r in found), ["pending", "running"])

    def test_find_in_empty_values_returns_nothing(self):
        store = self._store()
        store.put("a", {"status": "running"})
        self.assertEqual(store.find_in("status", []), [])

    def test_all_accepts_string_keys(self):
        """字符串主键不能被当成数字排序而炸。"""
        store = self._store()
        store.put("task_2", {"status": "x"})
        store.put("task_10", {"status": "y"})
        store.insert({"status": "z"})
        rows = store.all()
        self.assertEqual(len(rows), 3)
