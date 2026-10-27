"""通用批量动作任务化：一个机制覆盖上传 / 状态同步 / Plus 检测。

用户要求「整个项目大部分功能都统一基于任务」。

## 现状

已经是任务的：`refresh_token` / `backfill_refresh_token` / `bind_2fa` /
`probe_status`。

仍是**同步批量**（会挂住请求）的：
- 4 个上传动作（`upload_cpa` / `upload_sub2api` / `upload_chatgpt2api` /
  `upload_grok2api`）—— 一次可推几百个账号；
- 4 个面板状态同步（`sync_*_status`）；
- `check_plus_trial`。

## 设计：一个通用入口，而不是再加 9 个专用端点

`POST /api/tasks/batch-action` 接 `platform` + `action_id` + 范围筛选，
后台逐号跑 `execute_action` 并走 `_apply_action_result` 落库 —— 与单账号
按钮**同一条动作实现**。

这样新增动作时不需要再写任务端点，任务化是默认行为而不是逐个补齐。
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient


def _client():
    # 平台注册表要显式加载：`TestClient` 不走 lifespan 时 `/api/platforms`
    # 是空的，端点会报「平台未注册」而不是走到我们要测的校验分支
    # （实测：`test_unknown_action_is_rejected` 拿到 404 而非 400）。
    from core.registry import load_all

    load_all()
    import main

    return TestClient(main.app)


class GenericBatchActionTaskTests(unittest.TestCase):
    def test_endpoint_exists(self):
        client = _client()
        r = client.post(
            "/api/tasks/batch-action",
            json={"platform": "chatgpt", "action_id": "check_plus_trial",
                  "account_ids": [1]},
        )
        self.assertNotEqual(r.status_code, 405, "端点不存在")
        self.assertIn(r.status_code, (200, 400, 404), r.text[:200])

    def test_unknown_action_is_rejected(self):
        client = _client()
        r = client.post(
            "/api/tasks/batch-action",
            json={"platform": "chatgpt", "action_id": "no_such_action",
                  "account_ids": [1]},
        )
        self.assertEqual(r.status_code, 400, f"未知动作没被拒: {r.text[:200]}")

    def test_unknown_platform_is_rejected(self):
        client = _client()
        r = client.post(
            "/api/tasks/batch-action",
            json={"platform": "nope", "action_id": "check_plus_trial",
                  "account_ids": [1]},
        )
        self.assertEqual(r.status_code, 404, f"未知平台没被拒: {r.text[:200]}")

    def test_runner_is_registered(self):
        from services import task_runners

        self.assertTrue(
            hasattr(task_runners, "_run_batch_action"),
            "runner 不存在 —— 任务建了也没有执行体",
        )


class RunnerReusesTheSharedActionPathTests(unittest.TestCase):
    """结构性：runner 必须走 `execute_action` + `_apply_action_result`。

    各写一份的话状态策略（封禁/过期/失效判定）会漂移 —— 本仓已因
    「按钮路径与批量路径不一致」踩过（`probe` 的 `code == 200` vs
    `check_valid` 的 `_probe_verdict`）。
    """

    def _body(self) -> str:
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "services" / "task_runners.py").read_text(
            encoding="utf-8"
        )
        start = src.index("def _run_batch_action(")
        end = src.find("\ndef ", start + 10)
        return src[start:] if end == -1 else src[start:end]

    def test_uses_execute_action(self):
        self.assertIn("execute_action", self._body(),
                      "runner 没调用 execute_action —— 与按钮路径不是同一条实现")

    def test_applies_result_through_the_shared_helper(self):
        self.assertIn("_apply_action_result", self._body(),
                      "结果没走共享的状态落库路径")


if __name__ == "__main__":
    unittest.main()
