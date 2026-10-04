import logging
import threading
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from core.db import AccountModel
from core.task_runtime import StopTaskRequested
from platforms.chatgpt.rt_backfill import (
    STRATEGY_LOGIN,
    STRATEGY_SESSION,
    MailboxUnavailableProvider,
    RefreshTokenBackfiller,
)
from services.chatgpt_rt_backfill import (
    account_missing_rt,
    apply_backfill_result,
    build_extra_patch,
)


class _AuthResult:
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

    def __init__(self, *, session_result=None, login_result=None, login_error=None, session_error=None):
        self.result = _AuthResult()
        self._session_result = session_result
        self._login_result = login_result
        self._login_error = login_error
        self._session_error = session_error
        self.codex_calls = 0
        self.login_calls = []

    def from_existing_credentials(self, session_token, access_token, device_id):
        if session_token or access_token:
            self.result.session_token = session_token
            self.result.access_token = access_token or "at-refreshed"
        return self.result

    def oauth_codex_rt_exchange(self, mail_provider=None):
        self.codex_calls += 1
        if self._session_error:
            raise self._session_error
        if self._session_result:
            for key, value in self._session_result.items():
                setattr(self.result, key, value)
            return bool(self._session_result.get("refresh_token"))
        return False

    def run_protocol_login(self, mail_provider, email, password=""):
        self.login_calls.append((mail_provider, email, password))
        for key, value in (self._login_result or {}).items():
            setattr(self.result, key, value)
        if self._login_error:
            raise self._login_error
        return self.result


def _backfiller(flows, **overrides):
    """构造 backfiller，并按调用顺序发放预置的 flow。"""
    kwargs = dict(
        email="demo@example.com",
        password="pw",
        session_token="st-old",
        extra_config={},
    )
    kwargs.update(overrides)
    backfiller = RefreshTokenBackfiller(**kwargs)
    queue = list(flows)
    backfiller._build_flow = lambda overrides_dict: queue.pop(0)
    return backfiller


class RefreshTokenBackfillerTests(unittest.TestCase):
    def test_session_strategy_wins_without_touching_login(self):
        flow = _FakeFlow(session_result={"refresh_token": "rt-new", "access_token": "at-new"})
        result = _backfiller([flow]).run()

        self.assertTrue(result.success)
        self.assertEqual(result.strategy, STRATEGY_SESSION)
        self.assertEqual(result.refresh_token, "rt-new")
        self.assertEqual(result.access_token, "at-new")
        self.assertEqual(flow.codex_calls, 1)
        self.assertEqual(flow.login_calls, [])

    def test_falls_back_to_protocol_login_when_session_yields_nothing(self):
        session_flow = _FakeFlow()
        login_flow = _FakeFlow(login_result={"refresh_token": "rt-login", "session_token": "st-new"})
        result = _backfiller([session_flow, login_flow]).run()

        self.assertTrue(result.success)
        self.assertEqual(result.strategy, STRATEGY_LOGIN)
        self.assertEqual(result.refresh_token, "rt-login")
        self.assertEqual(len(login_flow.login_calls), 1)
        self.assertEqual(login_flow.login_calls[0][1:], ("demo@example.com", "pw"))

    def test_session_credentials_are_kept_when_login_also_runs(self):
        """会话那步刷新到的 access_token 不能被后面的空值抹掉。"""
        session_flow = _FakeFlow(session_result={"access_token": "at-refreshed"})
        login_flow = _FakeFlow(login_result={"refresh_token": "rt-login"})
        result = _backfiller([session_flow, login_flow]).run()

        self.assertEqual(result.access_token, "at-refreshed")
        self.assertEqual(result.refresh_token, "rt-login")

    def test_skips_session_strategy_without_stored_credentials(self):
        login_flow = _FakeFlow(login_result={"refresh_token": "rt-login"})
        backfiller = _backfiller([login_flow], session_token="", access_token="")
        result = backfiller.run()

        self.assertTrue(result.success)
        self.assertEqual(result.strategy, STRATEGY_LOGIN)
        self.assertIn("跳过会话复用", result.attempts[0].message)

    def test_without_password_login_strategy_is_not_attempted(self):
        session_flow = _FakeFlow()
        result = _backfiller([session_flow], password="").run()

        self.assertFalse(result.success)
        self.assertIn("没有密码", result.error_message)

    def test_allow_login_false_stops_after_session(self):
        session_flow = _FakeFlow()
        result = _backfiller([session_flow], allow_login=False).run()

        self.assertFalse(result.success)
        self.assertIn("已关闭协议重登", result.error_message)

    def test_login_error_is_reported_in_message(self):
        session_flow = _FakeFlow()
        login_flow = _FakeFlow(login_error=RuntimeError("密码错误"))
        result = _backfiller([session_flow, login_flow]).run()

        self.assertFalse(result.success)
        self.assertIn("密码错误", result.error_message)

    def test_rt_obtained_before_a_late_crash_is_still_kept(self):
        """RT 是链路中段换到的，末段再炸不该把它一起扔掉。"""
        session_flow = _FakeFlow()
        login_flow = _FakeFlow(
            login_result={"refresh_token": "rt-login"},
            login_error=RuntimeError("拉 session 超时"),
        )
        result = _backfiller([session_flow, login_flow]).run()

        self.assertTrue(result.success)
        self.assertEqual(result.refresh_token, "rt-login")
        self.assertIn("拉 session 超时", result.attempts[-1].message)

    def test_interruption_does_not_fall_through_to_the_next_strategy(self):
        """按了停止就别再跑协议重登 —— 那正是用户想省掉的几十秒。"""
        session_flow = _FakeFlow(session_error=StopTaskRequested())
        login_flow = _FakeFlow(login_result={"refresh_token": "rt-login"})

        with self.assertRaises(StopTaskRequested):
            _backfiller([session_flow, login_flow]).run()

        self.assertEqual(login_flow.login_calls, [])

    def test_missing_email_fails_fast(self):
        result = RefreshTokenBackfiller(email=" ").run()

        self.assertFalse(result.success)
        self.assertIn("没有邮箱", result.error_message)

    def test_login_uses_placeholder_provider_when_mailbox_unreachable(self):
        session_flow = _FakeFlow()
        login_flow = _FakeFlow(login_result={"refresh_token": "rt-login"})
        backfiller = _backfiller(
            [session_flow, login_flow],
            mail_unavailable_reason="临时邮箱已过期",
        )
        backfiller.run()

        provider = login_flow.login_calls[0][0]
        self.assertIsInstance(provider, MailboxUnavailableProvider)
        self.assertEqual(provider.create_mailbox(), "demo@example.com")
        with self.assertRaises(RuntimeError) as ctx:
            provider.wait_for_otp("demo@example.com")
        self.assertIn("临时邮箱已过期", str(ctx.exception))


class ProtocolLogMirrorTests(unittest.TestCase):
    """补一个号要跑几十秒，协议层的步骤必须能实时进到任务日志里。"""

    def setUp(self):
        self.protocol_logger = logging.getLogger("platforms.chatgpt.protocol")
        self.sms_logger = logging.getLogger("services.sms_service")
        self.original = [
            (target, target.level, list(target.handlers))
            for target in (self.protocol_logger, self.sms_logger)
        ]
        self.original_level = self.protocol_logger.level
        self.original_handlers = list(self.protocol_logger.handlers)

    def tearDown(self):
        for target, level, handlers in self.original:
            target.setLevel(level)
            target.handlers = handlers

    def test_protocol_steps_reach_the_task_log(self):
        lines: list[str] = []
        step_logger = logging.getLogger("platforms.chatgpt.protocol.auth_flow")

        class _ChattyFlow(_FakeFlow):
            def oauth_codex_rt_exchange(self, mail_provider=None):
                step_logger.info("尝试 Codex OAuth 直连换取 refresh_token ...")
                return super().oauth_codex_rt_exchange(mail_provider)

        flow = _ChattyFlow(session_result={"refresh_token": "rt-new"})
        result = _backfiller([flow], log_fn=lines.append).run()

        self.assertTrue(result.success)
        self.assertIn("[协议] 尝试 Codex OAuth 直连换取 refresh_token ...", lines)

    def test_other_threads_logs_do_not_leak_into_this_task(self):
        """批量补 RT 是多线程的，别把别的号的步骤串进来。"""
        lines: list[str] = []
        step_logger = logging.getLogger("platforms.chatgpt.protocol.auth_flow")

        class _ForeignThreadFlow(_FakeFlow):
            def oauth_codex_rt_exchange(self, mail_provider=None):
                other = threading.Thread(target=lambda: step_logger.info("别人的步骤"))
                other.start()
                other.join()
                step_logger.info("我的步骤")
                return super().oauth_codex_rt_exchange(mail_provider)

        flow = _ForeignThreadFlow(session_result={"refresh_token": "rt-new"})
        _backfiller([flow], log_fn=lines.append).run()

        self.assertIn("[协议] 我的步骤", lines)
        self.assertNotIn("[协议] 别人的步骤", lines)

    def test_sms_platform_logs_are_mirrored_too(self):
        """租号退款关系到钱，出问题时最需要在任务日志里看见。"""
        lines: list[str] = []
        sms_logger = logging.getLogger("services.sms_service")

        class _RentingFlow(_FakeFlow):
            def oauth_codex_rt_exchange(self, mail_provider=None):
                sms_logger.info("号 activation_id=%s 退款成功（原因: %s）", "1", "步骤失效")
                return super().oauth_codex_rt_exchange(mail_provider)

        flow = _RentingFlow(session_result={"refresh_token": "rt-new"})
        _backfiller([flow], log_fn=lines.append).run()

        self.assertIn("[接码平台] 号 activation_id=1 退款成功（原因: 步骤失效）", lines)

    def test_logger_is_left_as_found(self):
        flow = _FakeFlow(session_result={"refresh_token": "rt-new"})
        _backfiller([flow], log_fn=[].append).run()

        self.assertEqual(self.protocol_logger.level, self.original_level)
        self.assertEqual(self.protocol_logger.handlers, self.original_handlers)


class ProtocolOverridesTests(unittest.TestCase):
    """传给协议层的开关，决定了两条策略跑的到底是不是各自那条路。"""

    def _overrides_for(self, flows):
        with mock.patch("platforms.chatgpt.rt_backfill.AuthFlow") as flow_cls:
            flow_cls.side_effect = list(flows)
            RefreshTokenBackfiller(
                email="demo@example.com",
                password="pw",
                session_token="st-old",
                extra_config={"mailbox_otp_timeout_seconds": "90"},
            ).run()
        return [call.kwargs["env_overrides"] for call in flow_cls.call_args_list]

    def test_session_strategy_drops_prompt_login(self):
        overrides = self._overrides_for([_FakeFlow(session_result={"refresh_token": "rt"})])

        self.assertEqual(overrides[0]["OAUTH_CODEX_PROMPT"], "")
        self.assertNotIn("OAUTH_REFRESH_ONLY", overrides[0])

    def test_login_strategy_asks_only_for_refresh_token(self):
        overrides = self._overrides_for(
            [_FakeFlow(), _FakeFlow(login_result={"refresh_token": "rt"})]
        )

        self.assertEqual(overrides[1]["OAUTH_REFRESH_ONLY"], "1")
        self.assertEqual(overrides[1]["WEBUI_ALLOW_LOGIN"], "1")
        self.assertNotIn("OAUTH_CODEX_PROMPT", overrides[1])

    def test_otp_timeout_comes_from_config(self):
        overrides = self._overrides_for([_FakeFlow(session_result={"refresh_token": "rt"})])

        self.assertEqual(overrides[0]["OTP_TIMEOUT"], "90")


class BackfillPersistenceTests(unittest.TestCase):
    def _result(self, **overrides):
        from platforms.chatgpt.rt_backfill import BackfillAttempt, BackfillResult

        fields = dict(
            success=True,
            email="demo@example.com",
            strategy=STRATEGY_SESSION,
            refresh_token="rt-new",
            access_token="at-new",
            attempts=[BackfillAttempt(STRATEGY_SESSION, True, "拿到 refresh_token")],
        )
        fields.update(overrides)
        return BackfillResult(**fields)

    def test_patch_only_carries_non_empty_credentials(self):
        patch = build_extra_patch(self._result(session_token="", id_token=""))

        self.assertEqual(patch["refresh_token"], "rt-new")
        self.assertEqual(patch["access_token"], "at-new")
        self.assertNotIn("session_token", patch)
        self.assertNotIn("id_token", patch)
        self.assertTrue(patch["chatgpt_has_refresh_token_solution"])
        self.assertTrue(patch["chatgpt_rt_backfill"]["ok"])

    def test_failed_result_keeps_trace_without_claiming_rt(self):
        patch = build_extra_patch(
            self._result(success=False, strategy="", refresh_token="", access_token="")
        )

        self.assertNotIn("refresh_token", patch)
        self.assertNotIn("chatgpt_has_refresh_token_solution", patch)
        self.assertFalse(patch["chatgpt_rt_backfill"]["ok"])

    def test_apply_result_updates_extra_and_token_without_dropping_old_fields(self):
        model = AccountModel(platform="chatgpt", email="demo@example.com", password="pw", token="at-old")
        model.set_extra({"session_token": "st-old", "cookies": "c=1"})
        before = model.updated_at - timedelta(seconds=1)
        model.updated_at = before

        patch = apply_backfill_result(model, self._result())
        extra = model.get_extra()

        self.assertEqual(extra["refresh_token"], "rt-new")
        self.assertEqual(extra["session_token"], "st-old")
        self.assertEqual(extra["cookies"], "c=1")
        self.assertEqual(model.token, "at-new")
        self.assertGreater(model.updated_at.replace(tzinfo=timezone.utc), before.replace(tzinfo=timezone.utc))
        self.assertIn("chatgpt_rt_backfill", patch)

    def test_account_missing_rt_detects_both_key_spellings(self):
        model = AccountModel(platform="chatgpt", email="a@b.c", password="pw")
        model.set_extra({})
        self.assertTrue(account_missing_rt(model))

        model.set_extra({"refreshToken": "rt"})
        self.assertFalse(account_missing_rt(model))


class BackfillAccountDataTests(unittest.TestCase):
    def test_resolved_mail_provider_is_passed_to_engine(self):
        from services import chatgpt_rt_backfill

        sentinel = object()
        with mock.patch(
            "services.chatgpt_otp_mailbox.resolve_otp_mail_provider",
            return_value=(sentinel, ""),
        ), mock.patch.object(chatgpt_rt_backfill, "RefreshTokenBackfiller") as backfiller_cls:
            backfiller_cls.return_value.run.return_value = "result-sentinel"
            outcome = chatgpt_rt_backfill.backfill_account_data(
                email="demo@example.com",
                password="pw",
                extra={"session_token": "st", "access_token": "at"},
                config={"mail_provider": "cfworker"},
            )

        self.assertEqual(outcome, "result-sentinel")
        kwargs = backfiller_cls.call_args.kwargs
        self.assertIs(kwargs["mail_provider"], sentinel)
        self.assertEqual(kwargs["session_token"], "st")
        self.assertEqual(kwargs["access_token"], "at")

    def test_unreachable_mailbox_reason_reaches_engine(self):
        from services import chatgpt_rt_backfill

        with mock.patch(
            "services.chatgpt_otp_mailbox.resolve_otp_mail_provider",
            return_value=(None, "临时邮箱已过期"),
        ), mock.patch.object(chatgpt_rt_backfill, "RefreshTokenBackfiller") as backfiller_cls:
            backfiller_cls.return_value.run.return_value = "result-sentinel"
            chatgpt_rt_backfill.backfill_account_data(
                email="demo@example.com",
                password="pw",
                config={},
            )

        kwargs = backfiller_cls.call_args.kwargs
        self.assertIsNone(kwargs["mail_provider"])
        self.assertEqual(kwargs["mail_unavailable_reason"], "临时邮箱已过期")


class PluginActionTests(unittest.TestCase):
    """单个账号的补 RT 入口：平台动作 + 动作结果落库。"""

    def _run_action(self, result):
        from core.base_platform import Account, AccountStatus, RegisterConfig
        from platforms.chatgpt.plugin import ChatGPTPlatform

        platform = ChatGPTPlatform(
            config=RegisterConfig(proxy="http://127.0.0.1:7890", extra={"mail_provider": "cfworker"})
        )
        account = Account(
            platform="chatgpt",
            email="demo@example.com",
            password="pw",
            status=AccountStatus.REGISTERED,
            token="at-old",
            extra={"session_token": "st-old"},
        )
        with mock.patch(
            "services.chatgpt_rt_backfill.backfill_account_data", return_value=result
        ) as engine_call:
            action_result = platform.execute_action("backfill_refresh_token", account, {})
        return action_result, engine_call

    def test_action_reports_success_and_returns_extra_patch(self):
        from platforms.chatgpt.rt_backfill import BackfillResult

        action_result, engine_call = self._run_action(
            BackfillResult(success=True, strategy=STRATEGY_SESSION, refresh_token="rt-new")
        )

        self.assertTrue(action_result["ok"])
        self.assertIn("补 RT 成功", action_result["data"]["message"])
        self.assertEqual(action_result["account_extra_patch"]["refresh_token"], "rt-new")
        kwargs = engine_call.call_args.kwargs
        self.assertEqual(kwargs["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(kwargs["config"], {"mail_provider": "cfworker"})
        self.assertTrue(kwargs["allow_login"])

    def test_action_failure_surfaces_reason(self):
        from platforms.chatgpt.rt_backfill import BackfillResult

        action_result, _ = self._run_action(
            BackfillResult(success=False, error_message="补 RT 失败（复用会话：会话失效）")
        )

        self.assertFalse(action_result["ok"])
        self.assertIn("会话失效", action_result["error"])

    def test_action_result_syncs_access_token_onto_the_account_row(self):
        from api.actions import _apply_action_result

        model = AccountModel(platform="chatgpt", email="demo@example.com", password="pw", token="at-old")
        model.set_extra({"session_token": "st-old"})
        result = {
            "ok": True,
            "data": {"message": "补 RT 成功（复用会话）"},
            "account_extra_patch": {"refresh_token": "rt-new", "access_token": "at-new"},
        }

        _apply_action_result("chatgpt", "backfill_refresh_token", model, result, mock.Mock())

        self.assertEqual(model.token, "at-new")
        self.assertEqual(model.get_extra()["refresh_token"], "rt-new")
        self.assertEqual(model.get_extra()["session_token"], "st-old")


if __name__ == "__main__":
    unittest.main()
