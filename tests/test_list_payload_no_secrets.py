"""列表响应不得携带凭据正文。

安全审查（2026-10-27）加固项 1，实测确认：`GET /api/accounts` 的每一行都带
**`password` 明文**与 **`extra.totp_secret`（2FA 密钥本体）**。

`_slim_extra_for_list` 的注释说「`totp_secret`（2FA 状态）列表页要读」——
但列表页真正需要的是**有没有**绑 2FA（一个布尔），不是密钥本身。密钥泄漏
比密码更严重：拿到 TOTP 密钥等于永久通过第二因子，而密码还能改。

同理 `password`：列表页从不显示它（详情页才有复制入口，走
`GET /api/accounts/{id}`）。一次 `page_size=1000` 的列表请求就能拖走整个
账号池的明文密码。
"""

from __future__ import annotations

import json
import unittest


class ListPayloadCarriesNoSecretsTests(unittest.TestCase):
    def _slim(self, extra: dict, password: str = "Pw-Secret") -> dict:
        from api.accounts import _slim_extra_for_list

        raw = json.dumps(extra, ensure_ascii=False)
        out = _slim_extra_for_list(raw)
        return json.loads(out)

    def test_totp_secret_body_is_removed(self):
        slim = self._slim({"totp_secret": "A4M6LRKLL4UF5ONTI63TC732XIUST5ZK"})
        self.assertNotIn("totp_secret", slim, "列表仍带 2FA 密钥本体")
        self.assertTrue(slim.get("has_totp_secret"), "应保留「有没有」的布尔")

    def test_has_totp_is_false_when_absent(self):
        slim = self._slim({})
        self.assertFalse(slim.get("has_totp_secret"))

    def test_other_extra_keys_survive(self):
        """不能顺手把列表页真正要用的键也删了。"""
        slim = self._slim(
            {
                "totp_secret": "SECRET",
                "register_proxy": "socks5h://p:1",
                "chatgpt_local": {"plan": "plus"},
                "sync_statuses": {"chatgpt2api": {"uploaded": True}},
            }
        )
        self.assertEqual(slim.get("register_proxy"), "socks5h://p:1")
        self.assertEqual(slim.get("chatgpt_local"), {"plan": "plus"})
        self.assertIn("sync_statuses", slim)

    def test_row_payload_omits_password_column(self):
        """用**真实的** `AccountModel`，避免假对象骗过 `jsonable_encoder`。"""
        import json as _json

        from core.db.models_account import AccountModel

        from api.accounts import _slim_account_row

        row = AccountModel(
            id=1, platform="chatgpt", email="a@b.c", password="Pw-Secret",
            token="AT", status="registered",
        )
        row.extra_json = _json.dumps({"totp_secret": "SECRET", "register_proxy": "p"})

        payload = _slim_account_row(row)

        self.assertNotIn("password", payload, "列表行仍带明文密码")
        self.assertTrue(payload.get("has_password"), "应保留「有没有密码」的布尔")
        extra = _json.loads(payload["extra_json"])
        self.assertNotIn("totp_secret", extra, "列表仍带 2FA 密钥本体")
        self.assertTrue(extra.get("has_totp_secret"))
        self.assertEqual(extra.get("register_proxy"), "p", "误删了列表要用的键")


if __name__ == "__main__":
    unittest.main()
