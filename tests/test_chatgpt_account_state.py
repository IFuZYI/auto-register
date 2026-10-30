import unittest

from services.chatgpt_account_state import (
    apply_chatgpt_status_policy,
    classify_local_probe_state,
    classify_remote_sync_state,
)


class DummyAccount:
    def __init__(self, status="registered"):
        self.status = status


class ChatGPTAccountStateTests(unittest.TestCase):
    def test_local_401_marks_invalid(self):
        account = DummyAccount()
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
        self.assertEqual(reason, "auth_401")
        self.assertEqual(account.status, "invalid")

    def test_remote_401_marks_invalid(self):
        self.assertEqual(
            classify_remote_sync_state(
                {
                    "remote_state": "access_token_invalidated",
                    "last_probe_status_code": 401,
                    "last_probe_error_code": "token_invalidated",
                    "last_probe_message": "invalidated",
                }
            ),
            "remote_401",
        )

    def test_payment_is_not_treated_as_invalid_or_limited(self):
        """付费问题既不是「凭证失效」也不是「限流」—— 保持无判定。"""
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {"state": "access_token_valid", "http_status": 200},
                    "codex": {"state": "payment_required", "http_status": 403, "message": "payment required"},
                }
            ),
            "",
        )

    def test_quota_exhausted_is_rate_limited_not_invalid(self):
        """429 / 额度耗尽 → 限流（可恢复），**绝不能**判成凭证失效。

        2026-10-30 用户要求「GPT 限流…尝试恢复」：限流的号凭证是好的，
        判成 invalid 会把它排进「重新登录」队列 —— 重登解决不了额度问题，
        只会白打上游。这条同时钉住「不是 invalid」与「有新落点」。
        """
        self.assertEqual(
            classify_local_probe_state(
                {"auth": {"http_status": 429, "state": "quota_exhausted"}}
            ),
            "auth_rate_limited",
        )
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {"state": "access_token_valid", "http_status": 200},
                    "codex": {"state": "quota_exhausted", "http_status": 429},
                }
            ),
            "codex_rate_limited",
        )
        self.assertEqual(
            classify_remote_sync_state(
                {
                    "remote_state": "quota_exhausted",
                    "last_probe_status_code": 429,
                    "last_probe_error_code": "",
                    "last_probe_message": "usage limit reached",
                }
            ),
            "remote_rate_limited",
        )

    def test_deactivated_message_marks_invalid(self):
        self.assertEqual(
            classify_local_probe_state(
                {
                    "auth": {
                        "state": "banned_like",
                        "http_status": 403,
                        "error_code": "account_deactivated",
                        "message": "You do not have an account because it has been deleted or deactivated.",
                    }
                }
            ),
            "auth_deactivated",
        )


if __name__ == "__main__":
    unittest.main()
