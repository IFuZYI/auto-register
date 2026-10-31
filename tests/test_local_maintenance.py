"""本地自动维护：时间解析、抽检/刷新范围选择、每天定点触发。

这里钉的都是**纯函数**（不碰数据库与网络），跑得快、失败时能直接指出
是哪条规则算错了。真正打上游的那条链（`_run_actions`）刻意复用
`api.actions` 的手动路径，由既有的动作测试覆盖。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from services.local_maintenance import (
    DEFAULT_REFRESH_RANDOM_COUNT,
    Pick,
    clamp_count,
    clamp_days,
    clamp_percent,
    get_auto_probe_daily_at,
    get_auto_refresh_daily_at,
    get_log_cleanup_daily_at,
    last_probe_at,
    parse_hhmm,
    select_probe_targets,
    select_refresh_targets,
)


def _pick(idx: int, status: str = "registered", probed: float = 0.0) -> Pick:
    return Pick(id=idx, email=f"u{idx}@example.com", status=status, last_probe_at=probed)


class ParseHhmmTests(unittest.TestCase):
    """"用户手打的时间不该让定时任务静默失效。"""

    def test_accepts_and_pads(self):
        self.assertEqual(parse_hhmm("3:5"), "03:05")
        self.assertEqual(parse_hhmm(" 09:30 "), "09:30")
        self.assertEqual(parse_hhmm("23:59"), "23:59")
        self.assertEqual(parse_hhmm("00:00"), "00:00")

    def test_accepts_fullwidth_colon(self):
        """中文输入法下打出来的是全角冒号 —— 直接拒绝等于「我明明设了却不跑」。"""
        self.assertEqual(parse_hhmm("09：05"), "09:05")

    def test_rejects_out_of_range(self):
        for bad in ("24:00", "09:60", "-1:00", "abc", "", "  ", "0900", "09:", ":30"):
            self.assertEqual(parse_hhmm(bad), "", f"{bad!r} 应该被判为非法")

    def test_rejects_none_and_numbers(self):
        self.assertEqual(parse_hhmm(None), "")
        self.assertEqual(parse_hhmm(930), "")


class ClampTests(unittest.TestCase):
    def test_percent_bounds(self):
        self.assertEqual(clamp_percent("30"), 30)
        self.assertEqual(clamp_percent("0"), 20, "0% 退化成默认，否则永远不抽")
        self.assertEqual(clamp_percent("999"), 100)
        self.assertEqual(clamp_percent("abc"), 20)

    def test_count_bounds(self):
        self.assertEqual(clamp_count("10"), 10)
        self.assertEqual(clamp_count("0"), DEFAULT_REFRESH_RANDOM_COUNT)
        self.assertEqual(clamp_count("100000"), 1000)

    def test_days_floor_is_one(self):
        """0 天 = 下一次清理把全部历史删掉，绝不能通过。"""
        self.assertEqual(clamp_days("0"), 1)
        self.assertEqual(clamp_days("-5"), 1)
        self.assertEqual(clamp_days("30"), 30)
        self.assertEqual(clamp_days(""), 7)


class LastProbeAtTests(unittest.TestCase):
    def test_reads_checksum_from_both_shapes(self):
        self.assertGreater(
            last_probe_at({"chatgpt_local": {"checked_at": "2026-10-31T00:00:00Z"}}), 0
        )
        self.assertGreater(last_probe_at({"probe": {"checked_at": "2026-10-31T00:00:00Z"}}), 0)

    def test_never_probed_is_zero_not_now(self):
        """从没探测过必须是 0。

        抽检策略「优先最久未检测的」按这个值升序排；用 `now` 占位的话，
        新号会排到**最后**，越新的号越不被检查 —— 而新号恰恰是一次都没
        验证过的那批。
        """
        self.assertEqual(last_probe_at({}), 0.0)
        self.assertEqual(last_probe_at({"chatgpt_local": {}}), 0.0)
        self.assertEqual(last_probe_at({"chatgpt_local": {"checked_at": "not-a-time"}}), 0.0)


class ProbeSelectionTests(unittest.TestCase):
    def test_all_mode_returns_everything_in_order(self):
        rows = [_pick(i) for i in range(5)]
        self.assertEqual([p.id for p in select_probe_targets(rows, mode="all")], [0, 1, 2, 3, 4])

    def test_sample_oldest_puts_never_probed_first(self):
        rows = [
            _pick(1, probed=1_700_000_000),
            _pick(2, probed=0),  # 从没探测过
            _pick(3, probed=1_600_000_000),  # 最久
            _pick(4, probed=1_800_000_000),
        ]
        picked = select_probe_targets(rows, mode="sample", percent=50, strategy="oldest")
        self.assertEqual(len(picked), 2)
        self.assertEqual(picked[0].id, 2, "从没探测过的号没排在最前")
        self.assertEqual(picked[1].id, 3, "第二顺位应该是最久没检测的")

    def test_sample_percent_rounds_and_keeps_at_least_one(self):
        rows = [_pick(i) for i in range(3)]
        # 3 × 1% = 0.03 → 至少 1 个（一个都不抽比抽一个更没道理）
        self.assertEqual(len(select_probe_targets(rows, mode="sample", percent=1)), 1)
        self.assertEqual(len(select_probe_targets(rows, mode="sample", percent=100)), 3)

    def test_sample_random_is_reproducible_with_seed(self):
        import random

        rows = [_pick(i) for i in range(50)]
        a = select_probe_targets(rows, mode="sample", percent=20, strategy="random", rng=random.Random(7))
        b = select_probe_targets(rows, mode="sample", percent=20, strategy="random", rng=random.Random(7))
        self.assertEqual([p.id for p in a], [p.id for p in b])
        self.assertEqual(len(a), 10)

    def test_unknown_mode_picks_nothing(self):
        """范围配错时不跑，而不是退化成「全部」—— 静默放大执行量最危险。"""
        self.assertEqual(select_probe_targets([_pick(1)], mode="whatever"), [])
        self.assertEqual(select_probe_targets([], mode="all"), [])


class RefreshSelectionTests(unittest.TestCase):
    def test_failed_scope_only_takes_rescuable_states(self):
        rows = [
            _pick(1, "registered"),
            _pick(2, "expired"),
            _pick(3, "invalid"),
            _pick(4, "limited"),
            _pick(5, "banned"),
        ]
        picked = select_refresh_targets(rows, scope="failed")
        self.assertEqual([p.id for p in picked], [2, 3, 4], "正常号不该被刷，封禁号不该被刷")

    def test_banned_never_refreshed_in_any_scope(self):
        rows = [_pick(1, "banned"), _pick(2, "registered")]
        self.assertEqual([p.id for p in select_refresh_targets(rows, scope="all")], [2])
        for _ in range(5):
            picked = select_refresh_targets(rows, scope="random", count=10)
            self.assertNotIn(1, [p.id for p in picked], "随机范围也不该抽到封禁号")

    def test_random_respects_count(self):
        rows = [_pick(i) for i in range(100)]
        self.assertEqual(len(select_refresh_targets(rows, scope="random", count=10)), 10)
        self.assertEqual(len(select_refresh_targets(rows, scope="random", count=9999)), 100)

    def test_unknown_scope_picks_nothing(self):
        self.assertEqual(select_refresh_targets([_pick(1, "expired")], scope="whatever"), [])


class DailyScheduleGateTests(unittest.TestCase):
    """三个开关各自的「今天跑不跑」，以及时间默认值。"""

    def setUp(self):
        from core.config_store import config_store

        self.config_store = config_store
        for key in (
            "log_cleanup_enabled",
            "log_cleanup_time",
            "chatgpt_auto_probe_enabled",
            "chatgpt_probe_mode",
            "chatgpt_probe_time",
            "chatgpt_auto_refresh_enabled",
            "chatgpt_refresh_scope",
            "chatgpt_refresh_time",
        ):
            config_store.set(key, "")

    def test_everything_off_by_default(self):
        self.assertEqual(get_log_cleanup_daily_at(), "")
        self.assertEqual(get_auto_probe_daily_at("chatgpt"), "")
        self.assertEqual(get_auto_refresh_daily_at("chatgpt"), "")

    def test_defaults_when_time_blank(self):
        self.config_store.set("log_cleanup_enabled", "1")
        self.config_store.set("chatgpt_auto_probe_enabled", "1")
        self.config_store.set("chatgpt_probe_mode", "sample")
        self.config_store.set("chatgpt_auto_refresh_enabled", "1")
        self.config_store.set("chatgpt_refresh_scope", "failed")
        self.assertEqual(get_log_cleanup_daily_at(), "05:00")
        self.assertEqual(get_auto_probe_daily_at("chatgpt"), "03:00")
        self.assertEqual(get_auto_refresh_daily_at("chatgpt"), "04:00")

    def test_invalid_scope_kills_the_job(self):
        """范围配错 → 整条规则不跑。退化成「全部」会静默放大执行量。"""
        self.config_store.set("chatgpt_auto_probe_enabled", "1")
        self.config_store.set("chatgpt_probe_mode", "nonsense")
        self.assertEqual(get_auto_probe_daily_at("chatgpt"), "")

    def test_grok_is_independent_from_chatgpt(self):
        from core.config_store import config_store

        config_store.set("chatgpt_auto_probe_enabled", "1")
        config_store.set("chatgpt_probe_mode", "sample")
        self.assertNotEqual(get_auto_probe_daily_at("chatgpt"), "")
        self.assertEqual(get_auto_probe_daily_at("grok"), "", "开 ChatGPT 不该把 Grok 一起开起来")


class SchedulerDailyAtTests(unittest.TestCase):
    """`_Job` 的每天定点触发。

    判据是「`last_run_at` < 最近一次应跑时刻」，而不是「距上次跑满 24 小时」：
    后者在每次进程重启时都会漂移（`start()` 把 `last_run_at` 顶到当前时间），
    用户设的「凌晨三点」会慢慢挪到下午。
    """

    def _job(self, hhmm: str, last_run_at: float):
        from core.scheduler import _Job

        return _Job(
            name="probe",
            runner=lambda: None,
            daily_at=lambda: hhmm,
            last_run_at=last_run_at,
        )

    def _at(self, hour: int, minute: int) -> float:
        return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0).timestamp()

    def test_not_due_before_the_hour(self):
        """`start()` 会把 `last_run_at` 顶到当前时间，所以启动那一刻不该补跑。"""
        job = self._job("03:00", last_run_at=self._at(2, 59))
        self.assertEqual(job.due(self._at(2, 59)), 0.0)

    def test_due_after_the_hour(self):
        job = self._job("03:00", last_run_at=self._at(2, 59))
        self.assertGreater(job.due(self._at(3, 1)), 0.0)

    def test_never_ran_catches_up(self):
        """从没跑过（last_run_at=0）时会补跑一次。

        生产里不会出现这个状态（`start()` 会顶上当前时间），保留它是为了
        明确语义：这个判据问的是「最近一次应跑时刻之后跑过没有」，
        而不是「距上次跑满 24 小时没有」。
        """
        job = self._job("03:00", last_run_at=0.0)
        self.assertGreater(job.due(self._at(12, 0)), 0.0)

    def test_not_due_again_same_day(self):
        job = self._job("03:00", last_run_at=self._at(3, 0))
        self.assertEqual(job.due(self._at(9, 0)), 0.0)

    def test_due_again_next_day(self):
        yesterday = self._at(3, 0) - 86400
        job = self._job("03:00", last_run_at=yesterday)
        self.assertGreater(job.due(self._at(3, 5)), 0.0)

    def test_restart_does_not_drift_the_hour(self):
        """重启把 `last_run_at` 顶到当前时间后，**同一个钟面时刻**仍会触发。

        用 interval 的话这里会变成「上次跑的那一刻 + 24h」，也就是今天
        10:00 重启、明天 10:00 才跑 —— 用户设的 03:00 永远轮不到。
        """
        restarted_at = self._at(10, 0)
        job = self._job("03:00", last_run_at=restarted_at)
        self.assertEqual(job.due(self._at(23, 0)), 0.0, "当天已经过去了，不该补跑")
        tomorrow = self._at(3, 0) + 86400
        self.assertGreater(job.due(tomorrow), 0.0, "第二天 03:00 必须触发")

    def test_blank_or_invalid_time_never_runs(self):
        for spec in ("", "  ", "banana", "25:00"):
            self.assertEqual(self._job(spec, last_run_at=0.0).due(self._at(12, 0)), 0.0)

    def test_interval_jobs_are_unaffected(self):
        from core.scheduler import _Job

        job = _Job(name="panel_sync", runner=lambda: None, interval=lambda: 60, last_run_at=0.0)
        self.assertGreater(job.due(self._at(12, 0)), 0.0)

    def test_register_requires_a_trigger(self):
        """两个触发方式都不给 = 一个永远不会跑的任务，直接拒绝注册。"""
        from core.scheduler import register_job

        with self.assertRaises(ValueError):
            register_job("dangling", runner=lambda: None)


class JobRegistrationTests(unittest.TestCase):
    def test_local_maintenance_registers_five_daily_jobs(self):
        import services.local_maintenance  # noqa: F401
        from core.scheduler import _jobs

        expected = {
            "log_auto_cleanup",
            "chatgpt_auto_probe",
            "chatgpt_auto_refresh_daily",
            "grok_auto_probe",
            "grok_auto_refresh_daily",
        }
        for name in expected:
            self.assertIn(name, _jobs, f"{name} 没有注册")
            self.assertIsNotNone(_jobs[name].daily_at, f"{name} 不是每天定点任务")

class BannedSwitchDefaultTests(unittest.TestCase):
    """「封禁账号标记为禁用」默认开 —— 不能靠改默认值改掉既有行为。"""

    def test_key_is_listed_as_default_true(self):
        from services.local_maintenance import BOOLEAN_DEFAULTS_TRUE

        self.assertIn("chatgpt_refresh_disable_banned", BOOLEAN_DEFAULTS_TRUE)
        self.assertIn("grok_refresh_disable_banned", BOOLEAN_DEFAULTS_TRUE)

    def test_backend_reads_true_when_unset(self):
        """库里是空串（没配过）时按「开」处理，撤回逻辑才不会误触发。"""
        from core.config_store import config_store
        from services.local_maintenance import _config_bool

        config_store.set("chatgpt_refresh_disable_banned", "")
        self.assertTrue(_config_bool("chatgpt_refresh_disable_banned", True))

    def test_explicit_off_is_respected(self):
        from core.config_store import config_store
        from services.local_maintenance import _config_bool

        config_store.set("chatgpt_refresh_disable_banned", "0")
        self.assertFalse(_config_bool("chatgpt_refresh_disable_banned", True))
        config_store.set("chatgpt_refresh_disable_banned", "")


if __name__ == "__main__":
    unittest.main()
