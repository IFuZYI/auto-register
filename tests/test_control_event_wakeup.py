"""停止/跳过要能**立刻**打断等待，而不是等下一个 0.25 秒分片。

现状与问题
----------
`_sleep_with_checkpoint` 用「碎步睡 + 每 0.25 秒 checkpoint」实现中断，
实测（本机 2 核）：

- 单次 `checkpoint()` 0.92 µs，64 线程等 3 秒只占 1.14% CPU —— **开销不是问题**；
- 停止延迟约 0.5 秒 —— 也不算差。

真正的代价是**它挡住了把等待放长**：一旦想把轮询/等待间隔调大以降低上游
压力，停止延迟就跟着线性变差，于是只能一直用 0.25 这种保守值。

`threading.Event` 让两者解耦：等待方睡足整段（长间隔不再影响响应性），
`request_stop()` 直接唤醒它。停止延迟与「睡多久」无关。
"""
from __future__ import annotations

import threading
import time
import unittest

from core.task_runtime import (
    RegisterTaskControl,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)


class StopWakesSleeperTests(unittest.TestCase):
    def test_request_stop_wakes_a_long_sleep_immediately(self):
        """睡 30 秒时点停止，必须在毫秒级返回。"""
        control = RegisterTaskControl()
        attempt_id = control.start_attempt()
        started = threading.Event()
        finished = threading.Event()
        elapsed: list[float] = []

        def waiter():
            started.set()
            begin = time.monotonic()
            try:
                control.sleep(30.0, attempt_id=attempt_id)
            except StopTaskRequested:
                elapsed.append(time.monotonic() - begin)
            finally:
                finished.set()

        thread = threading.Thread(target=waiter)
        thread.start()
        started.wait(timeout=5)
        time.sleep(0.1)
        control.request_stop()
        self.assertTrue(finished.wait(timeout=5), "停止没有唤醒等待方")
        self.assertLess(elapsed[0], 0.2, f"停止延迟 {elapsed[0]:.2f}s，仍然靠碎步轮询")

    def test_sleep_returns_quietly_when_not_stopped(self):
        """没人叫停时正常睡完、不抛异常。"""
        control = RegisterTaskControl()
        attempt_id = control.start_attempt()
        begin = time.monotonic()
        control.sleep(0.15, attempt_id=attempt_id)
        self.assertGreaterEqual(time.monotonic() - begin, 0.14)

    def test_sleep_after_stop_raises_right_away(self):
        control = RegisterTaskControl()
        control.request_stop()
        begin = time.monotonic()
        with self.assertRaises(StopTaskRequested):
            control.sleep(30.0)
        self.assertLess(time.monotonic() - begin, 0.1)

    def test_skip_wakes_the_sleeper(self):
        """跳过当前账号也要能从长睡里立刻醒来。"""
        control = RegisterTaskControl()
        attempt_id = control.start_attempt()
        finished = threading.Event()
        caught: list[str] = []

        def waiter():
            try:
                control.sleep(30.0, attempt_id=attempt_id)
            except SkipCurrentAttemptRequested:
                caught.append("skip")
            finally:
                finished.set()

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.1)
        control.request_skip_current()
        self.assertTrue(finished.wait(timeout=5))
        self.assertEqual(caught, ["skip"])


class SleepDoesNotBurnCpuTests(unittest.TestCase):
    def test_long_sleep_does_not_poll_in_tight_loop(self):
        """长睡期间不该有忙等 —— 唤醒次数应是个位数。"""
        control = RegisterTaskControl()
        attempt_id = control.start_attempt()
        checks = 0
        original = control.checkpoint

        def counting_checkpoint(**kwargs):
            nonlocal checks
            checks += 1
            return original(**kwargs)

        control.checkpoint = counting_checkpoint
        process_before = time.process_time()
        control.sleep(0.6, attempt_id=attempt_id)
        cpu = time.process_time() - process_before

        self.assertLessEqual(checks, 4, f"0.6 秒内 checkpoint 了 {checks} 次，仍在忙等")
        self.assertLess(cpu, 0.05, f"睡了 0.6 秒用了 {cpu:.3f}s CPU")


class MailboxSleepTests(unittest.TestCase):
    """渠道里的 `_sleep_with_checkpoint` 必须走同一条唤醒路径。"""

    def test_mailbox_sleep_is_interrupted_promptly(self):
        from core.base_mailbox import BaseMailbox, MailboxAccount

        class _Box(BaseMailbox):
            def get_email(self):
                return MailboxAccount(email="x@y.z")

            def get_current_ids(self, account):
                return set()

            def wait_for_code(self, account, **kwargs):
                raise NotImplementedError

        box = _Box()
        box._task_control = RegisterTaskControl()
        box._task_control.request_stop()
        begin = time.monotonic()
        with self.assertRaises(StopTaskRequested):
            box._sleep_with_checkpoint(30.0)
        self.assertLess(time.monotonic() - begin, 0.1)


if __name__ == "__main__":
    unittest.main()