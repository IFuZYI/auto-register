"""JSON 后端的账号文件里不能有明文密码 / AT（data-layer D-01/D-02）。

用户明确保留 JSON 后端（Q2），但「换后端不换行为」包括**落盘形态**：
SQLite 那边 password 与 token 都是 `v1:` 信封，JSON 这边原来是明文 ——
`cat data/accounts.json` 就等于拿到全池凭据。

这条同时钉住读回：文件里是密文，`find_by_email` 拿到的必须是明文。
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

PASSWORD = "pw-plain-123"
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig"


class JsonBackendCredentialEncryptionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp())
        self.store = AccountStoreJson(self._tmp / "accounts.json")
        self._patch = mock.patch(
            "core.db.repository.AccountRepository._json_store",
            staticmethod(lambda: self.store),
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _upsert(self) -> object:
        return AccountRepository.upsert(
            Account(
                platform="chatgpt",
                email="json-enc@example.com",
                password=PASSWORD,
                token=TOKEN,
                extra={"k": "v"},
            )
        )

    def test_file_has_no_plaintext_credentials(self):
        self._upsert()
        raw = (self._tmp / "accounts.json").read_text(encoding="utf-8")
        self.assertNotIn(PASSWORD, raw, "JSON 文件里出现了明文密码")
        self.assertNotIn(TOKEN, raw, "JSON 文件里出现了明文 AT/SSO")
        record = self.store.find_by_email("chatgpt", "json-enc@example.com")
        self.assertTrue(str(record.get("password", "")).startswith("v1:"))
        self.assertTrue(str(record.get("token", "")).startswith("v1:"))

    def test_reads_back_as_plaintext(self):
        self._upsert()
        found = AccountRepository.find_by_email("chatgpt", "json-enc@example.com")
        self.assertEqual(found.password, PASSWORD)
        self.assertEqual(found.token, TOKEN)

    def test_repeated_upsert_does_not_double_encrypt(self):
        self._upsert()
        found = AccountRepository.find_by_email("chatgpt", "json-enc@example.com")
        AccountRepository.upsert(found)  # 读出来的再写回去
        again = AccountRepository.find_by_email("chatgpt", "json-enc@example.com")
        self.assertEqual(again.password, PASSWORD)
        self.assertEqual(again.token, TOKEN)
        record = json.loads((self._tmp / "accounts.json").read_text(encoding="utf-8"))
        stored = record[0] if isinstance(record, list) else next(iter(record.values()))
        self.assertFalse(str(stored.get("password", "")).startswith("v1:v1:"))


if __name__ == "__main__":
    unittest.main()
