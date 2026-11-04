"""`services/task_runners.py` 失败切换路径上的 4 个细节缺陷。

独立审查（2026-11-04）实测出来、与本文件一一对应的缺陷：

1. `_first_email_in` 会把**代理凭据里的域名**当成邮箱域名
   （`socks5h://user:s3cret@proxy.example.com:8080` → `proxy.example.com`），
   于是 `blocked_domains` 里多出一个不属于任何邮箱渠道的幽灵域名。
2. 失败切轮次（换邮箱域名）时日志把同一个轮号打了两次：调用是
   `_do_one_round(i, total_rounds, total_rounds, ...)`，`round_no == rounds ==
   total_rounds`，于是「第 2/2 轮」在日志里出现两次，看起来像原地打转。
3. `_build_mailbox` 建池时传的是**共享的** `_base_extra`，而本轮渠道覆盖写在
   `merged_extra` 里 —— 今天不出 bug 只是因为 `build_mailbox` 会拷贝，
   **靠巧合正确**。
4. `_pick_usable_exit` 的放弃文案：前一批修改已经让它按**实际**试过的个数说话
   （`len(tried) >= _GROK_EXIT_PROBE_TRIES` 才说「连试 5 个」），本文件用一条
   回归测试把它钉住，防止再退化。

本文件用**真正执行**被测函数的方式验证 1/2 的纯逻辑部分；对嵌在
`_run_register` 闭包里、无法单独驱动的那两行接线（2 的调用点、3 的 `extra`），
按仓库既有约定用 `inspect.getsource` 做**精确**子串断言（`test_grok_mail_domain_failover.py`
已就此写过 TDD skill 例外条款的理由）。
"""
from __future__ import annotations

import inspect
import re
import unittest
from unittest.mock import patch

from services import task_runners


class FirstEmailInMustIgnoreProxyCredentialsTests(unittest.TestCase):
    """缺陷 1：只有文案里**真的提到邮箱地址**时才回退到正则。

    代理串里 `user:pass@host` 的 `pass@host` 会被邮箱正则吞下，造出幽灵域名。
    """

    def test_scheme_proxy_credentials_yield_no_domain(self):
        """审查员原话的复现：带 scheme 的代理串不能产出域名。"""
        text = "连接代理 socks5h://user:s3cret@proxy.example.com:8080 失败"
        self.assertEqual(
            task_runners._first_email_in(text), "",
            "代理凭据不是邮箱，不该喂进 blocked_domains",
        )

    def test_bare_proxy_credentials_yield_no_domain(self):
        """去掉 scheme 的裸凭据形状（`curl -x user:pass@host:port`）同样不能。"""
        text = "curl 通过 user:pass@proxy.example.com:8080 打开注册页失败"
        self.assertEqual(task_runners._first_email_in(text), "")

    def test_real_email_beats_a_preceding_proxy_credential(self):
        """同一段文案里既有代理凭据又有真邮箱时，必须挑真邮箱。"""
        text = (
            "代理 socks5h://u:p@proxy.example.com:8080 注册 x@icloud.com 被拒"
            "（account:email-signup-unavailable）"
        )
        self.assertEqual(
            task_runners._mail_domain_of(task_runners._first_email_in(text)),
            "icloud.com",
            "不能把代理域名 proxy.example.com 当成被拒域名",
        )

    def test_plain_email_is_still_extracted(self):
        """对照组：没有代理串时，线上那两轮的原文照旧要认出 icloud.com。"""
        text = "again.ibex.6q@icloud.com：x.ai 拒绝邮箱注册（account:email-signup-unavailable）"
        self.assertEqual(task_runners._first_email_in(text), "again.ibex.6q@icloud.com")

    def test_email_after_a_plain_url_is_still_extracted(self):
        """对照组：普通 https 链接被抹掉后，后面的真邮箱仍要认出来。"""
        text = "见 https://accounts.x.ai/sign-up user@outlook.com 发码失败"
        self.assertEqual(task_runners._first_email_in(text), "user@outlook.com")


class RoundLogSuffixTests(unittest.TestCase):
    """缺陷 2 的纯逻辑：轮次日志后缀。

    `_round_log_suffix` 是失败切换标签的落点 —— 换域名那一轮不在 1..total_rounds
    的编号体系里，所以给它一个专用标签，不再复用最后一轮的编号。
    """

    def test_single_round_has_no_suffix(self):
        self.assertEqual(task_runners._round_log_suffix(round_no=1, rounds=1), "")

    def test_retry_round_shows_numbers(self):
        self.assertEqual(
            task_runners._round_log_suffix(round_no=2, rounds=2), "（第 2/2 轮）"
        )

    def test_label_overrides_the_number(self):
        label = task_runners._FAILOVER_ROUND_LABEL
        self.assertTrue(label, "专用标签不能是空串，否则等于没改")
        got = task_runners._round_log_suffix(round_no=2, rounds=2, label=label)
        self.assertIn(label, got)
        self.assertNotIn("2/2", got, "不能再出现与最后一轮重复的编号")

    def test_failover_label_differs_from_the_last_retry_round(self):
        """把缺陷本身钉死：最后一轮的后缀 ≠ 换域名轮的后缀。"""
        last_retry = task_runners._round_log_suffix(round_no=2, rounds=2)
        failover = task_runners._round_log_suffix(
            round_no=2, rounds=2, label=task_runners._FAILOVER_ROUND_LABEL
        )
        self.assertNotEqual(last_retry, failover)


class FailoverRoundLabellingTests(unittest.TestCase):
    """缺陷 2 的接线：换域名那一轮**不能**再用 `total_rounds, total_rounds`。"""

    def test_failover_round_is_not_labelled_with_the_last_round_number(self):
        src = inspect.getsource(task_runners._run_register)
        self.assertNotIn(
            "i, total_rounds, total_rounds, blocked_domains", src,
            "这会让日志把最后一轮的编号打两遍（第 N/N 轮）",
        )
        self.assertIn("label=_FAILOVER_ROUND_LABEL", src)


class MailboxUsesTheRoundExtraTests(unittest.TestCase):
    """缺陷 3：邮箱池必须吃**本轮**的 `merged_extra`，不是共享的 `_base_extra`。

    `_build_mailbox` 嵌在 `_run_register`/`_do_one_round` 里，没有可调用的面
    （驱动 `_run_register` 要搭整套假件），所以这里按仓库既有约定用源码断言，
    并把它守的**那一行**写清楚。
    """

    def _src(self):
        return inspect.getsource(task_runners._run_register)

    def test_mailbox_builder_no_longer_falls_back_to_the_shared_base_extra(self):
        """`extra=_base_extra` 只该剩 `_pick_usable_exit` 那一处。

        旧实现建池那一行也是 `extra=_base_extra`，于是全函数出现两次；
        修完后建池改成吃本轮的 `merged_extra`，只剩出口预检那一处
        （出口预检没有「本轮覆盖」这回事，理应用共享的 `_base_extra`）。
        """
        src = self._src()
        self.assertEqual(
            src.count("extra=_base_extra"), 1,
            "邮箱池那一行又吃回共享的 _base_extra 了（靠 create_mailbox 拷贝才没出 bug）",
        )

    def test_mailbox_builder_passes_the_round_extra_to_create_mailbox(self):
        """建池那一次 `create_mailbox(..., extra=…)` 必须传本轮 `merged_extra`。"""
        src = self._src()
        self.assertIsNotNone(
            re.search(r"extra=merged_extra,\s*\n\s*proxy=proxy,", src),
            "create_mailbox 的 extra 必须是本轮的 merged_extra",
        )

    def test_the_call_site_shape_is_preserved(self):
        """调用点保持字面形状（另一个测试文件把这一行钉住了）。"""
        self.assertIn(
            "_build_mailbox(_proxy, provider=round_mail_provider)", self._src()
        )


class GiveUpMessageCountsRealExitsTests(unittest.TestCase):
    """缺陷 4 的回归护栏：放弃文案要说**实际**试过的个数。

    前一批修改已修好这条（`len(tried) >= _GROK_EXIT_PROBE_TRIES` 才说「连试 5 个」）；
    这里用执行测试钉住，防止退化回「池子空了还嘴硬说连试 5 个」。
    """

    def _run(self, *, next_urls, reach, extra=None):
        from platforms.grok import exit_probe

        logs: list[str] = []
        urls = list(next_urls)

        with patch.object(exit_probe, "probe_reachable", lambda _p: reach):
            out = task_runners._pick_usable_exit(
                "p0",
                task_id="t",
                extra=dict(extra or {}),
                log=lambda _tid, msg: logs.append(msg),
                get_next=lambda: urls.pop(0) if urls else "p0",
                report_bad=lambda _p: None,
                normalize=lambda v: v,
            )
        return out, logs

    def test_pool_exhausted_early_reports_the_real_count(self):
        """池子只给得出 1 个出口就说「试了 1 个」，不许说「连试 5 个」。"""
        out, logs = self._run(
            next_urls=[],  # 没有新出口 → 只探了 1 个
            reach={"ok": False, "detail": "连不上"},
        )
        self.assertEqual(out, "")
        joined = " / ".join(logs)
        self.assertIn("试了 1 个", joined)
        self.assertNotIn("连试 5 个", joined)

    def test_budget_exhausted_reports_five(self):
        """对照组：真的用满 5 个额度时才说「连试 5 个」。"""
        out, logs = self._run(
            next_urls=["p1", "p2", "p3", "p4", "p5"],
            reach={"ok": False, "detail": "连不上"},
        )
        self.assertEqual(out, "")
        self.assertTrue(any("连试 5 个" in m for m in logs), logs)


if __name__ == "__main__":
    unittest.main()
