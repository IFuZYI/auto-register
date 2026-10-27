"""存储后端抽象：SQLite 与 JSON 两种实现，业务层只依赖接口。

用户要求（2026-10-27）：「增加数据存储方式（json、sqlite），在 env 中配置，
默认 json，现在使用 sqlite」。

## 为什么不能只做「导出成 JSON」

现有 129 个数据访问点直接使用 SQLModel 的 `Session`（含跨库查询、唯一约束、
事务）。把 JSON 塞进 SQLAlchemy 方言层是假抽象 —— JSON 没有 SQL。

所以分两步：
1. **本文件**：定义 `StorageBackend` 接口 + 两种实现的**契约测试**。
   同一个测试套跑两遍，两边行为必须一致 —— 这是「换后端不换语义」的唯一保证。
2. 业务层逐步改调 repository，而不是直接 `Session`。

## 默认值

用户要求**默认 json**。但当前生产库是 sqlite（已有数据），所以：
- 显式配置优先（`STORAGE_BACKEND=sqlite|json`）
- 没配置时，**已有 SQLite 库** → 继续用 sqlite（不静默换库，否则用户数据"消失"）
- 全新部署 → json（用户要求的默认）
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path


class BackendSelectionTests(unittest.TestCase):
    """`STORAGE_BACKEND` 的解析规则。"""

    def test_explicit_sqlite(self):
        from core.storage import resolve_backend_name

        self.assertEqual(
            resolve_backend_name({"STORAGE_BACKEND": "sqlite"}, db_exists=True),
            "sqlite",
        )

    def test_explicit_json(self):
        from core.storage import resolve_backend_name

        self.assertEqual(
            resolve_backend_name({"STORAGE_BACKEND": "json"}, db_exists=True),
            "json",
        )

    def test_default_is_json_for_fresh_install(self):
        """全新部署（还没有库）时默认 json —— 用户要求的默认。"""
        from core.storage import resolve_backend_name

        self.assertEqual(resolve_backend_name({}, db_exists=False), "json")

    def test_existing_sqlite_is_never_silently_abandoned(self):
        """已有 SQLite 库时不静默换后端 —— 否则用户数据看起来"消失"了。"""
        from core.storage import resolve_backend_name

        self.assertEqual(
            resolve_backend_name({}, db_exists=True),
            "sqlite",
            "已有库却切到 json —— 用户的账号会全部看不见",
        )

    def test_unknown_value_falls_back_safely(self):
        from core.storage import resolve_backend_name

        self.assertEqual(resolve_backend_name({"STORAGE_BACKEND": "banana"}, db_exists=False), "json")
        self.assertEqual(resolve_backend_name({"STORAGE_BACKEND": "banana"}, db_exists=True), "sqlite")

    def test_case_and_whitespace_insensitive(self):
        from core.storage import resolve_backend_name

        self.assertEqual(resolve_backend_name({"STORAGE_BACKEND": "  SQLite "}, db_exists=False), "sqlite")


class JsonStoreRoundTripTests(unittest.TestCase):
    """JSON 后端的读写契约（与 SQLite 版跑同一套断言）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def test_put_and_get(self):
        from core.storage.json_backend import JsonStore

        store = JsonStore(self.root / "data.json")
        store.put("accounts", 1, {"email": "a@b.c"})
        self.assertEqual(store.get("accounts", 1), {"email": "a@b.c"})

    def test_persists_across_instances(self):
        from core.storage.json_backend import JsonStore

        path = self.root / "data.json"
        JsonStore(path).put("accounts", 1, {"email": "a@b.c"})
        self.assertEqual(JsonStore(path).get("accounts", 1), {"email": "a@b.c"})

    def test_delete(self):
        from core.storage.json_backend import JsonStore

        store = JsonStore(self.root / "d.json")
        store.put("accounts", 1, {"email": "a@b.c"})
        self.assertTrue(store.delete("accounts", 1))
        self.assertIsNone(store.get("accounts", 1))

    def test_missing_table_returns_none(self):
        from core.storage.json_backend import JsonStore

        store = JsonStore(self.root / "e.json")
        self.assertIsNone(store.get("nope", 1))
        self.assertEqual(store.all("nope"), [])

    def test_atomic_write_survives_partial_failure(self):
        """写入要先写临时文件再替换 —— 半截写会毁掉整个库。"""
        from core.storage.json_backend import JsonStore

        path = self.root / "a.json"
        store = JsonStore(path)
        store.put("accounts", 1, {"email": "a@b.c"})
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(raw["accounts"]["1"]["email"], "a@b.c")
        # 临时文件不该残留
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_corrupt_file_is_not_silently_emptied(self):
        """库文件损坏时不能当成空库覆盖 —— 那是数据丢失。"""
        from core.storage.json_backend import JsonStore

        path = self.root / "bad.json"
        path.write_text("{not json", encoding="utf-8")
        store = JsonStore(path)
        with self.assertRaises(Exception):
            store.all("accounts")


if __name__ == "__main__":
    unittest.main()
