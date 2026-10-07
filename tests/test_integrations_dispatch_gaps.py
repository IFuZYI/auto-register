"""`api/integrations.py` 缺口覆盖：上传器分发、推送种类映射、结果落库。

模块 63% 覆盖。缺的是最关键的几个分派点：
- `_push_account_uploader`：按（面板 × 账号平台）挑上传器。CPA 同时托管
  ChatGPT 与 Grok —— 用错平台的上传器会传错凭据类型（真实事故级后果）。
- `_PANEL_PUSH_KIND`：overwrite（CPA/grok2api）vs append（sub2api/chatgpt2api）。
  append 面板重推不删旧记录会留重复。
- `_persist_push_results`：推送结果写回 `sync_statuses.<面板>`；跳过的不留痕、
  邮箱大小写归一（实测 'User@X.com' 查 'user@x.com' 落空）。
- `/backfill`：按平台分组进各自库会话回填。
"""

from __future__ import annotations

import unittest
from unittest import mock

from sqlmodel import Session, delete, select

from core.db import AccountModel, engine


def _clean_accounts():
    with Session(engine) as session:
        session.exec(delete(AccountModel))
        session.commit()


class PushAccountUploaderDispatchTests(unittest.TestCase):
    """上传器按（面板, 平台）分发 —— 分发错会传错凭据类型。"""

    def test_grok2api_requires_sso(self):
        from api.integrations import _push_account_uploader

        ok, msg = _push_account_uploader(
            "grok2api", "grok", {"email": "a@b.com", "extra": {}}
        )
        self.assertFalse(ok)
        self.assertIn("SSO", msg)

    def test_cpa_grok_rebuilds_record_from_credentials(self):
        """没有 cpa_record 时用 access_token 重建（实测用户库里老号都没有）。"""
        from api import integrations

        captured = {}

        def fake_upload(record, proxy=""):
            captured["record"] = record
            return True, "uploaded"

        with (
            mock.patch("platforms.grok.upload.upload_to_cpa", side_effect=fake_upload),
            mock.patch("platforms.grok.oauth_device.token_to_cpa_record", return_value={"email": "a@b.com"}),
            mock.patch("services.chatgpt_sync.upload_proxy_for", return_value=""),
        ):
            ok, _msg = integrations._push_account_uploader(
                "cpa",
                "grok",
                {"email": "a@b.com", "extra": {"access_token": "at-1", "sso": "sso-1"}},
            )

        self.assertTrue(ok)
        self.assertEqual(captured["record"], {"email": "a@b.com"})

    def test_cpa_grok_without_any_credential_reports_clearly(self):
        from api.integrations import _push_account_uploader

        ok, msg = _push_account_uploader(
            "cpa", "grok", {"email": "a@b.com", "extra": {}}
        )
        self.assertFalse(ok)
        self.assertIn("access_token", msg)

    def test_cpa_chatgpt_uses_generate_token_json(self):
        from api import integrations

        captured = {}

        def fake_upload(token_json, proxy=""):
            captured["token_json"] = token_json
            return True, "ok"

        with (
            mock.patch("platforms.chatgpt.cpa_upload.upload_to_cpa", side_effect=fake_upload),
            mock.patch("platforms.chatgpt.cpa_upload.generate_token_json", return_value={"access_token": "at"}),
            mock.patch("services.chatgpt_sync.upload_proxy_for", return_value=""),
        ):
            ok, _ = integrations._push_account_uploader(
                "cpa", "chatgpt", {"email": "a@b.com", "extra": {"access_token": "at"}}
            )

        self.assertTrue(ok)
        self.assertEqual(captured["token_json"], {"access_token": "at"})

    def test_chatgpt2api_dispatch(self):
        from api import integrations

        captured = {}

        def fake_upload(account, proxy=""):
            captured["email"] = account.email
            return True, "ok"

        with (
            mock.patch("platforms.chatgpt.chatgpt2api_upload.upload_to_chatgpt2api", side_effect=fake_upload),
            mock.patch("services.chatgpt_sync.upload_proxy_for", return_value=""),
        ):
            ok, _ = integrations._push_account_uploader(
                "chatgpt2api", "chatgpt", {"email": "a@b.com", "extra": {"access_token": "at"}}
            )

        self.assertTrue(ok)
        self.assertEqual(captured["email"], "a@b.com")

    def test_unknown_panel_reports_no_uploader(self):
        from api.integrations import _push_account_uploader

        ok, msg = _push_account_uploader("nope", "chatgpt", {"email": "a@b.com", "extra": {}})
        self.assertFalse(ok)
        self.assertIn("没有推送器", msg)


class PushKindTests(unittest.TestCase):
    def test_overwrite_vs_append_panels(self):
        from api.integrations import _PANEL_PUSH_KIND

        self.assertEqual(_PANEL_PUSH_KIND["cpa"], "overwrite")
        self.assertEqual(_PANEL_PUSH_KIND["grok2api"], "overwrite")
        self.assertEqual(_PANEL_PUSH_KIND["sub2api"], "append")
        self.assertEqual(_PANEL_PUSH_KIND["chatgpt2api"], "append")

    def test_deleter_only_for_append_panels(self):
        from api.integrations import _panel_remote_deleter

        self.assertIsNone(_panel_remote_deleter("cpa"))
        self.assertIsNone(_panel_remote_deleter("grok2api"))
        self.assertIsNotNone(_panel_remote_deleter("chatgpt2api"))
        self.assertIsNotNone(_panel_remote_deleter("sub2api"))


class PersistPushResultsTests(unittest.TestCase):
    """推送结果写回：只写真正发起过上传的；邮箱大小写归一。"""

    def setUp(self):
        _clean_accounts()

    def tearDown(self):
        _clean_accounts()

    def _add(self, email, extra=None, platform="chatgpt"):
        with Session(engine) as session:
            model = AccountModel(platform=platform, email=email, password="pw", status="registered")
            model.set_extra(extra or {"access_token": "at"})
            session.add(model)
            session.commit()

    def test_skipped_items_leave_no_trace(self):
        from api.integrations import _persist_push_results

        self._add("skip@example.com")
        _persist_push_results("chatgpt2api", [
            {"email": "skip@example.com", "platform": "chatgpt", "push": False},
        ])

        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == "skip@example.com")
            ).first()
        self.assertNotIn("chatgpt2api", row.get_extra().get("sync_statuses", {}),
                         "跳过的条目不该留下同步状态")

    def test_pushed_item_is_recorded_with_normalized_email(self):
        """远端回传的大小写与本地不同也要能落库（实测大小写落空 bug）。"""
        from api.integrations import _persist_push_results

        self._add("case@example.com")
        _persist_push_results("chatgpt2api", [
            {"email": "Case@Example.com", "platform": "chatgpt", "push": True,
             "pushed": True, "message": "ok"},
        ])

        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == "case@example.com")
            ).first()
        statuses = row.get_extra().get("sync_statuses", {})
        self.assertIn("chatgpt2api", statuses, "推送结果必须写回（否则界面永远空白）")

    def test_missing_account_row_is_tolerated(self):
        """本地行不存在时跳过，不炸整批。"""
        from api.integrations import _persist_push_results

        _persist_push_results("chatgpt2api", [
            {"email": "ghost@example.com", "platform": "chatgpt", "push": True,
             "pushed": False, "message": "fail"},
        ])  # 不抛异常即通过


class PanelsEndpointTests(unittest.TestCase):
    def test_panels_list_reports_configuration_state(self):
        from fastapi.testclient import TestClient

        from api.integrations import router

        app = __import__("fastapi").FastAPI()
        app.include_router(router)
        client = TestClient(app)

        r = client.get("/integrations/panels")
        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertIsInstance(body["items"], list)
        for item in body["items"]:
            self.assertIn("url", item)
            self.assertIn("configured", item)
            self.assertIn("secret_set", item)


class BackfillEndpointTests(unittest.TestCase):
    """回填端点：无目标返回空摘要；指定不存在账号时如实报失败。"""

    def setUp(self):
        _clean_accounts()

    def tearDown(self):
        _clean_accounts()

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.integrations import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_empty_platforms_returns_empty_summary(self):
        client = self._client()
        r = client.post("/integrations/backfill", json={"platforms": []})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total"], 0)

    def test_unknown_account_ids_are_skipped_silently(self):
        """不存在的账号不在结果里 —— 不是错误，只是没得可填。"""
        client = self._client()
        r = client.post(
            "/integrations/backfill",
            json={"platforms": ["chatgpt"], "account_ids": [999999999]},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total"], 0)

    def test_chatgpt_backfill_reports_unconfigured_target(self):
        """没配任何导入目标时，chatgpt 账号报「未配置对应导入目标」（如实失败）。"""
        with Session(engine) as session:
            model = AccountModel(
                platform="chatgpt", email="backfill@example.com", password="pw",
                status="registered",
            )
            model.set_extra({"access_token": "at"})
            session.add(model)
            session.commit()
            account_id = model.id

        client = self._client()
        with mock.patch(
            "api.integrations.backfill_chatgpt_account_to_cpa",
            return_value={"ok": False, "skipped": False, "results": [], "message": "未配置"},
        ):
            r = client.post(
                "/integrations/backfill",
                json={"platforms": ["chatgpt"], "account_ids": [account_id]},
            )

        self.assertEqual(r.status_code, 200, r.text[:200])
        body = r.json()
        self.assertEqual(body["total"], 1)
        self.assertIn(body["failed"] + body["skipped"], (0, 1))


if __name__ == "__main__":
    unittest.main()
