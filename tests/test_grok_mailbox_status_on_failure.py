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
