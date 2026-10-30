"""配置口令落盘加密（Q3 的最后一块）。

改造前 `configs` 表里 10 个口令键（`yescaptcha_key` / `sub2api_api_key` …）
是**明文**：`sqlite3 data/account_manager.db "select * from configs"` 直接看得到。
`GET /api/config` 会打码，但打码只挡接口，不挡文件。

这里钉住四件事：写入即密文、读回是明文、老明文能读、`get_all` 也是明文
（`get_all` 是任务与界面共同的入口，它若返回密文，所有调用方都会拿着
`v1:…` 去连面板）。
"""

from __future__ import annotations

import unittest

from sqlmodel import Session, select

from core.config_store import ConfigItem, config_store
from core.db import current_engine
from core.credential_crypto import ENVELOPE_PREFIX, is_ciphertext

SECRET_KEY = "yescaptcha_key"
PLAIN_KEY = "register_executor_type"


class ConfigSecretEncryptionTests(unittest.TestCase):
    def setUp(self):
        self._restore = {
            SECRET_KEY: config_store.get(SECRET_KEY, ""),
            PLAIN_KEY: config_store.get(PLAIN_KEY, ""),
        }

    def tearDown(self):
        for key, value in self._restore.items():
            if value:
                config_store.set(key, value)
            else:
                config_store.delete(key)

    def _raw(self, key: str) -> str:
        with Session(current_engine()) as session:
            item = session.exec(select(ConfigItem).where(ConfigItem.key == key)).first()
            return str(item.value) if item else ""

    def test_secret_is_stored_as_ciphertext(self):
        config_store.set(SECRET_KEY, "sk-live-123")
        raw = self._raw(SECRET_KEY)
        self.assertTrue(raw.startswith(ENVELOPE_PREFIX), f"库里不是密文: {raw[:12]}")
        self.assertNotIn("sk-live-123", raw, "库里出现了明文口令")

    def test_get_returns_plaintext(self):
        config_store.set(SECRET_KEY, "sk-live-123")
        self.assertEqual(config_store.get(SECRET_KEY), "sk-live-123")

    def test_non_secret_key_is_stored_as_is(self):
        config_store.set(PLAIN_KEY, "browser")
        self.assertEqual(self._raw(PLAIN_KEY), "browser")
        self.assertEqual(config_store.get(PLAIN_KEY), "browser")

    def test_legacy_plaintext_row_is_still_readable(self):
        """升级前写下的明文行必须能读（否则所有面板/打码平台的密钥一夜失效）。"""
        with Session(current_engine()) as session:
            item = session.exec(select(ConfigItem).where(ConfigItem.key == SECRET_KEY)).first()
            if item is None:
                item = ConfigItem(key=SECRET_KEY, value="legacy-plain")
                session.add(item)
            else:
                item.value = "legacy-plain"
            session.commit()

        self.assertEqual(config_store.get(SECRET_KEY), "legacy-plain")

    def test_get_all_returns_plaintext(self):
        config_store.set(SECRET_KEY, "sk-live-123")
        self.assertEqual(config_store.get_all().get(SECRET_KEY), "sk-live-123")

    def test_set_many_encrypts_secrets(self):
        config_store.set_many({SECRET_KEY: "sk-many-456"})
        self.assertTrue(is_ciphertext(self._raw(SECRET_KEY)))
        self.assertEqual(config_store.get(SECRET_KEY), "sk-many-456")


if __name__ == "__main__":
    unittest.main()
