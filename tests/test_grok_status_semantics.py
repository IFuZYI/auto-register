"""Grok 账号状态判定。

状态语义（用户要求 2026-10-06，与全站统一）：
- registered（正常）：正常能用；
- expired（过期）：AT 已过期（JWT exp 已过）；
- invalid（失效）：需要重新登录 —— 对齐 grok2api 的 reauthRequired 语义：
  SSO / OAuth 被上游明确拒绝（401、invalid_grant 等永久错误）；
- banned（禁用）：被封（上游明确 blocked-user / user is blocked）。
"""

from __future__ import annotations

import unittest

from core.base_platform import AccountStatus
from services.grok_account_state import (
    apply_grok_status_policy,
    looks_like_grok_blocked,
)

from tests.test_status_semantics import _expired_at, _valid_at


class _Account:
    def __init__(self, status: str = "registered", extra: dict | None = None):
        self.status = status
        self.extra = extra or {}


class BlockedSignalTests(unittest.TestCase):
    """封禁信号对齐 grok2api 的 IsDefinitiveAccountBlockBody。"""

    def test_blocked_user_marker(self):
        self.assertTrue(looks_like_grok_blocked("blocked-user"))
        self.assertTrue(looks_like_grok_blocked('{"error":"user is blocked"}'))

    def test_ordinary_errors_not_blocked(self):
        for text in ("unauthorized", "invalid token", "rate limit", ""):
            self.assertFalse(looks_like_grok_blocked(text), text)


class GrokExpiredVsInvalidTests(unittest.TestCase):
    """过期（AT exp 已过）与失效（被拒需重登）分开。"""

    def test_probe_401_with_expired_at_marks_expired(self):
        account = _Account(extra={"access_token": _expired_at()})
        reason = apply_grok_status_policy(account, probe_code=401, probe_summary="unauthorized")
        self.assertEqual(account.status, AccountStatus.EXPIRED.value)
        self.assertTrue(reason)

    def test_probe_401_with_unexpired_at_marks_invalid(self):
        """AT 未过期却被拒（吊销/SSO 被拒）→ 失效，需要重新登录。"""
        account = _Account(extra={"access_token": _valid_at()})
        apply_grok_status_policy(account, probe_code=401, probe_summary="unauthorized")
        self.assertEqual(account.status, AccountStatus.INVALID.value)

    def test_permanent_refresh_error_marks_invalid(self):
        """invalid_grant 等永久错误 → 失效（对齐 grok2api reauthRequired）。"""
        account = _Account(extra={"access_token": _valid_at()})
        reason = apply_grok_status_policy(
            account, refresh_kind="permanent", refresh_error="invalid_grant"
        )
        self.assertEqual(account.status, AccountStatus.INVALID.value)
        self.assertTrue(reason)

    def test_transient_refresh_error_does_not_touch(self):
        """瞬时错误（rate_limited / 网络）不该标失效。"""
        account = _Account()
        reason = apply_grok_status_policy(
            account, refresh_kind="retryable", refresh_error="HTTP 503"
        )
        self.assertEqual(reason, "")
        self.assertEqual(account.status, "registered")

    def test_configuration_error_does_not_touch(self):
        """网关配置问题不是账号问题（grok2api 同款口径）。"""
        account = _Account()
        reason = apply_grok_status_policy(
            account, refresh_kind="configuration", refresh_error="invalid_client"
        )
        self.assertEqual(reason, "")
        self.assertEqual(account.status, "registered")


class GrokBlockedTests(unittest.TestCase):
    def test_probe_blocked_marks_banned(self):
        account = _Account()
        apply_grok_status_policy(
            account, probe_code=403, probe_summary='{"code":"blocked-user"}'
        )
        self.assertEqual(account.status, AccountStatus.BANNED.value)

    def test_blocked_beats_expired(self):
        account = _Account(extra={"access_token": _expired_at()})
        apply_grok_status_policy(
            account, probe_code=403, probe_summary="user is blocked"
        )
        self.assertEqual(account.status, AccountStatus.BANNED.value)


class GrokRecoveryTests(unittest.TestCase):
    def test_probe_ok_recovers(self):
        account = _Account(status="invalid")
        apply_grok_status_policy(account, usable=True)
        self.assertEqual(account.status, "registered")

    def test_probe_200_recovers(self):
        account = _Account(status="expired")
        apply_grok_status_policy(account, probe_code=200)
        self.assertEqual(account.status, "registered")

    def test_probe_402_no_credits_recovers(self):
        """402 无额度仍算有效（与 _probe_verdict 同口径）—— 恢复「正常」。"""
        account = _Account(status="invalid")
        apply_grok_status_policy(
            account, probe_code=402, probe_summary="personal-team-blocked:spending-limit"
        )
        self.assertEqual(account.status, "registered")

    def test_refresh_ok_recovers(self):
        account = _Account(status="invalid")
        apply_grok_status_policy(account, usable=True)
        self.assertEqual(account.status, "registered")

    def test_banned_not_resurrected(self):
        account = _Account(status="banned")
        apply_grok_status_policy(account, usable=True)
        self.assertEqual(account.status, "banned")

    def test_version_gate_426_does_not_touch(self):
        """426 版本闸 = 探测本身没生效，不该动状态。"""
        account = _Account(status="invalid")
        reason = apply_grok_status_policy(account, probe_code=426, probe_summary="CLI version outdated")
        self.assertEqual(reason, "")
        self.assertEqual(account.status, "invalid")


class GrokSsoRejectedTests(unittest.TestCase):
    """SSO 被上游明确拒绝（对齐 grok2api 的 markSSOCredentialRejected）。"""

    def test_sso_rejected_marks_invalid(self):
        """SSO 被拒 = 需要重新登录 = 失效（即使 AT 未过期）。"""
        account = _Account(extra={"access_token": _valid_at()})
        reason = apply_grok_status_policy(account, sso_rejected=True)
        self.assertEqual(account.status, AccountStatus.INVALID.value)
        self.assertEqual(reason, "sso_rejected")

    def test_sso_rejected_with_expired_at_still_invalid(self):
        """SSO 被拒时刷新救不回 —— 按失效落（不是过期）。"""
        account = _Account(extra={"access_token": _expired_at()})
        apply_grok_status_policy(account, sso_rejected=True)
        self.assertEqual(account.status, AccountStatus.INVALID.value)


class GrokRemoteSyncTests(unittest.TestCase):
    """CPA 同步（remote_state）也要参与判定。"""

    def test_remote_invalidated_marks_invalid(self):
        account = _Account()
        reason = apply_grok_status_policy(
            account, remote_state="access_token_invalidated"
        )
        self.assertEqual(account.status, AccountStatus.INVALID.value)
        self.assertTrue(reason)

    def test_remote_usable_recovers(self):
        account = _Account(status="invalid")
        apply_grok_status_policy(account, remote_state="usable")
        self.assertEqual(account.status, "registered")

    def test_remote_unreachable_does_not_touch(self):
        """连不上远端不是账号问题。"""
        account = _Account(status="invalid")
        reason = apply_grok_status_policy(account, remote_state="unreachable")
        self.assertEqual(reason, "")
        self.assertEqual(account.status, "invalid")


class Grok403VerdictTests(unittest.TestCase):
    """403 的两面性：无额度仍有效；带 invalid 字样是凭证被拒。"""

    def test_probe_403_no_quota_recovers(self):
        """403 无额度（permission-denied）也是有效凭证 —— 恢复「正常」。"""
        account = _Account(status="invalid")
        apply_grok_status_policy(account, probe_code=403, probe_summary="permission-denied")
        self.assertEqual(account.status, "registered")

    def test_probe_403_with_invalid_keyword_does_not_recover(self):
        """403 带 invalid 字样 = 凭证被拒 —— 不能当成可用。"""
        account = _Account(status="invalid")
        reason = apply_grok_status_policy(
            account, probe_code=403, probe_summary='{"error":"token invalid"}'
        )
        self.assertEqual(account.status, "invalid")
        self.assertTrue(reason)


if __name__ == "__main__":
    unittest.main()
