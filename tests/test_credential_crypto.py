"""凭据字段级加密（T1.4）的行为契约。

盯四件事：

1. **落盘无明文** —— `extra_json` 里不能再出现 AT/RT/IDT/ST/SSO/totp_secret 的原文；
2. **读出来是明文** —— 调用方（注册链路、刷新、面板推送）一行都不用改；
3. **老数据能读** —— 迁移前的明文行原样可用，不需要一次性刷库；
4. **开关有效** —— `CREDENTIAL_ENCRYPTION=0` 时退化成恒等映射（排障用）。
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from core.credential_crypto import (
    SENSITIVE_EXTRA_KEYS,
    decrypt_extra,
    decrypt_text,
    enabled,
    encrypt_extra,
    encrypt_text,
    encrypted_key_count,
    is_ciphertext,
)
from core.db.models_account import AccountModel

SECRETS = {
    "access_token": "AT-plain-1",
    "refresh_token": "RT-plain-1",
    "id_token": "IDT-plain-1",
    "session_token": "ST-plain-1",
    "sso": "SSO-plain-1",
    "totp_secret": "JBSWY3DPEHPK3PXP",
    # cookies 是会话凭据（真库 607 行）—— 2026-10-30 起纳入加密白名单
    "cookies": "oai-did=plain-cookie-value; session=abc",
}


class RawExtraIsCiphertextTests(unittest.TestCase):
    def test_sensitive_values_are_not_written_in_plaintext(self):
        model = AccountModel(platform="chatgpt", email="crypto@x.ai", password="p")
        model.set_extra(dict(SECRETS))

        raw = json.loads(model.extra_json)
        for key in SENSITIVE_EXTRA_KEYS:
            self.assertNotIn(
                SECRETS[key], raw[key], f"{key} 落盘还是明文 —— 加密没生效"
            )
            self.assertTrue(is_ciphertext(raw[key]), f"{key} 没有密文信封前缀")

    def test_reading_back_gives_plaintext(self):
        model = AccountModel(platform="chatgpt", email="crypto2@x.ai", password="p")
        model.set_extra(dict(SECRETS))

        restored = model.get_extra()

        for key, value in SECRETS.items():
            self.assertEqual(restored[key], value, f"{key} 读回来不是明文")

    def test_non_sensitive_keys_stay_readable_in_sql(self):
        """`client_id` 之类要保持明文 —— 加密会让 SQL 的 LIKE 查询失效。"""
        model = AccountModel(platform="chatgpt", email="crypto3@x.ai", password="p")
        model.set_extra({"client_id": "app_EMoamEEZ73f0CkXaXp7hrann", "sso": "SSO-1"})

        raw = json.loads(model.extra_json)
        self.assertEqual(raw["client_id"], "app_EMoamEEZ73f0CkXaXp7hrann")
        self.assertTrue(is_ciphertext(raw["sso"]))

    def test_round_trip_is_stable(self):
        """读出来再写回去不能套娃（密文再加密一次就永远解不开）。"""
        model = AccountModel(platform="chatgpt", email="crypto4@x.ai", password="p")
        model.set_extra(dict(SECRETS))
        first = model.get_extra()
        model.set_extra(first)
        second = model.get_extra()
        self.assertEqual(first, second)
        self.assertEqual(encrypted_key_count(json.loads(model.extra_json)), len(SECRETS))


class LegacyPlaintextTests(unittest.TestCase):
    def test_plaintext_rows_still_readable(self):
        """迁移前的行：extra_json 里就是明文，必须照常能读。"""
        model = AccountModel(platform="chatgpt", email="legacy@x.ai", password="p")
        model.extra_json = json.dumps({"access_token": "AT-legacy", "sso": "SSO-legacy"})

        self.assertEqual(model.get_extra()["access_token"], "AT-legacy")
        self.assertEqual(model.get_extra()["sso"], "SSO-legacy")

    def test_decrypt_text_passes_through_plaintext(self):
        self.assertEqual(decrypt_text("not-a-cipher"), "not-a-cipher")
        self.assertEqual(decrypt_text(""), "")

    def test_encrypt_text_is_idempotent(self):
        sealed = encrypt_text("AT-1")
        self.assertEqual(encrypt_text(sealed), sealed, "已经是密文就别再加密")


class KillSwitchTests(unittest.TestCase):
    def test_disabled_keeps_plaintext(self):
        with mock.patch.dict(os.environ, {"CREDENTIAL_ENCRYPTION": "0"}):
            self.assertFalse(enabled())
            model = AccountModel(platform="chatgpt", email="off@x.ai", password="p")
            model.set_extra({"access_token": "AT-plain"})
            self.assertEqual(json.loads(model.extra_json)["access_token"], "AT-plain")

    def test_enabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CREDENTIAL_ENCRYPTION", None)
            self.assertTrue(enabled())


class PureFunctionTests(unittest.TestCase):
    def test_encrypt_extra_returns_a_copy(self):
        source = {"access_token": "AT-1", "note": "n"}
        sealed = encrypt_extra(source)
        self.assertEqual(source["access_token"], "AT-1", "不得就地改调用方的字典")
        self.assertNotEqual(sealed["access_token"], "AT-1")
        self.assertEqual(sealed["note"], "n")

    def test_decrypt_extra_returns_a_copy(self):
        sealed = encrypt_extra({"access_token": "AT-1"})
        opened = decrypt_extra(sealed)
        self.assertEqual(opened["access_token"], "AT-1")
        self.assertTrue(is_ciphertext(sealed["access_token"]), "原字典不该被解开")


if __name__ == "__main__":
    unittest.main()
