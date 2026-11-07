"""封禁措辞在子链里不能被吞掉 —— 必须一路传到 `banned`。

回归背景（2026-11-05 实测）：`curls-placebo.8p@icloud.com` 在恢复任务里
Codex 登录推进撞上 ``deleted or deactivated``，异常被 `codex.py` 的
``except Exception`` 吞掉，最终只报「协议登录完成，但未拿到有效
session/access token」，账号停在 `invalid`，没有落 `banned`。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from platforms.chatgpt.login_refresh import LoginAccessTokenRefresher, LoginRefreshResult
from platforms.chatgpt.protocol.mixins.codex import CodexMixin
from platforms.chatgpt.token_refresh import TokenRefreshManager, TokenRefreshResult
from services.chatgpt_account_state import classify_local_probe_state

DEACTIVATED_TEXT = (
    "You do not have an account because it has been deleted or deactivated. "
    "If you believe this was an error, please contact us through our help center."
)


class _CodexFlow(CodexMixin):
    """最小 AuthFlow 替身：只走到 Codex 登录推进那一步。"""

    def __init__(self):
        self.result = SimpleNamespace(banned=False)
        self._codex_rt_attempted = False

    def _env_flag(self, name, default="0"):
        return True

    def _build_codex_authorize(self):
        return (
            "https://auth.example/authorize",
            "state",
            "verifier",
            "https://app.example/callback",
            "client",
        )

    def _follow_authorize_for_callback(self, *args, **kwargs):
        return "", "https://auth.openai.com/log-in"

    def _codex_drive_login_from_log_in(self, mail_provider=None):
        raise RuntimeError(f"TOTP 验证失败: 403 - {DEACTIVATED_TEXT}")


class BannedPropagationTests(unittest.TestCase):
    def test_codex_login_banned_is_not_swallowed(self):
        flow = _CodexFlow()
        self.assertFalse(flow.oauth_codex_rt_exchange())
        self.assertTrue(flow.result.banned)

    def test_login_refresher_absorbs_banned_flag(self):
        refresher = LoginAccessTokenRefresher(email="a@b.c", password="pw")
        refresher._active_flow = SimpleNamespace(result=SimpleNamespace(banned=True))
        result = LoginRefreshResult()
        refresher._absorb(result)
        self.assertTrue(result.banned)

    def test_banned_flow_with_token_is_not_reported_as_success(self):
        """封禁标记优先于「吸收到 AT」—— 否则失败分支会被翻成成功。"""
        refresher = LoginAccessTokenRefresher(email="a@b.c", password="pw")
        with patch("platforms.chatgpt.login_refresh.AuthFlow") as flow_cls:
            flow_cls.return_value.result = SimpleNamespace(
                banned=True, access_token="at", session_token="", refresh_token="",
                id_token="", cookie_header="", device_id="",
            )
            result = refresher.run()
        self.assertFalse(result.success)
        self.assertTrue(result.banned)

    def test_verify_reads_banned_body(self):
        class _Resp:
            status_code = 403
            text = DEACTIVATED_TEXT

        class _Session:
            def get(self, *args, **kwargs):
                return _Resp()

        manager = TokenRefreshManager()
        manager._create_session = lambda: _Session()
        result = TokenRefreshResult(success=True, access_token="tok")
        manager._verify(result)
        self.assertFalse(result.verified)
        self.assertTrue(result.banned)

    def test_probe_state_without_message_is_still_deactivated(self):
        self.assertEqual(
            classify_local_probe_state(
                {"auth": {"state": "account_deactivated", "http_status": 403}}
            ),
            "auth_deactivated",
        )


if __name__ == "__main__":
    unittest.main()
