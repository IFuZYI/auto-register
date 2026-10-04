"""`POST /api/integrations/panels/{key}/sync` 的接线契约。

「同步到最新」= 把远端较新的凭证拉回本地。与「重新拉取对比」的区别：
后者只重拉对比表，前者会**改本地账号**。所以端点行为要有测试钉住：
- 正常路径：pulled/skipped 计数透传；
- 远端读取失败：返回 remote_error、不写任何账号；
- 未知面板：404。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class SyncEndpointTests(unittest.TestCase):
    def _client(self):
        import main as main_mod
        from fastapi.testclient import TestClient

        return TestClient(main_mod.app)

    def test_sync_endpoint_pulls_and_reports(self):
        from services.panel_comparison import RemoteAccount

        local_rows = [{
            "id": 1, "email": "a@x.com", "platform": "grok",
            "updated_at": (datetime(2026, 10, 1, tzinfo=timezone.utc)).isoformat(),
            "extra": {"access_token": "old-at", "refresh_token": "old-rt"},
        }]
        remote = [RemoteAccount(
            email="a@x.com", platform="grok",
            updated_at=datetime(2026, 10, 4, tzinfo=timezone.utc),
            credentials={"access_token": "new-at", "refresh_token": "new-rt"},
        )]

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, remote, ""),
        ), mock.patch(
            "services.panel_sync._persist_local",
        ) as persist:
            with self._client() as client:
                r = client.post("/api/integrations/panels/grok2api/sync")
        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertEqual(body["panel"], "grok2api")
        self.assertEqual(body["pulled"], 1)
        self.assertEqual(body["skipped"], 0)
        self.assertEqual(body["remote_error"], "")
        persist.assert_called_once()

    def test_sync_endpoint_reports_remote_error_without_writing(self):
        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=([], [], "面板地址未配置，无法读取远端账号"),
        ), mock.patch(
            "services.panel_sync._persist_local",
        ) as persist:
            with self._client() as client:
                r = client.post("/api/integrations/panels/grok2api/sync")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["pulled"], 0)
        self.assertIn("面板地址未配置", body["remote_error"])
        persist.assert_not_called()

    def test_sync_endpoint_404_for_unknown_panel(self):
        with self._client() as client:
            r = client.post("/api/integrations/panels/nonexistent/sync")
        self.assertEqual(r.status_code, 404)

    def test_sync_endpoint_normalizes_legacy_key(self):
        """旧 key `cliproxyapi` 归一成 `cpa`（与对比端点同口径）。"""
        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=([], [], "面板地址未配置，无法读取远端账号"),
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/cliproxyapi/sync")
        self.assertEqual(r.status_code, 200, r.text[:200])
        self.assertEqual(r.json()["panel"], "cpa")


if __name__ == "__main__":
    unittest.main()
