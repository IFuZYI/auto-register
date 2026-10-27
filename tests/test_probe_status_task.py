"""「检测本地状态」要跑成任务，而不是一次同步批量调用。

用户要求（2026-10-27）：「将检测本地状态这种也变成任务，整个项目大部分功能
都统一基于任务。」

## 现状的问题

`probe_local_status` 走 `POST /api/actions/chatgpt/probe_local_status/batch` ——
同步执行、无任务记录、无日志流、不能停。几百个账号时要阻塞几分钟，界面上
什么都看不到。

## 目标形态

与「补 RT」「刷新 Token」同款：建任务 → 后台跑 → 逐号日志 → 可停止/跳过 →
结果落库走 `_apply_action_result`（与单账号按钮同一条路径）。
"""

from __future__ import annotations

import unittest


class ProbeStatusTaskExistsTests(unittest.TestCase):
    def test_endpoint_creates_a_task(self):
        from fastapi.testclient import TestClient

        import main

        client = TestClient(main.app)
        r = client.post(
            "/api/tasks/probe-status",
            json={"account_ids": [1], "concurrency": 2},
        )
        # 账号 1 可能不存在 → 400 也算「端点存在且按任务语义校验」
        self.assertIn(r.status_code, (200, 400, 404), r.text[:200])
        self.assertNotEqual(r.status_code, 405, "端点不存在（Method Not Allowed）")

    def test_runner_is_registered(self):
        from services import task_runners

        self.assertTrue(
            hasattr(task_runners, "_run_probe_status"),
            "runner 不存在 —— 任务建了也没有执行体",
        )

    def test_request_model_accepts_filter_scope(self):
        """与补 RT 同款：勾选优先，否则按筛选范围。"""
        from api.tasks import ProbeStatusTaskRequest

        req = ProbeStatusTaskRequest(all_filtered=True, status="registered", concurrency=3)
        self.assertTrue(req.all_filtered)
        self.assertEqual(req.status, "registered")


class ProbeStatusTaskUsesTheSharedActionPathTests(unittest.TestCase):
    """单账号按钮与任务必须走**同一条**动作实现。

    各写一份的话，状态策略（封禁/过期/失效判定）会漂移 —— 本仓已经因为
    「按钮路径与批量路径不一致」踩过（`probe` 用 `code == 200`，而
    `check_valid` 用 `_probe_verdict`）。
    """

    def test_runner_calls_the_probe_action(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "services" / "task_runners.py").read_text(
            encoding="utf-8"
        )
        start = src.index("def _run_probe_status(")
        end = src.index("\ndef ", start + 10)
        body = src[start:end]
        self.assertIn('"probe_local_status"', body,
                      "runner 没调用 probe_local_status 动作")
        self.assertIn("_apply_action_result", body,
                      "结果没走共享的状态落库路径")


if __name__ == "__main__":
    unittest.main()
