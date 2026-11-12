"""`extra_json` 的两个历史迁移：删明文密码、补加密明文凭据。

第二轮审计实证：`upsert(AccountRecord)` 曾绕过 `encrypt_extra`，Kiro 来源
账号的 AT/RT/client_secret 在 SQLite 里是明文。这里钉住两个迁移的幂等性
与「只动敏感键」。
"""
from __future__ import annotations

import json
import unittest

from sqlmodel import Session, SQLModel, delete, select

from core.db import AccountModel, engine
from core.db.migrations import _encrypt_plaintext_extras, _scrub_extra_passwords


class ExtraMigrationTests(unittest.TestCase):
    def setUp(self):
        SQLModel.metadata.create_all(engine, tables=[AccountModel.__table__])
        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.commit()

    def _insert_plaintext(self, email: str, extra: dict) -> None:
        with Session(engine) as session:
            row = AccountModel(platform="aws", email=email, password="p")
            # 直接写列：绕过 set_extra，模拟历史明文行
            row.extra_json = json.dumps(extra, ensure_ascii=False)
            session.add(row)
            session.commit()

    def _raw(self, email: str) -> dict:
        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == email)
            ).first()
            return json.loads(row.extra_json)

    def test_scrub_removes_plaintext_password_only(self):
        self._insert_plaintext("scrub@x.ai", {"password": "pw-plain", "client_id": "cid"})
        _scrub_extra_passwords(engine)
        raw = self._raw("scrub@x.ai")
        self.assertNotIn("password", raw)
        self.assertEqual(raw["client_id"], "cid")

    def test_encrypts_plaintext_sensitive_keys(self):
        self._insert_plaintext(
            "enc@x.ai",
            {"access_token": "at-plain", "client_secret": "cs-plain", "client_id": "cid"},
        )
        _encrypt_plaintext_extras(engine)
        raw = self._raw("enc@x.ai")
        self.assertTrue(raw["access_token"].startswith("v1:"))
        self.assertTrue(raw["client_secret"].startswith("v1:"))
        self.assertEqual(raw["client_id"], "cid", "非密文键不该动")
        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == "enc@x.ai")
            ).first()
            self.assertEqual(row.get_extra()["access_token"], "at-plain")

    def test_upsert_of_existing_row_encrypts_extra(self):
        """回归：upsert(AccountRecord) 命中已存在行时曾把 extra 明文写回。"""
        from core.db import account_repository
        from core.models.account import AccountRecord

        account_repository.upsert(
            AccountRecord(
                platform="aws",
                email="upsert@x.ai",
                password="p",
                extra={"access_token": "old-at"},
            )
        )
        account_repository.upsert(
            AccountRecord(
                platform="aws",
                email="upsert@x.ai",
                password="p",
                extra={"access_token": "NEW-AT", "client_secret": "NEW-CS"},
            )
        )
        row = account_repository.find_by_email("aws", "upsert@x.ai")
        raw = json.loads(row.extra_json)
        self.assertTrue(raw["access_token"].startswith("v1:"), "已存在行 upsert 又写成明文")
        self.assertTrue(raw["client_secret"].startswith("v1:"))
        self.assertEqual(row.get_extra()["access_token"], "NEW-AT")

    def test_both_migrations_are_idempotent(self):
        self._insert_plaintext(
            "idem@x.ai", {"access_token": "at-plain", "password": "pw-plain"}
        )
        _scrub_extra_passwords(engine)
        _encrypt_plaintext_extras(engine)
        first = self._raw("idem@x.ai")
        _scrub_extra_passwords(engine)
        _encrypt_plaintext_extras(engine)
        second = self._raw("idem@x.ai")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
