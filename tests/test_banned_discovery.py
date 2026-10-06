"""封禁发掘：补 RT / 绑 2FA 的登录链撞上「号没了」措辞 → 落「禁用」状态。

用户要求（2026-10-06）：
- 「已封禁可能无法直接查询，需要走登录流程发掘」；
- 原文：You do not have an account because it has been deleted or
  deactivated. …（= 账号被停用的报错）。

用户修正（2026-10-06）：「Your sign-in session is no longer valid…」**不是
封禁** —— 会话/state 不同步（Cookie、会话或跳转不同步），可重开重试。
封禁判定只认「deleted or deactivated」这类「号没了」措辞。

实测（DB task_runs 日志）：补 RT 的登录链撞上这些措辞时只报普通失败、
账号状态不动 —— 号没了还留在重试队列里，白耗代理与风控额度。

本文件钉住整条链：识别（引擎）→ 传播（结果对象 / 动作数据）→ 落状态（库侧）。
"""

from __future__ import annotations

import unittest
from unittest import mock

DEACTIVATED_TEXT = (
    "You do not have an account because it has been deleted or deactivated. "
    "If you believe this was an error, please contact us through our help center at help.openai.com."
)
# 用户修正（2026-10-06）：这不是封禁 —— 会话/state 不同步，可重试。
SIGNIN_SESSION_TEXT = "Your sign-in session is no longer valid. Please start over to continue."


class _AuthResult:
    """登录链结果替身：字段形状对齐 AuthResult。"""

    def __init__(self, **overrides):
        self.email = ""
        self.password = ""
        self.access_token = ""
        self.session_token = ""
        self.refresh_token = ""
        self.id_token = ""
        self.cookie_header = ""
        self.totp_secret = ""
        for key, value in overrides.items():
            setattr(self, key, value)


class _FakeFlow:
    """只实现补 RT 会碰到的那几个 AuthFlow 方法。"""

    def __init__(self, *, login_error=None):
        self.result = _AuthResult()
        self._login_error = login_error
        self.login_calls: list[tuple[str, str]] = []

    def from_existing_credentials(self, session_token, access_token, device_id):
        if session_token or access_token:
            self.result.session_token = session_token
            self.result.access_token = access_token or "at-refreshed"
        return self.result

    def oauth_codex_rt_exchange(self, mail_provider=None):
        return False

    def run_protocol_login(self, mail_provider, email, password=""):
        self.login_calls.append((email, password))
        if self._login_error is not None:
            raise self._login_error
        return self.result


def _backfiller(login_error):
    from platforms.chatgpt.rt_backfill import RefreshTokenBackfiller

    backfiller = RefreshTokenBackfiller(
        email="demo@example.com", password="pw", session_token="st-old", extra_config={}
    )
    queue = [_FakeFlow(), _FakeFlow(login_error=login_error)]
    backfiller._build_flow = lambda overrides: queue.pop(0)
    return backfiller


class SharedMarkerSourceTests(unittest.TestCase):
    """「号没了」判定只有一份实现（登录链三条路径共用）。"""

    def test_markers_recognize_the_user_reported_texts(self):
        from platforms.chatgpt.protocol.banned_signals import looks_like_banned

        self.assertTrue(looks_like_banned(DEACTIVATED_TEXT))
        # 用户修正：sign-in session 措辞不是封禁（会话/state 不同步，可重试）。
        self.assertFalse(looks_like_banned(SIGNIN_SESSION_TEXT))
        self.assertFalse(looks_like_banned("invalid_grant"))
        self.assertFalse(looks_like_banned(""))

    def test_login_refresh_reexports_the_shared_judgement(self):
        from platforms.chatgpt.login_refresh import looks_like_banned as from_login_refresh
        from platforms.chatgpt.protocol.banned_signals import looks_like_banned as shared

        self.assertIs(shared, from_login_refresh, "login_refresh 又存了一份私有实现")


class BackfillBannedDetectionTests(unittest.TestCase):
    """补 RT 的登录链要能认出封禁措辞（此前只报普通失败）。"""

    def test_deactivated_message_marks_banned(self):
        result = _backfiller(RuntimeError(DEACTIVATED_TEXT)).run()
        self.assertFalse(result.success)
        self.assertTrue(result.banned, "「deleted or deactivated」没被认成封禁")

    def test_signin_session_message_is_not_banned(self):
        """用户修正：sign-in session 措辞 = 会话失效（可重试），不是封禁。"""
        result = _backfiller(RuntimeError(SIGNIN_SESSION_TEXT)).run()
        self.assertFalse(result.banned, "「sign-in session is no longer valid」被误判成封禁")

    def test_ordinary_failure_is_not_banned(self):
        result = _backfiller(RuntimeError("密码错误")).run()
        self.assertFalse(result.banned, "普通失败被误判成封禁")

    def test_banned_summary_says_so(self):
        result = _backfiller(RuntimeError(DEACTIVATED_TEXT)).run()
        self.assertIn("封禁", result.summary())

    def test_banned_session_stops_before_login(self):
        """会话复用一步就认出封禁 → 不再跑协议重登（只会被同样拒绝）。"""
        from platforms.chatgpt.rt_backfill import RefreshTokenBackfiller

        class _BannedFlow(_FakeFlow):
            def oauth_codex_rt_exchange(self, mail_provider=None):
                raise RuntimeError(DEACTIVATED_TEXT)

        backfiller = RefreshTokenBackfiller(
            email="demo@example.com", password="pw", session_token="st-old", extra_config={}
        )
        login_flow = _FakeFlow()
        queue = [_BannedFlow(), login_flow]
        backfiller._build_flow = lambda overrides: queue.pop(0)
        result = backfiller.run()

        self.assertTrue(result.banned)
        self.assertEqual(login_flow.login_calls, [], "封禁后仍跑了协议重登")
        self.assertEqual(len(result.attempts), 1, "封禁应终止后续策略")


class TwoFactorBannedDetectionTests(unittest.TestCase):
    """绑 2FA 的协议重登链同样要能认出封禁措辞。"""

    def _run(self, exc):
        from platforms.chatgpt.protocol import two_factor

        flow = mock.Mock()
        flow.authorize_continue.side_effect = exc
        with mock.patch.object(two_factor, "AuthFlow", return_value=flow):
            return two_factor.bind_totp_via_login(mock.Mock(), "a@b.c", "pw")

    def test_deactivated_message_marks_banned(self):
        result = self._run(RuntimeError(DEACTIVATED_TEXT))
        self.assertFalse(result.ok)
        self.assertTrue(result.banned)

    def test_signin_session_message_is_not_banned(self):
        result = self._run(RuntimeError(SIGNIN_SESSION_TEXT))
        self.assertFalse(result.banned, "sign-in session 措辞被误判成封禁")

    def test_ordinary_failure_is_not_banned(self):
        result = self._run(RuntimeError("invalid_state"))
        self.assertFalse(result.banned)

    def test_error_string_path_also_marks_banned(self):
        """错误串路径（`_login_for_access_token` 返回原因而非抛异常）同样定性。"""
        from platforms.chatgpt.protocol import two_factor

        flow = mock.Mock()
        with mock.patch.object(
            two_factor, "AuthFlow", return_value=flow
        ), mock.patch.object(
            two_factor, "_login_for_access_token", return_value=("", DEACTIVATED_TEXT)
        ):
            result = two_factor.bind_totp_via_login(mock.Mock(), "a@b.c", "pw")

        self.assertTrue(result.banned, "错误串里的封禁措辞没被认出来")

    def test_banned_summary_says_so(self):
        result = self._run(RuntimeError(DEACTIVATED_TEXT))
        self.assertIn("封禁", result.summary())


class _ModelTests(unittest.TestCase):
    def _model(self, status="registered"):
        from core.db import AccountModel

        model = AccountModel(platform="chatgpt", email="demo@example.com", password="pw", status=status)
        model.set_extra({})
        return model


class BackfillStatusWiringTests(_ModelTests):
    """补 RT 结果落库时把封禁结论写进状态。"""

    def test_apply_result_marks_banned(self):
        from platforms.chatgpt.rt_backfill import BackfillResult
        from services.chatgpt_rt_backfill import apply_backfill_result

        model = self._model()
        apply_backfill_result(
            model,
            BackfillResult(success=False, email="demo@example.com", banned=True, error_message="账号已封禁"),
        )
        self.assertEqual(model.status, "banned", "补 RT 发掘的封禁没有落状态")

    def test_apply_result_does_not_touch_status_without_banned(self):
        from platforms.chatgpt.rt_backfill import BackfillResult
        from services.chatgpt_rt_backfill import apply_backfill_result

        model = self._model()
        apply_backfill_result(
            model,
            BackfillResult(success=False, email="demo@example.com", error_message="补 RT 失败"),
        )
        self.assertEqual(model.status, "registered", "普通失败不该动状态")


class TwoFactorStatusWiringTests(_ModelTests):
    """绑 2FA 结果落库时把封禁结论写进状态。"""

    def test_apply_result_marks_banned(self):
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult
        from services.chatgpt_two_factor import apply_two_factor_result

        model = self._model()
        apply_two_factor_result(
            model, TwoFactorBindResult(banned=True, error_message="账号已封禁")
        )
        self.assertEqual(model.status, "banned", "绑 2FA 发掘的封禁没有落状态")

    def test_apply_result_does_not_touch_status_without_banned(self):
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult
        from services.chatgpt_two_factor import apply_two_factor_result

        model = self._model()
        apply_two_factor_result(model, TwoFactorBindResult(error_message="绑定失败"))
        self.assertEqual(model.status, "registered")


class ActionBannedWiringTests(_ModelTests):
    """动作结果 → 状态落库（api/actions.py `_apply_action_result`）。"""

    def _apply(self, action_id, data):
        from api.actions import _apply_action_result

        model = self._model()
        _apply_action_result(
            "chatgpt", action_id, model, {"ok": False, "data": data}, mock.Mock()
        )
        return model

    def test_refresh_action_marks_banned(self):
        model = self._apply("refresh_token", {"banned": True, "message": "账号已封禁"})
        self.assertEqual(model.status, "banned")

    def test_backfill_action_marks_banned(self):
        model = self._apply("backfill_refresh_token", {"banned": True, "message": "账号已封禁"})
        self.assertEqual(model.status, "banned", "补 RT 动作的封禁结论没落状态")

    def test_bind_action_marks_banned(self):
        model = self._apply("bind_2fa", {"banned": True, "message": "账号已封禁"})
        self.assertEqual(model.status, "banned", "绑 2FA 动作的封禁结论没落状态")

    def test_ordinary_action_failure_does_not_touch_status(self):
        model = self._apply("backfill_refresh_token", {"banned": False, "message": "补 RT 失败"})
        self.assertEqual(model.status, "registered")


class PluginBannedDataTests(unittest.TestCase):
    """单账号动作要把 banned 结论放进 data（批量界面与状态接线都读它）。"""

    def _platform(self):
        from core.base_platform import RegisterConfig
        from platforms.chatgpt.plugin import ChatGPTPlatform

        return ChatGPTPlatform(config=RegisterConfig(extra={}))

    def _account(self):
        from core.base_platform import Account, AccountStatus

        return Account(
            platform="chatgpt",
            email="demo@example.com",
            password="pw",
            status=AccountStatus.REGISTERED,
            extra={"session_token": "st"},
        )

    def test_backfill_action_exposes_banned(self):
        from platforms.chatgpt.rt_backfill import BackfillResult

        engine_result = BackfillResult(
            success=False, email="demo@example.com", banned=True, error_message="账号已封禁"
        )
        with mock.patch(
            "services.chatgpt_rt_backfill.backfill_account_data", return_value=engine_result
        ):
            action_result = self._platform().execute_action(
                "backfill_refresh_token", self._account(), {}
            )
        self.assertTrue(action_result["data"]["banned"], "补 RT 动作没把 banned 带出来")

    def test_bind_action_exposes_banned(self):
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult

        bind_result = TwoFactorBindResult(banned=True, error_message="账号已封禁")
        with mock.patch(
            "services.chatgpt_two_factor.bind_account_two_factor", return_value=bind_result
        ):
            action_result = self._platform().execute_action("bind_2fa", self._account(), {})
        self.assertTrue(action_result["data"]["banned"], "绑 2FA 动作没把 banned 带出来")


if __name__ == "__main__":
    unittest.main()
