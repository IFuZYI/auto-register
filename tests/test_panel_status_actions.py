"""「同步远端状态」在三个非 CPA 面板上的接线。

用户要求：「每个平台都最好都有 同步远端状态、更新远端凭证、更新本地凭证。」

CPA 已有 `sync_cliproxyapi_status`（token 探活）。其余三个面板的列表接口
自带权威状态，新增动作：

- grok2api → `sync_grok2api_status`（grok 平台）
- sub2api → `sync_sub2api_status`（chatgpt 平台）
- chatgpt2api → `sync_chatgpt2api_status`（chatgpt 平台）

批量端点走专用分支（一次拉远端列表写回所有账号 —— 逐账号拉会 N 次打远端）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.panel_registry import PANELS_BY_KEY  # noqa: E402


class PanelStatusActionDeclarationTests(unittest.TestCase):
    """三个面板都要声明「同步远端状态」动作。"""

    def test_sub2api_declares_sync_action(self):
        self.assertEqual(
            PANELS_BY_KEY["sub2api"].get("sync_action"), "sync_sub2api_status"
        )

    def test_grok2api_declares_sync_action(self):
        self.assertEqual(
            PANELS_BY_KEY["grok2api"].get("sync_action"), "sync_grok2api_status"
        )

    def test_chatgpt2api_declares_sync_action(self):
        self.assertEqual(
            PANELS_BY_KEY["chatgpt2api"].get("sync_action"), "sync_chatgpt2api_status"
        )

    def test_declared_sync_actions_exist_on_platforms(self):
        """声明的动作 id 必须在对应平台上真的声明了（否则按钮点了报未知操作）。"""
        from core.registry import get, load_all

        load_all()
        cases = [
            ("grok", "sync_grok2api_status"),
            ("chatgpt", "sync_sub2api_status"),
            ("chatgpt", "sync_chatgpt2api_status"),
        ]
        for platform, action_id in cases:
            with self.subTest(platform=platform, action=action_id):
                instance = get(platform)(config=None)
                available = {a.get("id") for a in instance.get_platform_actions()}
                self.assertIn(action_id, available)

    def test_status_actions_are_panel_scoped(self):
        """动作属于面板管理页 —— 不该出现在账号页菜单。"""
        from core.registry import get, load_all

        load_all()
        cases = [
            ("grok", "sync_grok2api_status"),
            ("chatgpt", "sync_sub2api_status"),
            ("chatgpt", "sync_chatgpt2api_status"),
        ]
        for platform, action_id in cases:
            with self.subTest(platform=platform, action=action_id):
                instance = get(platform)(config=None)
                actions = {a["id"]: a for a in instance.get_platform_actions()}
                self.assertEqual(actions[action_id].get("scope"), "panel")


class PanelStatusBatchEndpointTests(unittest.TestCase):
    """批量端点：一次拉远端列表，写回各账号的 sync_statuses。"""

    def _client(self):
        import main as main_mod
        from fastapi.testclient import TestClient

        return TestClient(main_mod.app)

    def _make_grok_account(self, email: str):
        from core.base_platform import Account, AccountStatus
        from core.db import account_repository

        return account_repository.upsert(Account(
            platform="grok", email=email, password="p",
            status=AccountStatus.REGISTERED, extra={},
        ))

    def test_batch_writes_remote_state(self):
        from services.panel_comparison import RemoteAccount

        row = self._make_grok_account("status-sync-1@x.com")
        remote = [RemoteAccount(
            email=row.email, platform="grok", remote_id="r-1",
            status="active", extra={"disabled": False},
        )]
        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=([], remote, ""),
        ):
            with self._client() as client:
                r = client.post(
                    "/api/actions/grok/sync_grok2api_status/batch",
                    json={"account_ids": [row.id]},
                )
        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["success"], 1)

        from core.db import account_repository

        fresh = account_repository.find_by_email("grok", row.email)
        state = (fresh.get_extra().get("sync_statuses") or {}).get("grok2api") or {}
        self.assertEqual(state.get("remote_state"), "active")

    def test_batch_not_found_is_failure_with_reason(self):
        row = self._make_grok_account("status-sync-2@x.com")
        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=([], [], ""),
        ):
            with self._client() as client:
                r = client.post(
                    "/api/actions/grok/sync_grok2api_status/batch",
                    json={"account_ids": [row.id]},
                )
        body = r.json()
        self.assertEqual(body["failed"], 1)
        self.assertIn("未找到", body["items"][0]["message"])

        from core.db import account_repository

        fresh = account_repository.find_by_email("grok", row.email)
        state = (fresh.get_extra().get("sync_statuses") or {}).get("grok2api") or {}
        self.assertEqual(state.get("remote_state"), "not_found")

    def test_batch_remote_unreachable_reports_cleanly(self):
        row = self._make_grok_account("status-sync-3@x.com")
        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=([], [], "面板地址未配置，无法读取远端账号"),
        ):
            with self._client() as client:
                r = client.post(
                    "/api/actions/grok/sync_grok2api_status/batch",
                    json={"account_ids": [row.id]},
                )
        body = r.json()
        self.assertEqual(body["failed"], 1)
        self.assertIn("面板地址未配置", body["items"][0]["message"])


if __name__ == "__main__":
    unittest.main()
