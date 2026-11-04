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

    def _pick(self, blocked):
        return task_runners._pick_mail_provider_failover(
            blocked_domains=set(blocked), candidates=list(self.CANDIDATES)
        )

    def test_icloud_window_exhausted_switches_to_outlook(self):
        """2026-11-03 线上 bug 的回归：默认 `mail_provider` 是空串，判定若依赖
        「当前渠道名」就永远匹配不上 —— 日志打了「换邮箱域名」，下一轮还是
        `@icloud.com`，白花 90 秒。所以判定必须只看**失败的域名**。"""
        self.assertEqual(self._pick({"icloud.com"}), "outlook")

    def test_switches_back_when_outlook_domain_is_blocked(self):
        self.assertEqual(self._pick({"outlook.kr"}), "icloud_remote")

    def test_no_change_when_nothing_is_blocked(self):
        self.assertEqual(self._pick(set()), "")

    def test_gives_up_when_every_candidate_is_blocked(self):
        self.assertEqual(self._pick({"icloud.com", "outlook.com"}), "")

    def test_unknown_domain_does_not_block_anything(self):
        self.assertEqual(self._pick({"example.invalid"}), "outlook")


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
        self.assertIn("域名窗口配额用尽 → 换到", src)
        self.assertIn("failover_used", src)
        self.assertIn("_should_try_domain_failover(", src)
        # 额外那次必须**强制指定渠道**，否则还是老域名
        self.assertIn("forced_provider=failover_provider", src)

    def test_round_mailbox_uses_the_failover_provider(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("_build_mailbox(_proxy, provider=round_mail_provider)", src)
        self.assertIn("round_mail_provider", src)

    def test_failover_requires_an_available_alternate_pool(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("_mail_provider_available(failover)", src)
        self.assertIn("_next_mail_provider_failover(blocked_domains)", src)


class ShouldTryDomainFailoverTests(unittest.TestCase):
    """换域名机会的**判定**必须逐条钉住（否则「加了个变量但永远不生效」没人发现）。"""

    def _call(self, *, failed=True, message="", failover_used=False, stop_requested=False,
              failover_provider="outlook"):
        return task_runners._should_try_domain_failover(
            failed=failed, message=message, failover_used=failover_used,
            stop_requested=stop_requested, failover_provider=failover_provider,
        )

    def test_domain_window_failure_gets_one_extra_try(self):
        self.assertTrue(self._call(
            message="x：x.ai 拒绝邮箱注册（account:email-signup-unavailable）…",
            failover_provider="outlook",
        ))

    def test_no_extra_try_without_an_available_alternate(self):
        """没有可换的渠道就别白花一次 —— 线上就是「说了换、其实没换」。"""
        self.assertFalse(self._call(
            message="x.ai 拒绝邮箱注册（account:email-signup-unavailable）",
            failover_provider="",
        ))

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


class NextMailProviderFailoverTests(unittest.TestCase):
    """换渠道前必须确认那边**真有号**（否则换到空池，等于换个地方失败）。"""

    def test_prefers_outlook_when_it_has_accounts(self):
        with patch.object(task_runners, "_mail_provider_available", side_effect=lambda p: p == "outlook"):
            self.assertEqual(task_runners._next_mail_provider_failover({"icloud.com"}), "outlook")

    def test_falls_back_to_icloud_when_outlook_is_empty(self):
        with patch.object(task_runners, "_mail_provider_available", side_effect=lambda p: p == "icloud_remote"):
            self.assertEqual(task_runners._next_mail_provider_failover({"outlook.com"}), "icloud_remote")

    def test_empty_when_nothing_has_accounts(self):
        with patch.object(task_runners, "_mail_provider_available", return_value=False):
            self.assertEqual(task_runners._next_mail_provider_failover({"icloud.com"}), "")


class ForcedProviderMustWinTests(unittest.TestCase):
    """「换到 outlook」必须真的让本轮邮箱池变成 outlook（否则只是换了句日志）。"""

    def test_round_honours_the_forced_provider(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn(
            'failover = str(forced_provider or "") or _pick_mail_provider_failover(', src
        )
        # 邮箱池要用「本轮渠道」，不是只改 extra
        self.assertIn("_build_mailbox(_proxy, provider=round_mail_provider)", src)


class PrefetchMustRespectRemovalTests(unittest.TestCase):
    """预取快照不能绕过「摘除」。

    2026-11-03 线上 bug：日志说「代理异常，已摘除 X」，下一轮 `_get_proxy()`
    又从预取快照里随机挑回同一个 X —— 摘除等于没摘，白烧一个别名加一轮时间。
    """

    def test_get_proxy_filters_out_inactive_prefetched_entries(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("_proxy_pool.is_available(candidate)", src)
        self.assertIn("_prefetched_proxies.remove(candidate)", src)

    def test_get_proxy_falls_back_to_pool_when_snapshot_is_exhausted(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("while True:", src)
        self.assertIn("_proxy_pool.get_next(tag=TAG_REGISTER)", src)


class ProxyCredentialMustNotReachLogsTests(unittest.TestCase):
    """代理串带账号密码，`/api/tasks/{id}/logs` 会原样回吐（安全审查同类问题）。"""

    def test_removal_log_is_redacted(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("已摘除 {_redact_proxy(_proxy)}", src)
        self.assertNotIn("已摘除 {_proxy}", src)


class FailedAttemptStillKnowsTheDomainTests(unittest.TestCase):
    """失败路径也必须知道「刚才是哪个域名被拒」。

    2026-11-03 线上 bug：`current_email` 只在 `register()` 成功返回后才赋值，
    发码被拒时它还是空串 → `blocked_domains` 永远空 → 换域名 failover 从不触发，
    icloud.com 连撞两轮。这里从错误文本里兜底提取。
    """

    def test_extracts_first_email_from_error_text(self):
        text = "83.plugins_rabbles@icloud.com：x.ai 拒绝邮箱注册（account:email-signup-unavailable）"
        self.assertEqual(task_runners._first_email_in(text), "83.plugins_rabbles@icloud.com")

    def test_returns_empty_when_no_email(self):
        self.assertEqual(task_runners._first_email_in("打开注册页失败（打开超时）"), "")
        self.assertEqual(task_runners._first_email_in(None), "")

    def test_domain_falls_back_to_error_text_email(self):
        """这是线上那两轮的原文：只有文本里有邮箱，也必须认出 icloud.com。"""
        text = "again.ibex.6q@icloud.com：x.ai 拒绝邮箱注册（account:email-signup-unavailable）"
        self.assertEqual(
            task_runners._mail_domain_of(task_runners._first_email_in(text)),
            "icloud.com",
        )

    def test_flow_uses_the_fallback(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertIn("_first_email_in(text)", src)


class RemoteMailProviderCounterTests(unittest.TestCase):
    """`icloud_remote` 渠道的「有没有号」必须真的读远程计数。

    2026-11-04 独立审查：`_mail_provider_available` 引的
    `services.icloud.remote.claimable_alias_total` **根本不存在**，ImportError
    被宽捕获吞掉 → 该渠道永远「没号」→ 域名窗口用尽时的邮箱渠道切换从未生效。

    这条测试用**两个方向**钉住接线：计数为 0 → False，计数 >0 → True。
    只断言其中一边的话，把函数换成 `return False` 常量仍然会绿。
    """

    def _call(self, count):
        from unittest.mock import patch

        from services.task_runners import _mail_provider_available

        with patch("services.icloud.remote.claimable_alias_total", lambda: count):
            return _mail_provider_available("icloud_remote")

    def test_positive_count_means_available(self):
        self.assertTrue(self._call(5))

    def test_zero_count_means_unavailable(self):
        self.assertFalse(self._call(0))
