"""探测时刻（`probe_checked_at`）的写回 —— AWS / Kiro 抽检轮转的前提。

「最久未检测优先」的抽检（`services.local_maintenance.select_probe_targets`）
靠 `last_probe_at` 排序。aws / kiro 的探测链路只落状态与凭据、从不写时间戳，
于是每轮读到的 `last_probe_at` 都是 0 —— 排序退化成 id 序，抽检**永远挑同一批
号**（第一批永远是最先入库的那些）。

契约：**任何一次探测都要盖时间戳**，包括 400 / 403 / 网络不明（"测不了"）。
失败也是「已经看过一眼」，不写就会一直被重新挑中。

毫秒 epoch 与 `extra["expires_at"]` 同款（`core/probe_credentials.py`）。
"""
from __future__ import annotations

import unittest
from unittest import mock

from core.probe_credentials import mark_probe_checked


def setUpModule():
    """动作链要 `core.registry.get("aws")` —— 插件模块必须先被导入注册。"""
    from core.registry import load_all

    load_all()


class _Row:
    """只实现 `mark_probe_checked` 用到的两个方法。"""

    def __init__(self, extra: dict | None = None) -> None:
        self._extra = dict(extra or {})

    def get_extra(self) -> dict:
        return dict(self._extra)

    def set_extra(self, extra: dict) -> None:
        self._extra = dict(extra)


def _model(platform: str = "aws", status: str = "registered"):
    from core.db.models_account import AccountModel

    model = AccountModel(
        platform=platform, email="probe-stamp@example.com", password="p", status=status
    )
    model.id = f"{platform}-stamp-1"
    return model


class _FakePlugin:
    """最小插件替身：`check_valid` + `apply_probe_status_policy` + `last_probe_detail`。"""

    last_probe_detail: dict = {"usable": False, "code": 400, "summary": "invalid_grant"}

    def __init__(self, config=None) -> None:
        self.config = config

    def check_valid(self, _account) -> bool:
        return False

    def apply_probe_status_policy(self, account, *, detail: dict) -> str:
        account.status = "invalid"
        return "刷新失败"


class MarkProbeCheckedTests(unittest.TestCase):
    def test_writes_millisecond_epoch(self):
        row = _Row()
        self.assertTrue(mark_probe_checked(row, {"code": 200, "usable": True}))
        stamp = row.get_extra()["probe_checked_at"]
        self.assertIsInstance(stamp, int, "毫秒 epoch 用 int（与 expires_at 同款）")
        self.assertGreater(stamp, 10**12, "单位是毫秒，不是秒")

    def test_stamps_every_probe_outcome(self):
        """成功、400、403、测不了 —— 都算「看过一眼」。"""
        for detail in (
            {"code": 200, "usable": True},
            {"code": 400, "usable": False},
            {"code": 403, "usable": False, "banned": True},
            {"usable": None, "indeterminate": True},
            {},
        ):
            with self.subTest(detail=detail):
                row = _Row()
                self.assertTrue(mark_probe_checked(row, detail))
                self.assertIn("probe_checked_at", row.get_extra())

    def test_keeps_existing_extra(self):
        row = _Row({"access_token": "at"})
        mark_probe_checked(row, {"code": 200})
        self.assertEqual(row.get_extra()["access_token"], "at", "写时间戳不能丢别的字段")

    def test_bad_model_does_not_raise(self):
        class _Boom:
            def get_extra(self):
                raise RuntimeError("坏行")

        self.assertFalse(mark_probe_checked(_Boom(), {"code": 200}))


class ActionChainStampsProbeTests(unittest.TestCase):
    """动作链（`apply_action_result` → `_apply_aws_kiro_status`）必须盖章。"""

    def _apply(self, platform: str, action_id: str, result: dict):
        from services.action_execution import apply_action_result

        model = _model(platform)
        apply_action_result(platform, action_id, model, result, mock.Mock())
        return model

    def test_probe_200_stamps(self):
        model = self._apply(
            "aws",
            "probe",
            {
                "ok": True,
                "data": {
                    "status": 200,
                    "probe": {
                        "usable": True,
                        "code": 200,
                        "access_token": "at",
                        "refresh_token": "rt",
                        "expires_in": 3600,
                    },
                },
            },
        )
        self.assertGreater(model.get_extra()["probe_checked_at"], 10**12)

    def test_probe_400_stamps(self):
        model = self._apply(
            "kiro",
            "probe",
            {
                "ok": False,
                "data": {
                    "status": 400,
                    "probe": {"usable": False, "code": 400, "summary": "invalid_grant"},
                },
            },
        )
        self.assertGreater(model.get_extra()["probe_checked_at"], 10**12)

    def test_probe_403_stamps(self):
        model = self._apply(
            "aws",
            "probe",
            {
                "ok": False,
                "data": {
                    "status": 403,
                    "probe": {"usable": False, "code": 403, "banned": True},
                },
            },
        )
        self.assertGreater(model.get_extra()["probe_checked_at"], 10**12)
        self.assertEqual(model.status, "banned")

    def test_refresh_action_does_not_stamp(self):
        """刷新不是「检测」：盖戳会让高频刷新的号在抽检排序里永远像刚看过。"""
        model = self._apply(
            "kiro",
            "refresh_token",
            {
                "ok": True,
                "data": {
                    "access_token": "at-new",
                    "refresh_token": "rt-new",
                    "expires_in": 3600,
                },
            },
        )
        self.assertNotIn("probe_checked_at", model.get_extra())
        self.assertEqual(model.get_extra()["access_token"], "at-new")

    def test_probe_indeterminate_stamps(self):
        """网络错误不改状态，但「看过一眼」要记下来。"""
        model = self._apply(
            "kiro",
            "probe",
            {
                "ok": False,
                "data": {
                    "status": None,
                    "probe": {"usable": None, "indeterminate": True, "summary": "timeout"},
                },
            },
        )
        self.assertGreater(model.get_extra()["probe_checked_at"], 10**12)
        self.assertEqual(model.status, "registered", "测不了不该改状态")


class SingleCheckStampsProbeTests(unittest.TestCase):
    """`api.accounts._do_check`（单账号「检测」按钮）也要盖章。"""

    def test_do_check_writes_probe_checked_at(self):
        from api.accounts import _do_check
        from core.db import account_repository

        row = _model("aws")
        with (
            mock.patch("core.registry.get", lambda _platform: _FakePlugin),
            mock.patch.object(account_repository, "get", side_effect=lambda *a, **k: row),
            mock.patch.object(account_repository, "upsert", lambda *a, **k: None),
        ):
            _do_check(str(row.id))
        self.assertGreater(row.get_extra()["probe_checked_at"], 10**12)


class SchedulerCheckStampsProbeTests(unittest.TestCase):
    """`core.scheduler.check_accounts_valid`（批量测活）也要盖章。"""

    def test_scheduler_writes_probe_checked_at(self):
        from core.db import account_repository
        from core.scheduler import scheduler

        row = _model("kiro")
        with (
            mock.patch("core.scheduler.load_all", lambda: None),
            mock.patch("core.scheduler.get", lambda _platform: _FakePlugin),
            mock.patch.object(
                account_repository, "list_all_accounts", lambda platform="": [row]
            ),
            mock.patch.object(account_repository, "get", side_effect=lambda *a, **k: row),
            mock.patch.object(account_repository, "upsert", lambda *a, **k: None),
        ):
            result = scheduler.check_accounts_valid("kiro", limit=5)
        self.assertEqual(result["invalid"], 1)
        self.assertGreater(row.get_extra()["probe_checked_at"], 10**12)


if __name__ == "__main__":
    unittest.main()
