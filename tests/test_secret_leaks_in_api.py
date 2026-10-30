"""凭据不能出现在「列表」类响应里（全量检测 api-security F3/F4/F5）。

三条都是同一类错误：**详情**该给的东西，**列表**也给了。

- `GET /api/accounts?page_size=1000` 一次拖走整个账号池的 AT/SSO（`token` 列）；
- `GET /api/proxies` 把 `socks5://user:pass@host:port` 原样吐出；
- 任务日志把代理串（含账号密码）写进去，再被 `/api/tasks/{id}/logs` 回吐。

列表页从不显示这些正文，所以裁掉不影响功能；详情端点保持全量。
"""

from __future__ import annotations

import unittest
from unittest import mock

from core.db.models_account import AccountModel
from core.models.proxy import ProxyRecord


def _account(token: str = "eyJhbGciOiJIUzI1NiJ9.payload.sig", password: str = "pw") -> AccountModel:
    acc = AccountModel(platform="chatgpt", email="leak@example.com", status="active")
    acc.token = token
    acc.password = password
    return acc


class AccountListPayloadTests(unittest.TestCase):
    def test_list_row_has_no_token_body(self):
        from api.accounts import _slim_account_row

        payload = _slim_account_row(_account())
        self.assertNotIn("token", payload, "列表仍在明文下发 AT/SSO")
        self.assertTrue(payload.get("has_token"), "裁掉正文后没留 has_token，页面判断不了有没有令牌")

    def test_list_row_still_hides_password(self):
        from api.accounts import _slim_account_row

        payload = _slim_account_row(_account())
        self.assertNotIn("password", payload)
        self.assertNotIn("password_cipher", payload)
        self.assertTrue(payload.get("has_password"))

    def test_empty_token_reports_false(self):
        from api.accounts import _slim_account_row

        payload = _slim_account_row(_account(token=""))
        self.assertFalse(payload.get("has_token"))


class GrokSsoLeakTests(unittest.TestCase):
    """grok 的 SSO 不能出现在列表里（data-layer 本轮新增 P0）。

    `_slim_extra_for_list` 先 `decrypt_extra` 再按黑名单 pop，而黑名单里
    既没有 `sso` 也不在 `has_*` 名单里 → 390 个 grok 号的 SSO 明文随列表下发。
    """

    def test_sso_is_replaced_by_a_boolean(self):
        import json

        from api.accounts import _slim_extra_for_list

        raw = json.dumps({"sso": "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.payload.sig", "k": "v"})
        slim = json.loads(_slim_extra_for_list(raw))
        self.assertNotIn("sso", slim, "列表仍在明文下发 grok SSO")
        self.assertTrue(slim.get("has_sso"), "裁掉正文后没留 has_sso")

    def test_sso_flag_survives_a_second_pass(self):
        import json

        from api.accounts import _slim_extra_for_list

        once = _slim_extra_for_list(json.dumps({"sso": "abc"}))
        twice = json.loads(_slim_extra_for_list(once))
        self.assertTrue(twice.get("has_sso"), "二次瘦身把 has_sso 翻成 False")


class ProxyListPayloadTests(unittest.TestCase):
    URL = "socks5h://gobngawr:06pk6as8qlwu@9.142.194.72:6740"

    def _record(self) -> ProxyRecord:
        return ProxyRecord(
            id=7,
            url=self.URL,
            region="US",
            tags=["bind"],
            success_count=3,
            fail_count=1,
            is_active=True,
            last_checked=None,
        )

    def test_proxy_list_masks_credentials(self):
        from api import proxies

        with mock.patch.object(proxies.proxy_repository, "list_all", return_value=[self._record()]):
            rows = proxies.list_proxies(session=None)

        self.assertEqual(len(rows), 1)
        url = rows[0]["url"]
        self.assertNotIn("06pk6as8qlwu", url, f"代理密码仍在响应里: {url}")
        self.assertNotIn("gobngawr", url, f"代理用户名仍在响应里: {url}")
        self.assertIn("9.142.194.72:6740", url, "打码不该把主机端口也抹掉")
        # 其余字段原样保留（前端表格要用）
        self.assertEqual(rows[0]["id"], 7)
        self.assertEqual(rows[0]["tags"], ["bind"])
        self.assertTrue(rows[0]["is_active"])

    def test_balance_rows_use_the_same_masking(self):
        """均衡接口与列表接口必须用同一种打码 —— 前端按 url 做 key 匹配。"""
        from core.proxy_utils import redact_proxy_url

        masked = redact_proxy_url(self.URL)
        self.assertNotIn("06pk6as8qlwu", masked)
        self.assertTrue(masked.startswith("socks5h://"))


class TaskLogRedactionTests(unittest.TestCase):
    def test_proxy_is_redacted_in_task_logs(self):
        from services.task_runners import _redact_proxy

        out = _redact_proxy("socks5h://user:secret@10.0.0.1:1080")
        self.assertNotIn("secret", out)
        self.assertIn("10.0.0.1:1080", out)

    def test_plain_proxy_is_unchanged(self):
        from services.task_runners import _redact_proxy

        self.assertEqual(_redact_proxy("http://127.0.0.1:8080"), "http://127.0.0.1:8080")


if __name__ == "__main__":
    unittest.main()
