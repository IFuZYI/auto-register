"""注册链、2FA 链、RT 链的**顺序契约**。

## 用户报的问题（2026-10-28）

「现在gpt注册链，2FA链，RT链整个逻辑顺序都有些问题」。

读参考实现（`reference/register/any-auto-register-gpt`）后确认：整条链的
顺序是**有意设计**的，而本仓有几处顺序**只在注释里约定、没有测试**——
顺序一变就静默失效。这条测试把契约钉住。

## 契约（参考实现 `auth_flow.py` 的注释与调用点）

    创建账户 → 重定向链 → 拿 session → ★绑 2FA★ → Codex 授权 → 接码

关键点：

1. **2FA 在 get_auth_session 之后**：绑 2FA 要 access_token 作 Bearer
   （`two_factor.py` 的 enroll 打 chatgpt.com mfa 接口）。
2. **2FA 在 Codex RT 交换之前**：RT 的登录回退要过 mfa-challenge，需要
   `totp_secret`——那正是快路径刚写进 `flow.result.totp_secret` 的。
   顺序颠倒 → 登录回退撞 mfa 且没有密钥 → 死路。
3 **挂 2FA 钩子必须跳过 callback 前那次 Codex 抢跑**：否则 Codex 跑到钩子
   前面，顺序等于没调。
4. **快路径失败要能回落慢路径**（重新登录再绑）。

## 为什么必须钉住

实测踩过：`_acquire_rt` 被赋值却从未调用（四个 Codex 调用点全绕过它），
顺序约定形同虚设 —— 这类「约定只写在注释里」的地方正是顺序 bug 的温床。
"""

from __future__ import annotations

import inspect
import unittest


class TwoFactorSitsBetweenSessionAndCodexTests(unittest.TestCase):
    """2FA 钩子必须在 `get_auth_session` 之后、Codex RT 交换之前。"""

    def _run_register_source(self) -> str:
        from platforms.chatgpt.protocol.auth_flow import AuthFlow

        src = inspect.getsource(AuthFlow.run_register)
        return src

    def test_hook_comes_after_get_auth_session(self):
        src = self._run_register_source()
        i_session = src.index("self.get_auth_session()")
        i_hook = src.index("self._on_session_ready(self, self.result.access_token)")
        self.assertLess(
            i_session, i_hook,
            "2FA 钩子跑在 get_auth_session 之前 —— 绑 2FA 需要 access_token",
        )

    def test_hook_comes_before_the_rt_exchange(self):
        src = self._run_register_source()
        i_hook = src.index("self._on_session_ready(self, self.result.access_token)")
        i_rt = src.index("self._ensure_refresh_token(mail_provider)")
        self.assertLess(
            i_hook, i_rt,
            "2FA 跑到 Codex RT 交换之后了 —— RT 登录回退需要快路径刚写的 "
            "totp_secret，顺序颠倒会让 mfa-challenge 变死路",
        )

    def test_hook_is_guarded_by_access_token(self):
        """没有 access_token 时不该调钩子（会白跑一次网络往返）。"""
        src = self._run_register_source()
        self.assertIn(
            "if self._on_session_ready is not None and self.result.access_token:",
            src,
            "2FA 钩子没有 access_token 门控",
        )


class HookSkipsTheSpeculativeCodexRunTests(unittest.TestCase):
    """挂了 2FA 钩子就必须跳过 callback 前那次 Codex 抢跑。

    否则 Codex 抢在钩子前面跑完，用户指定的顺序（先 2FA 后 Codex）等于没调。
    """

    def _speculative_guard_source(self) -> str:
        from platforms.chatgpt.protocol.auth_flow import AuthFlow

        src = inspect.getsource(AuthFlow.run_register)
        start = src.index("OAUTH_CODEX_RT_BEFORE_CALLBACK")
        return src[start - 600 : start + 300]

    def test_guard_checks_the_hook(self):
        block = self._speculative_guard_source()
        self.assertIn(
            "self._on_session_ready is None", block,
            "抢跑没有检查 2FA 钩子 —— Codex 会跑到 2FA 前面",
        )


class FastPathFailureFallsBackToSlowPathTests(unittest.TestCase):
    """快路径没绑上时要能回落慢路径（重新登录再绑）。"""

    def test_ensure_two_factor_has_a_slow_path(self):
        from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine

        src = inspect.getsource(ChatGPTRegistrationEngine._ensure_two_factor)
        self.assertIn(
            "bind_totp_via_login", src,
            "快路径失败没有回落慢路径 —— 绑 2FA 会静默失败",
        )

    def test_slow_path_primes_the_mailbox(self):
        """慢路径必须先打邮箱基线。

        注册那封验证码信还躺在收件箱里；不 prime 的话绑定链会把**旧码**当
        绑定码用（参考实现 `registration_engine` 的注释明确警告过）。
        """
        from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine

        src = inspect.getsource(ChatGPTRegistrationEngine._ensure_two_factor)
        self.assertIn(
            "prime()", src,
            "慢路径没有 prime 邮箱 —— 会拿注册那封旧码当绑定码",
        )

    def test_slow_path_skips_phone_identity(self):
        """手机号身份的号没有邮箱可登，慢路径要跳过（不是报错）。"""
        from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine

        src = inspect.getsource(ChatGPTRegistrationEngine._ensure_two_factor)
        self.assertIn(
            '"@" not in email', src,
            "慢路径没有排除手机号身份 —— 会拿手机号当邮箱去登录",
        )


class RtLoginFallbackGetsTheTotpSecretTests(unittest.TestCase):
    """RT 登录回退必须拿到 2FA 快路径写入的 totp_secret。"""

    def test_adapter_reads_totp_from_the_flow_result(self):
        from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine

        src = inspect.getsource(ChatGPTRegistrationEngine._acquire_rt_adapter)
        self.assertIn(
            "flow.result.totp_secret", src,
            "换 RT 时没读快路径写的 totp_secret —— 登录回退撞 mfa 会死路",
        )

    def test_login_route_receives_totp_secret(self):
        from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine

        src = inspect.getsource(ChatGPTRegistrationEngine._acquire_refresh_token)
        self.assertIn(
            "totp_secret=totp_secret", src,
            "登录回退没把 totp_secret 传下去",
        )


class AllRtCallSitesGoThroughTheHelperTests(unittest.TestCase):
    """**所有**换 RT 的调用点都要走 `_ensure_refresh_token`。

    实测踩过：`_acquire_rt` 被赋值却从未调用 —— 四个 Codex 调用点全都绕过它，
    路线选择（`chatgpt_rt_route`）等于没生效。
    """

    def test_no_direct_codex_exchange_in_run_register(self):
        from platforms.chatgpt.protocol.auth_flow import AuthFlow

        src = inspect.getsource(AuthFlow.run_register)
        offenders = [
            line.strip()
            for line in src.splitlines()
            if "self.oauth_codex_rt_exchange(" in line
            and "_speculative_rt_attempt" not in line
        ]
        self.assertEqual(
            offenders, [],
            "run_register 里直接调 oauth_codex_rt_exchange —— 绕过了路线选择：\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
