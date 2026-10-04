"""「上传代理」开关：上传凭据时是否带上账号绑定的代理。

用户要求：「如果面板支持上传凭据同时设置代理的话，那么设置里增加一个开关，
就是是否上传代理（绑定的代理）。」

各面板的支持情况（读参考实现确认，不是猜的）：

- **CPA**：auth 文件 JSON 顶层支持 `proxy_url`（`sdk/auth/filestore.go` 读
  metadata 的 `proxy_url`）—— 支持。
- **chatgpt2api**：导入对象支持 `proxy` 字段（`_add_account_payloads` 的
  proxy_payload_indices 分支）—— 支持。
- **sub2api**：需要 `proxy_id`（数字），要先在 Sub2API 里建代理记录再绑定 ——
  不做（避免半吊子实现）。
- **grok2api**：SSO 导入格式不含代理（egress 是另一套订阅机制）—— 不支持。

开关按面板分开（`cpa_upload_proxy_enabled` / `chatgpt2api_upload_proxy_enabled`），
默认关（不改变现有上传行为）。开启后取账号的 `register_proxy`（绑定代理）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class ConfigKeyTests(unittest.TestCase):
    def test_proxy_switch_keys_are_whitelisted(self):
        """键必须在白名单里 —— 否则 PUT /api/config 会静默丢弃。"""
        from api.config import CONFIG_KEYS

        for key in ("cpa_upload_proxy_enabled", "chatgpt2api_upload_proxy_enabled"):
            with self.subTest(key=key):
                self.assertIn(key, CONFIG_KEYS)

    def test_frontend_has_the_switches(self):
        src = (
            Path(__file__).resolve().parents[1]
            / "frontend/src/components/settings/PanelConfigPanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("cpa_upload_proxy_enabled", src)
        self.assertIn("chatgpt2api_upload_proxy_enabled", src)
        # 布尔键要进 BOOLEAN_KEYS 才会归一成 0/1
        self.assertIn("'cpa_upload_proxy_enabled'", src)
        self.assertIn("'chatgpt2api_upload_proxy_enabled'", src)


class UploadProxyForTests(unittest.TestCase):
    """`upload_proxy_for(panel, extra)`：按开关与绑定返回要上传的代理。"""

    def _call(self, panel: str, extra: dict, *, enabled: str):
        from services.chatgpt_sync import upload_proxy_for

        with mock.patch("core.config_store.config_store.get", side_effect=lambda key, default="": (
            enabled if key == f"{panel}_upload_proxy_enabled" else ""
        )):
            return upload_proxy_for(panel, extra)

    def test_disabled_returns_empty(self):
        out = self._call("cpa", {"register_proxy": "socks5://p:1"}, enabled="0")
        self.assertEqual(out, "", "开关关闭时不该带代理")

    def test_unset_returns_empty(self):
        """键没设置（空串）按关处理 —— 新开关默认不改行为。"""
        out = self._call("cpa", {"register_proxy": "socks5://p:1"}, enabled="")
        self.assertEqual(out, "")

    def test_enabled_returns_bound_proxy(self):
        out = self._call("cpa", {"register_proxy": "socks5://p:1"}, enabled="1")
        self.assertEqual(out, "socks5://p:1")

    def test_enabled_without_binding_returns_empty(self):
        out = self._call("cpa", {}, enabled="1")
        self.assertEqual(out, "")

    def test_unsupported_panel_returns_empty(self):
        """grok2api / sub2api 的导入格式不支持代理 —— 永远空。"""
        for panel in ("grok2api", "sub2api"):
            with self.subTest(panel=panel):
                out = self._call(panel, {"register_proxy": "socks5://p:1"}, enabled="1")
                self.assertEqual(out, "")


class CpaProxyInjectionTests(unittest.TestCase):
    """CPA 上传：开启时 auth 记录带 `proxy_url`。"""

    def test_proxy_url_injected_when_given(self):
        """上传的记录本体（multipart 文件内容）里要有 `proxy_url`。

        注意：不改传入的 `token_data`（副本注入）—— 调用方可能复用那个 dict。
        """
        import json as _json

        from platforms.chatgpt.cpa_upload import upload_to_cpa

        captured = {}

        class _FakeMime:
            def addpart(self, name, data, filename, content_type):
                captured["name"] = name
                captured["data"] = data
                captured["filename"] = filename

            def close(self):
                pass

        def _fake_post(url, **kwargs):
            return mock.MagicMock(status_code=200, text="{}")

        with mock.patch("platforms.chatgpt.cpa_upload.CurlMime", _FakeMime), mock.patch(
            "curl_cffi.requests.post", _fake_post
        ):
            ok, _ = upload_to_cpa(
                {"email": "a@x.com", "type": "codex", "access_token": "at"},
                api_url="http://cpa.test", api_key="k",
                proxy="socks5://p:1",
            )

        self.assertTrue(ok)
        record = _json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(record.get("proxy_url"), "socks5://p:1")
        self.assertEqual(captured["name"], "file")
        self.assertTrue(captured["filename"].endswith(".json"))

    def test_no_proxy_keeps_record_clean(self):
        import json as _json

        from platforms.chatgpt.cpa_upload import upload_to_cpa

        captured = {}

        class _FakeMime:
            def addpart(self, name, data, filename, content_type):
                captured["data"] = data

            def close(self):
                pass

        with mock.patch("platforms.chatgpt.cpa_upload.CurlMime", _FakeMime), mock.patch(
            "curl_cffi.requests.post", return_value=mock.MagicMock(status_code=200, text="{}")
        ):
            upload_to_cpa(
                {"email": "a@x.com", "type": "codex", "access_token": "at"},
                api_url="http://cpa.test", api_key="k",
            )

        record = _json.loads(captured["data"].decode("utf-8"))
        self.assertNotIn("proxy_url", record, "没给代理时不该凭空写 proxy_url")


class GrokCpaProxyInjectionTests(unittest.TestCase):
    """Grok 的 CPA 上传同样支持 `proxy_url`（同一个 CPA 面板）。"""

    def test_proxy_url_injected(self):
        import json as _json

        from platforms.grok.upload import upload_to_cpa

        captured = {}

        class _FakeMime:
            def addpart(self, name, data, filename, content_type):
                captured["data"] = data

            def close(self):
                pass

        # grok/upload.py 在函数内 `from curl_cffi import CurlMime` → patch 源模块
        with mock.patch("curl_cffi.CurlMime", _FakeMime), mock.patch(
            "curl_cffi.requests.post",
            return_value=mock.MagicMock(status_code=200, text="{}"),
        ):
            ok, _ = upload_to_cpa(
                {"email": "g@x.com", "type": "xai", "access_token": "at"},
                api_url="http://cpa.test", api_key="k",
                proxy="socks5://p:1",
            )

        self.assertTrue(ok)
        record = _json.loads(captured["data"].decode("utf-8"))
        self.assertEqual(record.get("proxy_url"), "socks5://p:1")


class Chatgpt2apiProxyInjectionTests(unittest.TestCase):
    """chatgpt2api 上传：开启时导入对象带 `proxy` 字段。"""

    def test_proxy_field_added_when_given(self):
        from platforms.chatgpt.chatgpt2api_upload import build_chatgpt2api_account

        class _A:
            email = "a@x.com"
            token = ""
            access_token = "at"

        item = build_chatgpt2api_account(_A(), proxy="socks5://p:1")
        self.assertEqual(item.get("proxy"), "socks5://p:1")

    def test_no_proxy_keeps_item_clean(self):
        from platforms.chatgpt.chatgpt2api_upload import build_chatgpt2api_account

        class _A:
            email = "a@x.com"
            token = ""
            access_token = "at"

        item = build_chatgpt2api_account(_A())
        self.assertNotIn("proxy", item)


if __name__ == "__main__":
    unittest.main()
