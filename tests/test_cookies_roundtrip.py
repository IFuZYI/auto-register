"""`cookies` 也是凭据，必须能往返 —— 现在导出/导入都把它丢了。

2026-11-04 独立审查（切片 E）：`core/credential_crypto.SENSITIVE_EXTRA_KEYS`
把 `cookies` 当作与 AT 同级的会话凭据（注释：「带着它就能免密续上已登录会话」），
但 `core/credential_fields.export_names()` 里没有它 —— 于是

    导出 → 导入   结果：created: 1，**cookies 消失**，无任何提示

`export_names()` 只被两条路用：`services/account_export`（导出）与
`api/accounts.py`（导入）。面板侧的比对/同步走的是另一张表 `sync_aliases()`，
所以把 `cookies` 加进导出清单**不会**改变推送/拉取的范围。

这里钉往返，不钉「某个常量里有没有这个词」—— 后者是源码扫描，改个名字就废了。
"""

from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

EMAIL = "cookies-roundtrip@example.com"
COOKIES = "session=COOKIE-SECRET; token=xyz"


class CookiesMustSurviveTheRoundTripTests(unittest.TestCase):
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

    def test_export_names_include_cookies(self):
        from core.credential_fields import export_names

        self.assertIn("cookies", export_names(), "导出清单漏了 cookies 这个会话凭据")

    def test_exported_json_carries_the_cookies_value(self):
        from core.db.models_account import AccountModel
        from services.account_export import render_accounts

        model = AccountModel(platform="chatgpt", email=EMAIL, password="pw-1")
        model.set_extra({"cookies": COOKIES, "access_token": "eyJhbGciOiJSUzI1NiJ9.at.sig"})

        out = render_accounts([model], "json")
        self.assertIn(COOKIES, out, "导出的 JSON 里必须带 cookies")

    def test_import_restores_the_cookies_value(self):
        from core.db import account_repository
        from core.db.models_account import AccountModel
        from services.account_export import render_accounts

        model = AccountModel(platform="chatgpt", email=EMAIL, password="pw-1")
        model.set_extra({"cookies": COOKIES})
        payload = render_accounts([model], "json")

        r = self.client.post("/api/accounts/import", json={
            "platform": "chatgpt",
            "lines": [payload],
            "format": "json",
        })
        self.assertEqual(r.status_code, 200, r.text[:300])

        row = account_repository.find_by_email("chatgpt", EMAIL)
        self.assertIsNotNone(row)
        self.assertEqual(
            row.get_extra().get("cookies"), COOKIES,
            "导入之后 cookies 必须还在 —— 丢了就等于丢了一个能免密续会话的凭据",
        )
        # 落盘仍然要是密文（别为了修往返把加密修回去）
        self.assertNotIn(COOKIES, row.extra_json or "")
