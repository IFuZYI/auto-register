"""任务表的仓储：两种后端跑同一套契约。

`task_runs` 的 id 是**字符串**（`task_1793...`）—— 不是自增整数，这是它与
账号/代理表最大的差别。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class _JsonFixture:
    name = "json"

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "task_runs.json"
        self._env = None
        self._patches: list = []

    def __enter__(self):
        from core.storage.json_table import JsonTableStore

        self._env = mock.patch.dict("os.environ", {"STORAGE_BACKEND": "json"}, clear=False)
        self._env.start()
        self._patches = [
            mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "json"),
            mock.patch(
                "core.db.task_repository.TaskRepository._store",
                staticmethod(lambda: JsonTableStore(self.path, table="task_runs")),
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        self._env.stop()
        return False


class _SqliteFixture:
    name = "sqlite"

    def __init__(self, root: Path):
        self.root = root

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TaskRepositoryContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _cases(self):
        return (_SqliteFixture(self.root), _JsonFixture(self.root))

    def _cleanup_sqlite(self):
        from core.db import current_engine
        from core.db.models_infra import TaskRunModel
        from sqlmodel import Session, select

        with Session(current_engine()) as s:
            for row in s.exec(
                select(TaskRunModel).where(TaskRunModel.id.like("contract_task_%"))
            ).all():
                s.delete(row)
            s.commit()

    def _snapshot(self, task_id: str, **overrides):
        data = {
            "id": task_id,
            "platform": "chatgpt",
            "source": "manual",
            "status": "running",
            "total": 3,
            "progress": "1/3",
            "success": 1,
            "registered": 1,
            "skipped": 0,
            "error": "",
            "meta": {"kind": "test"},
            "logs": ["line-1"],
            "errors": [],
            "cashier_urls": [],
            "control": {},
            "created_at": "2026-01-02T03:04:05+00:00",
            "updated_at": "2026-01-02T03:04:06+00:00",
        }
        data.update(overrides)
        return data

    def _put(self, snapshot):
        """写入走上层分派（`core` 不许 import `services`，见仓储 docstring）。

        SQLite 分支由 `services.task_store_io._upsert_task_run` 做 —— 测试直接
        调它，与生产同一条路径。
        """
        from core.db.task_repository import task_repository

        if task_repository.put_json(snapshot):
            return
        from services.task_store_io import _upsert_task_run

        _upsert_task_run(snapshot)

    def _get(self, task_id):
        from core.db.task_repository import task_repository

        from_json = task_repository.get_json(task_id)
        if from_json is not None:
            return from_json
        if task_repository._store() is not None:
            return None
        from services.task_store_io import _get_persisted_task

        return _get_persisted_task(task_id)

    def test_upsert_then_get(self):
        for fixture in self._cases():
            task_id = f"contract_task_{fixture.name}_a"
            with self.subTest(backend=fixture.name), fixture:
                self._put(self._snapshot(task_id))
                got = self._get(task_id)
                self.assertIsNotNone(got, f"{fixture.name}: upsert 后取不到")
                self.assertEqual(got["status"], "running")
                self.assertEqual(got["logs"], ["line-1"])
        self._cleanup_sqlite()

    def test_upsert_updates_existing(self):
        for fixture in self._cases():
            task_id = f"contract_task_{fixture.name}_b"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.task_repository import task_repository

                self._put(self._snapshot(task_id))
                self._put(self._snapshot(task_id, status="done", success=3))
                got = self._get(task_id)
                self.assertEqual(got["status"], "done")
                self.assertEqual(got["success"], 3)
        self._cleanup_sqlite()

    def test_list_by_status(self):
        for fixture in self._cases():
            a = f"contract_task_{fixture.name}_live"
            b = f"contract_task_{fixture.name}_done"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.task_repository import task_repository

                self._put(self._snapshot(a, status="running"))
                self._put(self._snapshot(b, status="done"))
                live = task_repository.list_json_by_status(["pending", "running"])
                if live is None:
                    from services.task_store_io import _list_persisted_tasks

                    live = [
                        t for t in _list_persisted_tasks()
                        if str(t.get("status")) in ("pending", "running")
                    ]
                ids = {row["id"] for row in live}
                self.assertIn(a, ids, f"{fixture.name}: 运行中的任务没列出来")
                self.assertNotIn(b, ids, f"{fixture.name}: 已结束的任务混进来了")
        self._cleanup_sqlite()

    def test_missing_task_returns_none(self):
        for fixture in self._cases():
            with self.subTest(backend=fixture.name), fixture:
                self.assertIsNone(self._get("contract_task_nope"))
        self._cleanup_sqlite()


if __name__ == "__main__":
    unittest.main()
