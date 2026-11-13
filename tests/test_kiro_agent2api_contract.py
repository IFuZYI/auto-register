"""Kiro 导出必须满足 Agent2API 的账号导入契约。

Agent2API 的 `account_transfer::import_one_item` 逐条要求非空 `id`；身份回退
`uid` 先于 `email`；Kiro 插件按小写 `"builderid"` 比较 `loginProvider`。
只对齐"字段形状"而漏掉这三点的文件，导入时会 23 条全失败。
"""
from __future__ import annotations

import unittest

from scripts.check_kiro_agent2api_export import check


class KiroAgent2ApiContractTests(unittest.TestCase):
    def test_export_satisfies_the_importer_contract(self):
        self.assertEqual(check(), [])
