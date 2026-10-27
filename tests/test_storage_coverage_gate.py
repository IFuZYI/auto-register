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


class AccountsFollowsTheMainSwitchTests(unittest.TestCase):
    """账号跟随总开关（门槛已撤）。

    历史上账号需要**单独**的显式开关，因为当时有约 60 处原生写路径
    （`sess.add(model)`）未迁移，跟着总开关会得到「新建的账号查不到」
    （读走 JSON、写走 SQLite）。

    2026-10-27：写路径已全部收敛到 `AccountRepository.save()`（23 处），
    原生账号写一个不剩，所以门槛撤掉 —— 直接跟随 `STORAGE_BACKEND`。
    """

    def test_accounts_follow_json_when_no_override(self):
        from core.db.repository import AccountRepository

        with mock.patch.dict("os.environ", {}, clear=False) as _env:
            import os

            os.environ.pop("STORAGE_BACKEND_ACCOUNTS", None)
            with mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "json"):
                self.assertIsNotNone(
                    AccountRepository._json_store(),
                    "账号没跟随总开关 —— 门槛撤掉后应默认生效",
                )

    def test_explicit_sqlite_still_keeps_accounts_on_sqlite(self):
        """`STORAGE_BACKEND_ACCOUNTS=sqlite`：配置走 JSON、账号留 SQLite。"""
        from core.db.repository import AccountRepository

        with mock.patch.dict(
            "os.environ", {"STORAGE_BACKEND_ACCOUNTS": "sqlite"}, clear=False
        ), mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "json"):
            self.assertIsNone(AccountRepository._json_store())

    def test_opt_in_json_is_accepted(self):
        from core.db.repository import AccountRepository

        with mock.patch.dict(
            "os.environ", {"STORAGE_BACKEND_ACCOUNTS": "json"}, clear=False
        ), mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "json"):
            self.assertIsNotNone(AccountRepository._json_store())

    def test_sqlite_backend_ignores_the_accounts_switch(self):
        """总开关是 sqlite 时账号开关不生效（以总开关为准）。"""
        from core.db.repository import AccountRepository

        with mock.patch.dict(
            "os.environ",
            {"STORAGE_BACKEND": "sqlite", "STORAGE_BACKEND_ACCOUNTS": "json"},
            clear=False,
        ), mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "sqlite"):
            self.assertIsNone(AccountRepository._json_store())


class NoRawAccountWritesRemainTests(unittest.TestCase):
    """结构性：原生账号写必须一个不剩 —— 这是撤门槛的前提。

    留一处就会重现「读走 JSON、写走 SQLite → 新建的账号查不到」。
    """

    def test_no_raw_account_session_add(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        offenders: list[str] = []
        #: 账号行在代码里的变量名（`save()` 之外的裸 add）
        # 只认**明确的账号变量名**：`row` / `existing` 在多张表上复用
        # （iCloud 别名、任务记录都用），按名字扫会大量误报 —— 第一版就
        # 报了 `icloud/aliases.py:204 .add(existing)`（那是别名表）。
        patterns = (
            r"\.add\(acc_model\)",
            r"\.add\(acc\)",
            r"\.add\(account\)",
        )
        allow_row_files: set[str] = set()
        for path in list((root / "api").rglob("*.py")) + list((root / "services").rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(root))
            text = path.read_text(encoding="utf-8", errors="replace")
            for pat in patterns:
                for m in re.finditer(pat, text):
                    if pat == patterns[-1] and rel in allow_row_files:
                        continue
                    line = text[:m.start()].count("\n") + 1
                    offenders.append(f"{rel}:{line} {m.group(0)}")
        self.assertEqual(
            offenders, [],
            "仍有原生账号写 —— 撤门槛会让这些写落到 SQLite 而读走 JSON:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
