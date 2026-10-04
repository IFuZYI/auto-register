"""CPA 状态同步里「账号凭证被拒」（credential_error）的语义。

背景（dogfood 实测）：线上 CPA 的 32 个 xai 账号里 21 个 CPA 侧标
`error / invalid grant` —— 探活时 CPA 管理接口返回 400
`{"error":"auth token refresh failed"}`。这是「账号的 RT 已死」而不是
「CPA 连不上」：批量结果里这 21 个账号之前全显示成「无法连接」，排查方向
被带偏；而且 `ok` 公式把它们当同步成功，界面上看着一切正常。

这些用例钉住：插件动作把 `credential_error` 报成失败（ok=False），
状态补丁里保留 `credential_error`（不退回 unreachable），消息带 CPA 的
错误文案。
"""

import unittest
from unittest.mock import patch

from core.base_platform import Account, AccountStatus, RegisterConfig


def _grok_account(**extra):
    merged = {"sso": "sso-value", "access_token": "at", "account_id": 7}
    merged.update(extra)
    return Account(
        platform="grok",
        email="g@x.com",
        password="pw",
        status=AccountStatus.REGISTERED,
        extra=merged,
    )


def _chatgpt_account(**extra):
    merged = {"account_id": 11}
    merged.update(extra)
    return Account(
        platform="chatgpt",
        email="c@x.com",
        password="pw",
        status=AccountStatus.REGISTERED,
        extra=merged,
    )


class GrokSyncCredentialErrorTests(unittest.TestCase):
    def test_credential_error_is_reported_as_failure(self):
        from platforms.grok.plugin import GrokPlatform

        platform = GrokPlatform(config=RegisterConfig(extra={}))
        account = _grok_account()
        credential_error = {
            "uploaded": True,
            "remote_state": "credential_error",
            "last_probe_status_code": 400,
            "last_probe_message": "auth token refresh failed",
            "message": "auth token refresh failed",
            "status": "error",
            "status_message": "invalid grant (retrying)",
        }

        with patch(
            "services.cliproxyapi_sync.sync_grok_cliproxyapi_status_batch",
            return_value={7: credential_error},
        ):
            result = platform.execute_action("sync_cliproxyapi_status", account, {})

        self.assertFalse(result["ok"], "凭证被拒不能报成同步成功")
        patch_data = result.get("account_extra_patch") or {}
        state = (patch_data.get("sync_statuses") or {}).get("cliproxyapi") or {}
        self.assertEqual(state.get("remote_state"), "credential_error")

    def test_usable_still_reports_success(self):
        from platforms.grok.plugin import GrokPlatform

        platform = GrokPlatform(config=RegisterConfig(extra={}))
        account = _grok_account()
        usable = {
            "uploaded": True,
            "remote_state": "usable",
            "status": "active",
            "status_message": "",
        }

        with patch(
            "services.cliproxyapi_sync.sync_grok_cliproxyapi_status_batch",
            return_value={7: usable},
        ):
            result = platform.execute_action("sync_cliproxyapi_status", account, {})

        self.assertTrue(result["ok"])


class ChatgptSyncCredentialErrorTests(unittest.TestCase):
    def test_credential_error_is_reported_as_failure(self):
        from platforms.chatgpt.plugin import ChatGPTPlatform

        platform = ChatGPTPlatform(config=RegisterConfig(extra={}))
        account = _chatgpt_account()
        credential_error = {
            "uploaded": True,
            "remote_state": "credential_error",
            "last_probe_status_code": 400,
            "last_probe_message": "auth token refresh failed",
            "message": "auth token refresh failed",
            "status": "error",
            "status_message": "invalid grant (retrying)",
        }

        with patch(
            "services.cliproxyapi_sync.sync_chatgpt_cliproxyapi_status",
            return_value=credential_error,
        ):
            result = platform.execute_action("sync_cliproxyapi_status", account, {})

        self.assertFalse(result["ok"], "凭证被拒不能报成同步成功")
        patch_data = result.get("account_extra_patch") or {}
        state = (patch_data.get("sync_statuses") or {}).get("cliproxyapi") or {}
        self.assertEqual(state.get("remote_state"), "credential_error")


if __name__ == "__main__":
    unittest.main()
