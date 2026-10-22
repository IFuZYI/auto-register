"""注册时只保留 websso 上传：派生 CLI 客户端的 grok2api 接入面必须只剩 Web。

用户要求：「注册 grok 时不需要注册 build 了，只要 websso（上传 grok2api）那个。」

与 `test_grok2api_web_only.py` 的分工：
  - 那个文件钉住「派生方法整块消失」（`hasattr` / `inspect.signature`）；
  - 这个文件钉住**剩下的东西还能不能用** —— 删掉 `find_account_by_email`
    这类共享工具之后，`find_web_account_by_email` 自己是否还真的会发请求、
    真的筛 `provider=grok_web`。共享工具被连带删空时，
    `hasattr` 式断言全绿，功能其实已经死了。

这里用 `_request` 的假响应驱动**真实**的 HTTP 路径（不 mock 被测方法），
断言可观测的三件事：请求真的发了、查询串带上了邮箱、结果按 provider 筛对。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from platforms.grok.grok2api import Grok2ApiClient


class _Resp:
    """最小的假响应：只提供 `status_code` / `text` / `json()`。"""

    def __init__(self, status: int = 200, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _client() -> Grok2ApiClient:
    c = Grok2ApiClient("http://g2a.test", "admin", "pw")
    c._token = "tok"
    c._token_exp = 10**12  # 不走登录
    return c


class WebLookupStillWorksTests(unittest.TestCase):
    """`find_web_account_by_email` 在共享工具被删后必须独立工作。"""

    def test_lookup_issues_request_and_filters_provider(self):
        """同一邮箱下的 build 记录排在 Web 记录前面时，必须返回 Web 那条。"""
        payload = {
            "data": {
                "items": [
                    {"id": "900", "email": "a@b.c", "provider": "grok_build"},
                    {"id": "41", "email": "a@b.c", "provider": "grok_web"},
                ]
            }
        }
        calls: list[tuple] = []

        def _fake_request(self, method, path, **kwargs):
            calls.append((method, path, kwargs.get("headers")))
            return _Resp(200, payload)

        with patch.object(Grok2ApiClient, "_request", _fake_request):
            found = _client().find_web_account_by_email("a@b.c")

        self.assertEqual(len(calls), 1, "查账号应当只发一次请求")
        method, path = calls[0][0], calls[0][1]
        self.assertEqual(method, "GET")
        self.assertIn("/api/admin/v1/accounts", path)
        # 邮箱进查询串：没带搜索条件会拉整页，按邮箱筛就成了本地猜
        self.assertIn("search=a@b.c", path)
        self.assertIsNotNone(found)
        self.assertEqual(found.get("id"), "41", "拿到了同邮箱的 build 记录")
        self.assertEqual(found.get("provider"), "grok_web")

    def test_lookup_returns_none_when_only_non_web_records(self):
        """同邮箱只有 build/console 时返回 None —— 账号级动作不能落到它们身上。"""
        payload = {
            "data": {
                "items": [
                    {"id": "1", "email": "a@b.c", "provider": "grok_build"},
                    {"id": "2", "email": "a@b.c", "provider": "grok_console"},
                ]
            }
        }
        with patch.object(Grok2ApiClient, "_request", lambda *a, **k: _Resp(200, payload)):
            self.assertIsNone(_client().find_web_account_by_email("a@b.c"))


class IngestStopsAtWebTests(unittest.TestCase):
    """接入流程在「上传 + NSFW」之后不再碰任何派生端点。

    与 `test_grok2api_web_only.py` 的摘要断言互补：那条只看文案，
    这条看**请求**——文案说没派生、实际偷偷打了 `convert-to-build` 也会红。
    """

    def test_no_derive_endpoints_are_called(self):
        paths: list[str] = []

        def _fake_request(self, method, path, **kwargs):
            paths.append(path)
            if "web/import" in path:
                return _Resp(200, text='data: {"created":1,"updated":0,"failed":0}\n')
            if path.startswith("/api/admin/v1/accounts?"):
                return _Resp(200, {"data": {"items": [
                    {"id": "41", "email": "a@b.c", "provider": "grok_web"},
                ]}})
            return _Resp(200, text='data: {"ok":true}\n')

        with patch.object(Grok2ApiClient, "_request", _fake_request):
            ok, summary = _client().ingest_sso("sso", "a@b.c", nsfw=True)

        self.assertTrue(ok, summary)
        joined = "\n".join(paths)
        for gone in ("sync-to-console", "convert-to-build", "accounts/import"):
            self.assertNotIn(gone, joined, f"仍在请求派生/导入端点: {gone}")
        # 反向确认假响应真的驱动了路径（否则「没调派生」是因为什么都没调）
        self.assertTrue(
            any("web/import" in p for p in paths), f"Web 导入没发出请求: {paths}"
        )
        self.assertTrue(
            any(p.endswith("/accept-terms") for p in paths), f"NSFW 流程没走: {paths}"
        )


if __name__ == "__main__":
    unittest.main()
