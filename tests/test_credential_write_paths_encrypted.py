"""凭据写入路径的加密不变式：**每一条**写路径都必须落密文。

2026-11-04 独立审查（切片 D）抓到两处新的明文落盘漏点，与之前修过的
「导入更新分支裸赋 `extra_json`」是同一形状，只是换到了仓储层：

1. `AccountRepository.update_extra()` —— 直接 `json.dumps(extra)` 写
   `extra_json`，而调用方传进来的是 `row.get_extra()`（**已解密的明文视图**）。
   于是「只改一个键」会把整份凭证还原成明文。可达路径：代理换绑/均衡
   （界面上点一次「均衡」就会遍历全池重写）、2FA journal 恢复、
   批量注册回写 `register_proxy`。
2. `AccountRepository._json_save()` —— JSON 后端写盘时 `token` 取的是
   **透明解密 property**，而隔壁 `password` 用的是 `password_cipher`。
   `data/accounts.json` 里 AT/SSO 因此是明文。

两条都钉同一个不变式：**落盘形态必须是密文**，且读回来必须还是原文。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.db import account_repository
from core.db.models_account import AccountModel
from core.db.repository import AccountRepository
from core.storage.account_store_json import AccountStoreJson

EMAIL = "cred-write-paths@example.com"
AT = "eyJhbGciOiJSUzI1NiJ9.write-path-access.signature"
RT = "eyJhbGciOiJSUzI1NiJ9.write-path-refresh.signature"
TOTP = "JBSWY3DPEHPK3PXP"


class UpdateExtraMustEncryptTests(unittest.TestCase):
    """`update_extra()` 是「只改一个键」的写路径，不能顺手把凭证还原成明文。"""

    def tearDown(self):
        row = account_repository.find_by_email("chatgpt", EMAIL)
        if row is not None:
            account_repository.delete(row.id, "chatgpt")

    def _seed(self) -> AccountModel:
        model = AccountModel(platform="chatgpt", email=EMAIL)
        model.set_extra({"access_token": AT, "refresh_token": RT, "totp_secret": TOTP})
        account_repository.upsert(model)
        row = account_repository.find_by_email("chatgpt", EMAIL)
        self.assertIsNotNone(row)
        return row

    def test_update_extra_keeps_credentials_as_ciphertext(self):
        row = self._seed()
        # 模拟真实调用方（proxy_pool.rebind / rebalance、2FA 恢复、注册回写）：
        # 拿到的是解密后的视图，改一个键，再交回 update_extra。
        payload = row.get_extra()
        payload["register_proxy"] = "http://user:pass@1.2.3.4:8080"

        self.assertTrue(account_repository.update_extra(row.id, "chatgpt", payload))

        after = account_repository.find_by_email("chatgpt", EMAIL)
        raw = after.extra_json or ""
        self.assertIn("v1:", raw, "落盘必须是密文信封")
        self.assertNotIn(AT, raw, "update_extra 不许把 AT 写成明文")
        self.assertNotIn(RT, raw, "update_extra 不许把 RT 写成明文")
        self.assertNotIn(TOTP, raw, "update_extra 不许把 2FA 密钥写成明文")

    def test_update_extra_still_returns_the_original_values_on_read(self):
        """加密不能把数据弄丢：读回来必须与写进去的一模一样。"""
        row = self._seed()
        payload = row.get_extra()
        payload["register_proxy"] = "http://user:pass@1.2.3.4:8080"
        account_repository.update_extra(row.id, "chatgpt", payload)

        extra = account_repository.find_by_email("chatgpt", EMAIL).get_extra()
        self.assertEqual(extra.get("access_token"), AT)
        self.assertEqual(extra.get("refresh_token"), RT)
        self.assertEqual(extra.get("totp_secret"), TOTP)
        self.assertEqual(extra.get("register_proxy"), "http://user:pass@1.2.3.4:8080")


class JsonSaveMustWriteCiphertextTests(unittest.TestCase):
    """JSON 后端的 `save()` 路径同样只能落密文（`_json_upsert` 已修，`_json_save` 漏了）。"""

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

    def test_save_writes_token_as_ciphertext(self):
        model = AccountModel(platform="chatgpt", email=EMAIL, password="pw-1")
        model.token = AT
        model.set_extra({"access_token": AT})

        AccountRepository.save(model)

        raw = (self._tmp / "accounts.json").read_text(encoding="utf-8")
        self.assertNotIn(AT, raw, "accounts.json 里不许出现明文 AT")
        self.assertIn("v1:", raw, "应当落密文信封")

    def test_save_then_read_returns_the_original_token(self):
        model = AccountModel(platform="chatgpt", email=EMAIL, password="pw-1")
        model.token = AT
        AccountRepository.save(model)

        found = AccountRepository.find_by_email("chatgpt", EMAIL)
        self.assertIsNotNone(found)
        self.assertEqual(found.token, AT, "读回来必须还是原文")
