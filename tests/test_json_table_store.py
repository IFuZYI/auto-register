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
