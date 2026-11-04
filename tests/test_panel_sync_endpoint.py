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
            # 已登记到 grok2api —— 只有登记的账号参与拉回（ADR-0002）
            "extra": {
                "access_token": "old-at", "refresh_token": "old-rt",
                "panel_uploads": {"grok2api": {"at": "2026-10-01T00:00:00+00:00"}},
            },
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


class SyncEndpointPlatformScopeTests(unittest.TestCase):
    """`POST /sync?platform=grok` 只拉该平台的行。

    「同步到最新」是**面板级**调用（一次拉全量），而平台筛选只作用在前端 ——
    用户筛了 Grok 再点同步，后端不筛的话会把 ChatGPT 的凭证也一起拉回本地，
    「看到的」和「被改的」对不上。前端把当前筛选的平台作为查询参数传下来。
    """

    def _client(self):
        import main as main_mod
        from fastapi.testclient import TestClient

        return TestClient(main_mod.app)

    def test_platform_param_scopes_the_pull(self):
        from services.panel_comparison import RemoteAccount

        def _row(account_id: int, email: str, platform: str, old: str):
            return {
                "id": account_id, "email": email, "platform": platform,
                "updated_at": (datetime(2026, 10, 1, tzinfo=timezone.utc)).isoformat(),
                "extra": {
                    "access_token": old, "refresh_token": old + "-rt",
                    "panel_uploads": {"cpa": {"at": "2026-10-01T00:00:00+00:00"}},
                },
            }

        local_rows = [
            _row(1, "g@x.com", "grok", "old-g"),
            _row(2, "c@x.com", "chatgpt", "old-c"),
        ]
        remote = [
            RemoteAccount(
                email="g@x.com", platform="grok",
                updated_at=datetime(2026, 10, 4, tzinfo=timezone.utc),
                credentials={"access_token": "new-g", "refresh_token": "new-g-rt"},
            ),
            RemoteAccount(
                email="c@x.com", platform="chatgpt",
                updated_at=datetime(2026, 10, 4, tzinfo=timezone.utc),
                credentials={"access_token": "new-c", "refresh_token": "new-c-rt"},
            ),
        ]

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, remote, ""),
        ), mock.patch("services.panel_sync._persist_local") as persist:
            with self._client() as client:
                r = client.post("/api/integrations/panels/cpa/sync?platform=grok")

        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertEqual(body["total"], 1, "只应处理 grok 的那一行")
        self.assertEqual(body["pulled"], 1)
        pulled_email = body["items"][0]["email"]
        self.assertEqual(pulled_email, "g@x.com")
        persist.assert_called_once()

    def test_no_platform_param_still_syncs_everything(self):
        """不带参数 = 全量（老行为不变，脚本/旧前端照常可用）。"""
        from services.panel_comparison import RemoteAccount

        local_rows = [{
            "id": 1, "email": "g@x.com", "platform": "grok",
            "updated_at": (datetime(2026, 10, 1, tzinfo=timezone.utc)).isoformat(),
            "extra": {
                "access_token": "old-g", "refresh_token": "old-g-rt",
                "panel_uploads": {"cpa": {"at": "2026-10-01T00:00:00+00:00"}},
            },
        }]
        remote = [RemoteAccount(
            email="g@x.com", platform="grok",
            updated_at=datetime(2026, 10, 4, tzinfo=timezone.utc),
            credentials={"access_token": "new-g", "refresh_token": "new-g-rt"},
        )]
        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, remote, ""),
        ), mock.patch("services.panel_sync._persist_local"):
            with self._client() as client:
                r = client.post("/api/integrations/panels/cpa/sync")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["pulled"], 1)


if __name__ == "__main__":
    unittest.main()


class EmptyEnrollmentReasonTests(unittest.TestCase):
    """一个账号都没登记时，拉回必须说明原因（panels 审计 F-06）。

    原来返回 `{pulled: 0, skipped: N}`，界面上就是「点了没反应」——
    生产库实测 CPA 0/667 登记。
    """

    def _client(self):
        import main as main_mod
        from fastapi.testclient import TestClient

        return TestClient(main_mod.app)

    def test_reason_is_returned_when_nothing_is_enrolled(self):
        local_rows = [
            {
                "id": 1,
                "email": "a@x.com",
                "platform": "chatgpt",
                "updated_at": "2026-10-01T00:00:00+00:00",
                "extra": {"access_token": "at"},  # 没有 panel_uploads → 未登记
            },
            {
                "id": 2,
                "email": "b@x.com",
                "platform": "chatgpt",
                "updated_at": "2026-10-01T00:00:00+00:00",
                "extra": {},
            },
        ]
        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, [], ""),
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/cpa/sync")
        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertEqual(body["enrolled"], 0)
        self.assertEqual(body["candidates"], 2)
        self.assertIn("没有任何账号登记到", body.get("reason", ""))

    def test_no_reason_when_something_is_enrolled(self):
        local_rows = [
            {
                "id": 1,
                "email": "a@x.com",
                "platform": "chatgpt",
                "updated_at": "2026-10-01T00:00:00+00:00",
                "extra": {"panel_uploads": {"cpa": {"at": "2026-10-01T00:00:00+00:00"}}},
            }
        ]
        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, [], ""),
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/cpa/sync")
        body = r.json()
        self.assertEqual(body["enrolled"], 1)
        self.assertNotIn("reason", body)


class SyncHonoursTheSelectedAccountIdsTests(unittest.TestCase):
    """「更新本地凭证」也必须只动界面筛出来的行。

    2026-11-04 独立审查（切片 G）：`sync_panel_endpoint` 的签名**声明了**
    `account_ids: str = ""`，函数体里却一次都没引用 —— 这种「声明了但没用」
    比「没声明」更隐蔽：`inspect.signature` 看起来是对的，定位问题时会被它骗过去。
    """

    def _fake(self):
        from services.panel_comparison import RemoteAccount
        from datetime import datetime, timezone

        local_rows = [
            {"id": i, "email": f"u{i}@x.com", "platform": "grok",
             "updated_at": "2026-10-04T10:00:00+00:00", "extra": {"sso": f"sso-{i}"}}
            for i in (1, 2, 3)
        ]
        remote = [
            RemoteAccount(email=f"u{i}@x.com", platform="grok", remote_id=f"r-{i}",
                          updated_at=datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
                          credentials={"sso_token": f"new-{i}"})
            for i in (1, 2, 3)
        ]
        return local_rows, remote

    def _call(self, query: str):
        import main as main_mod
        from fastapi.testclient import TestClient
        from unittest import mock

        local_rows, remote = self._fake()
        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, remote, ""),
        ), mock.patch(
            "services.panel_sync.sync_local_from_remote",
            side_effect=lambda rows, *a, **k: {"total": len(rows), "pulled": 0,
                                               "skipped": len(rows), "items": []},
        ) as spy:
            with TestClient(main_mod.app) as client:
                r = client.post(f"/api/integrations/panels/grok2api/sync{query}")
        return r, spy

    def test_only_the_selected_rows_are_considered(self):
        r, spy = self._call("?account_ids=2")
        self.assertEqual(r.status_code, 200, r.text[:300])
        rows = spy.call_args[0][0]
        self.assertEqual(
            [str(row["id"]) for row in rows], ["2"],
            f"只勾了 1 行就该只处理 1 行，实际交出去 {len(rows)} 行",
        )

    def test_no_account_ids_keeps_the_old_whole_platform_behaviour(self):
        r, spy = self._call("")
        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(len(spy.call_args[0][0]), 3, "不传时保持整平台老行为")
