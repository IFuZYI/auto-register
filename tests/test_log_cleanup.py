"""日志清理：按保留期清理任务历史（task_logs）与任务运行记录（task_runs）。

用户要求（2026-10-09）：「增加日志清理功能，比如清理1天/周/月前的日志。」

语义（两处共用一套保留期规则）：
- 保留 N 天 = 删除 `created_at` 早于「现在 - N 天」的记录（`<` 比较）；
- 任务运行记录只清已结束（done / failed / stopped）的行 —— 进行中的任务
  绝不能因为「建得早」被删掉；清理前先收尾孤儿（崩溃留下的 running/pending），
  否则它们永远不是终态、会被这条清理永久略过。

TDD 说明：本文件先于实现写完（RED），实现后必须全绿；随后对前端接线做
变异验证（改坏端点字符串 / 去掉 lib 调用，都必须让测试变红）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from core.db import TaskLog, TaskRunModel, engine

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
HARNESS = FRONTEND / "scripts" / "run_log_cleanup_checks.mjs"


def _client() -> TestClient:
    from api.tasks import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class TaskLogCleanupEndpointTests(unittest.TestCase):
    """任务历史（task_logs）：`POST /tasks/logs/cleanup`。"""

    def setUp(self):
        self.client = _client()
        with Session(engine) as s:
            s.exec(delete(TaskLog))
            s.commit()

    def _seed(self, email: str, *, age_days: float) -> None:
        with Session(engine) as s:
            s.add(
                TaskLog(
                    platform="chatgpt",
                    email=email,
                    status="success",
                    created_at=datetime.now(UTC) - timedelta(days=age_days),
                )
            )
            s.commit()

    def _emails(self) -> set[str]:
        with Session(engine) as s:
            return {row.email for row in s.exec(select(TaskLog)).all()}

    def test_deletes_records_older_than_retention(self):
        self._seed("old-2d@example.com", age_days=2)
        self._seed("old-3d@example.com", age_days=3)
        self._seed("fresh-1h@example.com", age_days=1 / 24)

        response = self.client.post("/tasks/logs/cleanup", json={"older_than_days": 1})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["deleted"], 2)
        self.assertEqual(self._emails(), {"fresh-1h@example.com"})
        cutoff = datetime.fromisoformat(body["cutoff"])
        expected = datetime.now(UTC) - timedelta(days=1)
        self.assertLess(abs((cutoff - expected).total_seconds()), 60)

    def test_keeps_everything_within_retention(self):
        self._seed("fresh-2h@example.com", age_days=2 / 24)

        response = self.client.post("/tasks/logs/cleanup", json={"older_than_days": 1})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 0)
        self.assertEqual(self._emails(), {"fresh-2h@example.com"})


class TaskRunCleanupEndpointTests(unittest.TestCase):
    """任务运行记录（task_runs）：同一保留期，但只清已结束的任务。"""

    LIVE_ID = "live-running-task"
    LIVE_PENDING_ID = "live-pending-task"

    def setUp(self):
        self.client = _client()
        self._purge()

    def tearDown(self):
        self._purge()

    def _purge(self):
        from api.tasks import _task_store

        with _task_store._lock:
            for task_id in (self.LIVE_ID, self.LIVE_PENDING_ID):
                _task_store._records.pop(task_id, None)
        with Session(engine) as s:
            s.exec(delete(TaskRunModel))
            s.commit()

    def _seed(self, task_id: str, *, status: str, age_days: float) -> None:
        with Session(engine) as s:
            s.add(
                TaskRunModel(
                    id=task_id,
                    platform="chatgpt",
                    status=status,
                    created_at=datetime.now(UTC) - timedelta(days=age_days),
                )
            )
            s.commit()

    def _ids(self) -> set[str]:
        with Session(engine) as s:
            return {row.id for row in s.exec(select(TaskRunModel)).all()}

    def test_deletes_only_finished_runs_older_than_retention(self):
        self._seed("old-done", status="done", age_days=10)
        self._seed("old-stopped", status="stopped", age_days=8)
        self._seed("old-failed", status="failed", age_days=9)
        self._seed("fresh-done", status="done", age_days=1 / 24)

        response = self.client.post("/tasks/runs/cleanup", json={"older_than_days": 7})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 3)
        self.assertEqual(self._ids(), {"fresh-done"})

    def test_never_deletes_a_live_task(self):
        """跑着的任务无论建得多早都不能删 —— 删了它下一次持久化又会写回来。"""
        from api.tasks import _task_store

        self._seed(self.LIVE_ID, status="running", age_days=30)
        self._seed(self.LIVE_PENDING_ID, status="pending", age_days=30)
        _task_store.create(self.LIVE_ID, platform="chatgpt", total=1, source="manual")
        _task_store.create(self.LIVE_PENDING_ID, platform="chatgpt", total=1, source="manual")

        response = self.client.post("/tasks/runs/cleanup", json={"older_than_days": 7})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 0)
        self.assertEqual(self._ids(), {self.LIVE_ID, self.LIVE_PENDING_ID})

    def test_orphaned_run_is_finalized_then_cleaned(self):
        """崩溃留下的孤儿（库里 running、内存里没有）先收尾成 stopped，再按保留期清掉。"""
        self._seed("orphan-running", status="running", age_days=30)

        response = self.client.post("/tasks/runs/cleanup", json={"older_than_days": 7})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted"], 1)
        self.assertEqual(self._ids(), set())


class LogCleanupValidationTests(unittest.TestCase):
    """保留期参数边界：下界 1 天（0 会清空全部历史），上界拦住误填的巨数。"""

    def setUp(self):
        self.client = _client()

    def test_zero_days_is_rejected(self):
        response = self.client.post("/tasks/logs/cleanup", json={"older_than_days": 0})
        self.assertEqual(response.status_code, 422)

    def test_absurd_retention_is_rejected(self):
        response = self.client.post("/tasks/runs/cleanup", json={"older_than_days": 10**9})
        self.assertEqual(response.status_code, 422)

    def test_missing_retention_is_rejected(self):
        response = self.client.post("/tasks/logs/cleanup", json={})
        self.assertEqual(response.status_code, 422)


class LogCleanupFrontendTests(unittest.TestCase):
    """前端接线：保留期选项与端点来自共用模块，两个页面各自接上清理入口。

    前端没有单元测试运行器（仓库约定），所以分两层守：
    ① `run_log_cleanup_checks.mjs` 用 rolldown 打包后**真实执行** `lib/logCleanup.ts`；
    ② 页面接线用源码断言（与 `test_task_kinds_and_visibility.py` 同一模式）。
    """

    def _src(self, rel: str) -> str:
        return (FRONTEND / "src" / rel).read_text(encoding="utf-8")

    def test_retention_options_behave_as_documented(self):
        if shutil.which("node") is None:
            self.skipTest("未安装 node —— 跳过前端纯函数行为检查")
        if not (FRONTEND / "node_modules" / "rolldown").exists():
            self.skipTest("frontend/node_modules/rolldown 不存在（未 npm install）")

        result = subprocess.run(
            ["node", str(HARNESS)],
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertTrue(result.stdout.strip(), f"harness 没有输出：{result.stderr[-500:]}")
        report = json.loads(result.stdout)
        self.assertTrue(
            report["passed"],
            "logCleanup.ts 行为不符：\n" + "\n".join(report["failures"]),
        )
        self.assertGreaterEqual(report["checked"], 8, "断言条数太少，网太稀")

    def test_task_history_page_wires_cleanup(self):
        src = self._src("pages/TaskHistory.tsx")
        self.assertIn("logCleanup", src, "任务历史页没有接保留期清理")
        self.assertIn("CLEANUP_ENDPOINTS.logs", src, "清理端点没走共用定义（会与运行页分叉）")
        self.assertIn("RETENTION_OPTIONS", src, "清理入口没有保留期选项")

    def test_running_tasks_page_wires_cleanup(self):
        src = self._src("pages/RunningTasks.tsx")
        self.assertIn("logCleanup", src, "任务运行页没有接保留期清理")
        self.assertIn("CLEANUP_ENDPOINTS.runs", src, "清理端点没走共用定义")


if __name__ == "__main__":
    unittest.main()
