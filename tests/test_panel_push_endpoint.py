"""`POST /api/integrations/panels/{key}/push`（「更新远程凭证」）的接线契约。

用户要求：「同步到最新要变成两个 —— 一个是更新本地、一个是更新远程。
更新本地是将远程新的数据同步到本地，更新远程是将本地新的推送到远程
（如可行删除远端旧的）。」

与 `/sync`（更新本地，只拉 remote_newer）对称：`/push` 只推
local_newer + 未上传，方向判定在 `services/panel_push.plan_push`。
`delete_old=true` 时对**新建式面板**（sub2api / chatgpt2api）推成功后
删除旧远端记录 —— 它们的上传每次新增一条，不删会留重复。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _row(account_id: int, email: str, platform: str, updated: datetime, extra: dict) -> dict:
    return {
        "id": account_id,
        "email": email,
        "platform": platform,
        "updated_at": updated.isoformat(),
        "extra": extra,
    }


class PushEndpointTests(unittest.TestCase):
    def _client(self):
        import main as main_mod
        from fastapi.testclient import TestClient

        return TestClient(main_mod.app)

    def _fake_grok2api_client(self, ok: bool = True, msg: str = "已接入（web + NSFW）"):
        client = mock.Mock()
        client.configured = True
        client.ingest_sso.return_value = (ok, msg)
        return client

    def test_pushes_local_newer_and_reports(self):
        """local_newer 行 → 调 ingest_sso → pushed=1。"""
        from services.panel_comparison import RemoteAccount

        local_rows = [_row(
            1, "a@x.com", "grok",
            datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            {"sso": "sso-1"},
        )]
        remote = [RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 4, 10, tzinfo=timezone.utc),
            credentials={"sso_token": "old-sso"},
        )]
        fake_client = self._fake_grok2api_client()

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, remote, ""),
        ), mock.patch(
            "platforms.grok.grok2api.Grok2ApiClient.from_config",
            return_value=fake_client,
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/grok2api/push")

        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertEqual(body["panel"], "grok2api")
        self.assertEqual(body["pushed"], 1)
        self.assertEqual(body["skipped"], 0)
        self.assertEqual(body["remote_error"], "")
        fake_client.ingest_sso.assert_called_once()
        self.assertEqual(body["items"][0]["reason"], "local_newer")

    def test_remote_newer_is_not_pushed(self):
        """远端较新 → 不推（那是「更新本地」的职责）。"""
        from services.panel_comparison import RemoteAccount

        local_rows = [_row(
            1, "a@x.com", "grok",
            datetime(2026, 10, 4, 10, tzinfo=timezone.utc),
            {"sso": "old-sso"},
        )]
        remote = [RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            credentials={"sso_token": "new-sso"},
        )]
        fake_client = self._fake_grok2api_client()

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, remote, ""),
        ), mock.patch(
            "platforms.grok.grok2api.Grok2ApiClient.from_config",
            return_value=fake_client,
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/grok2api/push")

        body = r.json()
        self.assertEqual(body["pushed"], 0)
        self.assertEqual(body["skipped"], 1)
        self.assertEqual(body["items"][0]["reason"], "remote_newer")
        fake_client.ingest_sso.assert_not_called()

    def test_not_uploaded_is_pushed(self):
        """远端没有这个账号 → 推送（补传）。"""
        local_rows = [_row(
            1, "a@x.com", "grok",
            datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            {"sso": "sso-1"},
        )]
        fake_client = self._fake_grok2api_client()

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, [], ""),
        ), mock.patch(
            "platforms.grok.grok2api.Grok2ApiClient.from_config",
            return_value=fake_client,
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/grok2api/push")

        body = r.json()
        self.assertEqual(body["pushed"], 1)
        self.assertEqual(body["items"][0]["reason"], "not_uploaded")

    def test_remote_error_returns_without_pushing(self):
        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=([], [], "面板地址未配置，无法读取远端账号"),
        ), mock.patch(
            "platforms.grok.grok2api.Grok2ApiClient.from_config",
        ) as factory:
            with self._client() as client:
                r = client.post("/api/integrations/panels/grok2api/push")

        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["pushed"], 0)
        self.assertIn("面板地址未配置", body["remote_error"])
        factory.assert_not_called()

    def test_unknown_panel_404(self):
        with self._client() as client:
            r = client.post("/api/integrations/panels/nonexistent/push")
        self.assertEqual(r.status_code, 404)

    def test_platform_param_scopes_the_push(self):
        """`?platform=grok` 只推 grok 的行（多平台面板 CPA 用）。"""
        from services.panel_comparison import RemoteAccount

        local_rows = [
            _row(1, "g@x.com", "grok",
                 datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
                 {"sso": "sso-g"}),
            _row(2, "c@x.com", "chatgpt",
                 datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
                 {"access_token": "at-c"}),
        ]
        fake_client = self._fake_grok2api_client()

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, [], ""),
        ), mock.patch(
            "platforms.grok.grok2api.Grok2ApiClient.from_config",
            return_value=fake_client,
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/grok2api/push?platform=grok")

        body = r.json()
        self.assertEqual(body["total"], 1, "只应处理 grok 的那一行")
        self.assertEqual(body["pushed"], 1)

    def test_delete_old_removes_stale_remote_record(self):
        """chatgpt2api 是新建式：推成功后删除旧远端记录（delete_old=true）。"""
        from services.panel_comparison import RemoteAccount

        local_rows = [_row(
            1, "a@x.com", "chatgpt",
            datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            {"access_token": "new-at"},
        )]
        remote = [RemoteAccount(
            email="a@x.com", platform="chatgpt", remote_id="old-id-9",
            updated_at=datetime(2026, 10, 4, 10, tzinfo=timezone.utc),
            credentials={"access_token": "old-at"},
        )]

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, remote, ""),
        ), mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.upload_to_chatgpt2api",
            return_value=(True, "上传成功（新增 1）"),
        ), mock.patch(
            "services.panel_push.delete_chatgpt2api_account",
            return_value=(True, "已删除"),
        ) as deleter:
            with self._client() as client:
                r = client.post(
                    "/api/integrations/panels/chatgpt2api/push?delete_old=true"
                )

        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        self.assertEqual(body["pushed"], 1)
        self.assertEqual(body["deleted"], 1)
        deleter.assert_called_once()
        # 删除目标是旧远端记录的 id
        self.assertEqual(deleter.call_args[0][0], "old-id-9")

    def test_delete_old_is_off_by_default(self):
        """不传 delete_old 时不删除（保守默认）。"""
        from services.panel_comparison import RemoteAccount

        local_rows = [_row(
            1, "a@x.com", "chatgpt",
            datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            {"access_token": "new-at"},
        )]
        remote = [RemoteAccount(
            email="a@x.com", platform="chatgpt", remote_id="old-id-9",
            updated_at=datetime(2026, 10, 4, 10, tzinfo=timezone.utc),
            credentials={"access_token": "old-at"},
        )]

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, remote, ""),
        ), mock.patch(
            "platforms.chatgpt.chatgpt2api_upload.upload_to_chatgpt2api",
            return_value=(True, "上传成功"),
        ), mock.patch(
            "services.panel_push.delete_chatgpt2api_account",
        ) as deleter:
            with self._client() as client:
                r = client.post("/api/integrations/panels/chatgpt2api/push")

        body = r.json()
        self.assertEqual(body["pushed"], 1)
        self.assertEqual(body["deleted"], 0)
        deleter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
