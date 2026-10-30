"""账号的 JSON 存储：与 SQLite 版语义一致。

`AccountRepository` 的每个方法都要能在 JSON 后端上跑出同样的结果 —— 否则
「换个存储后端」会变成「换一套行为」。

## 与 SQLite 版的对应

| SQLite 机制 | JSON 的等价物 |
| --- | --- |
| `AUTOINCREMENT` 主键 | `next_id()`：当前最大 id + 1 |
| `(platform, email)` 唯一 | 按该组合查找后再决定插入或更新 |
| `WHERE ... ORDER BY id` | Python 过滤 + 按 id 排序 |
| `LIMIT/OFFSET` | 切片（在排序之后） |

## 一条硬要求：id 不复用

删掉最后一个账号后新建，**不能**拿到刚删掉的那个 id —— 否则任务日志、
面板同步记录里引用的旧 id 会指向另一个账号（静默的错误数据）。
SQLite 的 AUTOINCREMENT 保证不复用，JSON 也要保证。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class AccountStoreJsonTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "accounts.json"
        from core.storage.account_store_json import AccountStoreJson

        self.store = AccountStoreJson(self.path)

    def _acct(self, **overrides):
        data = {
            "platform": "chatgpt",
            "email": "a@b.c",
            "password": "pw",
            "status": "registered",
            "extra_json": "{}",
            "cashier_url": "",
        }
        data.update(overrides)
        return data

    # ---------------------------------------------------------------- 基础

    def test_insert_assigns_id(self):
        """主键是 UUID4 字符串 —— 与 SQLite 侧同口径（ADR-0008）。"""
        row = self.store.insert(self._acct())
        self.assertIsInstance(row["id"], str)
        self.assertRegex(
            row["id"],
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )

    def test_ids_are_unique(self):
        """UUID 没有「递增」这回事，唯一性才是承重语义。"""
        a = self.store.insert(self._acct(email="a@x.c"))
        b = self.store.insert(self._acct(email="b@x.c"))
        self.assertNotEqual(b["id"], a["id"])

    def test_insert_keeps_caller_supplied_id(self):
        """调用方给了主键就用它：同一账号在 SQLite 与 JSON 里必须是同一个 id。"""
        row = self.store.insert(self._acct(id="11111111-1111-4111-8111-111111111111"))
        self.assertEqual(row["id"], "11111111-1111-4111-8111-111111111111")

    def test_ids_are_not_reused_after_delete(self):
        """删掉后新建不能复用旧 id（任务日志里会引用它）。"""
        a = self.store.insert(self._acct(email="a@x.c"))
        self.store.delete(a["id"])
        b = self.store.insert(self._acct(email="b@x.c"))
        self.assertNotEqual(b["id"], a["id"], "复用了已删除的 id —— 旧引用会指向新账号")

    def test_get_by_id(self):
        row = self.store.insert(self._acct())
        self.assertEqual(self.store.get(row["id"])["email"], "a@b.c")

    def test_get_missing_returns_none(self):
        self.assertIsNone(self.store.get(999))

    def test_delete(self):
        row = self.store.insert(self._acct())
        self.assertTrue(self.store.delete(row["id"]))
        self.assertIsNone(self.store.get(row["id"]))
        self.assertFalse(self.store.delete(row["id"]), "重复删除应返回 False")

    # ------------------------------------------------------------ 唯一键

    def test_find_by_platform_and_email(self):
        self.store.insert(self._acct())
        found = self.store.find_by_email("chatgpt", "a@b.c")
        self.assertIsNotNone(found)
        self.assertIsNone(self.store.find_by_email("grok", "a@b.c"))

    def test_update_keeps_the_same_id(self):
        row = self.store.insert(self._acct())
        updated = self.store.update(row["id"], {"password": "new-pw"})
        self.assertEqual(updated["id"], row["id"])
        self.assertEqual(self.store.get(row["id"])["password"], "new-pw")

    def test_update_partial_does_not_wipe_other_fields(self):
        row = self.store.insert(self._acct(token="AT"))
        self.store.update(row["id"], {"password": "p2"})
        self.assertEqual(self.store.get(row["id"])["token"], "AT")

    # ------------------------------------------------------------ 查询

    def _seed(self):
        for i, (plat, email, status) in enumerate([
            ("chatgpt", "a@x.c", "registered"),
            ("chatgpt", "b@x.c", "banned"),
            ("grok", "c@x.c", "registered"),
        ]):
            self.store.insert(self._acct(platform=plat, email=email, status=status))

    def test_list_all_sorted_by_created_at(self):
        """UUID 没有大小语义，排序改用 created_at：先建的在前。"""
        self._seed()
        rows = self.store.list_all()
        self.assertEqual([r["email"] for r in rows], ["a@x.c", "b@x.c", "c@x.c"])

    def test_filter_by_platform(self):
        self._seed()
        rows = self.store.list_all(platform="grok")
        self.assertEqual([r["email"] for r in rows], ["c@x.c"])

    def test_filter_by_status(self):
        self._seed()
        rows = self.store.list_all(status="banned")
        self.assertEqual([r["email"] for r in rows], ["b@x.c"])

    def test_filter_by_email_substring(self):
        self._seed()
        rows = self.store.list_all(email="b@x")
        self.assertEqual([r["email"] for r in rows], ["b@x.c"])

    def test_limit_and_offset(self):
        self._seed()
        all_rows = self.store.list_all()
        page = self.store.list_all(limit=1, offset=1)
        self.assertEqual([r["id"] for r in page], [all_rows[1]["id"]])

    def test_count(self):
        self._seed()
        self.assertEqual(self.store.count(), 3)
        self.assertEqual(self.store.count(platform="chatgpt"), 2)

    def test_delete_many(self):
        self._seed()
        ids = [r["id"] for r in self.store.list_all()]
        removed = self.store.delete_many(ids[:2])
        self.assertEqual(removed, 2)
        self.assertEqual(self.store.count(), 1)


if __name__ == "__main__":
    unittest.main()
