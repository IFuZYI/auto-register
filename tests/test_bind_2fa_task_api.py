"""批量绑 2FA 任务：选号规则、任务接口，以及密钥落库/上日志这条链。"""

import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from core.db import AccountModel, engine
from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult
from services.chatgpt_two_factor import select_two_factor_targets


def _account(email, *, extra=None, status="registered", platform="chatgpt"):
    model = AccountModel(platform=platform, email=email, password="pw", status=status)
    model.set_extra(extra or {})
    return model


class TwoFactorTargetSelectionTests(unittest.TestCase):
    def setUp(self):
        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all(
                [
                    _account("no-2fa-1@example.com"),
                    _account("no-2fa-2@example.com", status="trial"),
                    _account("has-2fa@example.com", extra={"totp_secret": "SECRET"}),
                    _account("other-platform@example.com", platform="cursor"),
                ]
            )
            session.commit()

    def test_all_filtered_keeps_only_accounts_without_a_secret(self):
        with Session(engine) as session:
            accounts, missing = select_two_factor_targets(session, all_filtered=True)

        self.assertEqual(
            sorted(row.email for row in accounts),
            ["no-2fa-1@example.com", "no-2fa-2@example.com"],
        )
        self.assertEqual(missing, [])

    def test_status_and_email_filters_are_applied(self):
        with Session(engine) as session:
            accounts, _ = select_two_factor_targets(session, all_filtered=True, status="trial")
            self.assertEqual([row.email for row in accounts], ["no-2fa-2@example.com"])

            accounts, _ = select_two_factor_targets(session, all_filtered=True, email="no-2fa-1")
            self.assertEqual([row.email for row in accounts], ["no-2fa-1@example.com"])

    def test_only_missing_2fa_off_includes_accounts_that_already_have_one(self):
        with Session(engine) as session:
            accounts, _ = select_two_factor_targets(
                session, all_filtered=True, only_missing_2fa=False
            )

        self.assertIn("has-2fa@example.com", [row.email for row in accounts])

    def test_explicit_ids_report_missing_rows(self):
        with Session(engine) as session:
            existing = session.exec(
                select(AccountModel).where(AccountModel.email == "no-2fa-1@example.com")
            ).first()
            accounts, missing = select_two_factor_targets(
                session, account_ids=[existing.id, 999999]
            )

        self.assertEqual([row.email for row in accounts], ["no-2fa-1@example.com"])
        self.assertEqual(missing, [999999])

    def test_no_scope_is_rejected(self):
        with Session(engine) as session:
            with self.assertRaises(ValueError):
                select_two_factor_targets(session)


class Bind2faTaskEndpointTests(unittest.TestCase):
    def setUp(self):
        from api.tasks import router

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all(
                [
                    _account("no-2fa-1@example.com"),
                    _account("has-2fa@example.com", extra={"totp_secret": "SECRET"}),
                ]
            )
            session.commit()

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def tearDown(self):
        """清掉本类建的任务记录。

        `_task_store` 是模块级单例，跨测试文件共享。这里有两个测试把
        `_run_bind_2fa` mock 掉了 —— runner 不跑，任务就停在 `pending`，
        `has_active()` 于是恒为 True。任何依赖「当前没有运行中任务」的代码
        都会跟着错（实测 `services/data_bundle.py` 的导入会被拒），
        表现为**别的测试文件失败**、单跑本文件却全绿 —— 极难排查。
        """
        from api.tasks import _task_store

        with _task_store._lock:
            for task_id in [
                tid for tid, rec in _task_store._records.items()
                if rec.status in ("pending", "running")
            ]:
                _task_store._records.pop(task_id, None)

    def _account_row(self, email="no-2fa-1@example.com"):
        with Session(engine) as session:
            return session.exec(
                select(AccountModel).where(AccountModel.email == email)
            ).first()

    def test_creates_task_for_accounts_without_a_secret(self):
        with mock.patch("api.tasks._run_bind_2fa") as runner:
            response = self.client.post("/tasks/bind-2fa", json={"all_filtered": True})

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertTrue(body["task_id"].startswith("bind_2fa_"))
        runner.assert_called_once()
        _task_id, account_ids, req = runner.call_args.args
        self.assertEqual(len(account_ids), 1)
        self.assertTrue(req.only_missing_2fa)

    def test_rejects_when_every_account_already_has_a_secret(self):
        with mock.patch("api.tasks._run_bind_2fa") as runner:
            response = self.client.post(
                "/tasks/bind-2fa", json={"all_filtered": True, "email": "has-2fa"}
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("已经有 2FA 密钥", response.json()["detail"])
        runner.assert_not_called()

    def test_missing_account_ids_say_so_instead_of_blaming_the_secret(self):
        response = self.client.post("/tasks/bind-2fa", json={"account_ids": [999999]})

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "所选账号不存在")

    def test_rejects_request_without_scope(self):
        response = self.client.post("/tasks/bind-2fa", json={})

        self.assertEqual(response.status_code, 400)
        self.assertIn("all_filtered", response.json()["detail"])

    def test_runner_persists_the_secret_and_puts_it_in_the_task_log(self):
        """TestClient 会在响应后同步跑后台任务，正好把整条链路串起来。"""
        result = TwoFactorBindResult(ok=True, secret="JBSWY3DPEHPK3PXP", factor_id="f1")

        with mock.patch(
            "services.chatgpt_two_factor.bind_account_two_factor", return_value=result
        ) as engine_call:
            response = self.client.post(
                "/tasks/bind-2fa", json={"all_filtered": True, "delay_seconds": 0}
            )
            task_id = response.json()["task_id"]
            snapshot = self.client.get(f"/tasks/{task_id}").json()

        engine_call.assert_called_once()
        self.assertEqual(engine_call.call_args.kwargs["email"], "no-2fa-1@example.com")
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)
        # 密钥只下发这一次，日志弹窗是用户当场导入验证器的唯一途径
        self.assertTrue(any("JBSWY3DPEHPK3PXP" in line for line in snapshot["logs"]))

        extra = self._account_row().get_extra()
        self.assertEqual(extra["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertTrue(extra["chatgpt_2fa"]["bound"])

    def test_runner_hands_its_stop_switch_to_the_engine(self):
        """会话失效时要重登收验证码，那几分钟归邮箱管，拿不到 control 就停不下来。"""
        result = TwoFactorBindResult(error_message="x")

        with mock.patch(
            "services.chatgpt_two_factor.bind_account_two_factor", return_value=result
        ) as engine_call:
            self.client.post("/tasks/bind-2fa", json={"all_filtered": True, "delay_seconds": 0})

        kwargs = engine_call.call_args.kwargs
        self.assertIsNotNone(kwargs["task_control"])
        self.assertTrue(hasattr(kwargs["task_control"], "checkpoint"))
        self.assertIsNotNone(kwargs["attempt_id"])

    def test_already_bound_counts_as_skipped_rather_than_failed(self):
        result = TwoFactorBindResult(already_bound=True, secret="OLD-SECRET")

        with mock.patch(
            "services.chatgpt_two_factor.bind_account_two_factor", return_value=result
        ):
            response = self.client.post(
                "/tasks/bind-2fa",
                json={"account_ids": [self._account_row().id], "only_missing_2fa": False},
            )
            snapshot = self.client.get(f"/tasks/{response.json()['task_id']}").json()

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(snapshot["errors"], [])
        self.assertTrue(any("[SKIP]" in line for line in snapshot["logs"]))

    def test_failure_records_the_attempt_without_writing_an_empty_secret(self):
        result = TwoFactorBindResult(error_message="2FA 绑定失败（enroll 被拒）")

        with mock.patch(
            "services.chatgpt_two_factor.bind_account_two_factor", return_value=result
        ):
            response = self.client.post(
                "/tasks/bind-2fa", json={"all_filtered": True, "delay_seconds": 0}
            )
            snapshot = self.client.get(f"/tasks/{response.json()['task_id']}").json()

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 1)

        extra = self._account_row().get_extra()
        self.assertNotIn("totp_secret", extra)
        self.assertFalse(extra["chatgpt_2fa"]["bound"])


if __name__ == "__main__":
    unittest.main()
