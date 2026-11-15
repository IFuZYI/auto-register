"""`/actions/{platform}/{action_id}/batch` 必须拒绝平台没声明的动作。

为什么这条重要（审计发现，2026-11-14）：`/tasks/batch-action` 有白名单校验
（`api/tasks.py`），而同一语义的批量端点 `/actions/{platform}/{action_id}/batch`
直接进执行循环。未知 action 在 `_execute_platform_action` 里会抛错，再被
逐条的 `except` 吞成 item 失败 —— 于是接口返回 **HTTP 200**、`failed=total`，
前端显示「部分失败」，看起来像上游的问题。对着几百个账号跑一个不存在的
动作，既浪费上游请求配额，也把「我们发错动作」伪装成「账号有问题」。
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient


def _client() -> TestClient:
    # 平台靠插件模块的装饰器注册（`core/registry`），导入插件才会填充 ——
    # 否则 `get(platform)` 抛 KeyError，测到的是「平台未注册」404，
    # 而不是我们真正关心的动作白名单。
    import platforms.grok.plugin  # noqa: F401
    from main import app

    return TestClient(app)


class BatchActionValidationTests(unittest.TestCase):
    def test_unknown_action_is_rejected_before_touching_accounts(self):
        client = _client()
        response = client.post(
            "/api/actions/grok/no_such_action/batch",
            json={"account_ids": ["whatever"]},
        )
        self.assertEqual(
            response.status_code, 400,
            f"未知动作应 400 拒绝，实际 {response.status_code}: {response.text[:160]}",
        )
        detail = str(response.json().get("detail") or "")
        self.assertIn("no_such_action", detail, "拒绝信息要指出是哪个动作不被支持")
        self.assertIn("可用", detail, "拒绝信息要列出该平台真正可用的动作")

    def test_known_action_is_not_rejected_by_the_guard(self):
        """真正存在的动作不能被这道校验误伤（guard 只挡未知 id）。

        这里不选账号，所以会撞上「请提供 account_ids」的参数校验 —— 那也是
        400，但**不是**白名单拒绝。按 detail 区分，而不是只比状态码。
        """
        client = _client()
        response = client.post(
            "/api/actions/grok/probe_refresh/batch",
            json={"account_ids": []},
        )
        detail = str(response.json().get("detail") or "")
        self.assertNotIn(
            "不支持操作", detail,
            f"合法动作被白名单拒了: {response.text[:160]}",
        )

    def test_unregistered_platform_still_404(self):
        client = _client()
        response = client.post(
            "/api/actions/no_such_platform/probe/batch",
            json={"account_ids": []},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()