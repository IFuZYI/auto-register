"""启动时要说清**哪些表在哪个后端**。

## 为什么需要它

JSON 后端目前覆盖 5/7 张表。剩下的 iCloud / 邮箱池仍在 SQLite —— 于是
设了 `STORAGE_BACKEND=json` 的部署**照样会建 `account_manager.db`**。

不把这个说出来，用户会以为「设了 json 就不该有 .db 文件」，然后去查一个
不存在的问题。文档写了，但**启动日志才是他会看到的地方**。

## 为什么邮箱池不接 JSON

那些表的查询含 `OR` 状态条件、`func.lower(email)` 归一、账号类型匹配 ——
JSON 没有 SQL，移植就得**在 Python 里重写一遍同样的规则**。两份定义必然
漂移（本仓已因同类问题踩过多次），所以刻意留到「有查询层」时再做。
"""

from __future__ import annotations

import unittest


class CoverageReportTests(unittest.TestCase):
    def test_reports_both_backends(self):
        from core.storage.coverage import describe_coverage

        lines = describe_coverage(backend="json")
        joined = "\n".join(lines)
        self.assertIn("json", joined)
        self.assertIn("sqlite", joined)

    def test_lists_json_tables(self):
        from core.storage.coverage import describe_coverage

        joined = "\n".join(describe_coverage(backend="json"))
        for table in ("proxies", "task_runs", "task_logs"):
            self.assertIn(table, joined, f"没提到 {table}")

    def test_lists_sqlite_only_tables(self):
        """仍然留在 SQLite 的表要说出来 —— 这本该看见。"""
        from core.storage.coverage import describe_coverage

        joined = "\n".join(describe_coverage(backend="json"))
        for table in ("icloud_aliases", "outlook_accounts"):
            self.assertIn(table, joined, f"没提到 {table}")

    def test_sqlite_backend_says_so_plainly(self):
        from core.storage.coverage import describe_coverage

        joined = "\n".join(describe_coverage(backend="sqlite"))
        self.assertIn("全部", joined, "sqlite 后端下应一句话说清「全部表都在 sqlite」")

    def test_mentions_account_and_config_domains(self):
        """账号与配置不走 `JSON_TABLES` 白名单（它们由各自的仓储分派），
        但也是 JSON 覆盖的一部分 —— 报告要包含它们，否则读起来像漏了。"""
        from core.storage.coverage import describe_coverage

        joined = "\n".join(describe_coverage(backend="json"))
        self.assertIn("配置", joined)
        self.assertIn("账号", joined)


if __name__ == "__main__":
    unittest.main()
