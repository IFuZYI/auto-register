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


class PushPersistsWritersBranchTests(unittest.TestCase):
    """writers 分支（cpa / sub2api / chatgpt2api 走 update_account_model_*_sync）。

    覆盖缺口（最终审查实测）：把 writers 分支整段禁用后全量测试无一变红 ——
    cpa 路径的落库正确性只能靠人工验证。这里补一条 cpa 用例钉住。
    """

    def _client(self):
        import main as main_mod
        from fastapi.testclient import TestClient

        return TestClient(main_mod.app)

    def _make_chatgpt_account(self, email: str) -> int:
        from core.db import AccountModel, platform_session

        with platform_session("chatgpt") as session:
            row = AccountModel(platform="chatgpt", email=email, password="pw", status="registered")
            row.set_extra({"access_token": "at-value"})
            session.add(row)
            session.commit()
            session.refresh(row)
            if row.id is None:
                raise RuntimeError("账号未落库")
            return row.id

    def _extra_of(self, email: str) -> dict:
        from core.db import AccountModel, platform_session
        from sqlmodel import select

        with platform_session("chatgpt") as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == email)
            ).first()
            return row.get_extra() if row else {}

    def test_cpa_failed_push_persists_sync_status(self):
        """CPA 推送失败 → sync_statuses.cpa 落库（last_attempt_ok=False）。

        用「未配置 CPA」触发失败（上传器直接返回 False），不需要 mock 网络。
        """
        email = "push-cpa-fail@example.com"
        account_id = self._make_chatgpt_account(email)
        local_rows = [_row(
            account_id, email, "chatgpt",
            datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            {"access_token": "at-value"},
        )]

        with mock.patch(
            "services.panel_comparison_cache.fetch_panel_raw",
            return_value=(local_rows, [], ""),
        ), mock.patch(
            "services.chatgpt_sync.upload_proxy_for",
            return_value="",
        ), mock.patch(
            "platforms.chatgpt.cpa_upload.upload_to_cpa",
            return_value=(False, "CPA 未配置"),
        ):
            with self._client() as client:
                r = client.post("/api/integrations/panels/cpa/push")

        self.assertEqual(r.status_code, 200, r.text[:300])
        self.assertEqual(r.json()["failed"], 1)

        state = (self._extra_of(email).get("sync_statuses") or {}).get("cpa") or {}
        self.assertIn("last_attempt_ok", state, f"cpa 失败推送没落库（实际：{state}）")
        self.assertFalse(state.get("last_attempt_ok"))
        self.assertIn("未配置", str(state.get("last_message") or ""))


class PushPersistEmailNormalizationTests(unittest.TestCase):
    """落库定位要过 `normalize_email`。

    远端返回的邮箱大小写可能与本地行不同（本地行经仓储统一为小写）；
    不过归一就精确匹配失败、静默不落库（实测：'User@X.com' 查 'user@x.com'
    落空）。
    """

    def _client(self):
        import main as main_mod
        from fastapi.testclient import TestClient

        return TestClient(main_mod.app)

    def test_mixed_case_remote_email_still_persists(self):
        from core.db import AccountModel, platform_session
        from sqlmodel import select

        email = "push-case@example.com"
        with platform_session("grok") as session:
            row = AccountModel(platform="grok", email=email, password="pw", status="registered")
            row.set_extra({"sso": "sso-value"})
            session.add(row)
            session.commit()
            session.refresh(row)
            account_id = row.id

        # 远端返回混合大小写邮箱（本地行是小写）
        local_rows = [_row(
            account_id, email, "grok",
            datetime(2026, 10, 4, 12, tzinfo=timezone.utc),
            {"sso": "sso-value"},
        )]

        from services.panel_comparison import RemoteAccount

        remote = [RemoteAccount(
            email="Push-Case@Example.COM", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 4, 10, tzinfo=timezone.utc),
            credentials={"sso_token": "old"},
        )]

        fake_client = mock.Mock()
        fake_client.configured = True
        fake_client.ingest_sso.return_value = (True, "已接入")

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

        with platform_session("grok") as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == email)
            ).first()
            if row is None:
                raise RuntimeError("账号行丢失")
            state = (row.get_extra().get("sync_statuses") or {}).get("grok2api") or {}
        self.assertTrue(
            state.get("last_attempt_ok"),
            f"远端混合大小写邮箱导致落库失败（实际：{state}）",
        )


if __name__ == "__main__":
    unittest.main()
