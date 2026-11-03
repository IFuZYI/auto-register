"""Grok 注册的「邮箱域名窗口用尽 → 换渠道」失败切换。

背景（2026-11-02 真机 A/B）：与 icloud.com 被拒**同一分钟、同一出口**
（9.142.194.72），换 `outlook.kr` 地址立刻发码成功并收到验证码。所以该换的是
**邮箱域名**，不是出口 —— 旧实现只会在第二轮再领一个同域名的 iCloud 别名，
撞同一堵墙、白烧两三分钟。
"""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from services import task_runners


class MailDomainOfTests(unittest.TestCase):
    def test_lowercases_and_splits(self):
        self.assertEqual(task_runners._mail_domain_of("A@Outlook.COM"), "outlook.com")
        self.assertEqual(task_runners._mail_domain_of("x@icloud.com"), "icloud.com")

    def test_empty_is_empty(self):
        self.assertEqual(task_runners._mail_domain_of(""), "")
        self.assertEqual(task_runners._mail_domain_of(None), "")


class PickMailProviderFailoverTests(unittest.TestCase):
    CANDIDATES = ["outlook", "icloud_remote"]

    def _pick(self, current, blocked):
        return task_runners._pick_mail_provider_failover(
            current_provider=current, blocked_domains=set(blocked), candidates=list(self.CANDIDATES)
        )

    def test_switches_when_current_domain_is_blocked(self):
        self.assertEqual(self._pick("icloud_remote", {"icloud.com"}), "outlook")

    def test_switches_back_when_outlook_domain_is_blocked(self):
        self.assertEqual(self._pick("outlook", {"outlook.kr"}), "icloud_remote")

    def test_no_change_when_nothing_is_blocked(self):
        self.assertEqual(self._pick("icloud_remote", set()), "")

    def test_no_change_when_blocked_domain_belongs_to_another_provider(self):
        """别人家的域名被阻塞，不该让我们换掉自己的渠道。"""
        self.assertEqual(self._pick("icloud_remote", {"outlook.com"}), "")

    def test_gives_up_when_every_candidate_is_blocked(self):
        self.assertEqual(self._pick("icloud_remote", {"icloud.com", "outlook.com"}), "")

    def test_unknown_provider_is_never_treated_as_blocked(self):
        self.assertEqual(self._pick("", set()), "")
        self.assertEqual(self._pick("tempmail", {"icloud.com"}), "")


class OutlookPoolAvailableTests(unittest.TestCase):
    def test_reads_available_count(self):
        class _Box:
            @staticmethod
            def pool_status_summary():
                return {"available": 4, "in_use": 1}

        with patch.dict(
            "sys.modules",
            {"core.mailboxes.channels.outlook": type("m", (), {"OutlookMailbox": _Box})},
        ):
            self.assertEqual(task_runners._outlook_pool_available(), 4)

    def test_failure_is_zero_not_an_exception(self):
        with patch.dict("sys.modules", {"core.mailboxes.channels.outlook": None}):
            self.assertEqual(task_runners._outlook_pool_available(), 0)


class FailoverWiringTests(unittest.TestCase):
    """失败分支必须真的把域名记下来、下一轮真的换渠道（防「只在注释里实现」）。"""

    def test_register_flow_records_the_blocked_domain(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("blocked_mail_domains.add(domain)", src)
        self.assertIn("email-signup-unavailable", src)

    def test_register_flow_switches_provider_for_the_next_round(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("_pick_mail_provider_failover(", src)
        self.assertIn('merged_extra["mail_provider"] = failover', src)


class FailoverExtraAttemptTests(unittest.TestCase):
    """换域名要拿到**独立的一次机会**，且邮箱池真的换成新渠道。

    实测教训：`register_retry_times=1` 时，第一轮撞域名窗口、第二轮才发现要换
    域名 —— 那时已经没有轮次了，日志里只有「配额用尽」和最终失败。
    """

    def test_failover_is_not_charged_to_the_retry_budget(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("域名窗口配额用尽 → 换邮箱域名再试一次", src)
        self.assertIn("failover_used", src)
        self.assertIn("_should_try_domain_failover(", src)

    def test_round_mailbox_uses_the_failover_provider(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("_build_mailbox(_proxy, provider=round_mail_provider)", src)
        self.assertIn("round_mail_provider", src)

    def test_failover_requires_an_available_alternate_pool(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("_outlook_pool_available() > 0", src)


class ShouldTryDomainFailoverTests(unittest.TestCase):
    """换域名机会的**判定**必须逐条钉住（否则「加了个变量但永远不生效」没人发现）。"""

    def _call(self, *, failed=True, message="", failover_used=False, stop_requested=False):
        return task_runners._should_try_domain_failover(
            failed=failed, message=message, failover_used=failover_used,
            stop_requested=stop_requested,
        )

    def test_domain_window_failure_gets_one_extra_try(self):
        self.assertTrue(self._call(message="x：x.ai 拒绝邮箱注册（account:email-signup-unavailable）…"))

    def test_other_failures_do_not(self):
        for message in ("打开注册页失败（打开超时）", "页面报错未发出验证码（something went wrong）", ""):
            with self.subTest(message=message):
                self.assertFalse(self._call(message=message))

    def test_success_does_not(self):
        self.assertFalse(self._call(failed=False, message="account:email-signup-unavailable"))

    def test_only_once_per_account(self):
        self.assertFalse(self._call(message="account:email-signup-unavailable", failover_used=True))

    def test_respects_stop_request(self):
        self.assertFalse(self._call(message="account:email-signup-unavailable", stop_requested=True))
