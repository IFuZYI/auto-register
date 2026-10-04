"""OTP 等待时长的单点解析。

回归：这段优先级规则（三个键、取第一个正整数）以前抄了四份，默认值还不一致
—— 三份手写用 180、`BasePlatform.get_mailbox_otp_timeout` 用 120。
等待时长直接决定「验证码还没到就放弃」的频率，不能按调用路径取不同的值。
"""

from __future__ import annotations

import unittest

from core.base_platform import BasePlatform, RegisterConfig, resolve_mailbox_otp_timeout


class ResolveOtpTimeoutTests(unittest.TestCase):
    def test_default_when_nothing_configured(self):
        self.assertEqual(resolve_mailbox_otp_timeout({}), 180)

    def test_first_key_wins(self):
        cfg = {
            "mailbox_otp_timeout_seconds": "90",
            "email_otp_timeout_seconds": "120",
            "otp_timeout": "150",
        }
        self.assertEqual(resolve_mailbox_otp_timeout(cfg), 90)

    def test_falls_through_to_the_next_key(self):
        cfg = {"email_otp_timeout_seconds": "120", "otp_timeout": "150"}
        self.assertEqual(resolve_mailbox_otp_timeout(cfg), 120)

    def test_accepts_int_values(self):
        self.assertEqual(resolve_mailbox_otp_timeout({"otp_timeout": 45}), 45)

    def test_blank_and_invalid_values_are_skipped(self):
        cfg = {
            "mailbox_otp_timeout_seconds": "",
            "email_otp_timeout_seconds": "abc",
            "otp_timeout": "60",
        }
        self.assertEqual(resolve_mailbox_otp_timeout(cfg), 60)

    def test_zero_and_negative_are_skipped(self):
        """0/负数不是"立即超时"，是没填 —— 该继续往下找。"""
        self.assertEqual(
            resolve_mailbox_otp_timeout({"mailbox_otp_timeout_seconds": "0", "otp_timeout": "30"}),
            30,
        )
        self.assertEqual(
            resolve_mailbox_otp_timeout({"mailbox_otp_timeout_seconds": "-5"}),
            180,
        )

    def test_none_config_is_safe(self):
        self.assertEqual(resolve_mailbox_otp_timeout(None), 180)

    def test_explicit_default_is_honored(self):
        self.assertEqual(resolve_mailbox_otp_timeout({}, default=60), 60)


class CallSitesAgreeTests(unittest.TestCase):
    """四份实现收敛后，各调用点必须给出同一个值。"""

    def test_rt_backfill_and_registration_engine_agree(self):
        from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine
        from platforms.chatgpt.rt_backfill import RefreshTokenBackfiller

        cfg = {"mailbox_otp_timeout_seconds": "77"}

        backfiller = RefreshTokenBackfiller(email="a@b.c", extra_config=cfg)
        engine = ChatGPTRegistrationEngine.__new__(ChatGPTRegistrationEngine)
        engine.extra_config = cfg

        self.assertEqual(backfiller._otp_timeout(), 77)
        self.assertEqual(engine._otp_timeout(), 77)

    def test_two_factor_helper_agrees(self):
        from services.chatgpt_two_factor import _otp_timeout

        self.assertEqual(_otp_timeout({"otp_timeout": "77"}), 77)

    def test_platform_helper_uses_the_same_default(self):
        """平台方法以前默认 120、手写的那几份默认 180 —— 现在要一致。"""

        class _P(BasePlatform):
            name = "t"
            display_name = "T"

            def check_valid(self, account):  # pragma: no cover - 抽象方法占位
                return True

            def register(self, *args, **kwargs):  # pragma: no cover - 抽象方法占位
                raise NotImplementedError

        platform = _P(RegisterConfig(extra={}))
        self.assertEqual(platform.get_mailbox_otp_timeout(), 180)
        self.assertEqual(platform.get_mailbox_otp_timeout(), resolve_mailbox_otp_timeout({}))


if __name__ == "__main__":
    unittest.main()
