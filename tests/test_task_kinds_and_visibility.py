"""任务可见性：自动维护进入「任务运行」页 + 任务类型彩色标签。

用户要求（2026-10-07）：「刷新token等各种任务请也在任务运行里面显示，
方便查看日志。请为不同任务标注不同标签方便区分。」

背景：自动维护（自动刷新 Token / chatgpt2api 凭证同步）此前跑在调度线程里，
日志只打在服务器 stdout —— 「任务运行」页完全看不到它们，出了问题也没有
可回看的日志。现在两趟维护在**有实际动作**时创建任务记录（空轮不建，
避免每 5 分钟刷一条空任务）；前端任务类型从灰色小字升级为彩色标签。

TDD 说明：本文件先于实现写完（RED），实现后必须全绿；随后做变异验证
（去掉记录创建 / 空轮也建 / 去掉停止接线 / 标签映射改错，都必须让测试变红）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
HARNESS = FRONTEND / "scripts" / "run_task_kind_checks.mjs"

#: 自动维护创建的任务记录 source（与前端 taskKindMeta 的键同源）。
AUTO_SOURCES = ("auto_refresh", "chatgpt2api_sync")


def _purge_auto_records() -> None:
    """清掉自动维护的任务记录（内存 + 库）—— 测试间互不污染。"""
    from sqlmodel import Session, delete

    from api.tasks import _task_store
    from core.db import TaskRunModel, current_engine

    with _task_store._lock:
        for tid in [
            tid for tid, rec in _task_store._records.items() if rec.source in AUTO_SOURCES
        ]:
            _task_store._records.pop(tid, None)
    with Session(current_engine()) as s:
        s.exec(delete(TaskRunModel).where(TaskRunModel.source.in_(AUTO_SOURCES)))
        s.commit()


def _auto_records() -> list[dict]:
    from api.tasks import _task_store

    return [
        snap for snap in _task_store.list_snapshots() if snap.get("source") in AUTO_SOURCES
    ]


class Chatgpt2apiSyncTaskRecordTests(unittest.TestCase):
    """chatgpt2api 凭证同步：有推送/失败/清理动作 → 建记录；纯空转不建。"""

    def setUp(self):
        _purge_auto_records()

    def tearDown(self):
        _purge_auto_records()

    def test_push_with_actions_creates_visible_record(self):
        from services.chatgpt_maintenance import run_chatgpt2api_auto_sync

        fake = {
            "panel": "chatgpt2api", "total": 3, "pushed": 1, "failed": 0,
            "skipped": 2, "deleted": 1, "remote_error": "",
            "items": [
                {"email": "push-ok@example.com", "platform": "chatgpt",
                 "push": True, "pushed": True, "message": "上传成功"},
            ],
        }
        summary = run_chatgpt2api_auto_sync(push=lambda: fake)
        self.assertEqual(summary, fake)

        records = _auto_records()
        self.assertEqual(len(records), 1, "有实际推送动作必须创建任务记录")
        rec = records[0]
        self.assertEqual(rec["source"], "chatgpt2api_sync")
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["success"], 1)
        joined = "\n".join(rec["logs"])
        self.assertIn("推送 1", joined)
        self.assertIn("push-ok@example.com", joined, "逐账号结果要进日志")

        from services.task_store_io import _list_persisted_tasks

        persisted = [t for t in _list_persisted_tasks() if t["id"] == rec["id"]]
        self.assertEqual(len(persisted), 1, "任务记录必须落库")

    def test_no_action_pass_creates_no_record(self):
        from services.chatgpt_maintenance import run_chatgpt2api_auto_sync

        fake = {
            "panel": "chatgpt2api", "total": 2, "pushed": 0, "failed": 0,
            "skipped": 2, "deleted": 0, "remote_error": "", "items": [],
        }
        run_chatgpt2api_auto_sync(push=lambda: fake)
        self.assertEqual(_auto_records(), [], "纯空转不得创建任务记录")

    def test_remote_error_pass_creates_no_record(self):
        """远端读取失败本轮跳过 —— 不建记录（调度器下一轮会重试）。"""
        from services.chatgpt_maintenance import run_chatgpt2api_auto_sync

        fake = {
            "panel": "chatgpt2api", "total": 0, "pushed": 0, "failed": 0,
            "skipped": 0, "deleted": 0, "remote_error": "面板地址未配置", "items": [],
        }
        run_chatgpt2api_auto_sync(push=lambda: fake)
        self.assertEqual(_auto_records(), [])


class TaskKindHarnessTests(unittest.TestCase):
    """任务类型标签：真实执行 `lib/taskKinds.ts` 的纯函数。"""

    def test_kind_meta_behaves_as_documented(self):
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
            "taskKindMeta 行为不符：\n" + "\n".join(report["failures"]),
        )
        self.assertGreaterEqual(report["checked"], 8, "断言条数太少，网太稀")


class TaskKindFrontendWiringTests(unittest.TestCase):
    """接线：任务运行页用彩色标签、日志面板认识新类型、新颜色钉住对比度。"""

    def _src(self, rel: str) -> str:
        return (FRONTEND / "src" / rel).read_text(encoding="utf-8")

    def test_running_tasks_uses_colored_kind_tags(self):
        src = self._src("pages/RunningTasks.tsx")
        self.assertIn("taskKindMeta", src, "任务运行页没有接 taskKindMeta")
        self.assertNotIn(
            "SOURCE_LABELS", src,
            "旧的灰色小字标签表还在 —— 用户要求彩色标签区分",
        )

    def test_task_log_panel_knows_the_new_kinds(self):
        src = self._src("components/TaskLogPanel.tsx")
        for kind in ("auto_refresh", "chatgpt2api_sync"):
            self.assertIn(kind, src, f"TaskLogPanel 不认识任务类型 {kind}")

    def test_new_tag_colors_are_pinned_for_contrast(self):
        css = self._src("index.css")
        for cls in (".ant-tag.ant-tag-cyan", ".ant-tag.ant-tag-magenta"):
            self.assertIn(cls, css, f"{cls} 没被钉住 —— preset 派生值不过 AA")
        theme = self._src("theme.ts")
        for key in ("cyan:", "cyanSoft:", "magenta:", "magentaSoft:"):
            self.assertGreaterEqual(
                theme.count(key), 2, f"两套主题都要定义 {key}"
            )


if __name__ == "__main__":
    unittest.main()
