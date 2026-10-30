"""2FA 写前日志的密钥加密（T1.5）。

写前日志存在的意义是「进程被杀也要保住密钥」—— 它是一份**唯一副本**。
所以这里盯三件事：

1. 文件里不能有明文密钥（否则备份/同步一份日志等于泄露 2FA）；
2. 升级前写下的**明文**日志仍能读（不能因为加密把老条目的密钥弄丢）；
3. 密文解不开时**保留文件**，绝不删除（删了等于把唯一副本扔了）。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone

from sqlmodel import Session, delete, select

from core.db import AccountModel, engine
from services import chatgpt_two_factor as mod

SECRET = "JBSWY3DPEHPK3PXP"


class JournalCryptoTests(unittest.TestCase):
    def setUp(self):
        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.commit()
        for path in mod._totp_journal_dir().glob("*.json"):
            path.unlink()

    def _journal(self, email: str) -> dict:
        return json.loads(mod._journal_path(email).read_text(encoding="utf-8"))

    def test_file_contains_no_plaintext_secret(self):
        mod.make_secret_persister(email="cipher@example.com")(SECRET)

        raw = mod._journal_path("cipher@example.com").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, raw, "写前日志里出现了明文密钥")
        self.assertIn("totp_secret_cipher", raw)

    def test_legacy_plaintext_entry_is_still_recovered(self):
        """升级前写下的明文条目：必须照常补写进库。"""
        path = mod._journal_path("legacy@example.com")
        path.write_text(
            json.dumps(
                {
                    "email": "legacy@example.com",
                    "totp_secret": SECRET,
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        with Session(engine) as session:
            session.add(
                AccountModel(platform="chatgpt", email="legacy@example.com", password="pw")
            )
            session.commit()

        applied = mod.recover_pending_totp_secrets()

        self.assertEqual(applied, 1)
        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == "legacy@example.com")
            ).first()
        self.assertEqual(row.get_extra()["totp_secret"], SECRET)

    def test_undecryptable_entry_keeps_the_file(self):
        """密文坏了 / 密钥文件换了：保留文件，报一行，不许删。"""
        path = mod._journal_path("broken@example.com")
        path.write_text(
            json.dumps(
                {
                    "email": "broken@example.com",
                    "totp_secret_cipher": "v1:bm90LWEtcmVhbC1jaXBoZXI=",
                    "at": datetime.now(timezone.utc).isoformat(),
                }
            ),
            encoding="utf-8",
        )
        with Session(engine) as session:
            session.add(
                AccountModel(platform="chatgpt", email="broken@example.com", password="pw")
            )
            session.commit()

        lines: list[str] = []
        mod.recover_pending_totp_secrets(log=lines.append)

        self.assertTrue(path.exists(), "解不开就删文件 = 丢掉唯一副本")
        self.assertTrue(any("恢复失败" in line for line in lines), lines)


if __name__ == "__main__":
    unittest.main()
