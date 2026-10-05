"""批量补 RT 任务：选号规则与任务创建接口。"""

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from core.db import AccountModel, engine
from services.chatgpt_rt_backfill import select_backfill_targets


def _account(email, *, extra=None, status="registered", platform="chatgpt"):
    model = AccountModel(platform=platform, email=email, password="pw", status=status)
    model.set_extra(extra or {})
    return model


class BackfillTargetSelectionTests(unittest.TestCase):
    def setUp(self):
        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all(
                [
                    _account("no-rt-1@example.com"),
                    _account("no-rt-2@example.com", status="expired"),
                    _account("has-rt@example.com", extra={"refresh_token": "rt"}),
                    _account("other-platform@example.com", platform="cursor"),
                ]
            )
            session.commit()

    def test_all_filtered_keeps_only_accounts_without_rt(self):
        with Session(engine) as session:
            accounts, missing = select_backfill_targets(session, all_filtered=True)

        self.assertEqual(
            sorted(row.email for row in accounts),
            ["no-rt-1@example.com", "no-rt-2@example.com"],
        )
        self.assertEqual(missing, [])

    def test_status_and_email_filters_are_applied(self):
        with Session(engine) as session:
            accounts, _ = select_backfill_targets(session, all_filtered=True, status="expired")
            self.assertEqual([row.email for row in accounts], ["no-rt-2@example.com"])

            accounts, _ = select_backfill_targets(session, all_filtered=True, email="no-rt-1")
            self.assertEqual([row.email for row in accounts], ["no-rt-1@example.com"])

    def test_only_missing_rt_off_includes_accounts_that_already_have_one(self):
        with Session(engine) as session:
            accounts, _ = select_backfill_targets(
                session, all_filtered=True, only_missing_rt=False
            )

        self.assertIn("has-rt@example.com", [row.email for row in accounts])

    def test_explicit_ids_report_missing_rows(self):
        with Session(engine) as session:
            existing = session.exec(
                select(AccountModel).where(AccountModel.email == "no-rt-1@example.com")
            ).first()
            accounts, missing = select_backfill_targets(
                session, account_ids=[existing.id, 999999]
            )

        self.assertEqual([row.email for row in accounts], ["no-rt-1@example.com"])
        self.assertEqual(missing, [999999])

    def test_no_scope_is_rejected(self):
        with Session(engine) as session:
            with self.assertRaises(ValueError):
                select_backfill_targets(session)


class BackfillTaskEndpointTests(unittest.TestCase):
    def setUp(self):
        from api.tasks import router

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all(
                [
                    _account("no-rt-1@example.com"),
                    _account("has-rt@example.com", extra={"refresh_token": "rt"}),
                ]
            )
            session.commit()

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self):
        """清掉本类建的任务记录。

        `_task_store` 是模块级单例，跨测试文件共享。这里有两个测试把
        `_run_backfill_rt` mock 掉了 —— runner 不跑，任务就停在 `pending`，
        `has_active()` 于是恒为 True。后果不止「脏状态」：任何依赖
        「当前没有运行中任务」的代码都会跟着错，实测 `services/data_bundle.py`
        的导入会被拒（「有运行中的注册任务」），表现为**别的测试文件失败**、
        单跑本文件却全绿 —— 极难排查。
        """
        from api.tasks import _task_store

        with _task_store._lock:
            for task_id in [
                tid for tid, rec in _task_store._records.items()
                if rec.status in ("pending", "running")
            ]:
                _task_store._records.pop(task_id, None)

    def test_creates_task_for_accounts_missing_rt(self):
        with mock.patch("api.tasks._run_backfill_rt") as runner:
            response = self.client.post("/tasks/backfill-rt", json={"all_filtered": True})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertTrue(body["task_id"].startswith("backfill_rt_"))
        runner.assert_called_once()
        _task_id, account_ids, req = runner.call_args.args
        self.assertEqual(len(account_ids), 1)
        self.assertTrue(req.only_missing_rt)

    def test_rejects_when_every_account_already_has_rt(self):
        with mock.patch("api.tasks._run_backfill_rt") as runner:
            response = self.client.post(
                "/tasks/backfill-rt", json={"all_filtered": True, "email": "has-rt"}
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("已经有 RT", response.json()["detail"])
        runner.assert_not_called()

    def test_missing_account_ids_say_so_instead_of_blaming_rt(self):
        response = self.client.post("/tasks/backfill-rt", json={"account_ids": [999999]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "所选账号不存在")

    def test_rejects_request_without_scope(self):
        response = self.client.post("/tasks/backfill-rt", json={})

        self.assertEqual(response.status_code, 400)
        self.assertIn("all_filtered", response.json()["detail"])

    def test_runner_persists_result_and_finishes_task(self):
        """TestClient 会在响应后同步跑后台任务，正好把整条链路串起来。"""
        from platforms.chatgpt.rt_backfill import (
            STRATEGY_SESSION,
            BackfillAttempt,
            BackfillResult,
        )

        result = BackfillResult(
            success=True,
            email="no-rt-1@example.com",
            strategy=STRATEGY_SESSION,
            refresh_token="rt-new",
            access_token="at-new",
            attempts=[BackfillAttempt(STRATEGY_SESSION, True, "拿到 refresh_token")],
        )

        with mock.patch(
            "services.chatgpt_rt_backfill.backfill_account_data", return_value=result
        ) as engine_call:
            response = self.client.post(
                "/tasks/backfill-rt", json={"all_filtered": True, "delay_seconds": 0}
            )
            task_id = response.json()["task_id"]
            snapshot = self.client.get(f"/tasks/{task_id}").json()

        engine_call.assert_called_once()
        self.assertEqual(engine_call.call_args.kwargs["email"], "no-rt-1@example.com")
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)

        with Session(engine) as session:
            account = session.exec(
                select(AccountModel).where(AccountModel.email == "no-rt-1@example.com")
            ).first()
        extra = account.get_extra()
        self.assertEqual(extra["refresh_token"], "rt-new")
        self.assertEqual(account.token, "at-new")
        self.assertTrue(extra["chatgpt_rt_backfill"]["ok"])

    def test_runner_hands_its_stop_switch_to_the_engine(self):
        """等验证码那几分钟归邮箱管，拿不到 control 就停不下来。"""
        from platforms.chatgpt.rt_backfill import BackfillResult

        result = BackfillResult(success=False, email="no-rt-1@example.com", error_message="x")

        with mock.patch(
            "services.chatgpt_rt_backfill.backfill_account_data", return_value=result
        ) as engine_call:
            self.client.post("/tasks/backfill-rt", json={"all_filtered": True, "delay_seconds": 0})

        kwargs = engine_call.call_args.kwargs
        self.assertIsNotNone(kwargs["task_control"])
        self.assertTrue(hasattr(kwargs["task_control"], "checkpoint"))
        self.assertIsNotNone(kwargs["attempt_id"])

    def test_runner_records_failure_without_touching_credentials(self):
        from platforms.chatgpt.rt_backfill import STRATEGY_LOGIN, BackfillAttempt, BackfillResult

        result = BackfillResult(
            success=False,
            email="no-rt-1@example.com",
            error_message="补 RT 失败（协议重登：密码错误）",
            attempts=[BackfillAttempt(STRATEGY_LOGIN, False, "密码错误")],
        )

        with mock.patch("services.chatgpt_rt_backfill.backfill_account_data", return_value=result):
            response = self.client.post(
                "/tasks/backfill-rt", json={"all_filtered": True, "delay_seconds": 0}
            )
            snapshot = self.client.get(f"/tasks/{response.json()['task_id']}").json()

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 1)

        with Session(engine) as session:
            account = session.exec(
                select(AccountModel).where(AccountModel.email == "no-rt-1@example.com")
            ).first()
        extra = account.get_extra()
        self.assertNotIn("refresh_token", extra)
        self.assertFalse(extra["chatgpt_rt_backfill"]["ok"])


if __name__ == "__main__":
    unittest.main()
