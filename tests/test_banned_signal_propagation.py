"""登录链里的封禁信号必须整链传播 —— 不被探测回退吞掉。

用户报告（2026-10-07）：一批账号服务端已明确回「deleted or deactivated」
（403），刷新任务结束后状态仍是「失效」（invalid）而不是「禁用」（banned）
—— 账号被误标后继续进重试队列，白耗代理与风控额度。用户原文：

    这种不是禁用了嘛，为什么没变为禁用。

根因：`run_protocol_login` 的 login screen_hint 探测块（auth_flow.py）用
宽 `except Exception` 把 TOTP 提交的 403 当作「探测失败」吞掉、回退
signup 探测；回退链最终以 409 invalid_state 收场 —— 封禁措辞在最终
错误里消失，`looks_like_banned` 判不出来。

本文件钉住两件事：

1. 探测块内任意一步（authorize/continue、密码校验、TOTP 提交）撞上
   「号没了」措辞 → 当场向上抛（终局结论，不回退）；
2. 非封禁的失败（如 409 invalid_state）仍按原逻辑回退 —— 用户修正
   （2026-10-06）：「sign-in session is no longer valid」不是封禁。
"""

from __future__ import annotations

import unittest
from unittest import mock

from platforms.chatgpt.protocol.banned_signals import looks_like_banned

# 用户给的原文（OpenAI 对已停用账号的措辞）
DEACTIVATED_TEXT = (
    "You do not have an account because it has been deleted or deactivated. "
    "If you believe this was an error, please contact us through our help center at help.openai.com."
)
SIGNIN_SESSION_TEXT = (
    "Your sign-in session is no longer valid. Please start over to continue. invalid_state"
)


class ProbeBlockBannedPropagationTests(unittest.TestCase):
    """探测块里 TOTP 提交回 403 封禁 → 异常必须传播（不落回退链）。"""

    def _flow(self):
        from platforms.chatgpt.protocol import AuthFlow, Config

        flow = AuthFlow(Config(proxy=None))
        # 前置链路全部打桩（本测试只关心探测块到 TOTP 提交这一段）
        flow.check_proxy = lambda: True
        flow.warmup = lambda: True
        flow.get_csrf_token = lambda: "csrf"
        flow.get_auth_url = lambda csrf, email="": "https://auth.openai.com/authorize?x=1"
        flow.auth_oauth_init = lambda url: "device-1"
        flow.get_sentinel_token = lambda device_id: "sentinel-1"
        flow.authorize_continue = lambda **kwargs: {
            "page": {"type": "login_password"},
            "continue_url": "https://auth.openai.com/log-in/password",
        }
        flow.login_password_verify = lambda password: {
            "page": {"type": "mfa_challenge"},
            "continue_url": "https://auth.openai.com/mfa-challenge/ch-123",
        }
        flow.result.totp_secret = "JBSWY3DPEHPK3PXP"
        # 回退链打桩：封禁若被吞掉，异常会从这条链上抛出来（409），
        # 从而让断言看到「封禁措辞消失」的确切失败形态。
        flow.signup = lambda email, sentinel: False
        flow.kickoff_otp_delivery = lambda mode="": False

        def _send_otp_409(*args, **kwargs):
            raise RuntimeError(f"发送 OTP 失败: 409 - {SIGNIN_SESSION_TEXT}")

        flow.send_otp = _send_otp_409
        return flow

    def test_totp_403_banned_signal_propagates(self):
        """TOTP 提交回 403 deleted or deactivated → 向上抛封禁异常。

        回归：探测块的宽 except 把它吞掉后回退 signup 探测，回退链以
        409 invalid_state 收场 —— 最终异常里没有封禁措辞，账号被误标
        「失效」而不是「禁用」（实测 11 个账号全部如此）。
        """
        flow = self._flow()

        def _mfa_banned(totp_code, challenge_id):
            raise RuntimeError(f"TOTP 验证失败: 403 - {DEACTIVATED_TEXT}")

        flow.submit_mfa_totp = _mfa_banned

        with self.assertRaises(RuntimeError) as ctx:
            flow.run_protocol_login(mock.Mock(), "user@example.com", "pw")

        self.assertTrue(
            looks_like_banned(str(ctx.exception)),
            f"TOTP 提交的封禁措辞被探测回退吞掉了（最终异常: {ctx.exception}）",
        )

    def test_totp_409_is_still_a_probe_failure(self):
        """409 invalid_state 不是封禁 —— 仍按探测失败回退（不误伤）。"""
        flow = self._flow()

        def _mfa_invalid_state(totp_code, challenge_id):
            raise RuntimeError(f"TOTP 验证失败: 409 - {SIGNIN_SESSION_TEXT}")

        flow.submit_mfa_totp = _mfa_invalid_state

        with self.assertRaises(RuntimeError) as ctx:
            flow.run_protocol_login(mock.Mock(), "user@example.com", "pw")

        self.assertFalse(
            looks_like_banned(str(ctx.exception)),
            "409 invalid_state 被误判成封禁",
        )


class KickoffDeliveryBannedPropagationTests(unittest.TestCase):
    """发码兜底链撞上封禁 → 向上抛；普通失败仍返回 False。"""

    def _flow(self, send_otp_exc):
        from platforms.chatgpt.protocol import AuthFlow, Config

        flow = AuthFlow(Config(proxy=None))
        flow._is_existing_account = True
        flow.resend_otp = lambda referer="": False

        def _send(*args, **kwargs):
            raise send_otp_exc

        flow.send_otp = _send
        return flow

    def test_banned_send_otp_reason_propagates(self):
        """send_otp 抛 403 封禁 → kickoff 不能吞掉返回 False。"""
        flow = self._flow(RuntimeError(f"发送 OTP 失败: 403 - {DEACTIVATED_TEXT}"))

        with self.assertRaises(RuntimeError) as ctx:
            flow.kickoff_otp_delivery("existing")

        self.assertTrue(looks_like_banned(str(ctx.exception)))

    def test_409_send_otp_reason_still_returns_false(self):
        """409 invalid_state 仍返回 False（调用方还有兜底路径）。"""
        flow = self._flow(RuntimeError(f"发送 OTP 失败: 409 - {SIGNIN_SESSION_TEXT}"))

        self.assertFalse(flow.kickoff_otp_delivery("existing"))


if __name__ == "__main__":
    unittest.main()
