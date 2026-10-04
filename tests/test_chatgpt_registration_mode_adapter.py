import unittest
from unittest import mock

from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
    resolve_chatgpt_bind_2fa,
    resolve_chatgpt_registration_mode,
)
from platforms.chatgpt.registration_engine import RegistrationResult


def _context(**overrides) -> ChatGPTRegistrationContext:
    defaults = dict(
        mailbox=object(),
        proxy_url="http://127.0.0.1:7890",
        callback_logger=lambda _message: None,
        email="demo@example.com",
        password="pw-demo",
        extra_config={},
        mailbox_kind="mailbox",
    )
    defaults.update(overrides)
    return ChatGPTRegistrationContext(**defaults)


class ChatGPTRegistrationModeAdapterTests(unittest.TestCase):
    def test_resolve_defaults_to_refresh_token_mode(self):
        self.assertEqual(
            resolve_chatgpt_registration_mode({}),
            CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
        )

    def test_resolve_supports_boolean_no_rt_flag(self):
        self.assertEqual(
            resolve_chatgpt_registration_mode({"chatgpt_has_refresh_token_solution": False}),
            CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        )

    def test_bind_2fa_is_opt_in_and_accepts_string_flags(self):
        self.assertFalse(resolve_chatgpt_bind_2fa({}))
        self.assertFalse(resolve_chatgpt_bind_2fa({"chatgpt_bind_2fa": False}))
        self.assertTrue(resolve_chatgpt_bind_2fa({"chatgpt_bind_2fa": True}))
        self.assertTrue(resolve_chatgpt_bind_2fa({"chatgpt_bind_2fa": "1"}))
        self.assertFalse(resolve_chatgpt_bind_2fa({"chatgpt_bind_2fa": "0"}))

    def test_bind_2fa_reaches_the_engine_and_the_account_record(self):
        captured = {}

        class FakeEngine:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self):
                return RegistrationResult(success=True, email="demo@example.com")

        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_bind_2fa": True})
        with mock.patch(
            "platforms.chatgpt.chatgpt_registration_mode_adapter.ChatGPTRegistrationEngine",
            FakeEngine,
        ):
            adapter.run(_context())

        self.assertTrue(captured["bind_2fa"])
        account = adapter.build_account(
            RegistrationResult(
                success=True, email="demo@example.com", metadata={"totp_secret": "JBSWY3DPEHPK3PXP"}
            ),
            fallback_password="fallback",
        )
        self.assertTrue(account.extra["chatgpt_bind_2fa"])
        self.assertEqual(account.extra["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_build_account_marks_selected_mode(self):
        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        result = RegistrationResult(
            success=True,
            email="demo@example.com",
            password="pw",
            account_id="acct-demo",
            access_token="at-demo",
            id_token="id-demo",
            session_token="session-demo",
            workspace_id="ws-demo",
        )

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.email, "demo@example.com")
        self.assertEqual(account.password, "pw")
        self.assertEqual(account.token, "at-demo")
        self.assertEqual(
            account.extra["chatgpt_registration_mode"],
            CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        )
        self.assertFalse(account.extra["chatgpt_has_refresh_token_solution"])

    def test_build_account_falls_back_to_generated_password(self):
        adapter = build_chatgpt_registration_mode_adapter({})
        account = adapter.build_account(
            RegistrationResult(success=True, email="demo@example.com"),
            fallback_password="fallback",
        )
        self.assertEqual(account.password, "fallback")

    def test_adapter_passes_runtime_context_to_engine(self):
        captured = {}

        class FakeEngine:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run(self):
                return RegistrationResult(success=True, email="demo@example.com")

        adapter = build_chatgpt_registration_mode_adapter(
            {"chatgpt_registration_mode": "access_token_only"}
        )
        context = _context(extra_config={"mailbox_otp_timeout_seconds": 90})

        with mock.patch(
            "platforms.chatgpt.chatgpt_registration_mode_adapter.ChatGPTRegistrationEngine",
            FakeEngine,
        ):
            result = adapter.run(context)

        self.assertTrue(result.success)
        self.assertEqual(captured["mode"], CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY)
        self.assertEqual(captured["email"], "demo@example.com")
        self.assertEqual(captured["password"], "pw-demo")
        self.assertEqual(captured["proxy"], "http://127.0.0.1:7890")
        self.assertEqual(captured["extra_config"], {"mailbox_otp_timeout_seconds": 90})

    def test_mailbox_link_failure_retries_with_next_mailbox(self):
        attempts = []

        class FakeEngine:
            def __init__(self, **kwargs):
                attempts.append(kwargs)

            def run(self):
                if len(attempts) < 3:
                    return RegistrationResult(
                        success=False, error_message="outlook IMAP 认证失败"
                    )
                return RegistrationResult(success=True, email="demo@example.com")

        adapter = build_chatgpt_registration_mode_adapter({})
        with mock.patch(
            "platforms.chatgpt.chatgpt_registration_mode_adapter.ChatGPTRegistrationEngine",
            FakeEngine,
        ):
            result = adapter.run(_context())

        self.assertTrue(result.success)
        self.assertEqual(len(attempts), 3)

    def test_non_mailbox_failure_does_not_retry(self):
        attempts = []

        class FakeEngine:
            def __init__(self, **kwargs):
                attempts.append(kwargs)

            def run(self):
                return RegistrationResult(success=False, error_message="warmup 失败")

        adapter = build_chatgpt_registration_mode_adapter({})
        with mock.patch(
            "platforms.chatgpt.chatgpt_registration_mode_adapter.ChatGPTRegistrationEngine",
            FakeEngine,
        ):
            result = adapter.run(_context())

        self.assertFalse(result.success)
        self.assertEqual(len(attempts), 1)


if __name__ == "__main__":
    unittest.main()
