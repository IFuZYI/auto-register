"""探测凭据写回与动作响应脱敏（第三轮 review 的 C1/I7 回归）。"""
from __future__ import annotations

import json
import unittest

from core.probe_credentials import persist_probe_credentials


class _Row:
    def __init__(self, extra: dict) -> None:
        self._extra = dict(extra)
        self.token = ""

    def get_extra(self) -> dict:
        return dict(self._extra)

    def set_extra(self, extra: dict) -> None:
        self._extra = dict(extra)


class PersistProbeCredentialsTests(unittest.TestCase):
    def test_writes_tokens_and_expiry(self):
        row = _Row({"access_token": "old", "refresh_token": "old-rt"})
        ok = persist_probe_credentials(
            row,
            {
                "usable": True,
                "access_token": "new-at",
                "refresh_token": "new-rt",
                "expires_in": 3600,
            },
        )
        self.assertTrue(ok)
        extra = row.get_extra()
        self.assertEqual(extra["access_token"], "new-at")
        self.assertEqual(extra["refresh_token"], "new-rt")
        self.assertGreater(extra["expires_at"], 10**12, "expires_at 用毫秒")
        self.assertEqual(row.token, "new-rt", "token 列镜像 RT")

    def test_skips_indeterminate(self):
        row = _Row({"access_token": "old"})
        ok = persist_probe_credentials(
            row, {"usable": None, "indeterminate": True, "access_token": "new"}
        )
        self.assertFalse(ok)
        self.assertEqual(row.get_extra()["access_token"], "old")

    def test_skips_when_nothing_to_write(self):
        row = _Row({"access_token": "old"})
        self.assertFalse(persist_probe_credentials(row, {"usable": True}))


class ProbeIndeterminateTests(unittest.TestCase):
    """5xx/429/网络错误是「测不了」，不是「失效」。"""

    def test_kiro_500_is_indeterminate(self):
        from unittest import mock

        from platforms.kiro.probe import probe_kiro

        with mock.patch(
            "platforms.kiro.probe.refresh_kiro",
            lambda **_kw: {"ok": False, "code": 500, "error": "upstream 500"},
        ):
            detail = probe_kiro(
                client_id="c", client_secret="s", refresh_token="r"
            )
        self.assertIsNone(detail["usable"])
        self.assertTrue(detail["indeterminate"])

    def test_aws_429_is_indeterminate(self):
        from unittest import mock

        from platforms.aws.probe import probe_builder_id

        with mock.patch(
            "platforms.aws.probe.refresh_builder_id",
            lambda **_kw: {"ok": False, "code": 429, "error": "rate limited"},
        ):
            detail = probe_builder_id(
                client_id="c", client_secret="s", refresh_token="r"
            )
        self.assertIsNone(detail["usable"])
        self.assertTrue(detail["indeterminate"])


class ActionRedactionTests(unittest.TestCase):
    def test_every_sensitive_key_is_redacted_except_totp(self):
        from api.actions import _redact_action_payload
        from core.credential_crypto import SENSITIVE_EXTRA_KEYS

        payload = {
            "data": {key: "SECRET" for key in SENSITIVE_EXTRA_KEYS},
            "account_extra_patch": {"cookies": "SECRET", "id_token": "SECRET"},
            "nested": [{"accessToken": "SECRET"}],
        }
        out = _redact_action_payload(payload)
        dumped = json.dumps(out)
        for key in SENSITIVE_EXTRA_KEYS:
            if key == "totp_secret":
                self.assertIn("SECRET", dumped, "totp_secret 要保留（bind_2fa 一次性下发）")
                continue
            self.assertNotIn(key, dumped, f"{key} 没被脱敏")
        self.assertNotIn("accessToken", dumped, "camelCase 别名也要脱敏")


if __name__ == "__main__":
    unittest.main()
