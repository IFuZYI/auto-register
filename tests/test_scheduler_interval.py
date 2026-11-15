"""周期任务的调度语义：间隔从「跑完」算，不是从「开始」算。

2026-11-15（全量验证 T8-4，Important / 已复现）：
`core/scheduler.py:174-177` 在 runner 返回后写 `job.last_run_at = now`，
而 `now` 是**本轮循环开始那一刻**的时间戳。于是任务耗时超过间隔时
（MIN_INTERVAL=300s、按账号 3s 的全量探活约 600s），下一 tick 立刻又到期 →
back-to-back 连续跑，正是那个 5 分钟下限想避免的上游限流/风控场景。

界面上写的是「距上次跑完」（MaintenancePanel），docs 也这么讲 —— 实现与承诺不一致。

这里钉的是行为：一个耗时超过间隔的任务，跑完后必须**等满间隔**才会再次到期。
用 monkeypatch 固定 `core.scheduler.time.time`，让假时钟对实现与断言同源 ——
否则（假时钟只影响测试、实现用真实时间）断言会恒真，变成假绿。
"""

from __future__ import annotations

import unittest
from unittest import mock


class SchedulerIntervalTests(unittest.TestCase):
    def setUp(self):
        import core.scheduler as sched

        self.sched = sched
        # **不要** `_jobs.clear()`：那是进程内全局注册表，local_maintenance /
        # chatgpt_maintenance 的测试依赖它已注册的 13 个任务。清空会污染后续
        # 测试（实测：aws_auto_probe 找不到）。只清理本类注册的临时任务名。
        self._mine: list[str] = []
        self.addCleanup(self._drop_mine)
        self.scheduler = sched.Scheduler()
        # 假时钟：实现与断言都读它
        self.clock = {"now": 1000.0}
        patcher = mock.patch.object(
            sched.time, "time", lambda: self.clock["now"]
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _drop_mine(self):
        with self.sched._jobs_lock:
            for name in self._mine:
                self.sched._jobs.pop(name, None)
            self.sched._jobs.pop("slow-job", None)

    def _job(self, name: str, interval: float, runner):
        self.sched.register_job(name, interval_seconds=lambda: interval, runner=runner)
        self._mine.append(name)
        return self.sched._jobs[name]

    def test_interval_is_measured_from_the_end_of_the_run(self):
        """跑完 600s、间隔 300s → 跑完后必须再等满 300s 才到期。"""
        def runner():
            # 任务本身耗掉 600 秒（超过 300s 间隔）
            self.clock["now"] += 600.0
            return {}

        job = self._job("slow-job", 300.0, runner)

        # 第一次 tick：注册后首见，按约定把 last_run_at 顶到现在并跳过
        self.scheduler._run_due_jobs(self.clock["now"])
        # 推进到确实到期，再跑一次
        self.clock["now"] += 301.0
        self.scheduler._run_due_jobs(self.clock["now"])
        ran_at = job.last_run_at
        self.assertGreater(ran_at, 0.0, "跑完后没有记录结束时刻")
        # runner 让时钟前进了 600s；记录的结束时刻必须包含这段耗时
        self.assertGreaterEqual(
            ran_at, 1000.0 + 301.0 + 600.0,
            "记录的结束时刻早于 runner 实际耗时 —— 间隔是从「开始」算的",
        )

        self.assertEqual(job.due(ran_at + 1.0), 0.0,
                         "任务跑完后 1 秒就再次到期 —— back-to-back 连轴转")
        self.assertEqual(job.due(ran_at + 299.0), 0.0, "还没等满间隔就到期")
        self.assertGreater(job.due(ran_at + 301.0), 0.0, "等满间隔后仍未到期")

    def test_nobody_registered_is_a_legal_state(self):
        """没人注册任务时循环不该抛。"""
        self.scheduler._run_due_jobs(1000.0)
