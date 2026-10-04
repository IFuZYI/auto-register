"""Sub2API 远端账号拉取：响应形状容错。

Sub2API 的列表响应不统一（`{code:0,data:{items}}` / 裸 `{items}` / `{data:[...]}`
都见过），写死一条路径会让对比结果静默变成"全是仅远端"。
"""

from __future__ import annotations

import unittest
from unittest import mock

from services.panel_comparison import fetch_sub2api_remote_accounts, _sub2api_extract_items


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"

    def json(self):
        return self._payload


def _account(**overrides):
    base = {
        "id": 7,
        "name": "user@example.com",
        "status": "active",
        "platform": "openai",
        "type": "oauth",
        "updated_at": "2026-03-31T04:00:00Z",
        "group_ids": [2],
    }
    base.update(overrides)
    return base


class ExtractItemsTests(unittest.TestCase):
    def test_wrapped_in_code_and_data(self):
        payload = {"code": 0, "data": {"items": [{"a": 1}]}}
        self.assertEqual(_sub2api_extract_items(payload), [{"a": 1}])

    def test_bare_items(self):
        self.assertEqual(_sub2api_extract_items({"items": [1, 2]}), [1, 2])

    def test_data_is_a_list(self):
        self.assertEqual(_sub2api_extract_items({"data": [1]}), [1])

    def test_bare_list(self):
        self.assertEqual(_sub2api_extract_items([1, 2, 3]), [1, 2, 3])

    def test_unknown_shape_is_empty(self):
        self.assertEqual(_sub2api_extract_items({"code": 0}), [])
        self.assertEqual(_sub2api_extract_items(None), [])


class Sub2ApiFetcherTests(unittest.TestCase):
    def _fetch(self, responses):
        calls = []

        def fake_get(url, params=None, headers=None, timeout=None, verify=None):
            calls.append({"url": url, "params": params, "headers": headers})
            return responses[min(len(calls) - 1, len(responses) - 1)]

        with mock.patch("requests.get", side_effect=fake_get):
            accounts = fetch_sub2api_remote_accounts(
                api_url="http://sub2api.local/", api_key="secret"
            )
        return accounts, calls

    def test_reads_the_wrapped_shape(self):
        accounts, calls = self._fetch([FakeResponse({"code": 0, "data": {"items": [_account()]}})])
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].email, "user@example.com")
        self.assertEqual(accounts[0].remote_id, "7")
        self.assertEqual(accounts[0].status, "active")

    def test_reads_the_bare_shape(self):
        accounts, _ = self._fetch([FakeResponse({"items": [_account()]})])
        self.assertEqual(len(accounts), 1)

    def test_account_wrapper_is_unwrapped(self):
        wrapped = {"account": _account(id=99, name="wrapped@example.com")}
        accounts, _ = self._fetch([FakeResponse({"items": [wrapped]})])
        self.assertEqual(accounts[0].email, "wrapped@example.com")
        self.assertEqual(accounts[0].remote_id, "99")

    def test_email_falls_back_to_extra(self):
        item = _account(name="", extra={"email": "from-extra@example.com"})
        accounts, _ = self._fetch([FakeResponse({"items": [item]})])
        self.assertEqual(accounts[0].email, "from-extra@example.com")

    def test_items_without_an_email_are_skipped(self):
        accounts, _ = self._fetch([FakeResponse({"items": [_account(name="no-email-here")]})])
        self.assertEqual(accounts, [])

    def test_business_error_code_raises(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._fetch([FakeResponse({"code": 401, "message": "invalid api key"})])
        self.assertIn("invalid api key", str(ctx.exception))

    def test_auth_header_is_x_api_key(self):
        _, calls = self._fetch([FakeResponse({"items": []})])
        self.assertEqual(calls[0]["headers"]["x-api-key"], "secret")

    def test_trailing_slash_is_trimmed(self):
        _, calls = self._fetch([FakeResponse({"items": []})])
        self.assertEqual(calls[0]["url"], "http://sub2api.local/api/v1/admin/accounts")

    def test_missing_url_raises(self):
        with self.assertRaises(RuntimeError):
            fetch_sub2api_remote_accounts(api_url="", api_key="k")

    def test_http_error_raises_with_status(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._fetch([FakeResponse({}, status_code=500)])
        self.assertIn("500", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
