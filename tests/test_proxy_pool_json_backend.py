"""全新 JSON 部署下代理池必须可用。

背景（审计 P0，2026-11-14 实测复现）：`proxies` 已列进 `JSON_TABLES`，所以
`init_db()` 在「全部表走 JSON」时**不建任何 SQLite 表**（那是刻意的：否则会留下
一堆永远为空的 `.db`，让人以为设了 json 还有库）。但 `ProxyPool` 的方法仍自己
`Session(current_engine())` 直查 `ProxyModel`，于是全新部署下必然：

    sqlite3.OperationalError: no such table: proxies

`get_next()` 是注册链路取代理的入口 —— 这条一炸，每个注册 attempt 都拿不到
代理，界面表现为「没有可用代理」/ 直接 500，而配置里代理明明填好了。

这里不重跑整个注册流程，只钉住根因：代理池的读写必须走仓储（仓储在 JSON 后端
下会落到 `proxies.json`），而不是自己开 SQLite 会话。
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from pathlib import Path


class JsonDeploymentProxyPoolTests(unittest.TestCase):
    def setUp(self):
        # 独立数据目录 + 显式 json 后端，模拟一次**全新部署**（没有 .db 文件）。
        self.tmp = tempfile.mkdtemp(prefix="proxy-json-")
        self._saved = {
            key: os.environ.get(key)
            for key in ("DATA_DIR", "STORAGE_BACKEND", "DATABASE_URL")
        }
        os.environ["DATA_DIR"] = self.tmp
        os.environ["STORAGE_BACKEND"] = "json"
        os.environ.pop("DATABASE_URL", None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_proxy_pool_works_without_a_sqlite_database(self):
        from core.storage import all_tables_use_json

        self.assertTrue(
            all_tables_use_json(),
            "前提失效：当前配置下并非「全部表走 JSON」，这条测试就没意义了",
        )

        from core.db import init_db

        init_db()

        from core.proxy_pool import ProxyPool

        pool = ProxyPool()

        # 空池也必须是「返回 None」，而不是抛 no such table。
        self.assertIsNone(pool.get_next(tag="register"), "空池取代理应返回 None")
        # 不在池里的代理**视为可用**是刻意的（手工指定的一次性代理没有坏证据）；
        # 这里要证明的是「没有炸」，不是真假值。
        self.assertTrue(pool.is_available("http://127.0.0.1:1"), "空池判可用不应抛错")
        self.assertEqual(pool.list_bindable(), [], "空池的可绑定列表应为空")

        # 写入后能读回来 —— 证明读写落在同一个存储上。
        # 写入走仓储（`api/proxies.py` 的添加接口也是这条路径），读取走 ProxyPool。
        from core.db import proxy_repository

        proxy_repository.add(
            url="http://user:pass@127.0.0.1:8080", region="us", tags="register",
        )
        nxt = pool.get_next(tag="register")
        self.assertEqual(nxt, "http://user:pass@127.0.0.1:8080", "写入的代理取不回来")

        # 计数也要能落库，否则成功/失败统计永远是 0。
        pool.report_success("http://user:pass@127.0.0.1:8080")
        pool.report_fail("http://user:pass@127.0.0.1:8080")
        # 计数写回同一行（`list_bindable` 只挑带 bind 标签的，这里用仓储查）。
        row = proxy_repository.find_by_url("http://user:pass@127.0.0.1:8080")
        self.assertIsNotNone(row, "计数回写后代理不该消失")
        self.assertEqual(row.success_count, 1, "成功计数没落库")
        self.assertEqual(row.fail_count, 1, "失败计数没落库")

    def test_pool_does_not_open_its_own_sqlite_session(self):
        """结构性约束：ProxyPool 里不该再有 `Session(current_engine())`。

        行为测试能覆盖「现在这段代码能跑」，但它挡不住有人下一次又顺手
        加一句 `with Session(current_engine())`。这条扫描负责那个方向。
        """
        source = Path(__file__).resolve().parents[1] / "core" / "proxy_pool.py"
        text = source.read_text(encoding="utf-8")
        # 先把三引号块（docstring）整段去掉，再扫代码 —— 文档里解释
        # 「为什么不能自己开会话」是必要的，不该被判成违规。
        code_only = re.sub(r'"""[\s\S]*?"""', "", text)
        offenders = [
            line.strip() for line in code_only.splitlines()
            if "Session(current_engine())" in line
        ]
        self.assertEqual(
            offenders, [],
            f"ProxyPool 又开始自己开 SQLite 会话了 —— JSON 后端下会 no such table: {offenders}",
        )


if __name__ == "__main__":
    unittest.main()