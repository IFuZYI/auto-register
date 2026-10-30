"""RT 三条路线的「收件渠道」必须真的传下去（tests-quality 审计 P0）。

为什么单独立一条：现有守卫是 `inspect.getsource` + `assertIn("self.mailbox", src)`
—— 注释里出现这个词就算过。mutation 实测：把 `mail_provider` 改成 `None`、
保留源码里的 `self.mailbox` 字面量，`test_registration_chain_ordering` +
`test_chatgpt_registration_engine` + `test_chatgpt_platform_rt_route` +
`test_chatgpt_rt_backfill` 共 **109 条全绿**。

而这两处正是两次真实事故的现场：

- `2026-10-28`：`self.mail_provider` 拼错（属性不存在）→ AttributeError 被吞 →
  Codex 换 RT **从未执行**，一批号 AT/session 有、RT 全空；
- `2026-10-30`：`_run_login_rt` 漏传 `mail_provider` → 登录链撞邮箱验证码时
  直接放弃（「该路线不可用」），RT 拿不到。

这里用 `Mock` flow 做**行为断言**：谁被调用、`mail_provider` 是不是
`engine.mailbox` 本体。
"""

from __future__ import annotations

import unittest
from unittest import mock

from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine


def _engine(logs: list[str] | None = None) -> tuple[ChatGPTRegistrationEngine, mock.Mock]:
    mailbox = mock.MagicMock(name="mailbox")
    engine = ChatGPTRegistrationEngine(
        mailbox=mailbox,
        log_fn=(logs.append if logs is not None else (lambda *_: None)),
    )
    return engine, mailbox


class CodexRtWiringTests(unittest.TestCase):
    def test_mailbox_is_passed_as_mail_provider(self):
        engine, mailbox = _engine()
        flow = mock.MagicMock()
        flow.result.refresh_token = "rt-from-codex"

        self.assertTrue(engine._run_codex_rt(flow))

        kwargs = flow.oauth_codex_rt_exchange.call_args.kwargs
        self.assertIs(
            kwargs.get("mail_provider"),
            mailbox,
            "Codex 换 RT 没把收件渠道传下去 —— 撞邮箱验证码时整条路线静默失败",
        )

    def test_missing_refresh_token_counts_as_failure(self):
        engine, _ = _engine()
        flow = mock.MagicMock()
        flow.result.refresh_token = ""
        self.assertFalse(engine._run_codex_rt(flow))

    def test_exception_is_swallowed_and_logged(self):
        """Codex 失败要能回退登录链，不能炸整条注册流程。"""
        logs: list[str] = []
        engine, _ = _engine(logs)
        flow = mock.MagicMock()
        flow.oauth_codex_rt_exchange.side_effect = RuntimeError("boom")

        self.assertFalse(engine._run_codex_rt(flow))
        self.assertTrue(any("boom" in line for line in logs), f"没记日志: {logs}")


class LoginRtWiringTests(unittest.TestCase):
    def test_mailbox_is_passed_as_mail_provider(self):
        engine, mailbox = _engine()
        flow = mock.MagicMock()
        flow.acquire_rt_via_platform_login.return_value = True

        self.assertTrue(
            engine._run_login_rt(flow, email="a@x.c", password="pw", totp_secret="SECRET")
        )

        kwargs = flow.acquire_rt_via_platform_login.call_args.kwargs
        self.assertIs(kwargs.get("mail_provider"), mailbox, "登录链没传收件渠道")
        self.assertEqual(kwargs.get("email"), "a@x.c")
        self.assertEqual(kwargs.get("password"), "pw")
        self.assertEqual(kwargs.get("totp_secret"), "SECRET")

    def test_failure_reason_is_logged(self):
        logs: list[str] = []
        engine, _ = _engine(logs)
        flow = mock.MagicMock()
        flow.acquire_rt_via_platform_login.return_value = False
        flow.last_platform_route_error = "409 invalid_state"

        self.assertFalse(engine._run_login_rt(flow, email="a@x.c", password="pw"))
        self.assertTrue(any("409" in line for line in logs), f"没把原因写进日志: {logs}")

    def test_exception_is_swallowed(self):
        engine, _ = _engine()
        flow = mock.MagicMock()
        flow.acquire_rt_via_platform_login.side_effect = RuntimeError("boom")
        self.assertFalse(engine._run_login_rt(flow, email="a@x.c", password="pw"))


if __name__ == "__main__":
    unittest.main()
