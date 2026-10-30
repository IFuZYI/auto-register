"""限流状态 / 异常账号恢复 / 不成功则禁用（用户要求 2026-10-30）。

三条语义各自要钉住，否则会出现「看起来做了、实际没生效」：

1. **限流（429）落 `limited`，不落 `invalid`** —— 判成失效会把它排进「重新登录」
   队列，而重登解决不了额度问题（实测 47 个号的限流痕迹只活在 extra 里，
   `status` 列里一个 `limited` 都没有）。
2. **异常账号能被主动恢复** —— 之前 65 个 `invalid` 连一次恢复都没试过
   （`chatgpt_auto_refresh_enabled` 默认 false）。
3. **恢复失败达上限 → 落禁用** —— 之前只置 `disabled: True`，`status` 一个字
   节都不改，界面永远显示「失效」，用户分不清「还能救」和「已放弃」。
"""

from __future__ import annotations

import unittest
from unittest import mock

from core.base_platform import AccountStatus
from services.chatgpt_account_state import (
    BANNED_ACCOUNT_STATUS,
    LIMITED_ACCOUNT_STATUS,
    apply_chatgpt_status_policy,
    probe_confirms_usable_strongly,
)
from services.chatgpt_maintenance import (
    AUTO_REFRESH_STATE_KEY,
    MAX_ATTEMPTS,
    record_auto_refresh_result,
)


class _Row:
    """最小的账号行替身（状态 + extra + id）。"""

    def __init__(self, status: str = "registered", extra: dict | None = None, row_id: str = "a-1"):
        self.status = status
        self.extra = dict(extra or {})
        self.id = row_id

    def get_extra(self) -> dict:
        return dict(self.extra)

    def set_extra(self, extra: dict) -> None:
        self.extra = dict(extra)


class LimitedStatusTests(unittest.TestCase):
    def test_enum_has_limited(self):
        self.assertIn("limited", AccountStatus.values())

    def test_limited_is_not_active_but_counts_as_registered(self):
        """限流号不该被派活，但邮箱仍被占用 —— 不能拿它重新注册。"""
        self.assertFalse(AccountStatus.is_active("limited"))
        self.assertTrue(AccountStatus.counts_as_registered("limited"))

    def test_429_probe_lands_limited_not_invalid(self):
        account = _Row("registered")
        reason = apply_chatgpt_status_policy(
            account,
            local_probe={"auth": {"http_status": 429, "state": "quota_exhausted"}},
        )
        self.assertEqual(reason, "auth_rate_limited")
        self.assertEqual(account.status, LIMITED_ACCOUNT_STATUS)
        self.assertNotEqual(account.status, "invalid")

    def test_codex_quota_lands_limited(self):
        account = _Row("registered")
        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "access_token_valid", "http_status": 200},
                "codex": {"state": "quota_exhausted", "http_status": 429},
            },
        )
        self.assertEqual(reason, "codex_rate_limited")
        self.assertEqual(account.status, LIMITED_ACCOUNT_STATUS)

    def test_limited_recovers_to_registered_when_usable(self):
        """额度回来了就该恢复 —— 限流不是终局。"""
        account = _Row(LIMITED_ACCOUNT_STATUS)
        reason = apply_chatgpt_status_policy(
            account,
            local_probe={"auth": {"state": "access_token_valid", "http_status": 200}},
        )
        self.assertEqual(reason, "recovered")
        self.assertEqual(account.status, "registered")


class BannedRecoveryEvidenceTests(unittest.TestCase):
    """禁用是强判断，但**双证据**可用时必须能复活。

    实测 14 个账号：`auth=access_token_valid` + `codex=usable`（2026-10-26 探的），
    状态却是 banned —— 它们从此再没被任何流程碰过。
    """

    def test_strong_evidence_requires_both_paths(self):
        self.assertTrue(
            probe_confirms_usable_strongly(
                {"auth": {"state": "access_token_valid"}, "codex": {"state": "usable"}}
            )
        )
        self.assertFalse(
            probe_confirms_usable_strongly(
                {"auth": {"state": "access_token_valid"}, "codex": {"state": "not_checked"}}
            )
        )
        self.assertFalse(
            probe_confirms_usable_strongly(
                {"auth": {"state": "unauthorized"}, "codex": {"state": "usable"}}
            )
        )

    def test_banned_recovers_with_strong_evidence(self):
        account = _Row(BANNED_ACCOUNT_STATUS)
        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {"state": "access_token_valid", "http_status": 200},
                "codex": {"state": "usable", "http_status": 200},
            },
        )
        self.assertEqual(reason, "recovered_from_banned")
        self.assertEqual(account.status, "registered")

    def test_banned_stays_banned_with_weak_evidence(self):
        """弱信号不得把禁用洗掉 —— 这是原有的防事故语义，必须保住。"""
        account = _Row(BANNED_ACCOUNT_STATUS)
        reason = apply_chatgpt_status_policy(
            account,
            local_probe={"auth": {"state": "access_token_valid", "http_status": 200}},
        )
        self.assertEqual(reason, "")
        self.assertEqual(account.status, BANNED_ACCOUNT_STATUS)


class RecoveryExhaustionDisablesTests(unittest.TestCase):
    """「不成功则禁用」：连续失败达上限就落 banned，不只是置 disabled。"""

    def test_first_failures_only_back_off(self):
        extra: dict = {}
        row = _Row("invalid")
        for attempt in (1, 2):
            state = record_auto_refresh_result(extra, ok=False, now=1000, row=row)
            self.assertEqual(state["attempts"], attempt)
            self.assertFalse(state["disabled"])
            self.assertEqual(row.status, "invalid", "还没到上限就落禁用了")

    def test_last_failure_marks_banned(self):
        extra: dict = {}
        row = _Row("invalid")
        for _ in range(MAX_ATTEMPTS):
            state = record_auto_refresh_result(extra, ok=False, now=1000, row=row)
        self.assertTrue(state["disabled"])
        self.assertEqual(state["disabled_reason"], "recovery_exhausted")
        self.assertEqual(row.status, BANNED_ACCOUNT_STATUS, "恢复用尽却没落禁用")

    def test_configuration_error_does_not_disable(self):
        """`invalid_client` 是我们的配错，不是账号坏了 —— 落禁用会误杀好号。"""
        extra = {"chatgpt_token_refresh": {"refresh_kind": "configuration"}}
        row = _Row("invalid")
        for _ in range(MAX_ATTEMPTS):
            state = record_auto_refresh_result(extra, ok=False, now=1000, row=row)
        self.assertTrue(state["disabled"])
        self.assertEqual(row.status, "invalid", "配置错误把好号标成了禁用")

    def test_success_clears_the_counter(self):
        extra: dict = {}
        row = _Row("invalid")
        record_auto_refresh_result(extra, ok=False, now=1000, row=row)
        state = record_auto_refresh_result(extra, ok=True, now=1000, row=row)
        self.assertEqual(state, {})
        self.assertNotIn(AUTO_REFRESH_STATE_KEY, extra)

    def test_success_revives_a_banned_account(self):
        """恢复成功要能把禁用解掉（否则「救回来了」但界面还是禁用）。"""
        extra = {AUTO_REFRESH_STATE_KEY: {"disabled": True, "attempts": MAX_ATTEMPTS}}
        row = _Row(BANNED_ACCOUNT_STATUS)
        record_auto_refresh_result(extra, ok=True, now=1000, row=row)
        self.assertNotIn(AUTO_REFRESH_STATE_KEY, extra)


class RecoverTaskSelectionTests(unittest.TestCase):
    """恢复任务的选号：只挑可恢复状态，跳过已达上限的。

    用真实临时库（conftest 已把 DATABASE_URL 指到 tmp），不 mock 后端 ——
    这条要验的正是「装配起来真的能挑对号」。
    """

    EMAILS = {
        "inv": ("invalid", None),
        "exp": ("expired", None),
        "lim": ("limited", None),
        "ok": ("registered", None),
        "dead": ("banned", {AUTO_REFRESH_STATE_KEY: {"disabled": True, "attempts": 3}}),
    }

    def setUp(self):
        from core.base_platform import Account
        from core.db import account_repository

        self._created = []
        for key, (status, extra) in self.EMAILS.items():
            acc = account_repository.upsert(
                Account(
                    platform="chatgpt",
                    email=f"rec-{key}@x.c",
                    password="p",
                    status=status,
                    extra=extra or {},
                )
            )
            self._created.append(str(acc.id))

    def tearDown(self):
        from core.db import account_repository

        for account_id in self._created:
            try:
                account_repository.delete(account_id, "chatgpt")
            except Exception:  # noqa: BLE001
                pass

    def test_candidates_endpoint_reports_by_status(self):
        # 直接调端点函数，不走 TestClient：`TestClient(main.app)` 会跑
        # lifespan（起 scheduler / 维护任务），实测把这条用例拖到 2 分钟。
        from api.tasks import preview_recover_candidates

        body = preview_recover_candidates()
        self.assertEqual(body["by_status"].get("invalid"), 1, body)
        self.assertEqual(body["by_status"].get("expired"), 1, body)
        self.assertEqual(body["by_status"].get("limited"), 1, body)
        self.assertNotIn("registered", body["by_status"], body)
        # banned + disabled=True 的号不该进「可恢复」列表
        self.assertEqual(body["exhausted"], 1, body)
        emails = {item["email"] for item in body["items"]}
        self.assertIn("rec-inv@x.c", emails)
        self.assertIn("rec-lim@x.c", emails)
        self.assertNotIn("rec-ok@x.c", emails)
        self.assertNotIn("rec-dead@x.c", emails)

    def test_recover_endpoint_refuses_when_nothing_to_recover(self):
        """没有可恢复的号时明确 400，而不是建一个空任务。"""
        from fastapi import HTTPException

        from api.tasks import RecoverAccountsTaskRequest, create_recover_accounts_task

        with mock.patch("fastapi.BackgroundTasks") as bg:
            with self.assertRaises(HTTPException) as ctx:
                create_recover_accounts_task(
                    RecoverAccountsTaskRequest(account_ids=[], statuses=["banned"]),
                    bg,
                )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("没有需要恢复的账号", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
