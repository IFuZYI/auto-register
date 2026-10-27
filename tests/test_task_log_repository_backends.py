"""任务日志表的仓储：两种后端跑同一套契约。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock


class _JsonFixture:
    name = "json"

    def __init__(self, root: Path):
        self.root = root
        self.path = root / "task_logs.json"
        self._env = None
        self._patches: list = []

    def __enter__(self):
        from core.storage.json_table import JsonTableStore

        self._env = mock.patch.dict("os.environ", {"STORAGE_BACKEND": "json"}, clear=False)
        self._env.start()
        self._patches = [
            mock.patch("core.storage.resolve_backend_name", lambda *a, **k: "json"),
            mock.patch(
                "core.db.task_log_repository.TaskLogRepository._store",
                staticmethod(lambda: JsonTableStore(self.path, table="task_logs")),
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


class TaskLogRepositoryContractTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)

    def _cases(self):
        return (_SqliteFixture(self.root), _JsonFixture(self.root))

    def _cleanup_sqlite(self):
        from core.db import current_engine
        from core.db.models_infra import TaskLog
        from sqlmodel import Session, select

        with Session(current_engine()) as s:
            for row in s.exec(
                select(TaskLog).where(TaskLog.email.like("contract-log-%"))
            ).all():
                s.delete(row)
            s.commit()

    def _add(self, *, email, detail=None):
        from core.db.task_log_repository import task_log_repository

        row = task_log_repository.add_json(
            {"platform": "chatgpt", "email": email, "status": "success",
             "error": "", "detail": detail or {}}
        )
        if row is not None:
            return row
        from api.tasks import _save_task_log_sync
        return _save_task_log_sync("chatgpt", email, "success", detail=detail or {})

    def _list(self, *, platform="", page=1, page_size=50):
        from core.db.task_log_repository import task_log_repository

        got = task_log_repository.list_json(page=page, page_size=page_size, platform=platform)
        if got is not None:
            return got
        from api.tasks import get_logs
        return get_logs(platform=platform or None, page=page, page_size=page_size)

    def _delete(self, ids):
        from core.db.task_log_repository import task_log_repository

        got = task_log_repository.delete_many_json(ids)
        if got is not None:
            return got
        from api.tasks import batch_delete_logs
        from api.tasks import TaskLogBatchDeleteRequest
        return batch_delete_logs(TaskLogBatchDeleteRequest(ids=ids))

    def test_add_then_list(self):
        for fixture in self._cases():
            email = f"contract-log-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.task_log_repository import task_log_repository

                self._add(email=email, detail={"a": 1})
                page = self._list()
                emails = [item["email"] for item in page["items"]]
                self.assertIn(email, emails, f"{fixture.name}: 写完查不到")
        self._cleanup_sqlite()

    def test_list_filters_by_platform(self):
        for fixture in self._cases():
            email = f"contract-log-plat-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.task_log_repository import task_log_repository

                self._add(email=email)
                page = self._list(platform="grok")
                emails = [item["email"] for item in page["items"]]
                self.assertNotIn(
                    email, emails, f"{fixture.name}: 平台过滤没生效"
                )
        self._cleanup_sqlite()

    def test_newest_first(self):
        for fixture in self._cases():
            prefix = f"contract-log-order-{fixture.name}"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.task_log_repository import task_log_repository

                for i in range(3):
                    self._add(email=f"{prefix}-{i}@x.c")
                page = self._list(page_size=100)
                mine = [it for it in page["items"] if prefix in str(it.get("email"))]
                ids = [it["id"] for it in mine]
                self.assertEqual(ids, sorted(ids, reverse=True),
                                 f"{fixture.name}: 不是倒序（新的应在前面）")
        self._cleanup_sqlite()

    def test_created_at_is_filled_when_caller_omits_it(self):
        """调用方不给 `created_at` 时要自动填上。

        实测的 bug：写路径传了 `{"created_at": None}`，而 `setdefault` 对
        **已存在的键**不生效 → 该字段永远是 None → 保留期清理一条都删不掉。
        """
        for fixture in self._cases():
            email = f"contract-log-ca-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                row = self._add(email=email)
                self.assertIsNotNone(
                    row.get("created_at"),
                    f"{fixture.name}: created_at 没被填上",
                )
        self._cleanup_sqlite()

    def test_delete_many_reports_not_found(self):
        for fixture in self._cases():
            email = f"contract-log-del-{fixture.name}@x.c"
            with self.subTest(backend=fixture.name), fixture:
                from core.db.task_log_repository import task_log_repository

                row = self._add(email=email)
                result = self._delete([row["id"]])
                self.assertEqual(result["deleted"], 1, f"{fixture.name}: 没删掉")
                self.assertEqual(result["not_found"], [], f"{fixture.name}: 误报未找到")

                again = self._delete([row["id"]])
                self.assertEqual(again["deleted"], 0)
                self.assertEqual(again["not_found"], [row["id"]],
                                 f"{fixture.name}: 已删的 id 没进 not_found")
        self._cleanup_sqlite()


if __name__ == "__main__":
    unittest.main()
