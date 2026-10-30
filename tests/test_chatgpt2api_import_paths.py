"""三条上传路径都必须把 password / totp_secret 带给对端。

背景（2026-10-26，对端 chatgpt2api 2.2.0）：对端文档明说「务必带
`password` + `totp_secret`。RT 会失效，没有密码就只能人工救」。而本仓的
password 存在**独立列**（不在 extra 里）、totp_secret 存在 extra 里 ——
两条路径各缺一半，等于新字段一个都没真正用上：

| 路径 | 改前 password | 改前 totp |
| --- | --- | --- |
| 单账号动作（`api/actions.py` → `plugin.py`） | ✓ | ✓ |
| 面板推送（`api/integrations.py`） | **✗** | ✓ |
| 注册自动上传（`services/external_sync.py`） | **✗** | **✗** |

这组测试钉住后两条补齐后的行为。
"""

from __future__ import annotations

import json
import unittest

from core.base_platform import Account, AccountStatus


class PanelPushCarriesPasswordTests(unittest.TestCase):
    """面板推送：password 在独立列上，必须按 id 回查后带上。"""

    def test_panel_push_sends_password_and_totp(self):
        from unittest import mock

        import services.panel_push as panel_push_mod

        captured = {}

        def _fake_upload(account, **kwargs):
            captured["account"] = account
            return True, "ok"

        row = {
            "id": 4242,
            "email": "probe@example.com",
            "platform": "chatgpt",
            "extra": {"access_token": "at-1", "totp_secret": "JBSWY3DPEHPK3PXP"},
        }

        # 按 id 回查的那一行（含独立列上的 password）
        class _Row:
            id = 4242
            password = "Pw-From-Column"
            token = "at-1"
            email = "probe@example.com"
            status = "registered"
            platform = "chatgpt"

        with mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.upload_to_chatgpt2api", _fake_upload
        ), mock.patch("services.chatgpt_sync.upload_proxy_for", lambda *a, **k: ""), \
             mock.patch.object(panel_push_mod, "_lookup_account_row", lambda _id: _Row(), create=True):
            ok, _ = panel_push_mod._push_account_uploader("chatgpt2api", "chatgpt", row)

        self.assertTrue(ok)
        account = captured["account"]
        self.assertEqual(
            getattr(account, "password", ""), "Pw-From-Column",
            "面板推送没把独立列上的 password 带上 —— 对端无法自动重登救号",
        )
        self.assertEqual(
            (account.extra or {}).get("totp_secret"), "JBSWY3DPEHPK3PXP",
            "面板推送丢了 totp_secret",
        )


class AutoUploadCarriesCredentialsTests(unittest.TestCase):
    """注册链路的自动上传：`_A` 这种轻量 shim 也要带上 password 与 totp。"""

    def test_auto_upload_account_has_password_and_totp(self):
        from services.external_sync import _build_chatgpt_upload_account_for_tests as build

        class _Model:
            email = "probe@example.com"
            password = "Pw-From-Column"
            token = "at-col"

            def get_extra(self):
                return {
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "id_token": "idt-1",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                }

        obj = build(_Model())
        self.assertEqual(getattr(obj, "password", ""), "Pw-From-Column",
                         "自动上传没带 password")
        self.assertEqual(getattr(obj, "extra", {}).get("totp_secret"),
                         "JBSWY3DPEHPK3PXP", "自动上传没带 totp_secret")

    def test_auto_upload_object_builds_a_complete_import_payload(self):
        """端到端：用 shim 产出的对象构造导入体，四个凭据字段都要在。"""
        from platforms.chatgpt.chatgpt2api_upload import build_chatgpt2api_account
        from services.external_sync import _build_chatgpt_upload_account_for_tests as build

        class _Model:
            email = "probe@example.com"
            password = "Pw-From-Column"
            token = "at-col"

            def get_extra(self):
                return {
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "id_token": "idt-1",
                    "totp_secret": "JBSWY3DPEHPK3PXP",
                }

        item = build_chatgpt2api_account(build(_Model()))
        for key in ("password", "totp_secret", "refresh_token", "id_token"):
            self.assertIn(key, item, f"导入体缺 {key}")


if __name__ == "__main__":
    unittest.main()
