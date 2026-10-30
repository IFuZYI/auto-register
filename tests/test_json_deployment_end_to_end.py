"""JSON 后端的**整机验收**：真启动、真接口、真落盘。

## 为什么需要它

前 12 轮的单测全绿，但 JSON 部署一次都跑不起来 —— 实测暴露 4 个缺陷：

1. 仓储把后端判定和 `session` 绑在一起（9 处）→ 写 SQLite、读 JSON；
2. `create_account` 的 `sess.refresh()` → 建账号 500；
3. `list_all_accounts` 缺 JSON 分支 → 列表空、统计对；
4. `count_all_accounts` 缺 JSON 分支 → `total` 恒为 0。

**三个是接线遗漏**（某个方法忘了加分支），单测覆盖不到：测试里后端被
mock、`session` 被喂内存库，正好绕过真实装配。

所以这条测试**不 mock 后端**：设 `DATA_DIR` + `STORAGE_BACKEND=json`，
跑真实 lifespan，然后检查接口返回**和磁盘上的文件**。

## 为什么不放进常规单测的 mock 体系

这一条的价值就在「不 mock」。任何把 `resolve_backend_name` 或 `_store()`
替换掉的写法都会让它退化成第 13 个假绿。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class JsonDeploymentEndToEndTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.data = Path(self._tmp.name)
        # 后端与数据目录必须**在 import main 之前**生效（`core.paths` 的
        # 常量是模块级求值）。用 patch.dict 保证测试后恢复。
        self._env = mock.patch.dict(
            "os.environ",
            {
                "DATA_DIR": str(self.data),
                "STORAGE_BACKEND": "json",
                # 让 TestClient 走本机白名单（否则 fail-closed 会 503）
                "APP_TRUST_TEST_CLIENT": "1",
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        # `core.paths.DATA_DIR` 已求值过，直接改模块属性
        import core.paths

        self._paths = mock.patch.object(core.paths, "DATA_DIR", self.data)
        self._paths.start()
        self.addCleanup(self._paths.stop)

    def test_recreating_an_existing_email_updates_instead_of_500(self):
        """同邮箱再建一次 = 走更新分支（data-layer 审计 D-04）。

        更新分支原来照抄了新建分支的 `sess.refresh(existing)`，但 JSON 后端下
        `existing` 来自 `json_record_to_model`，是 **detached** 的 ——
        `refresh` 抛 `InvalidRequestError: not persistent within this Session`，
        表现为「邮箱已存在时新增 → 500」。
        """
        import asyncio

        import main
        from fastapi.testclient import TestClient

        async def _run():
            async with main.lifespan(main.app):
                client = TestClient(main.app)
                first = client.post(
                    "/api/accounts",
                    json={"platform": "chatgpt", "email": "dup@x.c", "password": "pw1"},
                )
                self.assertEqual(first.status_code, 200, first.text[:300])

                second = client.post(
                    "/api/accounts",
                    json={
                        "platform": "chatgpt",
                        "email": "dup@x.c",
                        "password": "pw2",
                        "status": "active",
                    },
                )
                self.assertEqual(second.status_code, 200, second.text[:300])
                self.assertEqual(second.json().get("status"), "active")

                page = client.get("/api/accounts?page_size=10").json()
                self.assertEqual(page.get("total"), 1, f"重复创建插出了新行: {page}")

        asyncio.run(_run())

    def test_config_accounts_proxies_all_land_in_json(self):
        import asyncio

        import main
        from fastapi.testclient import TestClient

        async def _run():
            async with main.lifespan(main.app):
                client = TestClient(main.app)
                # 建代理
                r = client.post(
                    "/api/proxies", json={"url": "socks5h://e2e:1", "region": "US"}
                )
                self.assertEqual(r.status_code, 200, r.text[:200])

                # 建账号
                for i in range(3):
                    r = client.post(
                        "/api/accounts",
                        json={
                            "platform": "chatgpt",
                            "email": f"e2e-{i}@x.c",
                            "password": "pw",
                        },
                    )
                    self.assertEqual(r.status_code, 200, r.text[:300])

                # 列表：items 与 total **都要有**
                page = client.get("/api/accounts?page_size=5").json()
                self.assertEqual(
                    page.get("total"), 3, f"total 不对（count 缺 JSON 分支？）: {page}"
                )
                self.assertEqual(
                    len(page.get("items") or []), 3,
                    f"items 为空（list 缺 JSON 分支？）: {page}",
                )

                # 统计
                stats = client.get("/api/accounts/stats").json()
                self.assertEqual(stats.get("total"), 3, f"统计不对: {stats}")

                # 代理能读回来
                urls = [p["url"] for p in client.get("/api/proxies").json()]
                self.assertIn("socks5h://e2e:1", urls, f"代理读不回: {urls}")

        asyncio.run(_run())

        # **磁盘核对**：这才是 JSON 后端的定义
        accounts_file = self.data / "accounts.json"
        proxies_file = self.data / "proxies.json"
        self.assertTrue(
            accounts_file.exists(),
            f"没生成 accounts.json —— 账号没落在 JSON 里。目录内容: "
            f"{sorted(p.name for p in self.data.iterdir())}",
        )
        self.assertTrue(
            proxies_file.exists(),
            f"没生成 proxies.json —— 代理没落在 JSON 里（session 门控 bug？）",
        )

        # **JSON 部署下连 .db 文件都不该有**：全部 7 张表接线后，`init_db`
        # 不再建库（见 `core.storage.all_tables_use_json`）。
        #
        # 实测踩过的三层：create_all → run_migrations → 启动时的
        # grok 迁移 / 任务孤儿收尾，都在 JSON 部署下连过 SQLite。
        db_files = sorted(str(f.relative_to(self.data)) for f in self.data.rglob("*.db"))
        self.assertEqual(
            db_files, [],
            f"JSON 部署下不该创建 .db 文件，但有: {db_files}",
        )

        # 兜底：即使将来某处又建了空库，也**不许有数据**
        import sqlite3
        import sqlite3

        for db_file in self.data.rglob("*.db"):
            conn = sqlite3.connect(db_file)
            try:
                tables = [
                    r[0]
                    for r in conn.execute(
                        "select name from sqlite_master where type='table' "
                        "and name not like 'sqlite_%'"
                    )
                ]
                for table in tables:
                    count = conn.execute(f"select count(*) from {table}").fetchone()[0]
                    self.assertEqual(
                        count, 0,
                        f"{db_file.name}:{table} 有 {count} 行 —— "
                        f"JSON 部署下不该有数据落到 SQLite",
                    )
            finally:
                conn.close()

        stored = json.loads(accounts_file.read_text(encoding="utf-8"))
        emails = {
            row.get("email") for row in (stored.get("accounts") or {}).values()
        }
        self.assertEqual(
            emails, {"e2e-0@x.c", "e2e-1@x.c", "e2e-2@x.c"},
            f"accounts.json 里的邮箱不对: {emails}",
        )


if __name__ == "__main__":
    unittest.main()
