"""`accounts.token`（AT / SSO）必须加密落盘、透明读回（data-layer D-17 / api-security F8）。

Q3 的「全量凭据加密」点名了 AT / SSO，但 `accounts.token` 这一列被漏了 ——
真库 1057/1057 行是明文 JWT。修法与 `password` 完全一致：列名保持 `token`
（不动 schema），属性改名 `token_cipher`，`token` 变成 computed_field。

这条测试同时钉住三件事，缺一条都会在别处炸：

1. 落盘是密文（`v1:` 前缀），不是明文；
2. 通过属性读回是明文（否则所有读 token 的代码拿到乱码）；
3. 序列化出去仍是 `token`（老契约不变），且**不出现** `token_cipher`。
"""

from __future__ import annotations

import unittest

from sqlalchemy import text

from core.db import AccountModel, account_repository, current_engine

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJl"


class TokenEncryptionTests(unittest.TestCase):
    EMAIL = "token-enc@example.com"

    def tearDown(self):
        for row in account_repository.list_all_accounts():
            if (row.email or "").lower() == self.EMAIL:
                account_repository.delete(str(row.id), row.platform)

    def _make(self, token: str = JWT) -> AccountModel:
        model = AccountModel(platform="chatgpt", email=self.EMAIL, status="active")
        model.token = token
        saved = account_repository.upsert(model)
        return account_repository.get(str(saved.id), "chatgpt")

    def test_token_is_ciphertext_at_rest(self):
        saved = self._make()
        with current_engine().connect() as conn:
            raw = conn.execute(
                text("SELECT token FROM accounts WHERE id = :i"), {"i": str(saved.id)}
            ).scalar()
        self.assertTrue(str(raw).startswith("v1:"), f"token 列仍是明文: {str(raw)[:12]}")
        self.assertNotIn("eyJhbGci", str(raw), "明文 JWT 出现在库列里")

    def test_token_reads_back_as_plaintext(self):
        saved = self._make()
        self.assertEqual(saved.token, JWT)

    def test_token_survives_a_reload(self):
        self._make()
        again = [
            row for row in account_repository.list_all_accounts()
            if (row.email or "").lower() == self.EMAIL
        ][0]
        self.assertEqual(again.token, JWT)

    def test_serialization_exposes_token_but_not_cipher(self):
        saved = self._make()
        payload = saved.model_dump()
        self.assertEqual(payload.get("token"), JWT, "序列化出去必须是明文 token（老契约）")
        self.assertNotIn("token_cipher", payload, "密文列不该出现在响应里")

    def test_empty_token_stays_empty(self):
        saved = self._make(token="")
        self.assertEqual(saved.token, "")


if __name__ == "__main__":
    unittest.main()
