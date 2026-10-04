import unittest
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.base_platform import AccountStatus, RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.registration_engine import RegistrationResult


class _StubMailbox:
    def __init__(self, email="demo@example.com"):
        self.account = MailboxAccount(email=email, account_id="stub-mailbox")
        self.requeued = []

    def get_email(self):
        return self.account

    def wait_for_code(self, *args, **kwargs):
        return "123456"

    def requeue_account(self, account):
        self.requeued.append(account)


class _RecordingAdapter:
    """记录 plugin 传下来的注册上下文，并返回一个可控结果。"""

    def __init__(self, result=None):
        self.context = None
        self.build_account_args = None
        self._result = result or RegistrationResult(
            success=True,
            email="demo@example.com",
            password="pw-from-engine",
            access_token="at-demo",
        )

    def run(self, context):
        self.context = context
        return self._result

    def build_account(self, result, fallback_password):
        self.build_account_args = (result, fallback_password)
        return "account-sentinel"


class ChatGPTPluginTests(unittest.TestCase):
    def _register(self, platform, adapter):
        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            return platform.register()

    def test_register_passes_mailbox_and_runtime_context_to_adapter(self):
        mailbox = _StubMailbox()
        platform = ChatGPTPlatform(
            config=RegisterConfig(
                proxy="http://127.0.0.1:7890",
                extra={
                    "chatgpt_registration_mode": "refresh_token",
                    "mailbox_otp_timeout_seconds": 90,
                },
            ),
            mailbox=mailbox,
        )
        adapter = _RecordingAdapter()

        account = self._register(platform, adapter)

        self.assertEqual(account, "account-sentinel")
        context = adapter.context
        self.assertIs(context.mailbox, mailbox)
        self.assertEqual(context.mailbox_kind, "mailbox")
        self.assertEqual(context.proxy_url, "http://127.0.0.1:7890")
        self.assertEqual(context.extra_config["mailbox_otp_timeout_seconds"], 90)

    def test_register_generates_password_when_not_supplied(self):
        adapter = _RecordingAdapter()
        platform = ChatGPTPlatform(config=RegisterConfig(), mailbox=_StubMailbox())

        self._register(platform, adapter)

        generated = adapter.context.password
        self.assertTrue(generated)
        self.assertGreaterEqual(len(generated), 16)
        # 生成的密码同时作为兜底密码传给 build_account
        self.assertEqual(adapter.build_account_args[1], generated)

    def test_register_keeps_caller_supplied_credentials(self):
        adapter = _RecordingAdapter()
        platform = ChatGPTPlatform(config=RegisterConfig(), mailbox=_StubMailbox())

        with mock.patch(
            "platforms.chatgpt.plugin.build_chatgpt_registration_mode_adapter",
            return_value=adapter,
        ):
            platform.register(email="fixed@example.com", password="fixed-pw")

        self.assertEqual(adapter.context.email, "fixed@example.com")
        self.assertEqual(adapter.context.password, "fixed-pw")

    def test_register_without_a_mailbox_fails_instead_of_using_a_temp_mailbox(self):
        """没有绑定邮箱时必须报错，不能悄悄兜底成一次性临时邮箱。

        回归：这里曾兜底成 `TempMailLolMailbox` —— 注册出来的账号在临时邮箱
        过期后永远收不到验证码，而用户当时看到的是「注册成功」。一次性临时邮箱
        渠道已整体删除，兜底对象不存在了，行为改为明确失败。
        """
        adapter = _RecordingAdapter()
        platform = ChatGPTPlatform(config=RegisterConfig(proxy="http://proxy:1"))

        with self.assertRaises(RuntimeError) as ctx:
            self._register(platform, adapter)

        self.assertIn("必须先选定邮箱服务", str(ctx.exception))
        # 不能在报错前就已经拿一个临时邮箱去注册
        self.assertIsNone(adapter.context)

    def test_register_raises_with_engine_error_message(self):
        mailbox = _StubMailbox()
        adapter = _RecordingAdapter(
            RegistrationResult(success=False, error_message="邮箱未取到验证码")
        )
        platform = ChatGPTPlatform(config=RegisterConfig(), mailbox=mailbox)

        with self.assertRaises(RuntimeError) as ctx:
            self._register(platform, adapter)

        self.assertIn("邮箱未取到验证码", str(ctx.exception))
        # 邮箱归还由任务运行时统一处理，插件不得私自回收
        self.assertEqual(mailbox.requeued, [])

    def test_register_builds_account_through_adapter(self):
        platform = ChatGPTPlatform(
            config=RegisterConfig(extra={"chatgpt_registration_mode": "access_token_only"}),
            mailbox=_StubMailbox(),
        )
        result = RegistrationResult(
            success=True,
            email="demo@example.com",
            password="pw-from-engine",
            access_token="at-demo",
        )

        with mock.patch(
            "platforms.chatgpt.chatgpt_registration_mode_adapter.ChatGPTRegistrationEngine"
        ) as engine_cls:
            engine_cls.return_value.run.return_value = result
            account = platform.register()

        self.assertEqual(account.platform, "chatgpt")
        self.assertEqual(account.email, "demo@example.com")
        self.assertEqual(account.password, "pw-from-engine")
        self.assertEqual(account.token, "at-demo")
        self.assertEqual(account.status, AccountStatus.REGISTERED)
        self.assertEqual(
            account.extra["chatgpt_registration_mode"], "access_token_only"
        )


if __name__ == "__main__":
    unittest.main()
