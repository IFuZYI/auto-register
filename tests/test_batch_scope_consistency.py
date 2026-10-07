"""批量任务的「当前筛选」范围必须与页面显示的计数一致（复审发现，2026-10-07）。

独立 reviewer 发现：刷新 Token 弹窗显示「处理当前筛选的 {total} 个账号」，
而 `total` 来自 `/accounts` 的**带日期筛选**查询（created_at_start/end），
但提交给 `/tasks/refresh-token` 的 body 只带 email/status/plus_status ——
日期筛选没有对应字段。设了日期范围时：显示的 N 与实际处理的 M 不一致
（M 比 N 大）。同类的批量入口（补 RT / 绑 2FA / 批量动作）共用
`select_chatgpt_accounts` / `_resolve_batch_accounts`，一并修。

这里钉两层：

1. 后端：`select_chatgpt_accounts` 接受并应用 `created_at_start/end`
   （与 `/accounts` 的 `_list_filters` 同口径），三个任务的 Request 模型
   把日期透传进来；
2. 前端：三个 handler（backfill / refresh / bind 入口共用）在
   `all_filtered` 分支把 `createdAtStart/End` 带进 body。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, delete

from core.db import AccountModel, engine

ROOT = Path(__file__).resolve().parents[1]


def _account(email, *, extra=None, status="registered", created_at=None):
    model = AccountModel(platform="chatgpt", email=email, password="pw", status=status)
    model.set_extra(extra or {})
    if created_at is not None:
        model.created_at = created_at
    return model


class SelectionDateFilterTests(unittest.TestCase):
    """`select_chatgpt_accounts` 的日期筛选（与列表接口同口径）。"""

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.old = self.now - timedelta(days=30)
        self.mid = self.now - timedelta(days=10)

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all(
                [
                    _account("old@example.com", created_at=self.old),
                    _account("mid@example.com", created_at=self.mid),
                    _account("new@example.com", created_at=self.now),
                ]
            )
            session.commit()

    def test_date_range_narrows_selection(self):
        from services.chatgpt_account_selection import select_chatgpt_accounts

        with Session(engine) as session:
            accounts, missing = select_chatgpt_accounts(
                session,
                all_filtered=True,
                created_at_start=self.mid - timedelta(hours=1),
            )

        emails = sorted(a.email for a in accounts)
        self.assertEqual(
            emails, ["mid@example.com", "new@example.com"],
            "created_at_start 没生效 —— 批量任务会处理比显示计数更多的账号",
        )
        self.assertEqual(missing, [])

    def test_date_end_filters_too(self):
        from services.chatgpt_account_selection import select_chatgpt_accounts

        with Session(engine) as session:
            accounts, _ = select_chatgpt_accounts(
                session,
                all_filtered=True,
                created_at_end=self.mid + timedelta(hours=1),
            )

        emails = sorted(a.email for a in accounts)
        self.assertEqual(emails, ["mid@example.com", "old@example.com"])


class RefreshTokenEndpointDateTests(unittest.TestCase):
    """端点透传日期筛选（与 /accounts 的 total 同口径）。"""

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.old = self.now - timedelta(days=30)

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all(
                [
                    _account("old@example.com", extra={"session_token": "st"}, created_at=self.old),
                    _account("new@example.com", extra={"session_token": "st"}, created_at=self.now),
                ]
            )
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

    def test_date_filter_limits_task_scope(self):
        from unittest import mock

        start = (self.now - timedelta(days=1)).isoformat()
        with mock.patch("api.tasks._run_refresh_token") as runner:
            response = self.client.post(
                "/tasks/refresh-token",
                json={"all_filtered": True, "created_at_start": start},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.json()["total"], 1,
            "日期筛选没透传 —— 显示 1 个却会处理 2 个（显示与执行不一致）",
        )
        _task_id, account_ids, _req = runner.call_args.args
        self.assertEqual(len(account_ids), 1)


class BackfillAndTwoFactorDateTests(unittest.TestCase):
    """补 RT / 绑 2FA 两个端点同样透传日期（同一类缺陷）。"""

    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.old = self.now - timedelta(days=30)

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all(
                [
                    _account("old-no-rt@example.com", extra={"session_token": "st"}, created_at=self.old),
                    _account("new-no-rt@example.com", extra={"session_token": "st"}, created_at=self.now),
                ]
            )
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

    def test_backfill_rt_honours_date_filter(self):
        from unittest import mock

        start = (self.now - timedelta(days=1)).isoformat()
        with mock.patch("api.tasks._run_backfill_rt") as runner:
            response = self.client.post(
                "/tasks/backfill-rt",
                json={"all_filtered": True, "created_at_start": start},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1, "补 RT 未透传日期筛选")
        _task_id, account_ids, _req = runner.call_args.args
        self.assertEqual(len(account_ids), 1)

    def test_bind_2fa_honours_date_filter(self):
        from unittest import mock

        start = (self.now - timedelta(days=1)).isoformat()
        with mock.patch("api.tasks._run_bind_2fa") as runner:
            response = self.client.post(
                "/tasks/bind-2fa",
                json={"all_filtered": True, "created_at_start": start},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1, "绑 2FA 未透传日期筛选")
        _task_id, account_ids, _req = runner.call_args.args
        self.assertEqual(len(account_ids), 1)


class FrontendDateForwardingTests(unittest.TestCase):
    """前端三个批量 handler 的 all_filtered 分支必须带上日期。"""

    def _src(self):
        return (ROOT / "frontend" / "src" / "pages" / "Accounts.tsx").read_text(encoding="utf-8")

    def test_handlers_forward_dates(self):
        src = self._src()
        # 三个 all_filtered 分支（status-sync / backfill / refresh）
        occurrences = src.count("body.all_filtered = true")
        self.assertEqual(occurrences, 3, f"all_filtered 分支数变了: {occurrences}")

        forwarded = src.count("if (createdAtStart) body.created_at_start = createdAtStart")
        self.assertEqual(
            forwarded, occurrences,
            "有 all_filtered 分支没带 created_at_start —— "
            "显示的计数包含日期筛选、请求不带，处理数会大于显示数",
        )
        forwarded_end = src.count("if (createdAtEnd) body.created_at_end = createdAtEnd")
        self.assertEqual(forwarded_end, occurrences, "有分支没带 created_at_end")


if __name__ == "__main__":
    unittest.main()
