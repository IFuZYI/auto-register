"""批量刷新 Token 任务：端点、选号、执行落库、前端接线。

用户要求（2026-10-06）：「ChatGPT应该可以选中多个批量刷新token」——
此前「更多」菜单里只有补 RT，没有批量刷新 Token。

以及「GPT刷新token要确保AT真的刷新了」：刷新结果要如实区分
「AT 已换发」与「服务端认为无需换发」（AT 未到期时返回原值，实测）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from core.db import AccountModel, engine

ROOT = Path(__file__).resolve().parents[1]


def _account(email, *, extra=None, status="registered", platform="chatgpt"):
    model = AccountModel(platform=platform, email=email, password="pw", status=status)
    model.set_extra(extra or {})
    return model


class RefreshTokenTaskEndpointTests(unittest.TestCase):
    def setUp(self):
        from api.tasks import router

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all(
                [
                    _account("refresh-me@example.com", extra={"session_token": "st"}),
                    _account("refresh-me-2@example.com", extra={"session_token": "st"}, status="expired"),
                    _account("other@example.com", platform="grok"),
                ]
            )
            session.commit()

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

    def _id_of(self, email: str) -> int:
        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == email)
            ).first()
            return int(row.id)

    def test_creates_task_for_selected_accounts(self):
        account_id = self._id_of("refresh-me@example.com")
        with mock.patch("api.tasks._run_refresh_token") as runner:
            response = self.client.post(
                "/tasks/refresh-token", json={"account_ids": [account_id]}
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["total"], 1)
        self.assertTrue(body["task_id"].startswith("refresh_token_"))
        runner.assert_called_once()

    def test_all_filtered_selects_chatgpt_accounts_only(self):
        with mock.patch("api.tasks._run_refresh_token") as runner:
            response = self.client.post("/tasks/refresh-token", json={"all_filtered": True})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 2)
        _task_id, account_ids, _req = runner.call_args.args
        self.assertEqual(len(account_ids), 2)

    def test_status_filter_is_applied(self):
        with mock.patch("api.tasks._run_refresh_token") as runner:
            response = self.client.post(
                "/tasks/refresh-token", json={"all_filtered": True, "status": "expired"}
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["total"], 1)

    def test_rejects_request_without_scope(self):
        response = self.client.post("/tasks/refresh-token", json={})
        self.assertEqual(response.status_code, 400)
        self.assertIn("all_filtered", response.json()["detail"])

    def test_missing_account_ids_say_so(self):
        response = self.client.post("/tasks/refresh-token", json={"account_ids": [999999]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "所选账号不存在")


class RefreshTokenRunnerTests(unittest.TestCase):
    """runner 逐号跑刷新并把结果落库（mock 掉平台动作，验证接线与落库）。"""

    def setUp(self):
        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add(_account("runner-me@example.com", extra={"session_token": "st"}))
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

    def _id_of(self, email: str) -> int:
        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == email)
            ).first()
            return int(row.id)

    def test_runner_persists_refresh_result_and_finishes_task(self):
        """TestClient 同步跑后台任务，整条链路串起来。"""
        account_id = self._id_of("runner-me@example.com")
        fake_action_result = {
            "ok": True,
            "data": {
                "message": "Token 已刷新（session，AT 已换发）",
                "access_token": "at-new",
                "session_token": "st-rotated",
                "verified": True,
                "refreshed": True,
                "strategy": "session",
            },
            "account_extra_patch": {
                "chatgpt_token_refresh": {
                    "ok": True, "verified": True, "refreshed": True,
                    "strategy": "session", "message": "", "at": "2026-10-06T00:00:00+00:00",
                },
                "access_token": "at-new",
                "session_token": "st-rotated",
            },
        }

        with mock.patch(
            "platforms.chatgpt.plugin.ChatGPTPlatform.execute_action",
            return_value=fake_action_result,
        ) as action_call:
            response = self.client.post(
                "/tasks/refresh-token",
                json={"account_ids": [account_id], "delay_seconds": 0},
            )
            task_id = response.json()["task_id"]
            snapshot = self.client.get(f"/tasks/{task_id}").json()

        action_call.assert_called_once()
        self.assertEqual(action_call.call_args.args[0], "refresh_token")
        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 1)

        with Session(engine) as session:
            account = session.get(AccountModel, account_id)
        extra = account.get_extra()
        self.assertEqual(extra["access_token"], "at-new")
        self.assertEqual(extra["session_token"], "st-rotated")
        self.assertTrue(extra["chatgpt_token_refresh"]["refreshed"])

    def test_runner_does_not_hold_a_db_connection_during_network_calls(self):
        """网络链不能占着数据库连接 —— 并发跑一批号时连接池会被拖垮。

        约定见 `_load_account_fields` 的 docstring（补 RT / 绑 2FA 都遵守）：
        先把行读成纯数据、归还连接，跑完网络再开短会话落库。

        探针按**线程**统计在借连接（复审建议）：全局 checkedout 会被
        无关的后台写线程（`_save_task_log` daemon）干扰，线程作用域只反映
        本任务自己握着的连接。
        """
        import threading as _threading

        from sqlalchemy import event as _event

        account_id = self._id_of("runner-me@example.com")
        fake_action_result = {
            "ok": True,
            "data": {
                "message": "Token 已刷新（session，AT 已换发）",
                "access_token": "at-new",
                "session_token": "st-rotated",
                "verified": True,
                "refreshed": True,
                "strategy": "session",
            },
            "account_extra_patch": {
                "chatgpt_token_refresh": {
                    "ok": True, "verified": True, "refreshed": True,
                    "strategy": "session", "message": "", "at": "2026-10-06T00:00:00+00:00",
                },
                "access_token": "at-new",
                "session_token": "st-rotated",
            },
        }
        held_by_thread: dict[int, int] = {}
        # 无锁是有意的：每个线程只读写自己 tid 的键，Python dict 的单键
        # 读改写在这里由 GIL 保证原子性；探针若被复用到跨线程共享计数的
        # 场景，需要改成加锁或 Counter（复审建议）。

        def _on_checkout(_dbapi_conn, _conn_record, _conn_proxy):
            tid = _threading.get_ident()
            held_by_thread[tid] = held_by_thread.get(tid, 0) + 1

        def _on_checkin(_dbapi_conn, _conn_record):
            tid = _threading.get_ident()
            held_by_thread[tid] = held_by_thread.get(tid, 0) - 1

        _event.listen(engine, "checkout", _on_checkout)
        _event.listen(engine, "checkin", _on_checkin)
        self.addCleanup(lambda: _event.remove(engine, "checkout", _on_checkout))
        self.addCleanup(lambda: _event.remove(engine, "checkin", _on_checkin))

        observed: list[int] = []

        def _probe(_instance, _action_id, _account, _params):
            # 网络调用发生在这一层；此刻本线程不该还握着连接。
            observed.append(held_by_thread.get(_threading.get_ident(), 0))
            return fake_action_result

        with mock.patch(
            "platforms.chatgpt.plugin.ChatGPTPlatform.execute_action",
            autospec=True,
            side_effect=_probe,
        ):
            response = self.client.post(
                "/tasks/refresh-token",
                json={"account_ids": [account_id], "delay_seconds": 0},
            )
            task_id = response.json()["task_id"]
            snapshot = self.client.get(f"/tasks/{task_id}").json()

        self.assertEqual(snapshot["success"], 1)
        self.assertTrue(observed, "execute_action 没有被调用 —— 探针无效")
        self.assertEqual(
            observed[0], 0,
            f"网络调用期间本线程仍有 {observed[0]} 条连接在借 —— 并发批量会拖垮连接池",
        )

    def test_runner_binds_task_control_to_the_platform_instance(self):
        """停止/跳过开关要绑到平台实例（登录兜底等码时能当场打断）。"""
        account_id = self._id_of("runner-me@example.com")
        seen: dict = {}

        def _probe(instance, _action_id, _account, _params):
            seen["control"] = getattr(instance, "_task_control", None)
            seen["attempt"] = getattr(instance, "_task_attempt_token", None)
            return {"ok": True, "data": {"message": "ok"}, "account_extra_patch": {}}

        with mock.patch(
            "platforms.chatgpt.plugin.ChatGPTPlatform.execute_action",
            autospec=True,
            side_effect=_probe,
        ):
            response = self.client.post(
                "/tasks/refresh-token",
                json={"account_ids": [account_id], "delay_seconds": 0},
            )
            task_id = response.json()["task_id"]
            self.client.get(f"/tasks/{task_id}")

        self.assertIsNotNone(seen.get("control"), "停止/跳过开关没绑到平台实例")
        self.assertTrue(hasattr(seen["control"], "checkpoint"))

    def test_runner_counts_failure_and_keeps_credentials(self):
        account_id = self._id_of("runner-me@example.com")
        fake_action_result = {
            "ok": False,
            "error": "账号没有可用的刷新方式",
            "data": {"message": "账号没有可用的刷新方式"},
            "account_extra_patch": {
                "chatgpt_token_refresh": {
                    "ok": False, "verified": False, "refreshed": False,
                    "strategy": "", "message": "账号没有可用的刷新方式",
                    "at": "2026-10-06T00:00:00+00:00",
                }
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
            snapshot = self.client.get(f"/tasks/{response.json()['task_id']}").json()

        self.assertEqual(snapshot["status"], "done")
        self.assertEqual(snapshot["success"], 0)
        self.assertEqual(len(snapshot["errors"]), 1)


class FrontendRefreshWiringTests(unittest.TestCase):
    """前端「更多」菜单要有「刷新 Token」，任务面板认识 refresh_token 类型。"""

    def _src(self, rel: str) -> str:
        return (ROOT / "frontend" / "src" / rel).read_text(encoding="utf-8")

    def test_more_menu_has_refresh_token_item(self):
        src = self._src("pages/Accounts.tsx")
        self.assertIn("刷新 Token", src, "「更多」菜单里没有批量刷新 Token")
        self.assertIn("'/tasks/refresh-token'", src, "没有接刷新 Token 的任务端点")

    def test_task_panel_knows_refresh_kind(self):
        src = self._src("components/TaskLogPanel.tsx")
        self.assertIn("'refresh_token'", src, "TaskKind 没有 refresh_token")
        self.assertIn("refresh_token:", src, "KIND_TEXT 没有 refresh_token 文案")


if __name__ == "__main__":
    unittest.main()
