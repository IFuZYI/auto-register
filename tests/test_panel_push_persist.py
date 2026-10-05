"""push 端点落库：推送结果要写回账号行（与其它上传路径行为一致）。

背景（子代理检测 + 主会话复核，2026-10-04）：`POST /api/integrations/panels/
{key}/push` 的 grok2api 上传器调了 `record_grok2api_sync_result(extra, ...)`
—— 但 `extra` 是 `fetch_panel_raw` 的一次性副本，从不回写数据库；CPA /
sub2api / chatgpt2api 分支连 record 都没调。对照：手动动作
（api/actions.py 的 `_UPLOAD_SYNC_WRITERS`）与自动上传
（services/external_sync.py）都落库。结果是**经 push 路径上传的账号，
sync_statuses 永远空白**（前端目前无消费方所以被掩盖，但状态展示一接回
就会显形）。

这些用例钉住：push 完成后 `sync_statuses.<面板>` 写入对应账号行
（成功和失败都写 —— 与手动路径同语义：推了就要留痕；跳过的没发生任何事，
不写）。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _row(account_id, email, platform, updated, extra) -> dict:
    return {
        "id": account_id,
        "email": email,
        "platform": platform,
        "updated_at": updated.isoformat(),
        "extra": extra,
    }


class PushPersistsSyncStatusTests(unittest.TestCase):
    def _client(self):
        import main as main_mod
        from fastapi.testclient import TestClient

        return TestClient(main_mod.app)

    def _make_grok_account(self, email: str) -> int:
        """建一个真实账号行（落 grok 库），返回 id。"""
        from core.db import AccountModel, platform_session

        with platform_session("grok") as session:
            row = AccountModel(platform="grok", email=email, password="pw", status="registered")
            row.set_extra({"sso": "sso-value"})
            session.add(row)
            session.commit()
            session.refresh(row)
            if row.id is None:
                raise RuntimeError("账号未落库")
            return row.id

    def _extra_of(self, email: str) -> dict:
        from core.db import AccountModel, platform_session
        from sqlmodel import select

        with platform_session("grok") as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == email)
            ).first()
            return row.get_extra() if row else {}

    def _fake_grok2api_client(self, ok: bool = True, msg: str = "已接入（web）"):
        client = mock.Mock()
        client.configured = True
        client.ingest_sso.return_value = (ok, msg)
        return client

    def test_successful_push_persists_status(self):
        """推送成功 → sync_statuses.grok2api 落库（last_attempt_ok=True）。"""
        email = "push-persist-ok@example.com"
        account_id = self._make_grok_account(email)
        local_rows = [_row(
            account_id, email, "grok",
            datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            {"sso": "sso-value"},
        )]

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, [], ""),
        ), mock.patch(
            "platforms.grok.grok2api.Grok2ApiClient.from_config",
            return_value=self._fake_grok2api_client(),
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/grok2api/push")

        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["pushed"], 1)

        state = (self._extra_of(email).get("sync_statuses") or {}).get("grok2api") or {}
        self.assertTrue(state.get("last_attempt_ok"), f"成功推送没落库（实际：{state}）")
        self.assertIn("已接入", str(state.get("last_message") or ""))

    def test_failed_push_persists_status_too(self):
        """推送失败也要留痕（last_attempt_ok=False + 原因）。"""
        email = "push-persist-fail@example.com"
        account_id = self._make_grok_account(email)
        local_rows = [_row(
            account_id, email, "grok",
            datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            {"sso": "sso-value"},
        )]

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, [], ""),
        ), mock.patch(
            "platforms.grok.grok2api.Grok2ApiClient.from_config",
            return_value=self._fake_grok2api_client(ok=False, msg="远端拒绝"),
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/grok2api/push")

        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["failed"], 1)

        state = (self._extra_of(email).get("sync_statuses") or {}).get("grok2api") or {}
        self.assertIn("last_attempt_ok", state, f"失败推送没落库（实际：{state}）")
        self.assertFalse(state.get("last_attempt_ok"))
        self.assertIn("远端拒绝", str(state.get("last_message") or ""))

    def test_skipped_rows_do_not_touch_status(self):
        """跳过（已同步）的行不写状态 —— 没发生的事不留痕。"""
        email = "push-persist-skip@example.com"
        account_id = self._make_grok_account(email)
        same_time = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
        local_rows = [_row(account_id, email, "grok", same_time, {"sso": "sso-value"})]

        from services.panel_comparison import RemoteAccount

        remote = [RemoteAccount(
            email=email, platform="grok", remote_id="r-1",
            updated_at=same_time,
            credentials={"sso": "sso-value"},
        )]

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, remote, ""),
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/grok2api/push")

        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["skipped"], 1)

        state = (self._extra_of(email).get("sync_statuses") or {}).get("grok2api")
        self.assertIsNone(state, f"跳过行不该写状态（实际：{state}）")


if __name__ == "__main__":
    unittest.main()
