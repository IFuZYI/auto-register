"""全流程集成测试：HTTP 层驱动「注册 → 落库 → 读取 → 导出 → 刷新」整条链。

与 `test_api_integration.py`（形状契约）和 `test_register_task_controls.py`
（runner 单元）不同，这里把三者串起来：**通过真实 ASGI 栈提交注册任务**、
任务在 TestClient 的后台任务机制里真实跑完、账号真实落进（隔离的）数据库、
再通过账号 API 读出来并导出 —— 用户能走的路径每一步都被驱动了一遍。

打桩边界刻意收窄：
  - 平台注册（`_FakePlatform.register`）—— 代替真实网络注册；
  - 邮箱池（`core.base_mailbox.create_mailbox`）—— 代替真实收信；
  - 外部同步（`services.external_sync.sync_account`）—— 不碰外网。

其余全部真实：任务记录、后台线程、落库（save_account → 仓储 → SQLite）、
任务快照、`/api/accounts` 读取、`/accounts/export-text` 导出。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from core.base_platform import Account, BasePlatform
from core.db import AccountModel, engine
from core.mailboxes.base import BaseMailbox, MailboxAccount

ROOT = Path(__file__).resolve().parents[1]


class _FakeMailbox(BaseMailbox):
    """固定返回一个地址的邮箱池（不碰真实收信）。"""

    def get_email(self) -> MailboxAccount:
        return MailboxAccount(email="e2e-flow@example.com")

    def get_current_ids(self, account: MailboxAccount) -> set:
        return set()

    def wait_for_code(self, account, keyword="", timeout=120, before_ids=None, code_pattern=None, **kwargs) -> str:
        return "123456"


class _E2EPlatform(BasePlatform):
    """注册即成功，产出带 extra 的账号（模拟真实平台的产物形状）。"""

    name = "chatgpt"
    display_name = "ChatGPT(E2E)"
    supported_executors = ["protocol"]

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str, password: str = None) -> Account:
        account = self.mailbox.get_email()
        return Account(
            platform="chatgpt",
            email=account.email,
            password=password or "e2e-pw",
            token="at-e2e",
            extra={
                "access_token": "at-e2e",
                "refresh_token": "rt-e2e",
                "session_token": "st-e2e",
            },
        )

    def check_valid(self, account: Account) -> bool:
        return True


def _clean_accounts():
    with Session(engine) as session:
        session.exec(delete(AccountModel))
        session.commit()


class RegisterToExportFlowTests(unittest.TestCase):
    """注册任务 → 落库 → 账号 API → 导出，一条链全走 HTTP。"""

    def setUp(self):
        _clean_accounts()
        from api.accounts import router as accounts_router
        from api.tasks import router as tasks_router

        app = FastAPI()
        app.include_router(tasks_router)
        app.include_router(accounts_router)
        self.client = TestClient(app)

    def tearDown(self):
        from api.tasks import _task_store

        with _task_store._lock:
            for task_id in [
                tid for tid, rec in _task_store._records.items()
                if rec.status in ("pending", "running")
            ]:
                _task_store._records.pop(task_id, None)
        _clean_accounts()

    def test_full_chain_register_persist_read_export(self):
        """整条链：提交注册任务 → 任务跑完 → 账号可读 → 能导出。"""
        with (
            mock.patch("core.registry.get", return_value=_E2EPlatform),
            mock.patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            mock.patch("services.external_sync.sync_account", return_value=[]),
        ):
            response = self.client.post(
                "/tasks/register",
                json={
                    "platform": "chatgpt",
                    "count": 1,
                    "concurrency": 1,
                    "register_retry_times": 0,
                    "extra": {"mail_provider": "fake"},
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
            task_id = response.json()["task_id"]

            snapshot = self.client.get(f"/tasks/{task_id}").json()

        # 任务完成且统计正确
        self.assertEqual(snapshot["status"], "done", snapshot)
        self.assertEqual(snapshot["success"], 1)

        # 账号真实落库
        with Session(engine) as session:
            rows = session.exec(
                select(AccountModel).where(AccountModel.email == "e2e-flow@example.com")
            ).all()
        self.assertEqual(len(rows), 1, "注册产物必须落库")
        row = rows[0]
        self.assertEqual(row.platform, "chatgpt")
        extra = row.get_extra()
        self.assertEqual(extra.get("access_token"), "at-e2e")

        # 通过账号 API 读得出来（分页形状 + 内容）
        listing = self.client.get("/accounts?platform=chatgpt").json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["items"][0]["email"], "e2e-flow@example.com")

        # 导出链：导出含该账号
        export = self.client.post(
            "/accounts/export-text",
            json={"format": "email_pw", "platform": "chatgpt"},
        ).json()
        self.assertIn("e2e-flow@example.com", export["content"])

    def test_registered_email_is_not_registered_twice(self):
        """同一邮箱重复提交 → 第二次被判重跳过（邮箱是唯一业务键）。"""
        with (
            mock.patch("core.registry.get", return_value=_E2EPlatform),
            mock.patch("core.base_mailbox.create_mailbox", return_value=_FakeMailbox()),
            mock.patch("services.external_sync.sync_account", return_value=[]),
        ):
            first = self.client.post(
                "/tasks/register",
                json={
                    "platform": "chatgpt",
                    "count": 1,
                    "concurrency": 1,
                    "register_retry_times": 0,
                    "email": "e2e-flow@example.com",
                    "extra": {"mail_provider": "fake"},
                },
            ).json()
            self.client.get(f"/tasks/{first['task_id']}")

            second = self.client.post(
                "/tasks/register",
                json={
                    "platform": "chatgpt",
                    "count": 1,
                    "concurrency": 1,
                    "register_retry_times": 0,
                    "email": "e2e-flow@example.com",
                    "extra": {"mail_provider": "fake"},
                },
            ).json()
            snapshot = self.client.get(f"/tasks/{second['task_id']}").json()

        self.assertEqual(snapshot["skipped"], 1, snapshot)
        self.assertEqual(snapshot["success"], 0)
        joined = "\n".join(snapshot["logs"])
        self.assertIn("[SKIP]", joined)

        with Session(engine) as session:
            rows = session.exec(
                select(AccountModel).where(AccountModel.email == "e2e-flow@example.com")
            ).all()
        self.assertEqual(len(rows), 1, "判重失效会多插一行")


class RefreshTokenFlowTests(unittest.TestCase):
    """刷新任务全链：HTTP 提交 → 平台动作 → 结果落库 → 快照可见。"""

    def setUp(self):
        _clean_accounts()
        with Session(engine) as session:
            model = AccountModel(
                platform="chatgpt", email="refresh-e2e@example.com",
                password="pw", status="registered",
            )
            model.set_extra({"session_token": "st-old"})
            session.add(model)
            session.commit()

        from api.tasks import router

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self):
        from api.tasks import _task_store

        with _task_store._lock:
            for task_id in [
                tid for tid, rec in _task_store._records.items()
                if rec.status in ("pending", "running")
            ]:
                _task_store._records.pop(task_id, None)
        _clean_accounts()

    def test_refresh_chain_updates_credentials_and_reports(self):
        with Session(engine) as session:
            account_id = int(
                session.exec(
                    select(AccountModel).where(AccountModel.email == "refresh-e2e@example.com")
                ).first().id
            )

        fake_action_result = {
            "ok": True,
            "data": {
                "message": "Token 已刷新（session，AT 已换发）",
                "access_token": "at-new",
                "session_token": "st-new",
                "verified": True,
                "refreshed": True,
                "strategy": "session",
            },
            "account_extra_patch": {
                "chatgpt_token_refresh": {
                    "ok": True, "verified": True, "refreshed": True,
                    "strategy": "session", "message": "",
                    "at": "2026-10-06T00:00:00+00:00",
                },
                "access_token": "at-new",
                "session_token": "st-new",
            },
        }

        with mock.patch(
            "platforms.chatgpt.plugin.ChatGPTPlatform.execute_action",
            return_value=fake_action_result,
        ):
            response = self.client.post(
                "/tasks/refresh-token",
                json={"account_ids": [account_id], "delay_seconds": 0},
            )
            task_id = response.json()["task_id"]
            snapshot = self.client.get(f"/tasks/{task_id}").json()

        self.assertEqual(snapshot["status"], "done", snapshot)
        self.assertEqual(snapshot["success"], 1)

        with Session(engine) as session:
            row = session.get(AccountModel, account_id)
        extra = row.get_extra()
        self.assertEqual(extra["access_token"], "at-new", "刷新结果必须写回")
        self.assertEqual(extra["session_token"], "st-new")


if __name__ == "__main__":
    unittest.main()
