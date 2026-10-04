"""各面板「同步远端状态」：读远端状态写回本地。

用户要求：「每个平台都最好都有 同步远端状态、更新远端凭证、更新本地凭证。」

CPA 的版本在 `services/cliproxyapi_sync.py`（列表不带状态，靠 token 探活）。
其余三个面板的列表接口本身就带权威状态，读回来即可：

- grok2api：`authStatus` + `enabled`（fetcher 归一进 `extra.disabled`）
- sub2api：`status`（active / inactive / error）
- chatgpt2api：`status_label` / `status` + `disabled`（+ 有则读
  `credential_availability`）

**不做探活**：这三个面板自己跑 token 刷新，再用本地 token 打一遍会与面板
抢 RT（x.ai 的 RT 每次刷新轮换，互相作废 —— `services/panel_sync.py` 记录了
本地 22 个账号全部 `invalid_grant` 的事故）。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.panel_comparison import RemoteAccount  # noqa: E402
from services.panel_status_sync import (  # noqa: E402
    classify_remote_status,
    build_status_update,
)


def _remote(**kw) -> RemoteAccount:
    kw.setdefault("platform", "grok")
    return RemoteAccount(email="a@x.com", **kw)


class ClassifyRemoteStatusTests(unittest.TestCase):
    """远端状态 → 统一词表（active / disabled / invalid / error / not_found）。"""

    def test_missing_remote_is_not_found(self):
        self.assertEqual(classify_remote_status("grok2api", None), "not_found")

    def test_grok2api_active(self):
        r = _remote(status="active", extra={"disabled": False})
        self.assertEqual(classify_remote_status("grok2api", r), "active")

    def test_grok2api_disabled_flag(self):
        """fetcher 把 `enabled=False` 归一成 `extra.disabled`。"""
        r = _remote(status="active", extra={"disabled": True})
        self.assertEqual(classify_remote_status("grok2api", r), "disabled")

    def test_grok2api_disabled_status(self):
        r = _remote(status="disabled", extra={})
        self.assertEqual(classify_remote_status("grok2api", r), "disabled")

    def test_grok2api_expired_is_invalid(self):
        """`expired` = 凭证过期（要重新登录），与「被禁用」不是一回事。"""
        r = _remote(status="expired", extra={})
        self.assertEqual(classify_remote_status("grok2api", r), "invalid")

    def test_sub2api_inactive_is_disabled(self):
        r = _remote(platform="chatgpt", status="inactive")
        self.assertEqual(classify_remote_status("sub2api", r), "disabled")

    def test_sub2api_error(self):
        r = _remote(platform="chatgpt", status="error")
        self.assertEqual(classify_remote_status("sub2api", r), "error")

    def test_sub2api_active(self):
        r = _remote(platform="chatgpt", status="active")
        self.assertEqual(classify_remote_status("sub2api", r), "active")

    def test_chatgpt2api_unavailable_is_invalid(self):
        r = _remote(platform="chatgpt", status="", extra={"credential_availability": "unavailable"})
        self.assertEqual(classify_remote_status("chatgpt2api", r), "invalid")

    def test_chatgpt2api_disabled_flag(self):
        r = _remote(platform="chatgpt", status="已禁用", extra={"disabled": True})
        self.assertEqual(classify_remote_status("chatgpt2api", r), "disabled")

    def test_chatgpt2api_usable_is_active(self):
        r = _remote(platform="chatgpt", status="正常", extra={"credential_availability": "usable"})
        self.assertEqual(classify_remote_status("chatgpt2api", r), "active")

    def test_unknown_panel_is_unknown(self):
        self.assertEqual(classify_remote_status("nope", _remote()), "unknown")


class BuildStatusUpdateTests(unittest.TestCase):
    """写回本地的状态更新形状（sync_statuses.<panel> 的补丁）。"""

    def test_found_account_writes_remote_state(self):
        account = type("A", (), {"id": 7, "email": "a@x.com", "platform": "grok"})()
        r = _remote(status="active", extra={"disabled": False})
        update = build_status_update("grok2api", account, r)
        self.assertEqual(update["id"], 7)
        self.assertTrue(update["ok"])
        self.assertEqual(update["remote_state"], "active")
        self.assertIn("active", update["message"])
        patch = update["patch"]["sync_statuses"]["grok2api"]
        self.assertEqual(patch["remote_state"], "active")
        self.assertTrue(patch["last_synced_at"])

    def test_not_found_reports_clearly(self):
        account = type("A", (), {"id": 8, "email": "b@x.com", "platform": "grok"})()
        update = build_status_update("grok2api", account, None)
        self.assertFalse(update["ok"])
        self.assertEqual(update["remote_state"], "not_found")
        self.assertIn("未找到", update["message"])

    def test_disabled_is_not_ok(self):
        """远端明确禁用 → 不算成功（界面显示失败原因，用户能看到要处理）。"""
        account = type("A", (), {"id": 9, "email": "c@x.com", "platform": "grok"})()
        r = _remote(status="disabled", extra={"disabled": True})
        update = build_status_update("grok2api", account, r)
        self.assertFalse(update["ok"])
        self.assertEqual(update["remote_state"], "disabled")


if __name__ == "__main__":
    unittest.main()
