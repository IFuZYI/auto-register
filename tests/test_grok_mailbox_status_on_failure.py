"""注册失败时邮箱地址该记成什么状态 —— 瞬时拒绝不能烧号。

2026-11-03 线上损失：一小时的出口限流窗口把微软号池从 **6 个可用烧到 0 个**
（`outlook_accounts` 里 7 条 `failed`），而几分钟后同一个地址完全可用 ——
Outlook 渠道里 `failed` 等于「永久移出池子」，地址是稀缺资源，不该因为一个
跟它无关的瞬时拒绝就丢掉。
"""
from __future__ import annotations

import unittest

from platforms.grok.plugin import _mailbox_status_for_failure


class MailboxStatusOnFailureTests(unittest.TestCase):
    def test_transient_email_rejection_returns_address_to_pool(self):
        """`account:email-signup-unavailable` 是出口 IP/域名窗口的瞬时拒绝，地址没被消耗。"""
        self.assertEqual(
            _mailbox_status_for_failure({"email_unavailable": True, "consumes_address": False}),
            "available",
        )

    def test_rate_limited_returns_address_to_pool(self):
        self.assertEqual(_mailbox_status_for_failure({"rate_limited": True}), "available")

    def test_already_registered_marks_used(self):
        self.assertEqual(
            _mailbox_status_for_failure({"already_registered": True, "email_unavailable": True}),
            "used",
        )

    def test_consumed_wins_over_transient(self):
        """两条信号同时出现时，「已被消耗」优先 —— 宁可少发，不要发一个用过的地址。"""
        self.assertEqual(
            _mailbox_status_for_failure({"consumes_address": True, "rate_limited": True}),
            "used",
        )

    def test_other_failures_keep_channel_semantics(self):
        """导航失败等其它失败沿用旧行为（渠道自己解释 `failed`）。"""
        self.assertEqual(_mailbox_status_for_failure({"error": "打开注册页失败（打开超时）"}), "failed")
        self.assertEqual(_mailbox_status_for_failure({}), "failed")


class FailureBranchUsesTheDecisionTests(unittest.TestCase):
    """源码级钉住：失败分支必须走这个判定，不能写回 `"used" if consumed else "failed"`。"""

    def test_plugin_uses_the_pure_helper(self):
        import inspect

        from platforms.grok import plugin

        src = inspect.getsource(getattr(plugin.GrokPlatform, "_register_via_browser"))
        self.assertIn("_mailbox_status_for_failure(result)", src)
        self.assertNotIn('"used" if consumed else "failed"', src)


class AntibotNotReadyMustNotBurnTheAddressTests(unittest.TestCase):
    """**页面根本没提交**时，地址连碰都没碰过，不能记成 `failed`。

    2026-11-04 独立审查（切片 A1）发现：`antibot_not_ready` 这个键既不在
    `_TRANSIENT_REJECTION_KEYS` 里，也不是 `consumes_address`，于是落到最后的
    `return "failed"` —— 而 Outlook 渠道里 `failed` 等于**永久移出池子**。
    后果与 2026-11-03 那次烧号事故是同一类损失，只是触发条件换成了「反机器人
    令牌没就绪、我们压根没提交表单」。

    注册链路在 `_send_code_stage` 里对此写得非常明确：
    `consumes_address=False, antibot_not_ready=True, retryable=True`
    —— 三条信号没有一条指向「地址坏了」。
    """

    def test_antibot_not_ready_returns_address_to_pool(self):
        stage = {
            "error": "页面未就绪（反机器人令牌未出现）",
            "retryable": True,
            "consumes_address": False,
            "antibot_not_ready": True,
        }
        self.assertEqual(
            _mailbox_status_for_failure(stage),
            "available",
            "页面没提交 = 地址没被碰过，必须放回池子（Outlook 的 failed 是永久移出）",
        )

    def test_antibot_not_ready_that_consumed_the_address_still_marks_used(self):
        """万一两者同时出现，「已消耗」优先 —— 宁可少发，不要发一个用过的地址。"""
        self.assertEqual(
            _mailbox_status_for_failure({"antibot_not_ready": True, "consumes_address": True}),
            "used",
        )

    def test_real_hard_failure_still_marks_failed(self):
        """对照组：真正说不清的失败仍然 `failed`，别把这条规则放宽成「什么都放回池子」。"""
        self.assertEqual(
            _mailbox_status_for_failure({"error": "打开注册页失败（打开超时）", "retryable": False}),
            "failed",
        )
