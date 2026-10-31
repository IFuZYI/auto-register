"""详情端点必须返回**明文** extra 凭据。

事故（2026-10-31）：账号详情弹窗里「Access Token / Refresh Token / TOTP 2FA」
三行显示的是 `v1:0hh8AWoFj9noSt7rHDXIwVwgSLg63nrtgGWu5gDUa6NHSqGWxFnJhY...`，
复制出来也是这串。原因不是前端 —— `extra_json` 这一列**落盘就是密文**
（AT/RT/IDT/ST/SSO/totp_secret 六把凭证），而 `GET /api/accounts/{id}`
把模型直接丢给 FastAPI 序列化，密文原样下发。

列表端点裁正文是对的（`_slim_account_row`）；详情端点要给人看、给人复制，
必须解密。这两个方向不矛盾，是同一件事的两面：
**列表不给你不需要的，详情给你要的。**
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from core.db.models_account import AccountModel

AT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.sig"
RT = "M.C538_BAY.0.U.MsaArtifacts.-Cs7Y6IQ4OCtvgLl3Q05UptBt1eWnefdTsYmApSDq7mi23"
TOTP = "JBSWY3DPEHPK3PXP"


def _account() -> AccountModel:
    acc = AccountModel(platform="chatgpt", email="detail@example.com", status="registered")
    acc.password = "pw-123"
    acc.token = AT
    acc.set_extra({"access_token": AT, "refresh_token": RT, "totp_secret": TOTP})
    return acc


class AccountDetailPlaintextTests(unittest.TestCase):
    def _get(self):
        from api import accounts as accounts_api

        with mock.patch.object(
            accounts_api.account_repository, "get", return_value=_account()
        ):
            return accounts_api.get_account("acc-1", platform="chatgpt")

    def test_extra_is_decrypted(self):
        payload = self._get()
        extra = json.loads(payload["extra_json"])
        self.assertEqual(extra["access_token"], AT, "详情里的 access_token 仍是密文")
        self.assertEqual(extra["refresh_token"], RT)
        self.assertEqual(extra["totp_secret"], TOTP)
        self.assertFalse(
            str(payload["extra_json"]).startswith("v1:"), "extra_json 整串仍是密文"
        )

    def test_token_column_is_plaintext(self):
        payload = self._get()
        self.assertEqual(payload["token"], AT, "token 列没有解密")
        self.assertNotIn("token_cipher", payload, "密文列不该出现在响应里")

    def test_password_still_plaintext(self):
        payload = self._get()
        self.assertEqual(payload["password"], "pw-123")


if __name__ == "__main__":
    unittest.main()
