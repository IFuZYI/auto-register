"""services/task_store_io.py 的边界与错误路径测试。

补的是覆盖率缺口里的一批分支：
- `_json_dumps` / `_json_loads` 的序列化失败回退；
- `_to_epoch_seconds` / `_to_datetime` 的 datetime、毫秒、非正数与垃圾输入；
- `_normalize_snapshot` 的默认值填充（坏类型回落默认）；
- `_upsert_task_run` 的新建/更新语义（created_at 保留、updated_at 覆盖、
  无 id 直接跳过、历史库 created_at 为 NULL 时回填）；
- `_persist_task_snapshot` 的「任务不在 store」与 snapshot 抛异常路径；
- `_get_persisted_task` / `_list_persisted_tasks` 的取数与排序；
- `_finalize_orphan_tasks` 的孤儿标记与幂等（第二次运行不重复追加日志）；
- `_ensure_task_exists` / `_ensure_task_mutable` / `_get_task_snapshot`
  的 404/409 分支。

数据库隔离方式与 tests/test_db_modular.py 一致：每个用例把
`core.db.engine` 换成临时库 —— `current_engine()` 优先读这个属性，
被测代码会整体切到临时库，跑完还原。
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlmodel import Session, select

import core.db as _core_db
from core.db import TaskRunModel
from services import task_store_io as io_mod

UTC = timezone.utc
TIP = "[SYSTEM] 任务因服务重启中断，已自动标记为已停止"


def _snapshot(**overrides) -> dict:
    base = {
        "id": "task-1",
        "status": "running",
        "platform": "chatgpt",
        "source": "manual",
        "meta": {"scope": "unit"},
        "total": 3,
        "progress": "1/3",
        "logs": ["[00:00:01] hello"],
        "success": 1,
        "registered": 1,
        "skipped": 0,
        "errors": ["err-1"],
        "control": {"stop_requested": False},
        "cashier_urls": ["https://pay.example/1"],
        "error": "",
        "created_at": 1_700_000_000,
        "updated_at": 1_700_000_500,
    }
    base.update(overrides)
    return base


class _StoreStub:
    """`_store()` 替身：固定任务集合 + 调用记录。"""

    def __init__(self, tasks: dict | None = None, snapshot_error=None):
        self.tasks = dict(tasks or {})
        self.snapshot_error = snapshot_error
        self.exists_calls: list[str] = []
        self.snapshot_calls: list[str] = []

    def exists(self, task_id: str) -> bool:
        self.exists_calls.append(task_id)
        return task_id in self.tasks

    def snapshot(self, task_id: str) -> dict:
        self.snapshot_calls.append(task_id)
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return dict(self.tasks[task_id])


class _IsolatedDbTestCase(unittest.TestCase):
    """每个用例一个临时库 + 临时切换 core.db.engine。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self._tmp.name}/tasks.db")
        TaskRunModel.__table__.create(self.engine)
        self._saved_engine = _core_db.engine
        _core_db.engine = self.engine

    def tearDown(self):
        _core_db.engine = self._saved_engine
        self.engine.dispose()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    # -- 便捷读取 --------------------------------------------------------

    def _get_row(self, task_id: str) -> TaskRunModel | None:
        with Session(self.engine) as s:
            return s.get(TaskRunModel, task_id)

    def _all_rows(self) -> list[TaskRunModel]:
        with Session(self.engine) as s:
            return list(s.exec(select(TaskRunModel)).all())

    def _seed(self, row: TaskRunModel) -> None:
        with Session(self.engine) as s:
            s.add(row)
            s.commit()


# --------------------------------------------------------------------- JSON


class JsonHelperTests(unittest.TestCase):
    def test_dumps_keeps_unicode_readable(self):
        self.assertEqual(io_mod._json_dumps({"s": "中文"}, {}), '{"s": "中文"}')

    def test_dumps_falls_back_on_unserializable_value(self):
        # object() 无法 JSON 序列化 → 回退到 fallback（覆盖 except 分支）
        self.assertEqual(
            io_mod._json_dumps(object(), {"fallback": True}),
            '{"fallback": true}',
        )

    def test_loads_parses_valid_payloads(self):
        self.assertEqual(io_mod._json_loads('{"a": 1}', {}), {"a": 1})
        self.assertEqual(io_mod._json_loads("[1, 2]", []), [1, 2])

    def test_loads_falls_back_on_garbage(self):
        for raw in ("", None, "{broken", "not json at all"):
            with self.subTest(raw=raw):
                self.assertEqual(io_mod._json_loads(raw, {"fb": 1}), {"fb": 1})


# ------------------------------------------------------------- epoch/datetime


class EpochConversionTests(unittest.TestCase):
    def test_epoch_from_datetime_uses_timestamp(self):
        value = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        self.assertEqual(io_mod._to_epoch_seconds(value), value.timestamp())

    def test_epoch_from_numeric_inputs(self):
        self.assertEqual(io_mod._to_epoch_seconds(1_700_000_000), 1_700_000_000.0)
        self.assertEqual(io_mod._to_epoch_seconds("1700000000"), 1_700_000_000.0)
        self.assertEqual(io_mod._to_epoch_seconds(None), 0.0)

    def test_epoch_garbage_becomes_zero(self):
        self.assertEqual(io_mod._to_epoch_seconds("abc"), 0.0)
        self.assertEqual(io_mod._to_epoch_seconds(object()), 0.0)

    def test_datetime_from_seconds(self):
        value = io_mod._to_datetime(1_700_000_000)
        self.assertEqual(value, datetime.fromtimestamp(1_700_000_000, tz=UTC))
        self.assertEqual(value.tzinfo, UTC)

    def test_datetime_milliseconds_are_divided_by_1000(self):
        # >1e12 视为毫秒（JS 时间戳），不除会得到公元 5 万年
        value = io_mod._to_datetime(1_700_000_000_000)
        self.assertEqual(value, datetime.fromtimestamp(1_700_000_000, tz=UTC))

    def test_datetime_non_positive_falls_back_to_now(self):
        before = datetime.now(UTC)
        for raw in (0, -5, None, ""):
            with self.subTest(raw=raw):
                value = io_mod._to_datetime(raw)
                self.assertGreaterEqual(value, before - timedelta(seconds=1))
                self.assertEqual(value.tzinfo, UTC)

    def test_datetime_garbage_falls_back_to_now(self):
        before = datetime.now(UTC)
        for raw in ("abc", object(), 10**18):  # 10**18 换算后仍超年份上限
            with self.subTest(raw=raw):
                value = io_mod._to_datetime(raw)
                self.assertGreaterEqual(value, before - timedelta(seconds=1))
                self.assertEqual(value.tzinfo, UTC)


# ------------------------------------------------------------- normalize


class NormalizeSnapshotTests(unittest.TestCase):
    def test_empty_snapshot_gets_defaults(self):
        normalized = io_mod._normalize_snapshot({})
        self.assertEqual(normalized["id"], "")
        self.assertEqual(normalized["status"], "pending")
        self.assertEqual(normalized["platform"], "")
        self.assertEqual(normalized["source"], "manual")
        self.assertEqual(normalized["meta"], {})
        self.assertEqual(normalized["total"], 0)
        self.assertEqual(normalized["progress"], "0/0")
        self.assertEqual(normalized["logs"], [])
        self.assertEqual(normalized["success"], 0)
        self.assertEqual(normalized["registered"], 0)
        self.assertEqual(normalized["skipped"], 0)
        self.assertEqual(normalized["errors"], [])
        self.assertEqual(normalized["control"], {})
        self.assertEqual(normalized["cashier_urls"], [])
        self.assertEqual(normalized["error"], "")
        self.assertEqual(normalized["created_at"], 0.0)
        self.assertEqual(normalized["updated_at"], 0.0)

    def test_bad_types_fall_back_to_defaults(self):
        normalized = io_mod._normalize_snapshot(
            {
                "id": 123,
                "status": None,
                "meta": "not-a-dict",
                "logs": "not-a-list",
                "errors": {"nope": 1},
                "control": ["nope"],
                "cashier_urls": "nope",
                "total": "7",
                "source": None,
                "error": None,
            }
        )
        self.assertEqual(normalized["id"], "123")  # 非空标量统一转字符串
        self.assertEqual(normalized["status"], "pending")
        self.assertEqual(normalized["source"], "manual")
        self.assertEqual(normalized["meta"], {})
        self.assertEqual(normalized["logs"], [])
        self.assertEqual(normalized["errors"], [])
        self.assertEqual(normalized["control"], {})
        self.assertEqual(normalized["cashier_urls"], [])
        self.assertEqual(normalized["total"], 7)  # 数字字符串可强转
        self.assertEqual(normalized["error"], "")

    def test_datetime_created_at_is_converted_to_epoch(self):
        created = datetime(2026, 5, 6, 7, 8, 9, tzinfo=UTC)
        normalized = io_mod._normalize_snapshot({"created_at": created})
        self.assertEqual(normalized["created_at"], created.timestamp())


# ------------------------------------------------------------- upsert


class UpsertTaskRunTests(_IsolatedDbTestCase):
    def test_create_inserts_full_row(self):
        io_mod._upsert_task_run(_snapshot())

        row = self._get_row("task-1")
        self.assertIsNotNone(row)
        self.assertEqual(row.status, "running")
        self.assertEqual(row.platform, "chatgpt")
        self.assertEqual(row.source, "manual")
        self.assertEqual(row.total, 3)
        self.assertEqual(row.progress, "1/3")
        self.assertEqual(row.success, 1)
        self.assertEqual(row.registered, 1)
        self.assertEqual(row.skipped, 0)
        self.assertEqual(row.error, "")
        self.assertEqual(json.loads(row.meta_json), {"scope": "unit"})
        self.assertEqual(json.loads(row.logs_json), ["[00:00:01] hello"])
        self.assertEqual(json.loads(row.errors_json), ["err-1"])
        self.assertEqual(json.loads(row.cashier_urls_json), ["https://pay.example/1"])
        self.assertEqual(json.loads(row.control_json), {"stop_requested": False})
        self.assertEqual(int(row.created_at.timestamp()), 1_700_000_000)
        self.assertEqual(int(row.updated_at.timestamp()), 1_700_000_500)

    def test_missing_id_is_skipped(self):
        io_mod._upsert_task_run({"status": "running"})
        io_mod._upsert_task_run({"id": "", "status": "running"})
        self.assertEqual(self._all_rows(), [])

    def test_update_preserves_created_at_and_overwrites_updated_at(self):
        io_mod._upsert_task_run(_snapshot())
        io_mod._upsert_task_run(
            _snapshot(
                status="done",
                progress="3/3",
                created_at=1_650_000_000,  # 比原值更早，不应生效
                updated_at=1_700_001_000,
                meta={"scope": "second"},
            )
        )

        rows = self._all_rows()
        self.assertEqual(len(rows), 1, "更新不应产生第二行")
        row = rows[0]
        self.assertEqual(row.status, "done")
        self.assertEqual(row.progress, "3/3")
        self.assertEqual(json.loads(row.meta_json), {"scope": "second"})
        self.assertEqual(int(row.created_at.timestamp()), 1_700_000_000, "created_at 应保留")
        self.assertEqual(int(row.updated_at.timestamp()), 1_700_001_000, "updated_at 应覆盖")

    def test_update_fills_null_created_at_from_snapshot(self):
        """历史库的列可空（建表时没有 NOT NULL）：更新时要回填 created_at。"""
        with self.engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE task_runs")
            conn.exec_driver_sql(
                "CREATE TABLE task_runs ("
                "id VARCHAR NOT NULL, platform VARCHAR NOT NULL, source VARCHAR NOT NULL, "
                "status VARCHAR NOT NULL, total INTEGER NOT NULL, progress VARCHAR NOT NULL, "
                "success INTEGER NOT NULL, registered INTEGER NOT NULL, skipped INTEGER NOT NULL, "
                "error VARCHAR NOT NULL, meta_json VARCHAR NOT NULL, logs_json VARCHAR NOT NULL, "
                "errors_json VARCHAR NOT NULL, cashier_urls_json VARCHAR NOT NULL, "
                "control_json VARCHAR NOT NULL, created_at DATETIME, updated_at DATETIME, "
                "PRIMARY KEY (id))"
            )
            conn.exec_driver_sql(
                "INSERT INTO task_runs (id, platform, source, status, total, progress, "
                "success, registered, skipped, error, meta_json, logs_json, errors_json, "
                "cashier_urls_json, control_json, created_at, updated_at) VALUES "
                "('legacy-1', 'chatgpt', 'manual', 'running', 0, '0/0', 0, 0, 0, '', "
                "'{}', '[]', '[]', '[]', '{}', NULL, NULL)"
            )

        io_mod._upsert_task_run(
            _snapshot(id="legacy-1", status="done", created_at=1_700_000_000, updated_at=1_800_000_000)
        )

        row = self._get_row("legacy-1")
        self.assertEqual(row.status, "done")
        self.assertEqual(int(row.created_at.timestamp()), 1_700_000_000)
        self.assertEqual(int(row.updated_at.timestamp()), 1_800_000_000)


# ------------------------------------------------------------- persist


class PersistTaskSnapshotTests(_IsolatedDbTestCase):
    def test_skips_when_store_does_not_know_task(self):
        store = _StoreStub(tasks={})
        with mock.patch.object(io_mod, "_store", return_value=store):
            io_mod._persist_task_snapshot("ghost")
        self.assertEqual(self._all_rows(), [])
        self.assertEqual(store.snapshot_calls, [])

    def test_snapshot_exception_is_swallowed(self):
        store = _StoreStub(tasks={"task-1": _snapshot()}, snapshot_error=RuntimeError("炸了"))
        with mock.patch.object(io_mod, "_store", return_value=store):
            io_mod._persist_task_snapshot("task-1")  # 不应抛出
        self.assertEqual(self._all_rows(), [])

    def test_writes_row_when_store_has_task(self):
        store = _StoreStub(tasks={"task-1": _snapshot()})
        with mock.patch.object(io_mod, "_store", return_value=store):
            io_mod._persist_task_snapshot("task-1")
        row = self._get_row("task-1")
        self.assertIsNotNone(row)
        self.assertEqual(row.status, "running")


# ------------------------------------------------------- get / list persisted


class GetPersistedTaskTests(_IsolatedDbTestCase):
    def test_missing_returns_none(self):
        self.assertIsNone(io_mod._get_persisted_task("nope"))

    def test_returns_normalized_snapshot(self):
        io_mod._upsert_task_run(_snapshot(id="persisted-1"))
        snapshot = io_mod._get_persisted_task("persisted-1")
        self.assertEqual(snapshot["id"], "persisted-1")
        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(snapshot["meta"], {"scope": "unit"})
        self.assertEqual(snapshot["logs"], ["[00:00:01] hello"])
        self.assertEqual(snapshot["errors"], ["err-1"])
        self.assertEqual(snapshot["cashier_urls"], ["https://pay.example/1"])


class ListPersistedTasksTests(_IsolatedDbTestCase):
    def test_sort_order_by_status_then_newest_first(self):
        base = datetime(2026, 1, 1, tzinfo=UTC)

        def seed(task_id: str, status: str, offset: int) -> None:
            self._seed(
                TaskRunModel(
                    id=task_id,
                    platform="chatgpt",
                    status=status,
                    created_at=base + timedelta(seconds=offset),
                    updated_at=base + timedelta(seconds=offset),
                )
            )

        seed("run-old", "running", 10)
        seed("run-new", "running", 20)
        seed("pend", "pending", 30)
        seed("done", "done", 40)
        seed("fail", "failed", 50)
        seed("stop", "stopped", 60)
        seed("weird", "archived", 70)  # 未知状态排最后

        ids = [item["id"] for item in io_mod._list_persisted_tasks()]
        self.assertEqual(
            ids, ["run-new", "run-old", "pend", "done", "fail", "stop", "weird"]
        )


# ------------------------------------------------------- finalize orphans


class FinalizeOrphanTasksTests(_IsolatedDbTestCase):
    def _seed_orphan(self, task_id: str, status: str, error: str = "", logs=None) -> None:
        self._seed(
            TaskRunModel(
                id=task_id,
                platform="chatgpt",
                status=status,
                error=error,
                logs_json=json.dumps(logs or [], ensure_ascii=False),
            )
        )

    def test_marks_orphans_stopped_and_appends_system_log(self):
        self._seed_orphan("orphan-run", "running")
        self._seed_orphan("orphan-pend", "pending", error="老错误")
        self._seed_orphan("still-live", "running")
        self._seed_orphan("already-done", "done")

        store = _StoreStub(tasks={"still-live": _snapshot(id="still-live")})
        with mock.patch.object(io_mod, "_store", return_value=store):
            io_mod._finalize_orphan_tasks()

        run = self._get_row("orphan-run")
        self.assertEqual(run.status, "stopped")
        self.assertEqual(run.error, "任务因服务重启中断")
        logs = json.loads(run.logs_json)
        self.assertEqual(len(logs), 1)
        self.assertRegex(logs[0], r"^\[\d{2}:\d{2}:\d{2}\] ")
        self.assertIn(TIP, logs[0])

        pend = self._get_row("orphan-pend")
        self.assertEqual(pend.status, "stopped")
        self.assertEqual(pend.error, "老错误", "已有错误信息不应被覆盖")

        self.assertEqual(self._get_row("still-live").status, "running", "存活任务不能被误杀")
        self.assertEqual(self._get_row("already-done").status, "done")

    def test_second_run_does_not_duplicate_log(self):
        self._seed_orphan("orphan-run", "running")
        store = _StoreStub(tasks={})
        with mock.patch.object(io_mod, "_store", return_value=store):
            io_mod._finalize_orphan_tasks()
            first_logs = self._get_row("orphan-run").logs_json
            first_updated = self._get_row("orphan-run").updated_at
            io_mod._finalize_orphan_tasks()

        row = self._get_row("orphan-run")
        self.assertEqual(row.logs_json, first_logs, "第二次运行不应重复追加日志")
        self.assertEqual(row.updated_at, first_updated, "第二次运行不应再写 updated_at")
        self.assertEqual(len(json.loads(row.logs_json)), 1)

    def test_log_not_duplicated_when_tip_already_present(self):
        """日志里已带裸 tip 标记时不再追加（老版本留下的数据）。"""
        self._seed_orphan("orphan-run", "running", logs=[TIP])
        store = _StoreStub(tasks={})
        with mock.patch.object(io_mod, "_store", return_value=store):
            io_mod._finalize_orphan_tasks()

        row = self._get_row("orphan-run")
        self.assertEqual(row.status, "stopped")
        self.assertEqual(json.loads(row.logs_json), [TIP])


# ------------------------------------------------------- ensure / snapshot


class EnsureTaskExistsTests(_IsolatedDbTestCase):
    def test_returns_when_store_has_task(self):
        store = _StoreStub(tasks={"t": _snapshot(id="t")})
        with mock.patch.object(io_mod, "_store", return_value=store):
            io_mod._ensure_task_exists("t")  # 不应抛出

    def test_returns_when_only_persisted(self):
        io_mod._upsert_task_run(_snapshot(id="persisted"))
        store = _StoreStub(tasks={})
        with mock.patch.object(io_mod, "_store", return_value=store):
            io_mod._ensure_task_exists("persisted")

    def test_raises_404_when_nowhere(self):
        store = _StoreStub(tasks={})
        with mock.patch.object(io_mod, "_store", return_value=store):
            with self.assertRaises(HTTPException) as ctx:
                io_mod._ensure_task_exists("ghost")
        self.assertEqual(ctx.exception.status_code, 404)


class EnsureTaskMutableTests(_IsolatedDbTestCase):
    def test_allows_running_task_in_store(self):
        store = _StoreStub(tasks={"t": _snapshot(id="t", status="running")})
        with mock.patch.object(io_mod, "_store", return_value=store):
            io_mod._ensure_task_mutable("t")

    def test_rejects_finished_task_in_store(self):
        for status in ("done", "failed", "stopped"):
            with self.subTest(status=status):
                store = _StoreStub(tasks={"t": _snapshot(id="t", status=status)})
                with mock.patch.object(io_mod, "_store", return_value=store):
                    with self.assertRaises(HTTPException) as ctx:
                        io_mod._ensure_task_mutable("t")
                self.assertEqual(ctx.exception.status_code, 409)

    def test_rejects_finished_persisted_task(self):
        io_mod._upsert_task_run(_snapshot(id="t", status="done"))
        store = _StoreStub(tasks={})
        with mock.patch.object(io_mod, "_store", return_value=store):
            with self.assertRaises(HTTPException) as ctx:
                io_mod._ensure_task_mutable("t")
        self.assertEqual(ctx.exception.status_code, 409)

    def test_allows_pending_persisted_task(self):
        io_mod._upsert_task_run(_snapshot(id="t", status="pending"))
        store = _StoreStub(tasks={})
        with mock.patch.object(io_mod, "_store", return_value=store):
            io_mod._ensure_task_mutable("t")

    def test_missing_task_is_404(self):
        store = _StoreStub(tasks={})
        with mock.patch.object(io_mod, "_store", return_value=store):
            with self.assertRaises(HTTPException) as ctx:
                io_mod._ensure_task_mutable("ghost")
        self.assertEqual(ctx.exception.status_code, 404)


class GetTaskSnapshotTests(_IsolatedDbTestCase):
    def test_persists_and_returns_store_snapshot(self):
        store = _StoreStub(tasks={"t": _snapshot(id="t")})
        with mock.patch.object(io_mod, "_store", return_value=store):
            snapshot = io_mod._get_task_snapshot("t")

        self.assertEqual(snapshot["id"], "t")
        self.assertEqual(snapshot["status"], "running")
        self.assertIsNotNone(self._get_row("t"), "取快照时应顺带落库")

    def test_falls_back_to_store_snapshot_when_not_persisted(self):
        """store 里的快照没有 id（upsert 跳过）时，仍要返回归一化后的快照。"""
        store = _StoreStub(tasks={"t": {"status": "running", "platform": "chatgpt"}})
        with mock.patch.object(io_mod, "_store", return_value=store):
            snapshot = io_mod._get_task_snapshot("t")

        self.assertEqual(snapshot["id"], "")
        self.assertEqual(snapshot["status"], "running")
        self.assertEqual(self._all_rows(), [], "没有 id 的快照不应落库")

    def test_raises_404_when_task_vanishes_between_checks(self):
        """exists 在第 4 次调用起翻假：模拟任务在两次检查之间被清理。"""

        class _VanishingStore:
            def __init__(self):
                self._remaining = 3

            def exists(self, task_id):
                self._remaining -= 1
                return self._remaining >= 0

            def snapshot(self, task_id):
                raise RuntimeError("任务已被清理")

        with mock.patch.object(io_mod, "_store", return_value=_VanishingStore()):
            with self.assertRaises(HTTPException) as ctx:
                io_mod._get_task_snapshot("t")
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertIn("任务不存在", ctx.exception.detail)


if __name__ == "__main__":
    unittest.main()
