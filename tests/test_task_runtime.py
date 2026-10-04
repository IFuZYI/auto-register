import unittest

from core.task_runtime import (
    RegisterTaskControl,
    RegisterTaskStore,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)


class RegisterTaskControlTests(unittest.TestCase):
    def test_skip_request_is_consumed_only_once(self):
        control = RegisterTaskControl()

        control.request_skip_current()

        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint()

        control.checkpoint()

    def test_stop_request_is_sticky(self):
        control = RegisterTaskControl()

        control.request_stop()

        with self.assertRaises(StopTaskRequested):
            control.checkpoint()
        with self.assertRaises(StopTaskRequested):
            control.checkpoint()

    def test_skip_current_targets_only_active_attempts_in_multithread_mode(self):
        control = RegisterTaskControl()
        attempt_a = control.start_attempt()
        attempt_b = control.start_attempt()

        control.request_skip_current()

        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint(attempt_id=attempt_a)
        with self.assertRaises(SkipCurrentAttemptRequested):
            control.checkpoint(attempt_id=attempt_b)

        control.finish_attempt(attempt_a)
        control.finish_attempt(attempt_b)

        attempt_c = control.start_attempt()
        control.checkpoint(attempt_id=attempt_c)
        control.finish_attempt(attempt_c)


class RegisterTaskStoreTests(unittest.TestCase):
    def test_snapshot_contains_control_and_skip_fields(self):
        store = RegisterTaskStore()
        task_id = "task-runtime-snapshot"

        store.create(
            task_id,
            platform="chatgpt",
            total=2,
            source="manual",
            meta={"scope": "unit"},
        )
        store.request_skip_current(task_id)
        store.finish(
            task_id,
            status="done",
            success=1,
            skipped=1,
            errors=["error-a"],
        )

        snapshot = store.snapshot(task_id)

        self.assertEqual(snapshot["success"], 1)
        self.assertEqual(snapshot["skipped"], 1)
        self.assertEqual(snapshot["errors"], ["error-a"])
        self.assertEqual(
            snapshot["control"]["pending_skip_requests"],
            1,
        )


class SchedulerLifecycleTests(unittest.TestCase):
    """调度器生命周期：stop/start 不能跑出两个循环线程。

    回归的是这样一条真实缺陷：`stop()` 只置 `_running=False`，既不 join 也不
    唤醒正在 `time.sleep()` 的线程（默认 60s）。若在 sleep 窗口内再次
    `start()`，`_running` 被翻回 True，旧线程醒来后 `while self._running`
    又成立 —— 于是两个 `_loop` 并发，周期任务（如 CPA 维护）被重复执行。
    """

    @staticmethod
    def _live_loop_threads() -> int:
        import threading

        return sum(
            1
            for t in threading.enumerate()
            if t.is_alive()
            and t._target is not None
            and getattr(t._target, "__name__", "") == "_loop"
        )

    def test_restart_within_sleep_window_keeps_single_loop_thread(self):
        import time

        from core.scheduler import Scheduler

        scheduler = Scheduler()
        scheduler._loop_interval_seconds = 1  # 缩短，模拟 sleep 窗口
        try:
            scheduler.start()
            time.sleep(0.2)
            self.assertEqual(self._live_loop_threads(), 1)

            scheduler.stop()
            # join 过：线程应已退出，而不是还在 sleep
            self.assertEqual(self._live_loop_threads(), 0)

            scheduler.start()
            time.sleep(0.2)
            self.assertEqual(self._live_loop_threads(), 1)

            # 等过旧的 sleep 窗口，确认旧线程不会「复活」
            time.sleep(1.5)
            self.assertEqual(
                self._live_loop_threads(),
                1,
                "stop() 后立刻 start() 跑出了第二个循环线程",
            )
        finally:
            scheduler.stop()

    def test_stop_returns_promptly_even_with_long_interval(self):
        """stop() 不能等满整个轮询周期才返回（用 Event.wait 唤醒）。"""
        import time

        from core.scheduler import Scheduler

        scheduler = Scheduler()
        scheduler._loop_interval_seconds = 60
        scheduler.start()
        time.sleep(0.2)

        started = time.monotonic()
        scheduler.stop()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 5, f"stop() 耗时 {elapsed:.1f}s，未及时唤醒循环线程")

    def test_interval_function_that_raises_does_not_break_scheduler(self):
        """interval 函数抛异常时应跳过该任务，而不是让调度器崩溃。"""
        import time

        from core.scheduler import _Job

        def boom():
            raise RuntimeError("interval 炸了")

        job = _Job(name="boom", interval=boom, runner=lambda: None)
        self.assertEqual(job.due(time.time()), 0)

    def test_subsecond_interval_is_not_truncated_to_zero(self):
        """亚秒间隔不能被 int() 截断成 0（0 的语义是「本次不跑」）。

        回归的是这样一条坑：`due()` 曾用 `int(self.interval())`，
        于是 `interval=0.9` 变成 0 —— 任务永远不跑，且日志里毫无线索。
        """
        import time

        from core.scheduler import _Job

        job = _Job(name="sub", interval=lambda: 0.9, runner=lambda: None)
        job.last_run_at = time.time() - 10  # 早该到期
        self.assertGreater(job.due(time.time()), 0)

    def test_late_registered_job_does_not_fire_immediately(self):
        """运行期新注册的任务不能立刻触发（与 start() 的「不瞬间触发」一致）。

        回归：曾出现「start() 之后 register_job 的任务，第一个 tick 就跑」，
        因为它的 last_run_at 还是 0，与文档约定矛盾。
        """
        import time

        from core.scheduler import Scheduler, register_job

        scheduler = Scheduler()
        scheduler._loop_interval_seconds = 0.2
        fired = []
        try:
            scheduler.start()
            time.sleep(0.1)
            register_job(
                "late_job_test",
                interval_seconds=lambda: 3600,
                runner=lambda: fired.append(1),
            )
            time.sleep(0.8)  # 约 4 个 tick
            self.assertEqual(fired, [], "运行期新注册的任务被立刻触发了")
        finally:
            scheduler.stop()

    def test_late_registered_job_still_runs_when_due(self):
        """上述「不立刻触发」不能把任务彻底卡死：间隔走完后仍要正常执行。"""
        import time

        from core.scheduler import Scheduler, register_job

        scheduler = Scheduler()
        scheduler._loop_interval_seconds = 0.2
        fired = []
        try:
            scheduler.start()
            time.sleep(0.1)
            register_job(
                "late_due_test",
                interval_seconds=lambda: 1,
                runner=lambda: fired.append(1),
            )
            # 首个 tick 把 last_run_at 初始化到「现在」，之后满 1s 才到期；
            # 0.2s 的 tick 下 1.5s 足够走完一个间隔并执行一次
            time.sleep(1.6)
            self.assertGreaterEqual(len(fired), 1, "到期后仍未执行")
        finally:
            scheduler.stop()


class MailboxRegistryLookupTests(unittest.TestCase):
    """注册表查询函数不能因调用顺序给出不同答案。

    回归：`is_registered` / `provider_display_name` 曾绕过 `_load_optional_providers()`，
    于是在全新进程里首次调用返回 False / 原始名字，等某个别处调过
    `available_providers()` 之后又变 True / 展示名 —— 同一函数两种答案。
    """

    def test_is_registered_triggers_lazy_load(self):
        from core.mailboxes.registry import is_registered

        # icloud_local 来自 modules.mail，只能靠懒加载拿到
        self.assertTrue(is_registered("icloud_local"))

    def test_provider_display_name_triggers_lazy_load(self):
        from core.mailboxes.registry import provider_display_name

        self.assertIn("iCloud", provider_display_name("icloud_local"))

    def test_unknown_provider_display_name_falls_back_to_raw(self):
        from core.mailboxes.registry import provider_display_name

        self.assertEqual(provider_display_name("no-such-xyz"), "no-such-xyz")


class PlatformMailboxRequirementTests(unittest.TestCase):
    """`uses_mailbox=False` 的平台不能被塞一个外部邮箱池。

    回归：iCloud 曾自带隐私邮箱（远程 icloud-hme 那条链路），但运行时无条件
    `create_mailbox(...)`，默认渠道是 luckmail —— 未配置时任务直接失败，而那个
    邮箱根本不会被用到。

    iCloud 平台已随远程链路删除，现存两个平台都要邮箱池；下面守住「该要的还在
    要」，同时不再断言某个平台不需要。
    """

    def test_platforms_that_need_a_mailbox_keep_the_default(self):
        from core.registry import get, load_all

        load_all()
        for name in ("chatgpt", "grok"):
            self.assertTrue(getattr(get(name), "uses_mailbox", True), name)

    def test_platform_listing_exposes_the_flag(self):
        """前端要靠这个字段决定注册表单是否显示邮箱池选项。"""
        from core.registry import list_platforms, load_all

        load_all()
        by_name = {item["name"]: item for item in list_platforms()}
        self.assertTrue(by_name["chatgpt"]["uses_mailbox"])
        self.assertTrue(by_name["grok"]["uses_mailbox"])


if __name__ == "__main__":
    unittest.main()
