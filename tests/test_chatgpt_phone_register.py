"""手机号注册链路与绑定邮箱链路。

两条链都要花钱：号是租的、邮箱是池里领的。所以这里盯的主要是"什么时候该换号、
什么时候该收手、绑定失败会不会把已经注册好的号一起判死"。
"""

import json
import logging
import unittest
from unittest import mock

from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    CHATGPT_REGISTER_FLOW_EMAIL,
    CHATGPT_REGISTER_FLOW_PHONE,
    CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL,
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
    normalize_chatgpt_register_flow,
    resolve_chatgpt_register_flow,
)
from platforms.chatgpt.protocol.auth_flow import AuthFlow, AuthResult
from platforms.chatgpt.protocol.phone_flow import PhoneAccountCreatedError
from platforms.chatgpt.registration_engine import (
    REGISTER_FLOW_PHONE,
    REGISTER_FLOW_PHONE_WITH_EMAIL,
    ChatGPTRegistrationEngine,
    RegistrationResult,
)


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", url=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload) if payload is not None else "")
        self.headers = {}
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def _record(self, method, url, headers, payload, kwargs):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers or {},
                "json": payload,
                "kwargs": kwargs,
            }
        )
        if not self._responses:
            raise AssertionError(f"没有为 {url} 准备响应")
        return self._responses.pop(0)

    def post(self, url, headers=None, json=None, timeout=None, **kwargs):
        return self._record("POST", url, headers, json, kwargs)

    def get(self, url, headers=None, timeout=None, **kwargs):
        return self._record("GET", url, headers, None, kwargs)


def _flow(responses=()) -> AuthFlow:
    """绕开 AuthFlow.__init__（要建 http 客户端），只装被测方法用到的东西。"""
    flow = AuthFlow.__new__(AuthFlow)
    flow.session = _FakeSession(responses)
    flow.result = AuthResult()
    flow.result.device_id = ""
    flow._last_sentinel_token = ""
    flow._last_sentinel_so_token = ""
    flow._on_password = None
    flow._bind_email_error = ""
    flow._common_headers = lambda referer="": {"Referer": referer}
    flow._trace_http = lambda *args, **kwargs: None
    flow._get_env = lambda key, default="": default
    return flow


class _FakeMailProvider:
    def __init__(self, email="pool@example.com", code="123456"):
        self.email = email
        self.code = code
        self.created = 0
        self.waits = []

    def create_mailbox(self):
        self.created += 1
        return self.email

    def wait_for_otp(self, email, timeout=120, issued_after=None, exclude_codes=None):
        self.waits.append((email, timeout, issued_after, exclude_codes))
        return self.code


class PhoneIdentityRequestTests(unittest.TestCase):
    def test_authorize_continue_submits_phone_kind(self):
        flow = _flow([_FakeResponse(payload={"page": {"type": "create_account_password"}})])

        flow.phone_authorize_continue("+56971901026", "sentinel-token")

        call = flow.session.calls[0]
        self.assertEqual(
            call["url"], "https://auth.openai.com/api/accounts/authorize/continue"
        )
        self.assertEqual(
            call["json"]["username"],
            {"value": "+56971901026", "kind": "phone_number"},
        )
        self.assertEqual(call["headers"]["Referer"], "https://auth.openai.com/log-in?usernameKind=phone_number")
        self.assertEqual(call["headers"]["openai-sentinel-token"], "sentinel-token")

    def test_email_authorize_continue_keeps_email_kind(self):
        flow = _flow([_FakeResponse(payload={})])

        flow.authorize_continue("demo@example.com", "sentinel-token")

        self.assertEqual(
            flow.session.calls[0]["json"]["username"],
            {"value": "demo@example.com", "kind": "email"},
        )

    def test_register_user_uses_phone_as_username_and_persists_password(self):
        flow = _flow([_FakeResponse(payload={"page": {"type": "phone_otp_verification"}})])
        saved = []
        flow._on_password = lambda identity, password: saved.append((identity, password))

        flow.phone_register_user("+56971901026")

        call = flow.session.calls[0]
        self.assertEqual(call["url"], "https://auth.openai.com/api/accounts/user/register")
        self.assertEqual(call["json"]["username"], "+56971901026")
        self.assertTrue(call["json"]["password"])
        self.assertEqual(saved, [("+56971901026", flow.result.password)])

    def test_register_user_raises_on_non_200(self):
        flow = _flow(
            [
                _FakeResponse(
                    status_code=400,
                    text='{"error":{"message":"Invalid username","code":"invalid_username"}}',
                )
            ]
        )

        with self.assertRaises(RuntimeError) as ctx:
            flow.phone_register_user("+56971901026")

        self.assertIn("invalid_username", str(ctx.exception))


class BindEmailTests(unittest.TestCase):
    def test_add_email_send_posts_email(self):
        flow = _flow([_FakeResponse(payload={"page": {"type": "email_otp_verification"}})])

        flow.add_email_send("UraLepre287@outlook.com")

        call = flow.session.calls[0]
        self.assertEqual(call["url"], "https://auth.openai.com/api/accounts/add-email/send")
        self.assertEqual(call["json"], {"email": "UraLepre287@outlook.com"})
        self.assertEqual(call["headers"]["Referer"], "https://auth.openai.com/add-email")
        # 这一步真实浏览器不带 sentinel，带了反而是风控特征
        self.assertNotIn("openai-sentinel-token", call["headers"])

    def test_add_email_send_surfaces_server_message(self):
        flow = _flow([
            _FakeResponse(status_code=400, payload={"error": {"message": "email_already_in_use"}})
        ])

        with self.assertRaises(RuntimeError) as ctx:
            flow.add_email_send("taken@example.com")

        self.assertIn("email_already_in_use", str(ctx.exception))

    def test_bind_email_sends_code_then_validates(self):
        flow = _flow([
            _FakeResponse(payload={"page": {"type": "email_otp_verification"}}),
            _FakeResponse(payload={"continue_url": "https://auth.openai.com/authorize/continue?x=1"}),
        ])
        flow._normalize_continue_url = lambda url: url
        provider = _FakeMailProvider()

        next_url = flow.bind_email(provider)

        self.assertEqual(provider.created, 1)
        self.assertEqual(flow.result.bound_email, "pool@example.com")
        self.assertEqual(
            [call["url"] for call in flow.session.calls],
            [
                "https://auth.openai.com/api/accounts/add-email/send",
                "https://auth.openai.com/api/accounts/email-otp/validate",
            ],
        )
        self.assertEqual(flow.session.calls[1]["json"], {"code": "123456"})
        self.assertEqual(next_url, "https://auth.openai.com/authorize/continue?x=1")

    def test_bind_email_resends_once_when_code_is_rejected(self):
        flow = _flow([
            _FakeResponse(payload={}),
            _FakeResponse(status_code=401, text="invalid code"),
            _FakeResponse(payload={}),
            _FakeResponse(payload={"continue_url": "https://auth.openai.com/next"}),
        ])
        flow._normalize_continue_url = lambda url: url
        provider = _FakeMailProvider()

        flow.bind_email(provider)

        self.assertEqual(
            [call["url"].rsplit("/", 2)[-2:] for call in flow.session.calls],
            [
                ["add-email", "send"],
                ["email-otp", "validate"],
                ["add-email", "send"],
                ["email-otp", "validate"],
            ],
        )
        self.assertEqual(flow.result.bound_email, "pool@example.com")

    def test_bind_failure_keeps_the_account(self):
        flow = _flow([_FakeResponse(status_code=400, payload={"error": {"message": "invalid state"}})])
        flow._normalize_continue_url = lambda url: url
        provider = _FakeMailProvider()

        result = flow._try_bind_email(provider, "https://auth.openai.com/next")

        self.assertEqual(result, "https://auth.openai.com/next")
        self.assertFalse(flow.result.bound_email)
        self.assertIn("invalid state", flow._bind_email_error)

    def test_no_provider_means_no_binding(self):
        flow = _flow()

        self.assertEqual(flow._try_bind_email(None, "url"), "url")


class FlowStateDetectionTests(unittest.TestCase):
    def test_add_email_state(self):
        self.assertTrue(AuthFlow._is_add_email_state(page_type="add_email"))
        self.assertTrue(
            AuthFlow._is_add_email_state(continue_url="https://auth.openai.com/add-email")
        )
        self.assertFalse(AuthFlow._is_add_email_state(page_type="about_you"))

    def test_phone_otp_state(self):
        self.assertTrue(AuthFlow._is_phone_otp_state(page_type="phone_otp_verification"))
        self.assertTrue(
            AuthFlow._is_phone_otp_state(continue_url="https://auth.openai.com/phone-verification")
        )
        self.assertFalse(AuthFlow._is_phone_otp_state(page_type="login_password"))

    def test_contact_verification_is_the_sms_page(self):
        """手机号注册发完码就停在 /contact-verification，这就是输验证码的地方。"""
        self.assertTrue(AuthFlow._is_phone_otp_state(page_type="contact_verification"))
        self.assertTrue(
            AuthFlow._is_phone_otp_state(
                continue_url="https://auth.openai.com/contact-verification"
            )
        )
        # 别把它当成"还没发码"或"已经放行"
        self.assertFalse(AuthFlow._is_phone_otp_send_state(page_type="contact_verification"))
        self.assertFalse(AuthFlow._is_forward_state(page_type="contact_verification"))

    def test_phone_otp_send_state(self):
        self.assertTrue(AuthFlow._is_phone_otp_send_state(page_type="phone_otp_send"))
        self.assertTrue(
            AuthFlow._is_phone_otp_send_state(continue_url="/api/accounts/phone-otp/send")
        )
        # 已经在验证页上就不该再被当成"该发码了"
        self.assertFalse(AuthFlow._is_phone_otp_send_state(page_type="phone_otp_verification"))

    def test_forward_state(self):
        self.assertTrue(AuthFlow._is_forward_state(page_type="about_you"))
        self.assertTrue(
            AuthFlow._is_forward_state(continue_url="https://auth.openai.com/about-you")
        )
        self.assertFalse(AuthFlow._is_forward_state(page_type="phone_otp_send"))

    def test_existing_identity_state(self):
        self.assertTrue(AuthFlow._is_existing_identity_state(page_type="login_password"))
        self.assertTrue(
            AuthFlow._is_existing_identity_state(continue_url="https://auth.openai.com/log-in/password")
        )
        self.assertFalse(
            AuthFlow._is_existing_identity_state(page_type="create_account_password")
        )


class _FakeController:
    provider_key = "smsbower"

    def __init__(self, max_attempts=3):
        self.config = {
            "sms_per_phone_timeout": "40",
            "sms_max_phone_attempts": str(max_attempts),
            "sms_code_retries_per_phone": "1",
        }
        self.rented = []
        self.refunds = []
        self.reuse_stopped = []
        self.cleanups = 0
        self.successes = 0

    def get_phone(self):
        phone = f"+5697190{len(self.rented):04d}"
        self.rented.append(phone)
        return phone

    def get_code(self, timeout=0):
        return "654321"

    def mark_send_failed(self, reason=""):
        self.refunds.append(reason)

    def mark_send_succeeded(self):
        pass

    def stop_reuse(self, reason=""):
        self.reuse_stopped.append(reason)

    def mark_code_failed(self, reason=""):
        pass

    def report_success(self):
        self.successes += 1

    def cleanup(self):
        self.cleanups += 1


def _loop_flow() -> AuthFlow:
    flow = AuthFlow.__new__(AuthFlow)
    flow.result = AuthResult()
    flow._get_env = lambda key, default="": default
    flow.get_csrf_token = lambda: "csrf"
    flow.get_auth_url = lambda csrf, login_hint="": "https://auth.openai.com/authorize"
    flow.auth_oauth_init = lambda url: "device-1"
    flow.get_sentinel_token = lambda device_id: "sentinel"
    flow._normalize_continue_url = lambda url: url
    return flow


class PhoneRegisterLoopTests(unittest.TestCase):
    def test_already_registered_number_moves_to_the_next_one(self):
        ctrl = _FakeController(max_attempts=3)
        flow = _loop_flow()
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "login_password"}
        }

        with self.assertRaises(RuntimeError):
            flow._do_phone_register_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 3)
        self.assertEqual(len(ctrl.refunds), 3)

    def test_broken_flow_state_stops_burning_numbers(self):
        ctrl = _FakeController(max_attempts=30)
        flow = _loop_flow()

        def _continue(phone, sentinel):
            raise RuntimeError("Invalid authorization step.")

        flow.phone_authorize_continue = _continue

        with self.assertRaises(RuntimeError):
            flow._do_phone_register_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 1)

    def test_same_unknown_error_three_times_stops_the_round(self):
        ctrl = _FakeController(max_attempts=30)
        flow = _loop_flow()

        def _continue(phone, sentinel):
            raise RuntimeError("upstream hiccup")

        flow.phone_authorize_continue = _continue

        with self.assertRaises(RuntimeError):
            flow._do_phone_register_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 3)

    def test_login_hint_landing_on_the_password_page_skips_authorize_continue(self):
        """抓包里 login_hint 已经把身份带过去了，浏览器不会再提交一次。

        多打那一枪等于把同一个手机号交两遍，服务端有理由判 invalid state。
        """
        ctrl = _FakeController()
        flow = _loop_flow()

        def _init(url):
            flow._last_auth_landing_url = "https://auth.openai.com/create-account/password"
            return "device-1"

        flow.auth_oauth_init = _init
        flow.get_sentinel_token = lambda device_id: self.fail(
            "不该为一个根本不会发的 authorize/continue 算 PoW"
        )
        flow.phone_authorize_continue = lambda phone, sentinel: self.fail(
            "login_hint 已经落到设密码页了，不该再提交一次身份"
        )
        registered = []

        def _register(phone):
            registered.append(phone)
            return {"page": {"type": "phone_otp_verification"}}

        flow.phone_register_user = _register
        flow._phone_otp_validate = lambda code, referer="": {
            "continue_url": "https://auth.openai.com/about-you"
        }

        continue_url, _auth_url = flow._do_phone_register_loop(ctrl)

        self.assertEqual(registered, [ctrl.rented[0]])
        self.assertEqual(continue_url, "https://auth.openai.com/about-you")

    def test_other_landings_still_submit_the_identity(self):
        """login_hint 没被服务端认下来时，authorize/continue 这一步不能省。"""
        ctrl = _FakeController()
        flow = _loop_flow()

        def _init(url):
            flow._last_auth_landing_url = "https://auth.openai.com/log-in"
            return "device-1"

        flow.auth_oauth_init = _init
        submitted = []

        def _continue(phone, sentinel):
            submitted.append(phone)
            return {"page": {"type": "create_account_password"}}

        flow.phone_authorize_continue = _continue
        flow.phone_register_user = lambda phone: {"page": {"type": "phone_otp_verification"}}
        flow._phone_otp_validate = lambda code, referer="": {
            "continue_url": "https://auth.openai.com/about-you"
        }

        flow._do_phone_register_loop(ctrl)

        self.assertEqual(submitted, [ctrl.rented[0]])

    def test_happy_path_registers_then_validates_sms(self):
        ctrl = _FakeController()
        flow = _loop_flow()
        registered = []
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "create_account_password"}
        }

        def _register(phone):
            registered.append(phone)
            return {"page": {"type": "phone_otp_verification"}}

        flow.phone_register_user = _register
        flow._phone_otp_validate = lambda code, referer="": {"continue_url": "https://auth.openai.com/about-you"}

        continue_url, auth_url = flow._do_phone_register_loop(ctrl)

        self.assertEqual(registered, [ctrl.rented[0]])
        self.assertEqual(continue_url, "https://auth.openai.com/about-you")
        self.assertEqual(auth_url, "https://auth.openai.com/authorize")
        self.assertEqual(flow.result.phone_number, ctrl.rented[0])
        self.assertEqual(ctrl.successes, 1)

    def test_register_response_asking_for_a_send_triggers_the_sms(self):
        """user/register 之后服务端停在 phone_otp_send，要客户端自己去打发码接口。

        线上第一次实跑就死在这：当时把这个状态当成"没进短信页"，转头去打
        add-phone/send，被判 invalid authorization step —— 那是另一个步骤。
        """
        ctrl = _FakeController()
        flow = _loop_flow()
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "create_account_password"}
        }
        flow.phone_register_user = lambda phone: {
            "page": {"type": "phone_otp_send"},
            "continue_url": "/api/accounts/phone-otp/send",
        }
        sent = []

        def _send(phone, continue_url=""):
            sent.append((phone, continue_url))
            return {"page": {"type": "phone_otp_verification"}}

        flow.send_phone_otp = _send
        flow._phone_otp_validate = lambda code, referer="": {"continue_url": "https://auth.openai.com/about-you"}

        continue_url, _auth_url = flow._do_phone_register_loop(ctrl)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], ctrl.rented[0])
        self.assertIn("phone-otp/send", sent[0][1])
        self.assertEqual(continue_url, "https://auth.openai.com/about-you")

    def test_contact_verification_after_send_is_the_sms_page_not_a_failure(self):
        """线上实跑的完整形状：发码成功 → contact_verification → 等码 → validate。

        之前把 contact_verification 当成"没进短信页"直接判失败，号已经注册出去
        了却拿不到凭证，白扔一个账号。
        """
        ctrl = _FakeController()
        flow = _loop_flow()
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "create_account_password"}
        }
        flow.phone_register_user = lambda phone: {
            "page": {"type": "phone_otp_send"},
            "continue_url": "https://auth.openai.com/api/accounts/phone-otp/send",
        }
        flow.send_phone_otp = lambda phone, continue_url="": {
            "page": {"type": "contact_verification"},
            "continue_url": "https://auth.openai.com/contact-verification",
        }
        validated = []

        def _validate(code, referer=""):
            validated.append((code, referer))
            return {"page": {"type": "about_you"}, "continue_url": "https://auth.openai.com/about-you"}

        flow._phone_otp_validate = _validate

        continue_url, _auth_url = flow._do_phone_register_loop(ctrl)

        self.assertEqual(validated, [("654321", "https://auth.openai.com/contact-verification")])
        self.assertEqual(continue_url, "https://auth.openai.com/about-you")
        self.assertEqual(ctrl.successes, 1)
        self.assertEqual(ctrl.refunds, [])

    def test_verified_but_no_next_step_falls_through_to_create_account(self):
        ctrl = _FakeController()
        flow = _loop_flow()
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "create_account_password"}
        }
        flow.phone_register_user = lambda phone: {
            "page": {"type": "contact_verification"},
            "continue_url": "https://auth.openai.com/contact-verification",
        }
        flow.send_phone_otp = lambda phone, continue_url="": self.fail("服务端已经发过码了")
        flow._phone_otp_validate = lambda code, referer="": {}

        continue_url, _auth_url = flow._do_phone_register_loop(ctrl)

        # 空 continue_url 才会让上层去走 about-you → create_account
        self.assertEqual(continue_url, "")

    def test_number_already_in_use_rotates_to_the_next_number(self):
        """服务端说 phone_number_in_use 就是号被占了，换个号继续，别当未知错误。"""
        ctrl = _FakeController(max_attempts=4)
        flow = _loop_flow()

        def _continue(phone, sentinel):
            raise RuntimeError(
                "authorize/continue 失败(screen_hint=signup): HTTP 400 req_id=4082fdc9 "
                "Phone number already in use. Please try again. phone_number_in_use"
            )

        flow.phone_authorize_continue = _continue

        with self.assertRaises(RuntimeError):
            flow._do_phone_register_loop(ctrl)

        # 未知错误连续 3 次就停了，认成"号被占用"才会把 4 个号都试完
        self.assertEqual(len(ctrl.rented), 4)
        self.assertEqual(len(ctrl.refunds), 4)

    def test_server_forwarding_past_verification_does_not_send_another_code(self):
        ctrl = _FakeController()
        flow = _loop_flow()
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "create_account_password"}
        }
        flow.phone_register_user = lambda phone: {
            "continue_url": "https://auth.openai.com/about-you"
        }

        def _send(phone, continue_url=""):
            raise AssertionError("服务端已经放行，不该再发码")

        flow.send_phone_otp = _send

        continue_url, _auth_url = flow._do_phone_register_loop(ctrl)

        self.assertEqual(continue_url, "https://auth.openai.com/about-you")

    def test_created_account_stops_the_round_instead_of_renting_more(self):
        ctrl = _FakeController(max_attempts=3)
        flow = _loop_flow()
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "create_account_password"}
        }

        def _register(phone):
            flow.result.password = "pw-live"
            return {"page": {"type": "phone_otp_send"}}

        flow.phone_register_user = _register

        def _send(phone, continue_url=""):
            raise RuntimeError("Invalid authorization step.")

        flow.send_phone_otp = _send

        with self.assertRaises(PhoneAccountCreatedError) as ctx:
            flow._do_phone_register_loop(ctrl)

        # 账号已经建好了：不换号（否则每换一个就多一个孤号），
        # 也不把号上报成"有问题"去要退款
        self.assertEqual(len(ctrl.rented), 1)
        self.assertEqual(ctrl.refunds, [])
        # 外层整流程重试时必须换新号：这个号已经挂在半成品账号上了
        self.assertEqual(len(ctrl.reuse_stopped), 1)
        self.assertEqual(ctx.exception.phone, ctrl.rented[0])
        self.assertEqual(ctx.exception.password, "pw-live")
        self.assertIn("Invalid authorization step.", str(ctx.exception))
        self.assertIn("pw-live", str(ctx.exception))

    def test_sms_timeout_after_registration_also_stops_the_round(self):
        ctrl = _FakeController(max_attempts=3)
        ctrl.get_code = lambda timeout=0: ""
        flow = _loop_flow()
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "create_account_password"}
        }
        flow.phone_register_user = lambda phone: {"page": {"type": "phone_otp_verification"}}

        with self.assertRaises(PhoneAccountCreatedError) as ctx:
            flow._do_phone_register_loop(ctrl)

        self.assertEqual(len(ctrl.rented), 1)
        # 一条短信都没进来：这才是"号源被静默拦下"该有的证据
        self.assertFalse(ctx.exception.retryable)

    def test_a_code_that_did_arrive_keeps_the_round_retryable(self):
        """码进来了、后面才崩 —— 号源是活的，外层重开一轮有真机会。"""
        ctrl = _FakeController(max_attempts=3)
        flow = _loop_flow()
        flow.phone_authorize_continue = lambda phone, sentinel: {
            "page": {"type": "create_account_password"}
        }
        flow.phone_register_user = lambda phone: {"page": {"type": "phone_otp_verification"}}

        def _validate(code, referer=""):
            raise RuntimeError("phone-otp/validate 超时")

        flow._phone_otp_validate = _validate

        with self.assertRaises(PhoneAccountCreatedError) as ctx:
            flow._do_phone_register_loop(ctrl)

        self.assertTrue(ctx.exception.retryable)


class PhoneAccountCreatedMessageTests(unittest.TestCase):
    def test_timeout_message_says_the_platform_never_got_anything(self):
        """实测换国家救不了：别把人往「再换个国家试试」的坑里指。"""
        err = PhoneAccountCreatedError(
            "号 +66968649749 在 150s 内没收到短信", phone="+66968649749", password="pw"
        )
        text = str(err)
        self.assertIn("+66968649749", text)
        self.assertIn("pw", text)
        self.assertIn("接码平台全程没收到任何短信", text)
        self.assertIn("换号源", text)
        # 号源静默拦码时，外层整流程重试只会再造一个孤号
        self.assertFalse(err.retryable)

    def test_other_failures_still_deserve_a_fresh_round(self):
        err = PhoneAccountCreatedError(
            "phone-otp/validate 返回 400", phone="+66968649749", password="pw"
        )
        self.assertTrue(err.retryable)

    def test_an_unrelated_timeout_is_not_a_dead_end(self):
        """以前"超时"两个字就够判死一整轮，换 RT 慢一点都会连累用户填的重试轮数。"""
        err = PhoneAccountCreatedError(
            "Codex 换 refresh_token 超时", phone="+66968649749", password="pw"
        )
        self.assertTrue(err.retryable)

    def test_a_code_that_arrived_earlier_outweighs_a_later_timeout(self):
        err = PhoneAccountCreatedError(
            "号 +66968649749 在 150s 内没收到短信",
            phone="+66968649749",
            password="pw",
            sms_ever_received=True,
        )
        self.assertTrue(err.retryable)
        self.assertNotIn("接码平台全程没收到任何短信", str(err))


class SendPhoneOtpTests(unittest.TestCase):
    def _flow(self, responses):
        flow = _flow(responses)
        # XHR 头（两个 POST 兜底走这个）：真实实现会带上 Origin / Content-Type
        flow._phone_headers = lambda referer: {
            "Referer": referer,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://auth.openai.com",
        }
        # 导航头用真家伙，这样断言的就是线上真会发出去的那组
        flow._ua = "Mozilla/5.0 (Macintosh) Chrome/126"
        flow._fingerprint = {
            "lang_full": "en-US,en;q=0.9",
            "sec_ch_ua": '"Chromium";v="126"',
            "sec_ch_ua_mobile": "?0",
            "sec_ch_ua_platform": '"macOS"',
        }
        return flow

    def test_prefers_the_endpoint_the_server_pointed_at(self):
        flow = self._flow([_FakeResponse(payload={"page": {"type": "phone_otp_verification"}})])

        flow.send_phone_otp("+56971901026", "/api/accounts/phone-otp/send")

        call = flow.session.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], "https://auth.openai.com/api/accounts/phone-otp/send")

    def test_send_get_is_a_document_navigation_not_an_xhr(self):
        """抓包里这一枪是整页跳转：Accept 是 HTML，没有 Origin/Content-Type。

        按 XHR 发过去服务端不认这一步，短信压根发不出来。
        """
        flow = self._flow([_FakeResponse(payload={"page": {"type": "contact_verification"}})])

        flow.send_phone_otp("+56971901026")

        call = flow.session.calls[0]
        headers = call["headers"]
        self.assertEqual(call["method"], "GET")
        self.assertIn("text/html", headers["accept"])
        self.assertEqual(headers["sec-fetch-dest"], "document")
        self.assertEqual(headers["sec-fetch-mode"], "navigate")
        self.assertEqual(headers["sec-fetch-site"], "same-origin")
        self.assertEqual(headers["upgrade-insecure-requests"], "1")
        self.assertEqual(headers["Referer"], "https://auth.openai.com/create-account/password")
        self.assertNotIn("Origin", headers)
        self.assertNotIn("Content-Type", headers)
        # 302 之后才落到 /contact-verification，不跟就等于没发
        self.assertTrue(call["kwargs"].get("allow_redirects"))
        # 用户没点任何东西，这是脚本发起的跳转
        self.assertNotIn("sec-fetch-user", headers)

    def test_html_landing_page_becomes_the_next_step(self):
        """导航版回的是 HTML，没有 JSON —— 落点 URL 就是服务端给的下一步。"""
        flow = self._flow([
            _FakeResponse(
                text="<html>enter the code we sent</html>",
                url="https://auth.openai.com/contact-verification?flow=x",
            )
        ])

        result = flow.send_phone_otp("+56971901026")

        self.assertEqual(
            result, {"continue_url": "https://auth.openai.com/contact-verification"}
        )

    def test_html_landing_page_does_not_trigger_a_channel_warning(self):
        """页面上列着 WhatsApp/语音这些别的选项，不代表这次的码是那么发的。"""
        flow = self._flow([
            _FakeResponse(
                text="<html>Send via WhatsApp instead, or use a voice call</html>",
                url="https://auth.openai.com/contact-verification",
            )
        ])

        with self.assertLogs("platforms.chatgpt.protocol.phone_flow", level="INFO") as logs:
            flow.send_phone_otp("+56971901026")

        self.assertNotIn("WhatsApp", "\n".join(logs.output))

    def test_post_fallbacks_still_go_out_as_xhr(self):
        flow = self._flow([
            _FakeResponse(status_code=405, text="method not allowed"),
            _FakeResponse(payload={"page": {"type": "phone_otp_verification"}}),
        ])

        flow.send_phone_otp("+56971901026")

        post = flow.session.calls[1]
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["headers"]["Content-Type"], "application/json")
        self.assertEqual(post["headers"]["Origin"], "https://auth.openai.com")

    def test_falls_back_through_the_other_send_endpoints(self):
        flow = self._flow([
            _FakeResponse(status_code=405, text="method not allowed"),
            _FakeResponse(status_code=400, text="invalid authorization step"),
            _FakeResponse(payload={"page": {"type": "phone_otp_verification"}}),
        ])

        flow.send_phone_otp("+56971901026")

        # 只发一次码，不碰任何 resend 接口
        self.assertEqual(
            [(call["method"], call["url"].rsplit("/accounts/", 1)[-1]) for call in flow.session.calls],
            [
                ("GET", "phone-otp/send"),
                ("POST", "phone-otp/send"),
                ("POST", "add-phone/send"),
            ],
        )
        self.assertEqual(
            flow.session.calls[-1]["json"], {"phone_number": "+56971901026", "channel": "sms"}
        )

    def test_logs_the_next_step_not_the_whole_body(self):
        """HTTP 200 只说明这一步被受理了，日志要的是下一步，不是整段 JSON。"""
        body = '{"page":{"type":"contact_verification"},"channel":"whatsapp","continue_url":"https://auth.openai.com/contact-verification"}'
        payload = {
            "page": {"type": "contact_verification"},
            "channel": "whatsapp",
            "continue_url": "https://auth.openai.com/contact-verification",
        }
        flow = self._flow([_FakeResponse(text=body, payload=payload)])

        with self.assertLogs("platforms.chatgpt.protocol.phone_flow", level="INFO") as logs:
            flow.send_phone_otp("+56971901026")

        joined = "\n".join(logs.output)
        self.assertIn("page=contact_verification", joined)
        self.assertIn("continue=https://auth.openai.com/contact-verification", joined)
        # 通道异常仍要提醒，但响应体本身不该刷进日志
        self.assertIn("WhatsApp", joined)
        self.assertNotIn(body, joined)
        self.assertNotIn('"channel"', joined)

    def test_reports_every_endpoint_it_tried(self):
        body = '{"error":{"message":"invalid authorization step","code":"invalid_state"}}'
        flow = self._flow([_FakeResponse(status_code=400, text=body)] * 3)

        with self.assertRaises(RuntimeError) as ctx:
            flow.send_phone_otp("+56971901026")

        message = str(ctx.exception)
        self.assertIn("phone-otp/send", message)
        self.assertIn("add-phone/send", message)
        self.assertIn("invalid authorization step", message)

    def test_error_pages_are_summarised_instead_of_dumped(self):
        """服务端回错误页时只说"没给 message"，别把整页 HTML 抄进日志。"""
        html = "<html><body>502 Bad Gateway</body></html>"
        flow = self._flow([_FakeResponse(status_code=502, text=html)] * 3)

        with self.assertRaises(RuntimeError) as ctx, \
                self.assertLogs("platforms.chatgpt.protocol.phone_flow", level="WARNING") as logs:
            flow.send_phone_otp("+56971901026")

        joined = "\n".join(logs.output) + str(ctx.exception)
        self.assertIn("502", joined)
        self.assertNotIn("Bad Gateway", joined)


class PluginFailurePropagationTests(unittest.TestCase):
    def test_unretryable_result_reaches_the_task_layer_as_such(self):
        from core.base_platform import RegisterConfig
        from core.task_runtime import NonRetryableRegisterError
        from platforms.chatgpt.plugin import ChatGPTPlatform
        from platforms.chatgpt.registration_engine import RegistrationResult

        adapter = mock.Mock()
        adapter.run.return_value = RegistrationResult(
            success=False, error_message="账号已建好但一条短信都没收到", retryable=False
        )
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}), mailbox=object())

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            with self.assertRaises(NonRetryableRegisterError):
                platform.register(email="a@b.c", password="pw")

    def test_ordinary_failure_stays_retryable(self):
        from core.base_platform import RegisterConfig
        from core.task_runtime import NonRetryableRegisterError
        from platforms.chatgpt.plugin import ChatGPTPlatform
        from platforms.chatgpt.registration_engine import RegistrationResult

        adapter = mock.Mock()
        adapter.run.return_value = RegistrationResult(success=False, error_message="网络抖了")
        platform = ChatGPTPlatform(config=RegisterConfig(extra={}), mailbox=object())

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            with self.assertRaises(RuntimeError) as ctx:
                platform.register(email="a@b.c", password="pw")

        self.assertNotIsInstance(ctx.exception, NonRetryableRegisterError)


class RegistrationEngineFlowDispatchTests(unittest.TestCase):
    def _engine(self, register_flow, sms_callback=object()):
        engine = ChatGPTRegistrationEngine(
            mailbox=object(),
            proxy=None,
            extra_config={},
            log_fn=lambda _message: None,
            register_flow=register_flow,
        )
        engine._build_sms_callback = lambda: sms_callback
        return engine

    def test_phone_flow_does_not_claim_a_mailbox(self):
        engine = self._engine(REGISTER_FLOW_PHONE)
        auth_result = AuthResult()
        auth_result.phone_number = "+56971901026"
        auth_result.email = "+56971901026"
        auth_result.access_token = "at"
        auth_result.session_token = "st"
        flow = mock.Mock()
        flow.run_phone_register.return_value = auth_result
        flow._bind_email_error = ""

        with mock.patch.object(ChatGPTRegistrationEngine, "_build_flow", return_value=flow):
            result = engine.run()

        flow.run_phone_register.assert_called_once_with(mail_provider=None, bind_email=False)
        self.assertTrue(result.success)
        self.assertEqual(result.email, "+56971901026")
        self.assertEqual(result.metadata["phone_number"], "+56971901026")
        self.assertEqual(result.source, "phone_register")

    def test_phone_with_email_flow_passes_a_mail_provider(self):
        engine = self._engine(REGISTER_FLOW_PHONE_WITH_EMAIL)
        auth_result = AuthResult()
        auth_result.phone_number = "+56971901026"
        auth_result.email = "+56971901026"
        auth_result.bound_email = "pool@example.com"
        auth_result.access_token = "at"
        auth_result.session_token = "st"
        flow = mock.Mock()
        flow.run_phone_register.return_value = auth_result
        flow._bind_email_error = ""

        with mock.patch.object(ChatGPTRegistrationEngine, "_build_flow", return_value=flow):
            result = engine.run()

        kwargs = flow.run_phone_register.call_args.kwargs
        self.assertTrue(kwargs["bind_email"])
        self.assertIsNotNone(kwargs["mail_provider"])
        # 绑上邮箱之后账号按邮箱记账，手机号留在 metadata 里
        self.assertEqual(result.email, "pool@example.com")
        self.assertEqual(result.metadata["bound_email"], "pool@example.com")
        self.assertEqual(result.metadata["phone_number"], "+56971901026")

    def test_unbound_email_is_reported_without_failing_the_account(self):
        engine = self._engine(REGISTER_FLOW_PHONE_WITH_EMAIL)
        auth_result = AuthResult()
        auth_result.phone_number = "+56971901026"
        auth_result.email = "+56971901026"
        auth_result.access_token = "at"
        auth_result.session_token = "st"
        flow = mock.Mock()
        flow.run_phone_register.return_value = auth_result
        flow._bind_email_error = "invalid state"

        with mock.patch.object(ChatGPTRegistrationEngine, "_build_flow", return_value=flow):
            result = engine.run()

        self.assertTrue(result.success)
        self.assertEqual(result.email, "+56971901026")
        self.assertEqual(result.metadata["bind_email_error"], "invalid state")

    def test_dead_end_failure_is_marked_unretryable_for_the_task_layer(self):
        """半成品账号 + 平台零短信：这个信号要一路传到外层重试那里。"""
        engine = self._engine(REGISTER_FLOW_PHONE)
        flow = mock.Mock()
        flow.result = AuthResult()
        flow._bind_email_error = ""
        flow.run_phone_register.side_effect = PhoneAccountCreatedError(
            "号 +2349157587437 在 120s 内没收到短信", phone="+2349157587437", password="pw"
        )

        with mock.patch.object(ChatGPTRegistrationEngine, "_build_flow", return_value=flow):
            result = engine.run()

        self.assertFalse(result.success)
        self.assertFalse(result.retryable)

    def test_protocol_steps_reach_the_task_log(self):
        """协议层的步骤要看得见，否则线上出事只能靠猜。"""
        lines = []
        engine = ChatGPTRegistrationEngine(
            mailbox=object(),
            extra_config={},
            log_fn=lines.append,
            register_flow=REGISTER_FLOW_PHONE,
        )
        engine._build_sms_callback = lambda: object()
        auth_result = AuthResult()
        auth_result.access_token = "at"
        auth_result.session_token = "st"
        flow = mock.Mock()
        flow._bind_email_error = ""

        def _run(**kwargs):
            logging.getLogger("platforms.chatgpt.protocol.phone_flow").info(
                "user/register 成功，服务端下一步 page=phone_otp_send"
            )
            return auth_result

        flow.run_phone_register.side_effect = _run

        with mock.patch.object(ChatGPTRegistrationEngine, "_build_flow", return_value=flow):
            engine.run()

        self.assertTrue(
            any("phone_otp_send" in line for line in lines),
            f"协议日志没进任务日志: {lines}",
        )

    def test_phone_flow_without_sms_fails_with_a_clear_message(self):
        engine = self._engine(REGISTER_FLOW_PHONE, sms_callback=None)

        result = engine.run()

        self.assertFalse(result.success)
        self.assertIn("接码", result.error_message)


class RegisterFlowResolutionTests(unittest.TestCase):
    def test_defaults_to_email(self):
        self.assertEqual(resolve_chatgpt_register_flow({}), CHATGPT_REGISTER_FLOW_EMAIL)
        self.assertEqual(normalize_chatgpt_register_flow("nonsense"), CHATGPT_REGISTER_FLOW_EMAIL)

    def test_accepts_phone_variants(self):
        self.assertEqual(normalize_chatgpt_register_flow("phone"), CHATGPT_REGISTER_FLOW_PHONE)
        self.assertEqual(normalize_chatgpt_register_flow("SMS"), CHATGPT_REGISTER_FLOW_PHONE)
        self.assertEqual(
            normalize_chatgpt_register_flow("phone-with-email"),
            CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL,
        )
        self.assertEqual(
            normalize_chatgpt_register_flow("phone_bind_email"),
            CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL,
        )

    def test_adapter_reads_flow_from_task_extra(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_register_flow": "phone_with_email"}
        )
        self.assertEqual(adapter.register_flow, CHATGPT_REGISTER_FLOW_PHONE_WITH_EMAIL)

    def test_account_extra_records_flow_and_identities(self):
        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_register_flow": "phone"})
        result = RegistrationResult(
            success=True,
            email="+56971901026",
            password="pw",
            access_token="at",
            session_token="st",
            source="phone_register",
            metadata={"phone_number": "+56971901026", "bound_email": ""},
        )

        account = adapter.build_account(result, "fallback")

        self.assertEqual(account.email, "+56971901026")
        self.assertEqual(account.extra["chatgpt_register_flow"], CHATGPT_REGISTER_FLOW_PHONE)
        self.assertEqual(account.extra["phone_number"], "+56971901026")
        self.assertNotIn("bound_email", account.extra)

    def test_engine_receives_the_flow_from_the_adapter(self):
        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_register_flow": "phone"})
        context = ChatGPTRegistrationContext(
            mailbox=object(),
            proxy_url=None,
            callback_logger=lambda _message: None,
            email="",
            password="pw",
            extra_config={},
        )

        with mock.patch(
            "platforms.chatgpt.chatgpt_registration_mode_adapter.ChatGPTRegistrationEngine"
        ) as engine_cls:
            engine_cls.return_value.run.return_value = RegistrationResult(success=True)
            adapter.run(context)

        self.assertEqual(
            engine_cls.call_args.kwargs["register_flow"], CHATGPT_REGISTER_FLOW_PHONE
        )


if __name__ == "__main__":
    unittest.main()
