"""禁用（banned）账号不参与面板凭据上传 —— API 动作层也要排除。

用户要求（2026-10-07）：「禁用的不参与同步」「已封禁的这种是不是就没必要
在面板同步凭据了」。

对比页的两个方向（`panel_push.plan_push` / `panel_sync.plan_sync`）已排除
banned；但**动作端点**（`POST /actions/{platform}/{action_id}/batch` 与
`POST /actions/{platform}/{account_id}/{action_id}`）此前没有 ——
`upload_cpa` / `upload_sub2api` / `upload_chatgpt2api` / `upload_grok2api`
直调时仍会把死凭证推给远端面板（远端会拿死凭证去刷 token、污染号池）。

实测（修复前）：对 banned 账号批量调 `upload_cpa` → `success=1`、
`upload_to_cpa` 被真实调用一次。

修复口径：`_UPLOAD_ACTIONS` 集合里的动作在批量/单账号两条路径都跳过
banned 账号，结果里给出 `reason=banned`（批量）或明确 error（单账号）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/banned_upload.db")

import core.config_store  # noqa: E402,F401
from core.db import AccountModel, engine, init_db  # noqa: E402
from sqlmodel import Session  # noqa: E402

init_db()


def _mk_account(email: str, status: str, *, platform: str = "chatgpt") -> int:
    with Session(engine) as s:
        m = AccountModel(platform=platform, email=email, password="pw", status=status)
        m.set_extra({"access_token": "at-x", "refresh_token": "rt-x"})
        s.add(m)
        s.commit()
        return int(m.id)


def _client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.actions import router

    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


class BatchUploadBannedExclusionTests(unittest.TestCase):
    """批量 upload_* 端点必须跳过 banned 账号。"""

    def test_batch_upload_cpa_skips_banned(self):
        banned_id = _mk_account("banned-batch-upload@x.com", "banned")
        ok_id = _mk_account("ok-batch-upload@x.com", "registered")

        uploaded: list = []

        def _fake_upload(record, **kwargs):
            uploaded.append(record)
            return True, "ok"

        with mock.patch("platforms.chatgpt.cpa_upload.upload_to_cpa", side_effect=_fake_upload), \
             mock.patch("platforms.chatgpt.cpa_upload.generate_token_json",
                        side_effect=lambda acc: {"access_token": "at-x", "email": getattr(acc, "email", "")}):
            with _client() as client:
                r = client.post(
                    "/actions/chatgpt/upload_cpa/batch",
                    json={"account_ids": [banned_id, ok_id], "params": {}},
                )

        self.assertEqual(r.status_code, 200, r.text[:300])
        body = r.json()
        # banned 不该被上传
        self.assertEqual(len(uploaded), 1, f"banned 账号的凭证被上传了（uploaded={len(uploaded)}）")
        self.assertNotIn(
            "banned-batch-upload@x.com",
            [str(u.get("email") or "") for u in uploaded],
        )
        # 结果里 banned 那条要给出原因
        banned_item = next(
            (i for i in body["items"] if i.get("id") == banned_id), None
        )
        self.assertIsNotNone(banned_item, "banned 账号没有出现在结果里")
        self.assertFalse(banned_item.get("ok"), "banned 账号不该报成功")
        self.assertIn("禁用", str(banned_item.get("message") or ""))

    def test_batch_upload_chatgpt2api_skips_banned(self):
        banned_id = _mk_account("banned-2api-upload@x.com", "banned")

        with mock.patch("platforms.chatgpt.chatgpt2api_upload.upload_to_chatgpt2api") as up:
            with _client() as client:
                r = client.post(
                    "/actions/chatgpt/upload_chatgpt2api/batch",
                    json={"account_ids": [banned_id], "params": {}},
                )

        self.assertEqual(r.status_code, 200)
        up.assert_not_called()

    def test_batch_non_upload_action_still_runs_for_banned(self):
        """非上传动作（如刷新）不受此过滤影响 —— 修复不误伤。"""
        banned_id = _mk_account("banned-refresh-still@x.com", "banned")

        with mock.patch(
            "platforms.chatgpt.plugin.ChatGPTPlatform.execute_action",
            return_value={"ok": True, "data": {"message": "ok", "refreshed": True, "verified": True}},
        ) as action:
            with _client() as client:
                r = client.post(
                    "/actions/chatgpt/refresh_token/batch",
                    json={"account_ids": [banned_id], "params": {}},
                )

        self.assertEqual(r.status_code, 200)
        self.assertTrue(action.called, "刷新动作被误过滤了")


class SingleUploadBannedExclusionTests(unittest.TestCase):
    """单账号 upload_* 端点必须拒绝 banned 账号。"""

    def test_single_upload_cpa_rejects_banned(self):
        banned_id = _mk_account("banned-single-upload@x.com", "banned")

        with mock.patch("platforms.chatgpt.cpa_upload.upload_to_cpa") as up:
            with _client() as client:
                r = client.post(
                    f"/actions/chatgpt/{banned_id}/upload_cpa",
                    json={"params": {}},
                )

        self.assertEqual(r.status_code, 200, r.text[:300])
        up.assert_not_called()
        body = r.json()
        self.assertFalse(body.get("ok"), "banned 账号上传应报失败")
        self.assertIn("禁用", str(body.get("error") or body.get("data") or ""))

    def test_single_upload_cpa_still_works_for_normal(self):
        ok_id = _mk_account("ok-single-upload@x.com", "registered")

        with mock.patch("platforms.chatgpt.cpa_upload.upload_to_cpa", return_value=(True, "ok")) as up, \
             mock.patch("platforms.chatgpt.cpa_upload.generate_token_json",
                        return_value={"access_token": "at-x"}):
            with _client() as client:
                r = client.post(
                    f"/actions/chatgpt/{ok_id}/upload_cpa",
                    json={"params": {}},
                )

        self.assertEqual(r.status_code, 200)
        self.assertTrue(up.called, "正常账号的上传被误拦截")


if __name__ == "__main__":
    unittest.main()
