"""2FA 写前日志的恢复必须走仓储（data-layer 审计 D-05）。

事故形态：`_apply_journal_entry` 原来开 `platform_session("chatgpt")` 直接
`select(AccountModel)`。SQLite 部署没问题；**JSON 部署下那是个空库** ——
启动时每条日志都撞 `no such table: accounts`，被兜底 except 吞成
「写前日志恢复失败」，密钥永远补不回去（2FA 等于永久锁死，服务端不下发第二次）。

这里用 JSON 后端（patch `_json_store`）跑真实恢复流程。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.base_platform import Account
from core.db.repository import AccountRepository
from core.storage.account_store_json import AccountStoreJson


class JsonBackendJournalRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.store = AccountStoreJson(self._tmp / "accounts.json")
        self.journal = self._tmp / "totp_journal"
        self.journal.mkdir(parents=True, exist_ok=True)
        self._patches = [
            mock.patch(
                "core.db.repository.AccountRepository._json_store",
                staticmethod(lambda: self.store),
            ),
            mock.patch(
                "services.chatgpt_two_factor._totp_journal_dir",
                lambda: self.journal,
            ),
        ]
        for patch in self._patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()

    def _journal_file(self, email: str, secret: str) -> Path:
        from services.chatgpt_two_factor import _write_secret_journal

        # 用生产同一条写入路径（加密 + 原子替换），不要手搓格式
        _write_secret_journal(email=email, secret=secret)
        files = list(self.journal.glob("*.json"))
        self.assertTrue(files, "日志文件没写出来")
        return files[0]

    def test_recovery_applies_secret_without_a_sqlite_table(self):
        AccountRepository.upsert(
            Account(platform="chatgpt", email="j@x.c", password="p")
        )
        self._journal_file("j@x.c", "JBSWY3DPEHPK3PXP")

        from services.chatgpt_two_factor import recover_pending_totp_secrets

        logs: list[str] = []
        applied = recover_pending_totp_secrets(log=logs.append)

        self.assertEqual(applied, 1, f"没补写成功: {logs}")
        row = AccountRepository.find_by_email("chatgpt", "j@x.c")
        self.assertEqual((row.get_extra() or {}).get("totp_secret"), "JBSWY3DPEHPK3PXP")
        # 成功补写后日志文件应被清掉
        self.assertEqual(list(self.journal.glob("*.json")), [])

    def test_missing_account_keeps_the_journal_file(self):
        self._journal_file("nobody@x.c", "JBSWY3DPEHPK3PXP")

        from services.chatgpt_two_factor import recover_pending_totp_secrets

        logs: list[str] = []
        applied = recover_pending_totp_secrets(log=logs.append)

        self.assertEqual(applied, 0)
        self.assertEqual(len(list(self.journal.glob("*.json"))), 1, "账号还没落库就把唯一副本删了")
        self.assertTrue(any("尚未落库" in line for line in logs), logs)

    def test_conflict_keeps_file_and_reports(self):
        AccountRepository.upsert(
            Account(
                platform="chatgpt",
                email="c@x.c",
                password="p",
                extra={"totp_secret": "AAAAAAAAAAAAAAAA"},
            )
        )
        self._journal_file("c@x.c", "JBSWY3DPEHPK3PXP")

        from services.chatgpt_two_factor import recover_pending_totp_secrets

        logs: list[str] = []
        recover_pending_totp_secrets(log=logs.append)

        self.assertEqual(len(list(self.journal.glob("*.json"))), 1, "冲突时不该删日志")
        self.assertTrue(any("不一致" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
