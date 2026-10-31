"""本地自动维护：时间解析、抽检/刷新范围选择、每天定点触发。

这里钉的都是**纯函数**（不碰数据库与网络），跑得快、失败时能直接指出
是哪条规则算错了。真正打上游的那条链（`_run_actions`）刻意复用
`api.actions` 的手动路径，由既有的动作测试覆盖。
"""

from __future__ import annotations

import pathlib
import unittest
from datetime import datetime

from services.local_maintenance import (
    DEFAULT_REFRESH_RANDOM_COUNT,
    MAX_INTERVAL_SECONDS,
    MIN_INTERVAL_SECONDS,
    Pick,
    clamp_count,
    clamp_days,
    clamp_percent,
    format_interval,
    get_auto_probe_interval_seconds,
    get_auto_refresh_interval_seconds,
    get_log_cleanup_interval_seconds,
    last_probe_at,
    parse_interval,
    select_probe_targets,
    select_refresh_targets,
)


def _pick(idx: int, status: str = "registered", probed: float = 0.0) -> Pick:
    return Pick(id=idx, email=f"u{idx}@example.com", status=status, last_probe_at=probed)


class ParseIntervalTests(unittest.TestCase):
    """间隔解析。用户 2026-10-31 把「执行时间」改成「执行间隔」。"""

    def test_accepts_number_plus_unit(self):
        self.assertEqual(parse_interval("30m"), 1800)
        self.assertEqual(parse_interval("6h"), 21600)
        self.assertEqual(parse_interval("1d"), 86400)
        self.assertEqual(parse_interval(" 6H "), 21600)

    def test_bare_number_means_minutes(self):
        """手打 `90` 时最自然的单位是分钟（界面上会带单位，这里是兜底）。"""
        self.assertEqual(parse_interval("90"), 5400)

    def test_clamps_instead_of_rejecting(self):
        """`1m` 的本意是「尽量勤」—— 给他下限比让他什么都不跑更接近意图。"""
        self.assertEqual(parse_interval("1m"), MIN_INTERVAL_SECONDS)
        self.assertEqual(parse_interval("90d"), MAX_INTERVAL_SECONDS)

    def test_rejects_garbage(self):
        for bad in ("abc", "", "  ", "h", "6x", "-3h", "0h", None):
            self.assertEqual(parse_interval(bad), 0, f"{bad!r} 应该判为非法")

    def test_format_round_trips_to_the_biggest_unit(self):
        self.assertEqual(format_interval(21600), "6h")
        self.assertEqual(format_interval(1800), "30m")
        self.assertEqual(format_interval(86400), "1d")
        self.assertEqual(format_interval(300), "5m")
        self.assertEqual(format_interval(0), "")

    def test_format_output_is_parseable(self):
        for seconds in (300, 1800, 21600, 86400, 7 * 86400):
            self.assertEqual(
                parse_interval(format_interval(seconds)), seconds,
                f"{seconds}s 的展示形式不能被自己解析回来",
            )


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


class IntervalGateTests(unittest.TestCase):
    """三个开关各自的「多久跑一轮」，以及默认值。"""

    def setUp(self):
        from core.config_store import config_store

        self.config_store = config_store
        for key in (
            "log_cleanup_enabled", "log_cleanup_interval",
            "chatgpt_auto_probe_enabled", "chatgpt_probe_mode", "chatgpt_probe_interval",
            "chatgpt_auto_refresh_enabled", "chatgpt_refresh_scope", "chatgpt_refresh_interval",
        ):
            config_store.set(key, "")

    def test_everything_off_by_default(self):
        self.assertEqual(get_log_cleanup_interval_seconds(), 0)
        self.assertEqual(get_auto_probe_interval_seconds("chatgpt"), 0)
        self.assertEqual(get_auto_refresh_interval_seconds("chatgpt"), 0)

    def test_defaults_when_blank(self):
        self.config_store.set("log_cleanup_enabled", "1")
        self.config_store.set("chatgpt_auto_probe_enabled", "1")
        self.config_store.set("chatgpt_probe_mode", "sample")
        self.config_store.set("chatgpt_auto_refresh_enabled", "1")
        self.config_store.set("chatgpt_refresh_scope", "failed")
        self.assertEqual(get_log_cleanup_interval_seconds(), 86400)
        self.assertEqual(get_auto_probe_interval_seconds("chatgpt"), 21600)
        self.assertEqual(get_auto_refresh_interval_seconds("chatgpt"), 21600)

    def test_configured_interval_wins(self):
        self.config_store.set("chatgpt_auto_probe_enabled", "1")
        self.config_store.set("chatgpt_probe_mode", "sample")
        self.config_store.set("chatgpt_probe_interval", "30m")
        self.assertEqual(get_auto_probe_interval_seconds("chatgpt"), 1800)

    def test_invalid_scope_kills_the_job(self):
        """范围配错 → 整条规则不跑。退化成「全部」会静默放大执行量。"""
        self.config_store.set("chatgpt_auto_probe_enabled", "1")
        self.config_store.set("chatgpt_probe_mode", "nonsense")
        self.assertEqual(get_auto_probe_interval_seconds("chatgpt"), 0)

    def test_grok_is_independent_from_chatgpt(self):
        from core.config_store import config_store

        config_store.set("chatgpt_auto_probe_enabled", "1")
        config_store.set("chatgpt_probe_mode", "sample")
        self.assertNotEqual(get_auto_probe_interval_seconds("chatgpt"), 0)
        self.assertEqual(get_auto_probe_interval_seconds("grok"), 0, "开 ChatGPT 不该把 Grok 一起开起来")


class SchedulerIntervalTests(unittest.TestCase):
    """`_Job` 的间隔触发。

    语义是「距上次跑满 N 秒」—— 用户要的「执行间隔」就是这个。`start()` 会把
    `last_run_at` 顶到当前时间，所以重启进程不会一启动就瞬间触发，也不会
    改变节奏（它本来就不对齐整点）。
    """

    def _job(self, seconds, last_run_at):
        from core.scheduler import _Job

        return _Job(name="probe", runner=lambda: None, interval=lambda: seconds, last_run_at=last_run_at)

    def test_not_due_before_the_interval(self):
        self.assertEqual(self._job(3600, last_run_at=1_000_000).due(1_003_000), 0.0)

    def test_due_after_the_interval(self):
        job = self._job(3600, last_run_at=1_000_000)
        self.assertGreater(job.due(1_003_601), 0.0)

    def test_zero_interval_skips(self):
        """0 = 主动不跑（开关关着 / 配置非法），不是「立刻跑」。"""
        self.assertEqual(self._job(0, last_run_at=0).due(1_000_000), 0.0)

    def test_raising_interval_callback_skips_instead_of_crashing(self):
        from core.scheduler import _Job

        def boom() -> int:
            raise RuntimeError("配置读不到")

        job = _Job(name="probe", runner=lambda: None, interval=boom, last_run_at=0.0)
        self.assertEqual(job.due(1_000_000), 0.0)

    def test_sub_second_interval_is_not_truncated_to_zero(self):
        """`int(0.9)` 会变成 0，而 0 的语义是「主动跳过」—— 那会静默吞掉这个任务。"""
        from core.scheduler import _Job

        job = _Job(name="probe", runner=lambda: None, interval=lambda: 0.9, last_run_at=0.0)
        self.assertGreater(job.due(1_000_000), 0.0)

    def test_register_requires_an_interval(self):
        from core.scheduler import register_job

        with self.assertRaises(TypeError):
            register_job("dangling", runner=lambda: None)


class JobRegistrationTests(unittest.TestCase):
    def test_local_maintenance_registers_five_interval_jobs(self):
        import services.local_maintenance  # noqa: F401
        from core.scheduler import _jobs

        expected = {
            "log_auto_cleanup",
            "chatgpt_auto_probe",
            "chatgpt_auto_refresh",
            "grok_auto_probe",
            "grok_auto_refresh",
        }
        for name in expected:
            self.assertIn(name, _jobs, f"{name} 没有注册")
            self.assertIsNotNone(_jobs[name].interval, f"{name} 不是间隔任务")

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


class RunActionsTaskRecordTests(unittest.TestCase):
    """`_run_actions` 的任务可见性（用户要求 2026-10-07）。

    这套断言原来长在 `test_task_kinds_and_visibility.py`，驱动的是**已删除**的
    `run_auto_refresh_pass`。行为本身还在（`_open_auto_task` / `_close_auto_task`
    三个 helper 一字未改，只是换了个调用方），所以把它重挂到现在的执行器上
    —— 否则「空轮不建记录」「停止要如实标 stopped」就没人管了。
    """

    SOURCE = "test_local_maintenance_probe"

    def setUp(self):
        from core.config_store import config_store

        config_store.set("chatgpt_probe_sample_percent", "20")
        self._purge()

    def tearDown(self):
        self._purge()

    def _purge(self):
        from sqlmodel import Session, delete

        from api.tasks import _task_store
        from core.db import TaskRunModel, current_engine

        with _task_store._lock:
            for tid in [k for k, r in _task_store._records.items() if r.source == self.SOURCE]:
                _task_store._records.pop(tid, None)
        with Session(current_engine()) as s:
            s.exec(delete(TaskRunModel).where(TaskRunModel.source == self.SOURCE))
            s.commit()

    def _records(self):
        from api.tasks import _task_store

        return [s for s in _task_store.list_snapshots() if s.get("source") == self.SOURCE]

    def _fake(self, *, ok: bool = True, boom: bool = False, delay: float = 0.0):
        """把动作链整体替换掉：只留 `_run_actions` 自己的编排逻辑可测。"""
        import contextlib

        from unittest import mock

        from services import local_maintenance as lm

        account = mock.MagicMock()
        account.status = "registered"
        account.get_extra.return_value = {}

        class _Instance:
            def __init__(self, **kwargs):
                self.config = mock.MagicMock()

            def execute_action(self, action_id, acct, params):
                if boom:
                    raise RuntimeError("上游炸了")
                return {"ok": ok, "error": "" if ok else "没换发"}

        platform_session = mock.MagicMock()
        platform_session.return_value.__enter__ = mock.MagicMock(return_value=mock.MagicMock())
        platform_session.return_value.__exit__ = mock.MagicMock(return_value=False)

        return [
            mock.patch.object(lm, "PER_ACCOUNT_DELAY_SECONDS", delay),
            mock.patch("core.registry.get", return_value=_Instance),
            mock.patch("core.db.platform_session", platform_session),
            mock.patch("core.db.account_repository.get", return_value=account),
            mock.patch("services.action_execution.resolve_action_proxy", return_value=""),
            mock.patch("services.action_execution.to_platform_account", return_value=mock.MagicMock()),
            mock.patch("services.action_execution.apply_action_result"),
            mock.patch("services.action_execution.result_message", return_value="没换发"),
            mock.patch.object(lm, "_open_task", side_effect=self._open),
            mock.patch.object(lm, "_close_auto_task"),
            mock.patch.object(lm, "_make_logger", return_value=lambda m: None),
        ]

    def _open(self, *, source, platform, total, meta=None):
        from services import task_service as _api

        task_id = f"{source}_t"
        _api.task_store.create(task_id, platform=platform, total=total, source=source, meta=dict(meta or {}))
        _api.task_store.mark_running(task_id)
        return task_id, None

    def test_empty_round_creates_no_record(self):
        """空轮不建记录 —— 每天一条空任务会把「任务运行」淹掉。

        整条动作链都 patch 上（而不是只断言 `total == 0`）：这样去掉早返回时
        失败原因是「多了一条记录」这个**真正的**断言，而不是一个无关的
        `KeyError` —— 后者看着红、其实没测到东西。
        """
        from services import local_maintenance as lm

        patches = self._fake()
        with patches[0]:
            for pt in patches[1:]:
                pt.start()
            try:
                summary = lm._run_actions("chatgpt", "probe_local_status", [], source=self.SOURCE, label="检测")
            finally:
                for pt in reversed(patches[1:]):
                    pt.stop()

        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["ok"], 0)
        self.assertEqual(self._records(), [], "空轮建了任务记录")

    def test_successful_round_creates_one_record(self):
        from services import local_maintenance as lm

        patches = self._fake()
        with mock.patch("core.db.platform_session"):
            with patches[0]:
                for pt in patches[1:]:
                    pt.start()
                try:
                    summary = lm._run_actions(
                        "chatgpt", "probe_local_status",
                        [lm.Pick(1, "a@x.com", "registered", 0.0)], source=self.SOURCE, label="检测",
                    )
                finally:
                    for pt in reversed(patches[1:]):
                        pt.stop()

        self.assertEqual(summary["ok"], 1)
        records = self._records()
        self.assertEqual(len(records), 1, "跑了一轮却没有任务记录 —— 页面上看不见")
        self.assertEqual(records[0]["source"], self.SOURCE)

    def test_per_account_exception_is_counted_not_fatal(self):
        """单个号炸了要记进 errors，而不是毁掉整轮。"""
        from services import local_maintenance as lm

        patches = self._fake(boom=True)
        with patches[0]:
            for pt in patches[1:]:
                pt.start()
            try:
                summary = lm._run_actions(
                    "chatgpt", "probe_local_status",
                    [lm.Pick(1, "a@x.com", "registered", 0.0)], source=self.SOURCE, label="检测",
                )
            finally:
                for pt in reversed(patches[1:]):
                    pt.stop()

        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["ok"], 0)
        self.assertTrue(summary["errors"], "异常没有被记进 errors")


from unittest import mock  # noqa: E402  —— 上面的测试类要用


class RefreshSyncStrategyTests(unittest.TestCase):
    """「刷新策略」三个档的**接线**（用户要求的三选一）。

    这三条分支在本部署里没法真机验证 —— 4 个面板都没配连接参数，拉取/推送
    那两条路跑不起来。所以在这里把编排钉死：**先拉、再刷、后推**的顺序，
    以及 `local_only` 绝不碰远端。

    顺序不是可选的细节：「先同步远程」的意义是面板侧可能已经用别的路径换过
    凭证，本地拿旧的去刷等于白刷一次（上游会把旧凭证判失效）。推在刷之前，
    推上去的就是马上要过期的那个。
    """

    def _run(self, strategy: str, panels=("chatgpt2api",)):
        from unittest import mock

        from services import local_maintenance as lm
        from core.config_store import config_store

        config_store.set("chatgpt_sync_strategy", strategy)
        config_store.set("chatgpt_refresh_scope", "failed")
        order: list[str] = []

        with mock.patch.object(lm, "_enabled_panels", return_value=list(panels)), \
             mock.patch.object(lm, "_iter_picks", return_value=[lm.Pick(1, "a@x.com", "invalid", 0.0)]), \
             mock.patch.object(lm, "_pull_from_panels", side_effect=lambda *a: order.append("pull")), \
             mock.patch.object(lm, "_push_to_panels", side_effect=lambda *a: order.append("push") or []), \
             mock.patch.object(lm, "_run_actions", side_effect=lambda *a, **k: order.append("refresh") or {
                 "total": 1, "ok": 1, "failed": 0, "skipped": 0, "errors": []
             }):
            lm.run_auto_refresh("chatgpt")
        return order

    def tearDown(self):
        from core.config_store import config_store

        config_store.set("chatgpt_sync_strategy", "")
        config_store.set("chatgpt_refresh_scope", "")

    def test_local_only_touches_nothing_remote(self):
        """默认档：不碰面板。这是加这个开关之前的行为，升级不该改变它。"""
        self.assertEqual(self._run("local_only"), ["refresh"])

    def test_remote_first_pulls_then_refreshes_then_pushes(self):
        self.assertEqual(self._run("remote_first"), ["pull", "refresh", "push"])

    def test_local_then_push_only_pushes_after(self):
        self.assertEqual(self._run("local_then_push"), ["refresh", "push"])

    def test_no_enabled_panel_means_no_remote_calls(self):
        """一个面板都没开自动同步时，两个远程分支都不该发生。"""
        self.assertEqual(self._run("remote_first", panels=()), ["refresh"])
        self.assertEqual(self._run("local_then_push", panels=()), ["refresh"])

    def test_unknown_strategy_falls_back_to_local_only(self):
        """策略配错（不在白名单）→ 按最保守的 `local_only`，不去动远端。"""
        self.assertEqual(self._run("nonsense"), ["refresh"])

    def test_pull_failure_does_not_block_the_refresh(self):
        """拉取失败只记日志 —— 刷新本身不该被一个连不上的面板拖住。"""
        from unittest import mock

        from services import local_maintenance as lm
        from core.config_store import config_store

        config_store.set("chatgpt_sync_strategy", "remote_first")
        config_store.set("chatgpt_refresh_scope", "failed")
        order: list[str] = []

        with mock.patch.object(lm, "_enabled_panels", return_value=["chatgpt2api"]), \
             mock.patch.object(lm, "_iter_picks", return_value=[lm.Pick(1, "a@x.com", "invalid", 0.0)]), \
             mock.patch.object(lm, "_pull_from_panels", side_effect=RuntimeError("面板连不上")), \
             mock.patch.object(lm, "_push_to_panels", side_effect=lambda *a: order.append("push") or []), \
             mock.patch.object(lm, "_run_actions", side_effect=lambda *a, **k: order.append("refresh") or {
                 "total": 1, "ok": 1, "failed": 0, "skipped": 0, "errors": []
             }):
            lm.run_auto_refresh("chatgpt")
        self.assertIn("refresh", order, "拉取失败把刷新也带崩了")
        config_store.set("chatgpt_sync_strategy", "")
        config_store.set("chatgpt_refresh_scope", "")


class MaintenancePanelFrontendTests(unittest.TestCase):
    """自动维护面板的接线（源码级契约）。

    前端没有单测基础设施，能钉住的是「这段结构必须存在」—— 都是踩过坑的点。
    """

    SRC = pathlib.Path("/data/workspace/Register/frontend/src/components/settings/MaintenancePanel.tsx")

    def _src(self) -> str:
        return self.SRC.read_text(encoding="utf-8")

    def test_interval_row_registers_the_field(self):
        """间隔控件必须先 `Form.Item name=...` 注册，`useWatch` 才看得到值。

        踩过：`IntervalRow` 里只放了两个普通受控组件，字段没注册 ——
        `Form.useWatch(name)` 对未注册字段返回 undefined，于是数字框**永远是空的**，
        即使库里配了 `6h`。布局看着完全正常，只有读那个 input 的 value 才发现。

        （同一个坑 `ConfigPanels.tsx` 里注释过：`_set` 键 watch 不到。）
        """
        src = self._src()
        block = src[src.index("function IntervalRow("):src.index("/** 一行下拉参数。 */")]
        self.assertIn("Form.useWatch(name)", block, "间隔控件没读表单值")
        self.assertIn("<Form.Item name={name}", block, "字段没注册进 Form —— 读到的永远是 undefined")
        self.assertIn("splitInterval(", block, "没把 `6h` 拆成数字 + 单位")

    def test_no_leftover_execution_time_keys(self):
        """「执行时间」的键名必须清干净（改成间隔后它们不再被读写）。

        断言的是**键名**和**界面标签**，不是「文件里出现过这四个字」——
        注释里正当地解释「为什么从执行时间改成执行间隔」时会出现这个词，
        按字面禁掉会让文档和测试互相打架。
        """
        src = self._src()
        for stale in ("_probe_time", "_refresh_time", "log_cleanup_time"):
            self.assertNotIn(stale, src, f"面板里还残留旧的配置键 {stale}")
        self.assertNotIn('label="执行时间"', src, "界面上还留着「执行时间」标签")

    def test_default_intervals_are_present(self):
        """空值时的默认间隔 —— 不给默认的话开关一开就是空框，看着像配错了。"""
        src = self._src()
        for default in ("'6h'", "'24h'"):
            self.assertIn(default, src, f"缺少默认间隔 {default}")
