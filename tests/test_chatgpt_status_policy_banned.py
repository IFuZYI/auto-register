"""状态策略：封禁（banned）与凭证失效（invalid）必须分开落。

回归：`apply_chatgpt_status_policy` 把所有判定理由一律写成 `invalid`，
而 `AccountStatus.counts_as_registered` 认为 `invalid` 的邮箱**可以重新用** ——
于是探测/同步认出「号已删除或停用」时，取号环节会把这个邮箱当成没注册过再发一次。
只有刷新那条路走了 action 级特判才写成 banned，另外两条路仍然写 invalid。
"""

from __future__ import annotations

import unittest

from core.base_platform import AccountStatus
from services.chatgpt_account_state import (
    BANNED_ACCOUNT_STATUS,
    apply_chatgpt_status_policy,
)

# OpenAI 对已停用账号的原话
DEACTIVATED_BODY = (
    "You do not have an account because it has been deleted or deactivated."
)


class _Account:
    def __init__(self, status: str = "registered"):
        self.status = status


def _deactivated_probe():
    return {
        "auth": {
            "state": "account_deactivated",
            "http_status": 403,
            "error_code": "account_deactivated",
            "message": DEACTIVATED_BODY,
        }
    }


def _expired_probe():
    """凭证失效但号还在 —— 该写 invalid，可以重登。"""
    return {"auth": {"state": "access_token_invalidated", "http_status": 401}}


class BannedVsInvalidTests(unittest.TestCase):
    def test_deactivated_probe_marks_banned_not_invalid(self):
        account = _Account()
        reason = apply_chatgpt_status_policy(account, local_probe=_deactivated_probe())
        self.assertEqual(reason, "auth_deactivated")
        self.assertEqual(
            account.status, BANNED_ACCOUNT_STATUS,
            "封禁被判成了普通失效 —— 取号时会把这个邮箱当没注册过再发一次",
        )

    def test_expired_credentials_stay_invalid(self):
        """401 只是凭证过期，不能升级成封禁（否则号会被误判成永久报废）。"""
        account = _Account()
        apply_chatgpt_status_policy(account, local_probe=_expired_probe())
        self.assertEqual(account.status, AccountStatus.INVALID.value)

    def test_remote_deactivated_sync_also_marks_banned(self):
        """同步那条路同样要能认出来（原来只有刷新那条路特判过）。"""
        account = _Account()
        reason = apply_chatgpt_status_policy(
            account,
            remote_sync={
                "remote_state": "account_deactivated",
                "last_probe_status_code": 403,
                "last_probe_error_code": "account_deactivated",
                "last_probe_message": DEACTIVATED_BODY,
            },
        )
        self.assertEqual(reason, "remote_deactivated")
        self.assertEqual(account.status, BANNED_ACCOUNT_STATUS)

    def test_refresh_banned_flag_marks_banned(self):
        """刷新链已经自己认过措辞，传 banned=True 时策略要落 banned。"""
        account = _Account()
        reason = apply_chatgpt_status_policy(account, banned=True)
        self.assertEqual(reason, "refresh_banned")
        self.assertEqual(account.status, BANNED_ACCOUNT_STATUS)

    def test_refresh_without_banned_leaves_status_alone(self):
        account = _Account("registered")
        reason = apply_chatgpt_status_policy(account, banned=False)
        self.assertEqual(reason, "")
        self.assertEqual(account.status, "registered")

    def test_no_signal_leaves_status_alone(self):
        account = _Account("registered")
        self.assertEqual(apply_chatgpt_status_policy(account), "")
        self.assertEqual(account.status, "registered")


class EmailReuseSemanticsTests(unittest.TestCase):
    """封禁与失效在「邮箱还能不能拿去注册」上语义相反。"""

    def test_banned_email_still_counts_as_taken(self):
        self.assertTrue(
            AccountStatus.counts_as_registered(BANNED_ACCOUNT_STATUS),
            "封禁账号的邮箱仍被该平台占用，不该被当成没注册过",
        )

    def test_invalid_email_can_be_reused(self):
        self.assertFalse(AccountStatus.counts_as_registered(AccountStatus.INVALID.value))

    def test_banned_is_not_active(self):
        """封禁不该进测活/重试队列。"""
        self.assertFalse(AccountStatus.is_active(BANNED_ACCOUNT_STATUS))

    def test_the_two_states_are_actually_distinct(self):
        self.assertNotEqual(BANNED_ACCOUNT_STATUS, AccountStatus.INVALID.value)


if __name__ == "__main__":
    unittest.main()
