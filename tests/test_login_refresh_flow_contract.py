"""把 login_refresh 挂到真实 AuthFlow 形状上跑一遍（不联网）。

验证两件事：
1. `_build_flow` 传的 `env_overrides` / `account_callback` 与真实 AuthFlow 构造签名兼容；
2. 登录链拿到 AT 后，`_absorb` 能把它捞回来（走的是 flow.result 的真字段名）。
"""

from __future__ import annotations

import unittest

from platforms.chatgpt.protocol import AuthFlow, Config
from platforms.chatgpt.login_refresh import LoginAccessTokenRefresher


class LoginRefreshAgainstRealAuthFlowTests(unittest.TestCase):
    def test_flow_is_constructed_with_real_signature(self):
        refresher = LoginAccessTokenRefresher(
            email="someone@example.com",
            password="pw-123456",
            totp_secret="JBSWY3DPEHPK3PXP",
        )
        flow = refresher._build_flow()
        self.assertIsInstance(flow, AuthFlow)
        # 2FA 密钥要提前挂上，mfa-challenge 时才不用再去库里翻
        self.assertEqual(flow.result.totp_secret, "JBSWY3DPEHPK3PXP")
        # account_callback 得能回答协议层的询问
        cred = refresher._account_callback("someone@example.com")
        self.assertEqual(cred["password"], "pw-123456")
        self.assertEqual(cred["totp_secret"], "JBSWY3DPEHPK3PXP")

    def test_credentials_are_absorbed_from_the_real_result_object(self):
        refresher = LoginAccessTokenRefresher(email="a@b.c", password="pw")
        flow = refresher._build_flow()
        refresher._active_flow = flow
        # 模拟登录链中段把凭证写进 flow.result（字段名必须对得上）
        flow.result.access_token = "AT-XYZ"
        flow.result.session_token = "ST-XYZ"
        flow.result.refresh_token = "RT-XYZ"

        from platforms.chatgpt.login_refresh import LoginRefreshResult

        result = LoginRefreshResult()
        refresher._absorb(result)
        self.assertEqual(result.access_token, "AT-XYZ")
        self.assertEqual(result.session_token, "ST-XYZ")
        self.assertEqual(result.refresh_token, "RT-XYZ")

    def test_env_overrides_use_names_the_flow_actually_reads(self):
        refresher = LoginAccessTokenRefresher(
            email="a@b.c", password="pw", extra_config={"mailbox_otp_timeout_seconds": "90"}
        )
        overrides = refresher._env_overrides()
        self.assertEqual(overrides["OTP_TIMEOUT"], "90")
        # 这两个名字是 auth_flow 里真的会 _get_env 读的（否则就是写了个没人看的键）
        self.assertEqual(overrides["WEBUI_ALLOW_LOGIN"], "1")


if __name__ == "__main__":
    unittest.main()
