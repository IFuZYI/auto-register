"""grok2api 远端账号拉取：字段名与去重。

字段名是实测出来的，不是猜的 —— 写错的话对比结果会全是"无法比较"，
看起来像功能坏了，其实是解析不到时间。
"""

from __future__ import annotations

import unittest
from unittest import mock

from services.panel_comparison import fetch_grok2api_remote_accounts


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class FakeClient:
    """按 grok2api 实测的响应形状喂数据。"""

    def __init__(self, pages):
        self._pages = pages
        self.login_called = False

    def login(self, *, force=False):
        self.login_called = True
        return "token"

    def _request(self, method, path, headers=None, timeout=None):
        page = 1
        if "page=" in path:
            page = int(path.split("page=")[1].split("&")[0])
        payload = self._pages[page - 1] if page <= len(self._pages) else {"data": {"items": []}}
        return FakeResponse(payload)

    def _auth_headers(self, content_type=""):
        return {}


def _item(**overrides):
    base = {
        "id": "1",
        "provider": "grok_console",
        "email": "a@example.com",
        "enabled": True,
        "authStatus": "active",
        "lastUsedAt": "2026-10-02T05:56:43.675233+08:00",
        "createdAt": "2026-10-02T02:25:45.686973+08:00",
        "linkedAccounts": [{"id": "2", "provider": "grok_web", "email": "a@example.com"}],
    }
    base.update(overrides)
    return base


class Grok2ApiFetcherTests(unittest.TestCase):
    def _fetch(self, pages):
        client = FakeClient(pages)
        with mock.patch(
            "platforms.grok.grok2api.Grok2ApiClient.from_config",
            return_value=client,
        ):
            return fetch_grok2api_remote_accounts(api_url="http://grok.local", api_key="pw"), client

    def test_reads_the_real_field_names(self):
        """时间在 lastUsedAt，状态在 authStatus —— 不是 updated_at / status。"""
        accounts, _ = self._fetch([{"data": {"items": [_item()]}}])
        self.assertEqual(len(accounts), 1)
        self.assertIsNotNone(accounts[0].updated_at)
        self.assertEqual(accounts[0].status, "active")

    def test_falls_back_to_created_at_when_never_used(self):
        accounts, _ = self._fetch(
            [{"data": {"items": [_item(lastUsedAt="", observedModelAt="")]}}]
        )
        self.assertIsNotNone(accounts[0].updated_at)
        self.assertIn("2026-10-02", accounts[0].updated_at_raw)

    def test_duplicate_emails_collapse_to_the_most_recent(self):
        """linkedAccounts 会让同一个邮箱出现两次，只保留更近的那条。"""
        older = _item(id="9", lastUsedAt="2026-10-02T01:00:00+08:00")
        newer = _item(id="10", lastUsedAt="2026-10-02T06:00:00+08:00")
        accounts, _ = self._fetch([{"data": {"items": [older, newer]}}])
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0].remote_id, "10")

    def test_disabled_flag_comes_from_enabled(self):
        accounts, _ = self._fetch([{"data": {"items": [_item(enabled=False)]}}])
        self.assertTrue(accounts[0].extra["disabled"])

    def test_login_is_called(self):
        _, client = self._fetch([{"data": {"items": []}}])
        self.assertTrue(client.login_called)

    def test_pagination_stops_on_a_short_page(self):
        """满页继续翻，不满一页就收手。"""
        full_page = {
            "data": {
                "items": [
                    _item(id=str(i), email=f"u{i}@example.com")
                    for i in range(100)
                ]
            }
        }
        short_page = {"data": {"items": [_item(id="200", email="last@example.com")]}}
        accounts, _ = self._fetch([full_page, short_page])
        # 100 + 1，没有第三页
        self.assertEqual(len(accounts), 101)

    def test_non_200_raises_with_the_status(self):
        client = FakeClient([])
        with mock.patch(
            "platforms.grok.grok2api.Grok2ApiClient.from_config", return_value=client
        ):
            with mock.patch.object(
                client, "_request", return_value=FakeResponse({}, status_code=500)
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    fetch_grok2api_remote_accounts(api_url="http://grok.local", api_key="pw")
        self.assertIn("500", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
