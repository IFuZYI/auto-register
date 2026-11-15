"""收件轮询要能扛住「上游暂时不可达」。

实测（2026-11-13）：`icloud-hme` 读超时，`poll_once()` 抛的
`ICloudError("upstream_unavailable")` 直接穿透 `_run_polling_wait` 的循环 ——
既不会被转成 `TimeoutError`、也不会触发补发重试，**一次 30 秒超时就让整轮
注册失败**。两批任务里约 80% 的失败是这一条。

但「扛住」必须有边界：服务真挂时不能让 200 个 worker 各自陪跑到时限。
所以这里同时钉住两件事 —— 可恢复错误继续等，但连续失败够多就放弃。
"""
from __future__ import annotations

import unittest

from core.base_mailbox import BaseMailbox, MailboxAccount
from platforms.icloud.errors import ICloudError, is_recoverable


class _FlakyMailbox(BaseMailbox):
    """按剧本返回：前 N 次抛错，然后给码。"""

    def __init__(self, script: list, error_budget: int = 4):
        self.script = list(script)
        self.poll_count = 0
        self.logs: list[str] = []
        self.error_budget = error_budget
        #: 与生产渠道（modules/mail/icloud_remote.py）同一套接线：熔断判定
        #: 走 `is_upstream_busy`。替身不接这根线就测不到那条分支。
        self.fatal_busy = None

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="demo@example.com")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def _log(self, message: str) -> None:
        self.logs.append(str(message))

    def wait_for_code(self, account, keyword="", timeout=120, before_ids=None,
                      code_pattern=None, **kwargs) -> str:
        def poll_once():
            self.poll_count += 1
            step = self.script.pop(0) if self.script else None
            if isinstance(step, BaseException):
                raise step
            return step

        from services.icloud.upstream_gate import is_upstream_busy

        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=0.01,
            poll_once=poll_once,
            is_transient=self.is_transient_error,
            max_consecutive_errors=self.error_budget,
            is_fatal_busy=self.fatal_busy or is_upstream_busy,
        )

    @staticmethod
    def is_transient_error(exc: BaseException) -> bool:
        return (
            isinstance(exc, ICloudError)
            and exc.code in ("upstream_unavailable", "upstream_error", "session_expired")
        )


def _transient(message="iCloud 远程服务不可达：read timeout"):
    return ICloudError("upstream_unavailable", message)


class TransientErrorTests(unittest.TestCase):
    def test_recovers_when_upstream_comes_back(self):
        """前两次超时、第三次拿到码 —— 这一轮不该失败。"""
        mailbox = _FlakyMailbox([_transient(), _transient(), "406553"])
        code = mailbox.wait_for_code(mailbox.get_email(), timeout=5)
        self.assertEqual(code, "406553")
        self.assertEqual(mailbox.poll_count, 3)

    def test_transient_errors_are_logged(self):
        """继续轮询时要说出来，而不是静默吞掉。"""
        mailbox = _FlakyMailbox([_transient(), "406553"])
        mailbox.wait_for_code(mailbox.get_email(), timeout=5)
        self.assertTrue(
            any("暂时不可用" in line for line in mailbox.logs),
            f"没有记录上游暂时不可用: {mailbox.logs}",
        )

    def test_success_resets_the_error_streak(self):
        """错误-成功-错误 不算连续失败，不该因为累计次数被放弃。"""
        mailbox = _FlakyMailbox(
            [_transient(), "406553"]
        )
        self.assertEqual(mailbox.wait_for_code(mailbox.get_email(), timeout=5), "406553")


class NonTransientErrorTests(unittest.TestCase):
    def test_config_error_fails_immediately(self):
        """配置错误重试多少次都一样，必须立刻抛，不能空转到时限。"""
        mailbox = _FlakyMailbox([ICloudError("invalid_config", "秘钥错误")])
        with self.assertRaises(ICloudError) as ctx:
            mailbox.wait_for_code(mailbox.get_email(), timeout=5)
        self.assertEqual(ctx.exception.code, "invalid_config")
        self.assertEqual(mailbox.poll_count, 1, "配置错误被重试了")

    def test_permission_error_fails_immediately(self):
        mailbox = _FlakyMailbox([ICloudError("permission_denied", "无权限")])
        with self.assertRaises(ICloudError):
            mailbox.wait_for_code(mailbox.get_email(), timeout=5)
        self.assertEqual(mailbox.poll_count, 1)

    def test_plain_exception_still_propagates(self):
        """不是 ICloudError 的异常维持原行为：直接穿透。"""
        mailbox = _FlakyMailbox([RuntimeError("boom")])
        with self.assertRaises(RuntimeError):
            mailbox.wait_for_code(mailbox.get_email(), timeout=5)
        self.assertEqual(mailbox.poll_count, 1)

    def test_no_is_transient_hook_keeps_old_behaviour(self):
        """没传判定函数时行为完全不变 —— 别的渠道零风险。"""

        class _Plain(BaseMailbox):
            def __init__(self):
                self.poll_count = 0

            def get_email(self):
                return MailboxAccount(email="x@y.z")

            def get_current_ids(self, account):
                return set()

            def wait_for_code(self, account, **kwargs):
                def poll_once():
                    self.poll_count += 1
                    raise ICloudError("upstream_unavailable", "挂了")

                return self._run_polling_wait(
                    timeout=2, poll_interval=0.01, poll_once=poll_once
                )

        box = _Plain()
        with self.assertRaises(ICloudError):
            box.wait_for_code(box.get_email())
        self.assertEqual(box.poll_count, 1)


class BreakerDuringPollingTests(unittest.TestCase):
    """熔断打开时必须**立刻**放弃，且要让日志说清「是熔断，不是抖动」。

    这条分支曾经是死代码：`is_recoverable(UpstreamBusy)` 为 False（它既不是
    `upstream_unavailable` 也不是 `upstream_error`），于是异常在
    「非暂时错误直接穿透」那一行就抛出去了 —— 结果碰巧是对的，但
    「本轮立刻放弃（不空等）」这条**永远执行不到**。

    后果有两个，一真一潜：
    - 真：日志里只有一句 `熔断中，约 Ns`，看不出这一轮是「主动立刻放弃」还是
      「碰巧撞上非暂时错误」；排查时无法区分「熔断生效了」与「熔断没生效」。
    - 潜：一旦有人把熔断加进可恢复集合（"熔断也是一种暂时故障"听起来很合理），
      5 个 worker 就会各自空等到时限 —— 正是熔断想避免的浪费。

    这里钉的是**可达性**（用生产真实的两个分类器），不是措辞：
    把判定顺序换回去，这条日志就不会出现。
    """

    def test_breaker_branch_is_reachable_with_production_classifiers(self):
        """用生产真实的 `is_recoverable` + `is_upstream_busy` 跑一遍。"""
        from services.icloud.upstream_gate import UpstreamBusy

        mailbox = _FlakyMailbox([UpstreamBusy("svc", 30.0)])
        mailbox.is_transient_error = is_recoverable  # 生产用的那个
        with self.assertRaises(UpstreamBusy):
            mailbox.wait_for_code(mailbox.get_email(), timeout=3)

        self.assertEqual(mailbox.poll_count, 1, "熔断后还在继续轮询")
        self.assertTrue(
            any("熔断" in line and "立刻放弃" in line for line in mailbox.logs),
            f"熔断这条分支没有执行（死代码）：{mailbox.logs}",
        )

    def test_breaker_still_aborts_when_someone_makes_it_recoverable(self):
        """潜在回归：把熔断也算「可恢复」时，仍必须立刻放弃而不是空等到时限。

        旧顺序下 `is_transient` 先判 → 熔断被当抖动 → 一直轮到 3s 超时，
        抛的是 `TimeoutError` 而不是 `UpstreamBusy`。
        """
        from services.icloud.upstream_gate import UpstreamBusy

        mailbox = _FlakyMailbox([UpstreamBusy("svc", 30.0)])
        mailbox.is_transient_error = staticmethod(lambda exc: True)  # type: ignore[assignment]
        with self.assertRaises(UpstreamBusy):
            mailbox.wait_for_code(mailbox.get_email(), timeout=3)
        self.assertEqual(mailbox.poll_count, 1, "把熔断当抖动，空等到时限")

    def test_breaker_does_not_consume_the_error_budget(self):
        """熔断是确定性结论，不该先花掉一次「连续错误」额度再放弃。"""
        from services.icloud.upstream_gate import UpstreamBusy

        mailbox = _FlakyMailbox([UpstreamBusy("svc", 30.0)], error_budget=1)
        with self.assertRaises(UpstreamBusy):
            mailbox.wait_for_code(mailbox.get_email(), timeout=3)
        self.assertEqual(mailbox.poll_count, 1)


class ErrorBudgetTests(unittest.TestCase):
    def test_gives_up_after_consecutive_error_budget(self):
        """服务一直挂着时要放弃，而不是陪跑到时限。"""
        mailbox = _FlakyMailbox([_transient() for _ in range(50)])
        with self.assertRaises(ICloudError) as ctx:
            mailbox.wait_for_code(mailbox.get_email(), timeout=60)
        self.assertEqual(ctx.exception.code, "upstream_unavailable")
        self.assertLess(mailbox.poll_count, 50, "没有放弃，一直在空转")

    def test_gives_up_raises_the_last_error_not_a_timeout(self):
        """放弃时抛原错误 —— 上层要据此判断「服务问题」而不是「没收到码」。"""
        mailbox = _FlakyMailbox([_transient("第 N 次") for _ in range(50)])
        with self.assertRaises(ICloudError) as ctx:
            mailbox.wait_for_code(mailbox.get_email(), timeout=60)
        self.assertIn("第 N 次", str(ctx.exception))


class BackoffScheduleTests(unittest.TestCase):
    """分段退避：验证码多在前 20 秒到，后面的空转可以放慢。"""

    def test_schedule_picks_interval_by_elapsed(self):
        schedule = ((0, 5.0), (20, 8.0), (60, 15.0))
        box = _FlakyMailbox([])
        self.assertEqual(box._poll_interval_for(0, schedule), 5.0)
        self.assertEqual(box._poll_interval_for(19.9, schedule), 5.0)
        self.assertEqual(box._poll_interval_for(20, schedule), 8.0)
        self.assertEqual(box._poll_interval_for(59.9, schedule), 8.0)
        self.assertEqual(box._poll_interval_for(60, schedule), 15.0)
        self.assertEqual(box._poll_interval_for(300, schedule), 15.0)

    def test_no_schedule_keeps_fixed_interval(self):
        box = _FlakyMailbox([])
        self.assertEqual(box._poll_interval_for(120, None), 5.0)

    def test_backoff_reduces_request_count_in_a_window(self):
        """同样的 180 秒窗口，退避后请求数应显著少于固定 5 秒。"""

        def simulate(schedule):
            elapsed = 0.0
            polls = 0
            while elapsed < 180:
                polls += 1
                elapsed += 1.0  # 假设每次请求 1 秒
                if elapsed >= 180:
                    break
                interval = box._poll_interval_for(elapsed, schedule)
                elapsed += min(interval, 180 - elapsed)
            return polls

        box = _FlakyMailbox([])
        fixed = simulate(None)
        backed_off = simulate(((0, 5.0), (20, 8.0), (60, 15.0)))
        self.assertLess(backed_off, fixed * 0.75, f"{backed_off} vs {fixed}")


if __name__ == "__main__":
    unittest.main()