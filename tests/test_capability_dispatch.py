"""按能力分派（T2.2）必须与按动作 id 分派**行为一致**。

`_apply_action_result` 原来逐个比较动作 id：

    if action_id == "probe_local_status": ...
    elif action_id == "sync_cliproxyapi_status": ...

合并成「按能力」之后，`check_plus_trial`（同属 PROBE）和
`sync_sub2api_status` / `sync_chatgpt2api_status`（同属 SYNC_STATUS）也会走进
对应分支。它们的结果里没有 `probe` / `sync` 键，所以策略函数收到 `None`。

这个文件先把「现状」钉死：这些动作**现在不改状态**，合并后也不许改。
`classify_*_state(None)` 返回空串、`apply_*_policy` 因此不落库 —— 这条链路
一旦被改成「收到 None 也落 invalid」，这里立刻红。
"""

from __future__ import annotations

import unittest
from unittest import mock

from core.db.models_account import AccountModel


def _model(platform: str = "chatgpt", status: str = "registered") -> AccountModel:
    return AccountModel(
        platform=platform, email="dispatch@example.com", password="p", status=status
    )


class NoOpActionsStayNoOpTests(unittest.TestCase):
    """结果里没有对应键的动作，不得因为「同能力」而被落状态。"""

    def _apply(self, platform, action_id, result, status="registered"):
        from api.actions import _apply_action_result

        model = _model(platform, status=status)
        _apply_action_result(platform, action_id, model, result, mock.Mock())
        return model.status

    def test_check_plus_trial_does_not_touch_status(self):
        status = self._apply(
            "chatgpt",
            "check_plus_trial",
            {"ok": True, "data": {"plus": {"plan": "plus"}}},
        )
        self.assertEqual(status, "registered")

    def test_sync_sub2api_status_does_not_touch_status(self):
        status = self._apply(
            "chatgpt",
            "sync_sub2api_status",
            {"ok": True, "data": {"message": "已同步"}},
        )
        self.assertEqual(status, "registered")

    def test_sync_chatgpt2api_status_does_not_touch_status(self):
        status = self._apply(
            "chatgpt",
            "sync_chatgpt2api_status",
            {"ok": True, "data": {"message": "已同步"}},
        )
        self.assertEqual(status, "registered")

    def test_banned_is_not_cleared_by_a_noop_sync(self):
        """禁用是粘性的：一次没有 sync 数据的同步不得把它洗回 registered。"""
        status = self._apply(
            "chatgpt",
            "sync_sub2api_status",
            {"ok": True, "data": {"message": "已同步"}},
            status="banned",
        )
        self.assertEqual(status, "banned")


class SignalBearingActionsStillWorkTests(unittest.TestCase):
    """带信号的同能力动作必须照旧落状态（合并不能把它们一起变成 no-op）。"""

    def _apply(self, platform, action_id, result, status="registered"):
        from api.actions import _apply_action_result

        model = _model(platform, status=status)
        _apply_action_result(platform, action_id, model, result, mock.Mock())
        return model.status

    def test_probe_local_status_still_marks_invalid(self):
        status = self._apply(
            "chatgpt",
            "probe_local_status",
            {
                "ok": False,
                "data": {
                    "probe": {
                        "auth": {"state": "unauthorized", "http_status": 401},
                    }
                },
            },
        )
        self.assertEqual(status, "invalid")

    def test_sync_cliproxyapi_status_still_marks_invalid(self):
        status = self._apply(
            "chatgpt",
            "sync_cliproxyapi_status",
            {
                "ok": False,
                "data": {"sync": {"remote_state": "unauthorized", "message": "远端失效"}},
            },
        )
        self.assertEqual(status, "invalid")


if __name__ == "__main__":
    unittest.main()
