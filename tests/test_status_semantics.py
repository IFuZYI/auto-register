"""状态语义：正常 / 过期 / 失效 / 禁用 —— 判定分档。

用户要求（2026-10-06）：
- 「已注册」改名「正常」= 正常能使用的账号；
- 「已过期」改名「过期」= AT 已过期（时间维度：JWT exp 已过）；
- 「已失效」改名「失效」= 需要重新登录的（凭证被拒），走流程登录；
- 「已封禁」改名「禁用」= 被封了的账号（登录流程发掘）。

判定优先级（探测发现凭证被拒时）：禁用 > 过期 > 失效。
- 封禁措辞（deleted or deactivated）→ banned；
- 否则 AT 的 exp 已过 → expired（刷新可能救回）；
- 否则（AT 未过期却被拒 = 被吊销/会话失效）→ invalid（需要重新登录）。

用户修正（2026-10-06）：「Your sign-in session is no longer valid…invalid_state」
不是封禁 —— 会话/state 不同步而已，可重试；只有「deleted or deactivated」
这类「号没了」措辞才判禁用。
"""

from __future__ import annotations

import base64
import json
import time
import unittest

from core.base_platform import AccountStatus
from platforms.chatgpt.login_refresh import looks_like_banned as looks_like_login_banned
from services.chatgpt_account_state import (
    BANNED_ACCOUNT_STATUS,
    apply_chatgpt_status_policy,
    classify_local_probe_state,
)

# 用户修正（2026-10-06）：这不是封禁 —— 会话/state 不同步，可重试。
SIGNIN_SESSION_TEXT = (
    "Your sign-in session is no longer valid. Please start over to continue."
)
DEACTIVATED_TEXT = (
    "You do not have an account because it has been deleted or deactivated."
)


def _jwt(claims: dict) -> str:
    """构造一个 payload 段可解的假 JWT（不验签，只需 base64 合法）。"""
    def seg(data: dict) -> str:
        raw = json.dumps(data).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"


def _expired_at() -> str:
    """exp 已过（1 小时前）的 AT。"""
    return _jwt({"iat": int(time.time()) - 7200, "exp": int(time.time()) - 3600})


def _valid_at() -> str:
    """exp 还早（24 小时后）的 AT。"""
    return _jwt({"iat": int(time.time()) - 60, "exp": int(time.time()) + 86400})


class _Account:
    def __init__(self, status: str = "registered", extra: dict | None = None):
        self.status = status
        self.extra = extra or {}


class ExpiredVsInvalidTests(unittest.TestCase):
    """过期（AT exp 已过）与失效（需要重新登录）要分开。"""

    def test_expired_access_token_marks_expired(self):
        """AT 已过期（401 且 JWT exp 已过）→ expired，不是 invalid。"""
        account = _Account(extra={"access_token": _expired_at()})
        reason = apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {
                    "state": "access_token_invalidated",
                    "http_status": 401,
                    "error_code": "token_invalidated",
                    "message": "invalidated",
                }
            },
        )
        self.assertEqual(account.status, AccountStatus.EXPIRED.value,
                         "AT 已过期的账号应标「过期」")
        self.assertTrue(reason)

    def test_unexpired_but_rejected_token_marks_invalid(self):
        """AT 未过期却被拒（被吊销/会话失效）→ invalid（需要重新登录）。"""
        account = _Account(extra={"access_token": _valid_at()})
        apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {
                    "state": "access_token_invalidated",
                    "http_status": 401,
                    "error_code": "token_invalidated",
                    "message": "invalidated",
                }
            },
        )
        self.assertEqual(account.status, AccountStatus.INVALID.value,
                         "AT 未过期却被拒 = 需要重新登录，应标「失效」")

    def test_missing_access_token_marks_invalid(self):
        """没有 AT 可探测 → invalid（需要重新登录拿凭证）。"""
        account = _Account(extra={})
        apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {
                    "state": "missing_access_token",
                    "http_status": 0,
                    "message": "账号缺少 access_token",
                }
            },
        )
        self.assertEqual(account.status, AccountStatus.INVALID.value)


class BannedSignalTests(unittest.TestCase):
    """禁用（banned）信号：只认「deleted or deactivated」这类「号没了」措辞。

    用户修正（2026-10-06）：「Your sign-in session is no longer valid…invalid_state」
    **不是封禁** —— 那是会话失效 / OAuth state 参数不同步（Cookie、会话或跳转
    不同步导致），可重开入口重试。只有「deleted or deactivated」才是封禁。
    """

    def test_signin_session_message_is_not_banned(self):
        """用户修正：sign-in session 消息 = 会话失效，不是封禁。"""
        self.assertFalse(
            looks_like_login_banned(SIGNIN_SESSION_TEXT),
            "sign-in session 消息被误判成封禁 —— 它只是会话/state 不同步",
        )

    def test_deactivated_message_still_banned(self):
        self.assertTrue(looks_like_login_banned(DEACTIVATED_TEXT))

    def test_ordinary_failures_not_banned(self):
        for text in (
            "invalid_grant",
            "invalid_state",
            SIGNIN_SESSION_TEXT,
            "network timeout",
            "",
        ):
            self.assertFalse(looks_like_login_banned(text), text)

    def test_banned_beats_expired(self):
        """同时有封禁措辞与过期 AT 时，封禁优先（号没了，谈过期没意义）。"""
        account = _Account(extra={"access_token": _expired_at()})
        apply_chatgpt_status_policy(
            account,
            local_probe={
                "auth": {
                    "state": "account_deactivated",
                    "http_status": 403,
                    "error_code": "account_deactivated",
                    "message": DEACTIVATED_TEXT,
                }
            },
        )
        self.assertEqual(account.status, BANNED_ACCOUNT_STATUS)


class ClassifyProbeStateTests(unittest.TestCase):
    """classify_local_probe_state 要能区分 deactivated 与普通 401。"""

    def test_deactivated_probe(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {
                        "state": "account_deactivated",
                        "http_status": 403,
                        "error_code": "account_deactivated",
                        "message": DEACTIVATED_TEXT,
                    }
                }
            ),
            "auth_deactivated",
        )

    def test_401_probe(self):
        self.assertEqual(
            classify_local_probe_state(
                {"auth": {"state": "access_token_invalidated", "http_status": 401}}
            ),
            "auth_401",
        )


class RecoveryTests(unittest.TestCase):
    """正向确认：探测/刷新确认可用时，过期/失效恢复为「正常」。

    语义（用户要求）：「正常」= 正常能使用的账号 —— 状态要跟着实际可用性走，
    否则刷新好凭证后还挂着「失效」，筛选出来的行就不对。
    禁用不自动恢复（封禁是强判断，要人工确认）。
    """

    def test_valid_probe_recovers_expired(self):
        account = _Account(status="expired", extra={"access_token": _valid_at()})
        apply_chatgpt_status_policy(
            account,
            local_probe={"auth": {"state": "access_token_valid", "http_status": 200}},
        )
        self.assertEqual(account.status, "registered", "探测确认可用后应恢复「正常」")

    def test_valid_probe_recovers_invalid(self):
        account = _Account(status="invalid", extra={"access_token": _valid_at()})
        apply_chatgpt_status_policy(
            account,
            local_probe={"auth": {"state": "access_token_valid", "http_status": 200}},
        )
        self.assertEqual(account.status, "registered")

    def test_valid_probe_does_not_resurrect_banned(self):
        """禁用是强判断（登录流程发掘），不因一次探测就自动恢复。"""
        account = _Account(status="banned", extra={"access_token": _valid_at()})
        apply_chatgpt_status_policy(
            account,
            local_probe={"auth": {"state": "access_token_valid", "http_status": 200}},
        )
        self.assertEqual(account.status, "banned")

    def test_refresh_success_recovers(self):
        """刷新成功（usable=True）→ 过期/失效恢复「正常」。"""
        account = _Account(status="expired")
        apply_chatgpt_status_policy(account, usable=True)
        self.assertEqual(account.status, "registered")

    def test_no_signal_does_not_touch_registered(self):
        account = _Account(status="registered")
        apply_chatgpt_status_policy(account, usable=True)
        self.assertEqual(account.status, "registered")


class ProbeUsableSignalTests(unittest.TestCase):
    """「探测确认可用」的信号源必须是明确的正向状态，不能靠缺省。"""

    def test_access_token_valid_is_usable(self):
        from services.chatgpt_account_state import probe_confirms_usable

        self.assertTrue(
            probe_confirms_usable({"auth": {"state": "access_token_valid"}})
        )

    def test_empty_probe_is_not_usable(self):
        from services.chatgpt_account_state import probe_confirms_usable

        self.assertFalse(probe_confirms_usable(None))
        self.assertFalse(probe_confirms_usable({}))
        self.assertFalse(
            probe_confirms_usable({"auth": {"state": "probe_failed"}})
        )


class FrontendStatusLabelContractTests(unittest.TestCase):
    """前端状态展示：显示中文语义标签（正常/过期/失效/禁用）。

    用户要求（2026-10-06）：「已注册」改名「正常」；「已过期」改名「过期」；
    「已失效」改名「失效」；「已封禁」改名「禁用」。

    实测（2026-10-06）：账号列表的状态列此前直接渲染**英文原值**
    （`<Tag>{status}</Tag>` → 显示 "registered"），筛选下拉是旧文案。
    """

    def _src(self, rel: str) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "frontend" / "src" / rel).read_text(
            encoding="utf-8"
        )

    def test_account_format_exposes_status_meta(self):
        """`accountStatusMeta` 是标签的唯一来源（Accounts/Dashboard 共用）。"""
        src = self._src("lib/accountFormat.ts")
        self.assertIn("export function accountStatusMeta", src)
        for label in ("'正常'", "'过期'", "'失效'", "'禁用'"):
            self.assertIn(label, src, f"accountFormat.ts 缺 {label} 映射")

    def test_accounts_page_uses_status_meta(self):
        """账号页状态列必须走 accountStatusMeta —— 不再显示英文原值。"""
        src = self._src("pages/Accounts.tsx")
        self.assertIn("accountStatusMeta", src, "Accounts.tsx 没有接 accountStatusMeta")
        self.assertNotIn(
            ">{status}</Tag>", src,
            "状态列还在直接渲染英文原值（status 字符串）",
        )

    def test_accounts_filter_options_use_new_labels(self):
        src = self._src("pages/Accounts.tsx")
        for value, label in (
            ("'registered'", "'正常'"),
            ("'expired'", "'过期'"),
            ("'invalid'", "'失效'"),
            ("'banned'", "'禁用'"),
        ):
            self.assertIn(
                f"value: {value}, label: {label}",
                src,
                f"筛选/详情选项缺 {value} → {label}",
            )

    def test_dashboard_uses_status_meta(self):
        """仪表盘的状态分布与卡片也要走同一映射。"""
        src = self._src("pages/Dashboard.tsx")
        self.assertIn("accountStatusMeta", src, "Dashboard.tsx 没有接 accountStatusMeta")
        for stale in ("'已注册'", "'已封禁'"):
            self.assertNotIn(stale, src, f"仪表盘还留着旧文案 {stale}")


class ActionWiringContractTests(unittest.TestCase):
    """动作结果 → 状态落库的接线（api/actions.py `_apply_action_result`）。

    - grok 的 probe / probe_refresh / refresh_token 此前**不写状态**（只有
      chatgpt 有策略）—— 用户要求 grok 失效判定与 grok2api 对齐，必须接线；
    - chatgpt 的 refresh_token 成功时要走正向恢复（过期/失效 → 正常）。

    行为测试（真调 `_apply_action_result`）而不是源码扫描 —— 扫描对
    「接线被改成死分支」这类变异无感（实测变异 9 逃逸）。
    """

    def _model(self, platform: str, status: str = "registered"):
        from core.db.models_account import AccountModel

        return AccountModel(platform=platform, email="wire@example.com", password="p", status=status)

    def test_grok_probe_401_writes_status(self):
        from unittest import mock

        from api.actions import _apply_action_result

        model = self._model("grok")
        _apply_action_result(
            "grok", "probe", model,
            {"ok": False, "data": {"status": 401, "summary": "unauthorized"}},
            mock.Mock(),
        )
        self.assertEqual(model.status, "invalid", "grok probe 失败没有落状态 —— 接线断了")

    def test_grok_probe_200_recovers(self):
        from unittest import mock

        from api.actions import _apply_action_result

        model = self._model("grok", status="invalid")
        _apply_action_result(
            "grok", "probe", model,
            {"ok": True, "data": {"status": 200, "summary": "{}"}},
            mock.Mock(),
        )
        self.assertEqual(model.status, "registered")

    def test_chatgpt_refresh_marks_recovery(self):
        """chatgpt 刷新成功 → 过期/失效恢复「正常」。"""
        from unittest import mock

        from api.actions import _apply_action_result

        model = self._model("chatgpt", status="invalid")
        _apply_action_result(
            "chatgpt", "refresh_token", model,
            {"ok": True, "data": {"access_token": "fresh"}},
            mock.Mock(),
        )
        self.assertEqual(model.status, "registered")


class RemoteRecoveryTests(unittest.TestCase):
    """CPA 远端同步确认可用（remote_state=usable）→ 恢复「正常」。"""

    def test_remote_usable_recovers(self):
        account = _Account(status="invalid")
        apply_chatgpt_status_policy(account, remote_sync={"remote_state": "usable"})
        self.assertEqual(account.status, "registered")

    def test_remote_usable_does_not_resurrect_banned(self):
        account = _Account(status="banned")
        apply_chatgpt_status_policy(account, remote_sync={"remote_state": "usable"})
        self.assertEqual(account.status, "banned")

    def test_remote_unreachable_does_not_touch(self):
        account = _Account(status="invalid")
        reason = apply_chatgpt_status_policy(
            account, remote_sync={"remote_state": "unreachable"}
        )
        self.assertEqual(reason, "")
        self.assertEqual(account.status, "invalid")


if __name__ == "__main__":
    unittest.main()
