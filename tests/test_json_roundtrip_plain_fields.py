"""JSON 导出/导入往返不能丢 `user_id` / `region` / `cashier_url`。

2026-11-04 独立审查（切片 E，L3）：`services/account_export._render_json` 只写
platform/email/password/totp_secret/凭证/status/created_at，**没有**这三个字段；
而 `api/accounts.py` 的 JSON 导入端却**专门有** `for column in ("user_id",
"region", "status", "cashier_url")` 的分支 —— 也就是说导入端准备接收的字段，
导出端从来不写，那些分支对本仓库自己的导出格式是**死的**。

后果：导出 → 导入 之后账号的 `user_id` / `region` / `cashier_url` 全变成空串，
`created: 1`、无任何提示。这三个不是凭据，但迁移后要重填、或依赖它们的下游
（区域筛选、收银台链接）会静默失效。
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

EMAIL = "json-roundtrip-fields@example.com"


class JsonRoundTripKeepsPlainFieldsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main as main_mod

        cls._ctx = TestClient(main_mod.app)
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def tearDown(self):
        from core.db import account_repository

        row = account_repository.find_by_email("chatgpt", EMAIL)
        if row is not None:
            account_repository.delete(row.id, "chatgpt")

    def _seed(self):
        from core.db import account_repository
        from core.db.models_account import AccountModel

        model = AccountModel(platform="chatgpt", email=EMAIL, password="pw-1")
        model.user_id = "uid-42"
        model.region = "us"
        model.cashier_url = "https://pay.example/session/abc"
        model.set_extra({"access_token": "eyJhbGciOiJSUzI1NiJ9.at.sig"})
        account_repository.upsert(model)
        return account_repository.find_by_email("chatgpt", EMAIL)

    def test_exported_json_carries_the_plain_fields(self):
        from services.account_export import render_accounts

        row = self._seed()
        out = render_accounts([row], "json")
        for value in ("uid-42", "us", "https://pay.example/session/abc"):
            self.assertIn(value, out, f"导出的 JSON 里缺 {value}")

    def test_import_restores_the_plain_fields(self):
        from core.db import account_repository
        from services.account_export import render_accounts

        row = self._seed()
        payload = render_accounts([row], "json")
        # 先删掉，确保这次是**新建**（走导入的建号分支）
        account_repository.delete(row.id, "chatgpt")

        r = self.client.post("/api/accounts/import", json={
            "platform": "chatgpt",
            "lines": [payload],
            "format": "json",
        })
        self.assertEqual(r.status_code, 200, r.text[:300])

        back = account_repository.find_by_email("chatgpt", EMAIL)
        self.assertIsNotNone(back)
        self.assertEqual(back.user_id, "uid-42", "user_id 往返丢了")
        self.assertEqual(back.region, "us", "region 往返丢了")
        self.assertEqual(back.cashier_url, "https://pay.example/session/abc",
                         "cashier_url 往返丢了")
