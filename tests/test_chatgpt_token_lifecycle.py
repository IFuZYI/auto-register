"""ChatGPT AT 生命周期：从 JWT 计算生成时间 / 到期时间 / 状态。

用户要求：「Chatgpt能不能增加一个计算 AT 生成时间和到期时间的功能，就像
reference/panel/chatgpt2api 这个里面的。」

参考实现（`reference/panel/chatgpt2api/services/account_credentials.py`）：
- `iat` = 签发时间（生成时间）、`exp` = 到期时间；
- 状态三档：`valid` / `expiring`（剩余 ≤ 24h）/ `invalid`（已过期或确认失效）；
- 只解 JWT payload（不验签），拿不到 claims 返回 None 而不是编一个。

本模块把它落成纯函数，账号页展示与上传前预检共用。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.chatgpt_token_lifecycle import (  # noqa: E402
    ACCESS_TOKEN_EXPIRING_SKEW_SECONDS,
    decode_jwt_claims,
    project_access_token_lifecycle,
)


def _jwt(payload: dict) -> str:
    """构造一个只有 payload 的假 JWT（header 与签名随便填）。"""
    import base64
    import json

    def _b64(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{_b64({'alg': 'RS256'})}.{_b64(payload)}.sig"


class DecodeJwtClaimsTests(unittest.TestCase):
    def test_decodes_payload(self):
        token = _jwt({"iat": 1000, "exp": 2000, "sub": "u-1"})
        claims = decode_jwt_claims(token)
        self.assertEqual(claims.get("iat"), 1000)
        self.assertEqual(claims.get("exp"), 2000)

    def test_garbage_returns_empty(self):
        for bad in ("", "not-a-jwt", "a.b", None):
            with self.subTest(token=bad):
                self.assertEqual(decode_jwt_claims(bad), {})


class ProjectAccessTokenLifecycleTests(unittest.TestCase):
    def test_valid_token_reports_times(self):
        token = _jwt({"iat": 1000, "exp": 1000 + 10 * 86400})
        result = project_access_token_lifecycle(token, now_seconds=2000)
        self.assertEqual(result["status"], "valid")
        self.assertEqual(result["issued_at"], 1000)
        self.assertEqual(result["expires_at"], 1000 + 10 * 86400)
        self.assertGreater(result["expires_in_seconds"], 0)

    def test_expiring_within_skew(self):
        """剩余时间 ≤ 24h → expiring（与参考实现同一档）。"""
        token = _jwt({"iat": 0, "exp": ACCESS_TOKEN_EXPIRING_SKEW_SECONDS - 10})
        result = project_access_token_lifecycle(token, now_seconds=0)
        self.assertEqual(result["status"], "expiring")

    def test_expired_is_invalid(self):
        token = _jwt({"iat": 0, "exp": 100})
        result = project_access_token_lifecycle(token, now_seconds=200)
        self.assertEqual(result["status"], "invalid")
        self.assertLess(result["expires_in_seconds"], 0)

    def test_confirmed_invalid_overrides(self):
        """接口已确认失效（401）时即使 exp 还没到也算 invalid。"""
        token = _jwt({"iat": 0, "exp": 10**12})
        result = project_access_token_lifecycle(
            token, confirmed_invalid=True, now_seconds=0
        )
        self.assertEqual(result["status"], "invalid")

    def test_missing_token_is_invalid(self):
        result = project_access_token_lifecycle("", now_seconds=0)
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["issued_at"])
        self.assertIsNone(result["expires_at"])

    def test_no_exp_claim_is_unknown_not_invalid(self):
        """没有 exp claim（不是标准 JWT）→ 状态 unknown，不误判成失效。"""
        token = _jwt({"iat": 1000, "sub": "u-1"})
        result = project_access_token_lifecycle(token, now_seconds=2000)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["issued_at"], 1000)
        self.assertIsNone(result["expires_at"])

    def test_seconds_until_expiry(self):
        """expires_in_seconds 是「还有多少秒」，供界面显示。"""
        token = _jwt({"iat": 0, "exp": 3600})
        result = project_access_token_lifecycle(token, now_seconds=600)
        self.assertEqual(result["expires_in_seconds"], 3000)

    def test_reference_impl_agrees(self):
        """与参考实现（chatgpt2api 的 account_credentials）口径一致。

        同输入下：valid / expiring / invalid 三档判定必须相同 —— 直接跑
        参考实现对照，避免「我们抄了一份但抄错了」。
        """
        import importlib.util
        import sys as _sys

        ref_path = (
            Path(__file__).resolve().parents[1]
            / "reference/panel/chatgpt2api/services/account_credentials.py"
        )
        if not ref_path.exists():
            self.skipTest("参考实现不存在（reference/ 未拉取）")
        spec = importlib.util.spec_from_file_location("ref_account_credentials", ref_path)
        ref = importlib.util.module_from_spec(spec)
        # dataclass 需要模块在 sys.modules 里注册（否则 @dataclass 解析
        # 注解时找不到 cls.__module__）。
        _sys.modules[spec.name] = ref
        spec.loader.exec_module(ref)

        cases = [
            ({"iat": 0, "exp": 10**9}, 1000, "valid"),
            # exp=2000、now=2000-86400+10 → 剩余 86390s（≤24h）→ expiring
            ({"iat": 0, "exp": 2000}, 2000 - 86400 + 10, "expiring"),
            ({"iat": 0, "exp": 100}, 200, "invalid"),
        ]
        for claims, now, expected in cases:
            token = _jwt(claims)
            ours = project_access_token_lifecycle(token, now_seconds=now)
            theirs = ref.project_access_token_lifecycle(token, now_seconds=now)
            with self.subTest(claims=claims, now=now):
                self.assertEqual(ours["status"], expected)
                self.assertEqual(
                    ours["status"], theirs.status,
                    "与参考实现判定不一致 —— 口径抄歪了",
                )
                self.assertEqual(ours["issued_at"], theirs.issued_at)
                self.assertEqual(ours["expires_at"], theirs.expires_at)


if __name__ == "__main__":
    unittest.main()
