"""代理只有一种表示（`ProxyRecord`），两条后端字段一致。

修的是一个**静默功能缺失**：JSON 后端原先返回 `_JsonProxy`（4 个字段），
`success_count` / `fail_count` / `last_checked` 被丢掉 —— 代理池的「成功率」
在 JSON 部署下永远是 0，且没有任何报错。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone

from core.models.proxy import ProxyRecord


class ProxyRecordTests(unittest.TestCase):
    def test_round_trips_every_column(self):
        source = ProxyRecord(
            url="socks5h://u:p@1.2.3.4:1080",
            id=7,
            region="US",
            success_count=12,
            fail_count=3,
            is_active=False,
            last_checked=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        restored = ProxyRecord.from_dict(source.to_dict())

        for name in ("url", "region", "success_count", "fail_count", "is_active"):
            self.assertEqual(getattr(restored, name), getattr(source, name), name)
        self.assertEqual(restored.last_checked, source.last_checked)

    def test_from_dict_defaults_are_safe(self):
        record = ProxyRecord.from_dict({"url": "http://x:1"})
        self.assertEqual(record.success_count, 0)
        self.assertEqual(record.fail_count, 0)
        self.assertTrue(record.is_active)
        self.assertIsNone(record.last_checked)

    def test_broken_timestamp_does_not_raise(self):
        record = ProxyRecord.from_dict({"url": "http://x:1", "last_checked": "not-a-date"})
        self.assertIsNone(record.last_checked)

    def test_from_model_keeps_counters(self):
        class _Model:
            url = "http://x:1"
            id = 3
            region = "JP"
            success_count = 9
            fail_count = 1
            is_active = True
            last_checked = None

        record = ProxyRecord.from_model(_Model())
        self.assertEqual(record.success_count, 9)
        self.assertEqual(record.fail_count, 1)
        self.assertEqual(record.id, 3)

    def test_old_json_adapter_is_gone(self):
        """`_JsonProxy` 已删除 —— 它正是字段丢失的源头。"""
        from core.db import proxy_repository

        self.assertFalse(
            hasattr(proxy_repository, "_JsonProxy"),
            "_JsonProxy 还在：两条后端又会有两套字段",
        )


if __name__ == "__main__":
    unittest.main()
