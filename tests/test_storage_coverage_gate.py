"""JSON 后端对账号的**覆盖门槛**：不完全覆盖就不许默默启用。

## 为什么必须设门槛

账号有 60 处原生写路径（`sess.add(model); sess.commit()`），而仓储只覆盖了
读写的一部分。若 `STORAGE_BACKEND=json` 直接生效：

    create_account：仓储**读**（JSON）→ 没找到 → 原生 `session` **写**（SQLite）
    下一次 find_by_email：仓储**读**（JSON）→ 仍然没找到

**新建的账号查不到** —— 静默的数据不一致，比报错严重得多。

所以账号的 JSON 后端要**单独的显式开关**（`STORAGE_BACKEND_ACCOUNTS=json`），
默认关闭；`STORAGE_BACKEND=json` 只覆盖配置。这样：

- 只设 `STORAGE_BACKEND=json`：配置走 JSON、账号仍走 SQLite —— **一致**；
- 显式开账号开关：用户明确知道「只有仓储路径走 JSON」。
"""

from __future__ import annotations

import unittest
from unittest import mock


class AccountsJsonNeedsItsOwnOptInTests(unittest.TestCase):
    def test_repository_stays_on_sqlite_by_default(self):
        """只设 `STORAGE_BACKEND=json` 时，仓储**不**切到 JSON。"""
        from core.db.repository import AccountRepository

        with mock.patch.dict(
            "os.environ", {"STORAGE_BACKEND": "json"}, clear=False
        ), mock.patch(
            "core.storage.resolve_backend_name", lambda *a, **k: "json"
        ):
            import os

            os.environ.pop("STORAGE_BACKEND_ACCOUNTS", None)
            self.assertIsNone(
                AccountRepository._json_store(),
                "账号在未显式开启时切到了 JSON —— 原生写路径会把数据写进 SQLite，"
                "而读取走 JSON，新建的账号查不到",
            )

    def test_explicit_opt_in_activates_json(self):
        from core.db.repository import AccountRepository

        with mock.patch.dict(
            "os.environ", {"STORAGE_BACKEND_ACCOUNTS": "json"}, clear=False
        ), mock.patch(
            "core.storage.resolve_backend_name", lambda *a, **k: "json"
        ):
            self.assertIsNotNone(
                AccountRepository._json_store(),
                "显式开启后仍未生效",
            )

    def test_explicit_opt_in_is_ignored_when_backend_is_sqlite(self):
        """`STORAGE_BACKEND=sqlite` 时账号开关不生效（以总开关为准）。"""
        from core.db.repository import AccountRepository

        with mock.patch.dict(
            "os.environ",
            {"STORAGE_BACKEND": "sqlite", "STORAGE_BACKEND_ACCOUNTS": "json"},
            clear=False,
        ), mock.patch(
            "core.storage.resolve_backend_name", lambda *a, **k: "sqlite"
        ):
            self.assertIsNone(AccountRepository._json_store())

    def test_wrong_value_is_ignored(self):
        from core.db.repository import AccountRepository

        with mock.patch.dict(
            "os.environ", {"STORAGE_BACKEND_ACCOUNTS": "banana"}, clear=False
        ), mock.patch(
            "core.storage.resolve_backend_name", lambda *a, **k: "json"
        ):
            self.assertIsNone(AccountRepository._json_store())


if __name__ == "__main__":
    unittest.main()
