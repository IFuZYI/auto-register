import unittest
from unittest import mock

from platforms.chatgpt.protocol import totp
from platforms.chatgpt.protocol.two_factor import (
    bind_totp_inline,
    bind_totp_via_login,
    enroll_totp,
)
from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine
from services import chatgpt_two_factor

SECRET = "JBSWY3DPEHPK3PXP"


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else ("{}" if payload is None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    """按 URL 排队响应，顺便记下每个请求的 headers / body。"""

    def __init__(self, responses):
        self._responses = {url: list(items) for url, items in responses.items()}
        self.calls = []

    def _pop(self, method, url, headers, json_body):
        self.calls.append({"method": method, "url": url, "headers": headers, "json": json_body})
        queue = self._responses.get(url)
        if not queue:
            raise AssertionError(f"没有为 {method} {url} 准备响应")
        return queue.pop(0)

    def get(self, url, headers=None, timeout=None, **kwargs):
        return self._pop("GET", url, headers or {}, None)

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):
        return self._pop("POST", url, headers or {}, json)


class _FakeFlow:
    def __init__(self, responses):
        self.session = _FakeSession(responses)
        self.result = mock.Mock(access_token="at-inline", totp_secret="")

    def _common_headers(self, referer=""):
        return {"Referer": referer}

    def _trace_http(self, *args, **kwargs):
        return None


def _bind_responses(*, mfa_enabled=False, enroll=None, activate_status=200):
    enroll_payload = (
        {"secret": SECRET, "session_id": "sess-1", "factor": {"id": "factor-1"}}
        if enroll is None
        else enroll
    )
    return {
        "https://chatgpt.com/backend-api/accounts/mfa_info": [
            _Resp(200, {"mfa_enabled": mfa_enabled, "factors": {"totp": mfa_enabled}}),
            _Resp(200, {"mfa_enabled": True, "factors": {"totp": True}}),
        ],
        "https://chatgpt.com/backend-api/accounts/mfa/enroll": [_Resp(200, enroll_payload)],
        "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment": [
            _Resp(activate_status, {"success": activate_status == 200}, text='{"message":"太频繁"}')
        ],
    }


class TotpTests(unittest.TestCase):
    def test_known_vector_matches_rfc_reference(self):
        # RFC 6238 附录 B 的 SHA1 向量：T = 59s 对应 94287082
        self.assertEqual(totp.hotp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", 1, digits=8), "94287082")

    def test_code_is_six_digits_and_verifies_within_window(self):
        code = totp.totp_now(SECRET, at=1_700_000_000)
        self.assertRegex(code, r"^\d{6}$")
        self.assertTrue(totp.verify_totp(SECRET, code, at=1_700_000_000))
        # 前后各一个窗口都认，跨窗口提交不至于白跑一次
        self.assertTrue(totp.verify_totp(SECRET, code, at=1_700_000_000 + 30))
        self.assertFalse(totp.verify_totp(SECRET, code, at=1_700_000_000 + 300))

    def test_secret_is_normalized_before_decoding(self):
        spaced = "jbsw y3dp ehpk 3pxp"
        self.assertEqual(totp.totp_now(spaced, at=1_700_000_000), totp.totp_now(SECRET, at=1_700_000_000))

    def test_otpauth_uri_carries_secret_and_issuer(self):
        uri = totp.otpauth_uri(SECRET, "demo@example.com")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn(f"secret={SECRET}", uri)
        self.assertIn("issuer=ChatGPT", uri)
        self.assertEqual(totp.otpauth_uri("", "demo@example.com"), "")


class EnrollTests(unittest.TestCase):
    def setUp(self):
        # 复核前那 2 秒是留给服务端落库的，测试里没有服务端
        patcher = mock.patch("platforms.chatgpt.protocol.two_factor.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_happy_path_enrolls_activates_and_returns_secret(self):
        flow = _FakeFlow(_bind_responses())

        result = enroll_totp(flow, "at-1")

        self.assertTrue(result.ok)
        self.assertEqual(result.secret, SECRET)
        self.assertEqual(result.factor_id, "factor-1")
        # 密钥要在返回之前就挂到 result 上，activate 再炸也不至于丢
        self.assertEqual(flow.result.totp_secret, SECRET)

        activate = [c for c in flow.session.calls if c["url"].endswith("activate_enrollment")][0]
        self.assertEqual(activate["json"]["factor_type"], "totp")
        self.assertEqual(activate["json"]["session_id"], "sess-1")
        self.assertTrue(totp.verify_totp(SECRET, activate["json"]["code"]))
        self.assertEqual(activate["headers"]["Authorization"], "Bearer at-1")

    def test_already_bound_account_is_not_re_enrolled(self):
        flow = _FakeFlow(_bind_responses(mfa_enabled=True))

        result = enroll_totp(flow, "at-1")

        self.assertFalse(result.ok)
        self.assertTrue(result.already_bound)
        self.assertEqual(result.secret, "")
        self.assertNotIn(
            "https://chatgpt.com/backend-api/accounts/mfa/enroll",
            [call["url"] for call in flow.session.calls],
        )

    def test_enroll_without_secret_is_a_failure(self):
        flow = _FakeFlow(_bind_responses(enroll={"session_id": "sess-1"}))

        result = enroll_totp(flow, "at-1")

        self.assertFalse(result.ok)
        self.assertIn("secret", result.error_message)

    def test_unusable_secret_is_reported_instead_of_raising(self):
        flow = _FakeFlow(
            _bind_responses(enroll={"secret": "not base32!", "session_id": "sess-1"})
        )

        result = enroll_totp(flow, "at-1")

        self.assertFalse(result.ok)
        self.assertIn("Base32", result.error_message)

    def test_activate_failure_keeps_the_secret_for_the_caller(self):
        flow = _FakeFlow(_bind_responses(activate_status=429))

        result = enroll_totp(flow, "at-1")

        self.assertFalse(result.ok)
        self.assertEqual(result.secret, SECRET)
        self.assertIn("429", result.error_message)

    def test_missing_access_token_short_circuits(self):
        flow = _FakeFlow({})
        self.assertFalse(enroll_totp(flow, "").ok)
        self.assertEqual(flow.session.calls, [])


class BindInlineTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("platforms.chatgpt.protocol.two_factor.time.sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_inline_falls_back_to_flow_access_token(self):
        flow = _FakeFlow(_bind_responses())

        result = bind_totp_inline(flow)

        self.assertTrue(result.ok)
        enroll = [c for c in flow.session.calls if c["url"].endswith("/mfa/enroll")][0]
        self.assertEqual(enroll["headers"]["Authorization"], "Bearer at-inline")

    def test_inline_never_raises(self):
        flow = _FakeFlow({})
        flow.result.access_token = "at-inline"

        result = bind_totp_inline(flow)

        self.assertFalse(result.ok)
        self.assertIn("没有为", result.error_message)

    def test_slow_path_requires_email_and_password(self):
        self.assertFalse(bind_totp_via_login(mock.Mock(), "", "pw").ok)
        self.assertFalse(bind_totp_via_login(mock.Mock(), "a@b.com", "").ok)


class SlowPathCallbackTests(unittest.TestCase):
    """慢路径不能因为「cookie 没读出来」就放弃。

    回归（实测日志）：OTP 验证成功后卡在「消费 callback 失败，拿不到会话」，
    整轮绑定白跑。根因是 `_consume_callback_for_session` 的返回值被当成致命
    判据 —— 而它只是个「是否读到 session cookie」的探测，curl_cffi 分域隔离时
    朴素读法读不到。注册链与手机链都忽略这个返回值、直接走 `get_auth_session()`
    （那个函数有三路 cookie 兜底）。
    """

    def _flow_with_otp(self, *, consume_ok: bool, access_token: str = "at-1"):
        """造一条「OTP 已验证完、只剩 callback + session」的 flow。"""
        flow = mock.Mock()
        flow.check_proxy.return_value = True
        flow.warmup.return_value = True
        flow._is_existing_account = False
        flow.get_csrf_token.return_value = "csrf"
        flow.get_auth_url.return_value = "https://auth.openai.com/authorize"
        flow.auth_oauth_init.return_value = "device"
        flow.get_sentinel_token.return_value = "sentinel"
        flow._last_sentinel_token = "sentinel"
        # authorize 直接进 OTP 页，跳过密码分支
        flow.authorize_continue.return_value = {"page": {"type": "email_otp_verification"}}
        flow._extract_page_type.return_value = "email_otp_verification"
        flow._extract_continue_url_from_step.return_value = "https://auth.openai.com/email-verification"
        flow._normalize_continue_url.side_effect = lambda u: u
        flow.verify_otp.return_value = {"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=x"}
        flow._is_mfa_challenge_state.return_value = False
        flow._consume_callback_for_session.return_value = consume_ok
        flow.get_auth_session.return_value = ("st-1", access_token)
        return flow

    def _run(self, flow, provider=None):
        from platforms.chatgpt.protocol import two_factor

        with mock.patch.object(two_factor, "AuthFlow", return_value=flow), mock.patch.object(
            two_factor, "_obtain_otp", return_value="123456"
        ):
            return two_factor._login_for_access_token(
                flow, "a@b.com", "pw", provider or mock.Mock()
            )

    def test_callback_false_does_not_abort_the_login(self):
        """消费 callback 返回 False 时仍要继续拉 session —— 这是回归点。"""
        flow = self._flow_with_otp(consume_ok=False)
        token, error = self._run(flow)

        self.assertEqual(error, "", f"不该因 callback 探测失败就放弃：{error}")
        self.assertEqual(token, "at-1")
        flow.get_auth_session.assert_called_once()

    def test_callback_true_still_works(self):
        flow = self._flow_with_otp(consume_ok=True)
        token, error = self._run(flow)
        self.assertEqual(error, "")
        self.assertEqual(token, "at-1")

    def test_missing_access_token_is_still_an_error(self):
        """放宽 callback 判据不等于放宽 session 判据 —— 真拿不到 AT 仍要报错。"""
        flow = self._flow_with_otp(consume_ok=False, access_token="")
        token, error = self._run(flow)
        self.assertEqual(token, "")
        self.assertIn("没拿到 access_token", error)

    def test_consume_uses_the_shared_cookie_fallback(self):
        """`_consume_callback_for_session` 的返回值要与 `get_auth_session` 同口径。"""
        import inspect

        from platforms.chatgpt.protocol.auth_flow import AuthFlow

        src = inspect.getsource(AuthFlow._consume_callback_for_session)
        self.assertIn(
            "_extract_session_cookie", src,
            "返回值应走三路兜底的读法，而不是朴素 session.cookies.get",
        )
        self.assertNotIn(
            'self.session.cookies.get("__Secure-next-auth.session-token"', src,
            "朴素读法在 curl_cffi 分域隔离下读不到，会误报失败",
        )


class RegistrationEngineTwoFactorTests(unittest.TestCase):
    def setUp(self):
        self.logs: list[str] = []

    def _engine(self, **overrides):
        kwargs = dict(mailbox=mock.Mock(), email="demo@example.com", bind_2fa=True)
        kwargs.update(overrides)
        return ChatGPTRegistrationEngine(log_fn=self.logs.append, **kwargs)

    def test_hook_is_only_attached_when_binding_is_enabled(self):
        with mock.patch(
            "platforms.chatgpt.registration_engine.AuthFlow"
        ) as auth_flow, mock.patch(
            "platforms.chatgpt.registration_engine.resolve_sms_settings", return_value={}
        ):
            self._engine(bind_2fa=False)._build_flow()
            self.assertIsNone(auth_flow.call_args.kwargs["on_session_ready"])

            self._engine(bind_2fa=True)._build_flow()
            self.assertIsNotNone(auth_flow.call_args.kwargs["on_session_ready"])

    def test_session_ready_hook_binds_and_logs_the_secret(self):
        engine = self._engine()
        flow = mock.Mock()

        with mock.patch(
            "platforms.chatgpt.registration_engine.bind_totp_inline",
            return_value=mock.Mock(ok=True, secret=SECRET, already_bound=False),
        ) as bind:
            engine._on_session_ready(flow, "at-1")

        # 第三个参数是「密钥一到手就写前日志」的回调（`_journal_secret`）。
        # 这个 mock flow 没有可用邮箱，回调是 None —— 回调本身的行为由
        # test_chatgpt_two_factor_persistence.py::SecretJournalTests 覆盖。
        bind.assert_called_once_with(flow, "at-1", on_secret=None)
        self.assertTrue(any(SECRET in line for line in self.logs))

    def test_slow_path_runs_only_when_fast_path_left_no_secret(self):
        engine = self._engine()
        flow = mock.Mock()
        flow.result = mock.Mock(totp_secret=SECRET, email="demo@example.com", password="pw")

        with mock.patch("platforms.chatgpt.registration_engine.bind_totp_via_login") as slow:
            engine._ensure_two_factor(flow, None)

        slow.assert_not_called()

    def test_slow_path_binds_and_writes_the_secret_back(self):
        engine = self._engine()
        flow = mock.Mock()
        flow.result = mock.Mock(
            totp_secret="", bound_email="", email="demo@example.com", password="pw"
        )

        with mock.patch(
            "platforms.chatgpt.registration_engine.bind_totp_via_login",
            return_value=mock.Mock(ok=True, secret=SECRET, already_bound=False),
        ) as slow:
            engine._ensure_two_factor(flow, None)

        self.assertEqual(slow.call_args.args[1], "demo@example.com")
        self.assertEqual(slow.call_args.args[2], "pw")
        self.assertEqual(flow.result.totp_secret, SECRET)

    def test_phone_only_account_skips_the_slow_path(self):
        engine = self._engine()
        flow = mock.Mock()
        flow.result = mock.Mock(totp_secret="", bound_email="", email="+66123456789", password="pw")

        with mock.patch("platforms.chatgpt.registration_engine.bind_totp_via_login") as slow:
            engine._ensure_two_factor(flow, None)

        slow.assert_not_called()
        self.assertTrue(any("没有邮箱身份" in line for line in self.logs))

    def test_binding_disabled_does_nothing(self):
        engine = self._engine(bind_2fa=False)
        flow = mock.Mock()
        flow.result = mock.Mock(totp_secret="", bound_email="", email="demo@example.com", password="pw")

        with mock.patch("platforms.chatgpt.registration_engine.bind_totp_via_login") as slow:
            engine._ensure_two_factor(flow, None)

        slow.assert_not_called()


class PluginActionTests(unittest.TestCase):
    def _account(self, extra=None):
        from core.base_platform import Account, AccountStatus

        return Account(
            platform="chatgpt",
            email="demo@example.com",
            password="pw",
            token="at-1",
            status=AccountStatus.REGISTERED,
            extra=dict(extra or {}),
        )

    def test_bind_2fa_action_returns_the_secret_and_a_persistable_patch(self):
        from platforms.chatgpt.plugin import ChatGPTPlatform
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult

        with mock.patch.object(
            chatgpt_two_factor,
            "bind_account_two_factor",
            return_value=TwoFactorBindResult(ok=True, secret=SECRET),
        ) as bind:
            result = ChatGPTPlatform().execute_action("bind_2fa", self._account(), {})

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["totp_secret"], SECRET)
        self.assertEqual(result["account_extra_patch"]["totp_secret"], SECRET)
        self.assertEqual(bind.call_args.kwargs["email"], "demo@example.com")
        self.assertTrue(bind.call_args.kwargs["allow_login"])

    def test_bind_2fa_action_reports_failure_without_touching_the_secret(self):
        from platforms.chatgpt.plugin import ChatGPTPlatform
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult

        with mock.patch.object(
            chatgpt_two_factor,
            "bind_account_two_factor",
            return_value=TwoFactorBindResult(error_message="enroll 403"),
        ):
            result = ChatGPTPlatform().execute_action("bind_2fa", self._account(), {})

        self.assertFalse(result["ok"])
        self.assertIn("enroll 403", result["error"])
        self.assertNotIn("totp_secret", result["account_extra_patch"])

    def test_bind_2fa_action_is_offered_on_the_accounts_page(self):
        from platforms.chatgpt.plugin import ChatGPTPlatform

        actions = {item["id"]: item["label"] for item in ChatGPTPlatform().get_platform_actions()}
        self.assertEqual(actions["bind_2fa"], "绑定 2FA")


class AccountBindServiceTests(unittest.TestCase):
    def test_existing_secret_is_never_overwritten(self):
        result = chatgpt_two_factor.bind_account_two_factor(
            email="demo@example.com", extra={"totp_secret": SECRET}, config={}, log_fn=lambda _: None
        )

        self.assertTrue(result.already_bound)
        self.assertEqual(result.secret, SECRET)

    def test_session_reuse_is_tried_before_re_login(self):
        with mock.patch.object(
            chatgpt_two_factor,
            "_bind_via_session",
            return_value=mock.Mock(ok=True, secret=SECRET, already_bound=False),
        ) as session_bind, mock.patch.object(chatgpt_two_factor, "bind_totp_via_login") as login_bind:
            result = chatgpt_two_factor.bind_account_two_factor(
                email="demo@example.com",
                password="pw",
                extra={"access_token": "at-1"},
                config={},
                log_fn=lambda _: None,
            )

        session_bind.assert_called_once()
        login_bind.assert_not_called()
        self.assertEqual(result.secret, SECRET)

    def test_re_login_takes_over_when_the_session_is_dead(self):
        with mock.patch.object(
            chatgpt_two_factor,
            "_bind_via_session",
            return_value=mock.Mock(ok=False, already_bound=False, summary=lambda: "会话失效"),
        ), mock.patch.object(
            chatgpt_two_factor, "_resolve_mail_provider", return_value=None
        ), mock.patch.object(
            chatgpt_two_factor,
            "bind_totp_via_login",
            return_value=mock.Mock(ok=True, secret=SECRET, already_bound=False),
        ) as login_bind:
            result = chatgpt_two_factor.bind_account_two_factor(
                email="demo@example.com",
                password="pw",
                extra={"access_token": "at-1"},
                config={},
                log_fn=lambda _: None,
            )

        login_bind.assert_called_once()
        self.assertEqual(result.secret, SECRET)

    def test_re_login_is_skipped_without_a_password(self):
        with mock.patch.object(
            chatgpt_two_factor,
            "_bind_via_session",
            return_value=mock.Mock(ok=False, already_bound=False, summary=lambda: "会话失效"),
        ), mock.patch.object(chatgpt_two_factor, "bind_totp_via_login") as login_bind:
            result = chatgpt_two_factor.bind_account_two_factor(
                email="demo@example.com",
                extra={"access_token": "at-1"},
                config={},
                log_fn=lambda _: None,
            )

        login_bind.assert_not_called()
        self.assertIn("没有密码", result.error_message)

    def test_patch_records_the_attempt_without_clobbering_a_missing_secret(self):
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult

        failed = chatgpt_two_factor.build_extra_patch(
            TwoFactorBindResult(error_message="enroll 403")
        )
        self.assertNotIn("totp_secret", failed)
        self.assertFalse(failed["chatgpt_2fa"]["bound"])

        ok = chatgpt_two_factor.build_extra_patch(TwoFactorBindResult(ok=True, secret=SECRET))
        self.assertEqual(ok["totp_secret"], SECRET)
        self.assertTrue(ok["chatgpt_2fa"]["bound"])


if __name__ == "__main__":
    unittest.main()
