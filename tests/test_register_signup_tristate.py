"""`signup()` 的三态必须分清（历史上「注册出来的号全是无密码号」的现场）。

`authorize/continue` 返回三种页面，代码用一个 bool 表达，很容易压成两态：

1. `create_account_password`        → 真·新账号，走设密码分支；
2. `email_otp_verification` +
   `email_verification_mode=passwordless_signup` → **也是新账号**，只是服务端选了
   「不设密码直接发码」；若把它当「已有账号」，`register_password` 永远不会被调用，
   注册出来的号全没有密码（域名一失效就永久丢失）；
3. `email_otp_verification` + 其它 mode（或未知 page_type）→ 已有账号，走 OTP 登录。

这里直接把 `authorize_continue` 打桩成三种返回，断言 `signup()` 的返回值与
内部状态 —— 不读源码、不做字符串匹配。
"""

from __future__ import annotations

import unittest
from unittest import mock


def _flow():
    from platforms.chatgpt.protocol import AuthFlow, Config

    return AuthFlow(Config(proxy=None))


def _page(page_type: str, *, mode: str = "", continue_url: str = "") -> dict:
    payload = {"email_verification_mode": mode} if mode else {}
    return {"page": {"type": page_type, "payload": payload}, "continue_url": continue_url}


class SignupTriStateTests(unittest.TestCase):
    def test_password_page_is_a_new_account(self):
        flow = _flow()
        with mock.patch.object(
            flow, "authorize_continue", return_value=_page("create_account_password")
        ):
            self.assertTrue(flow.signup("a@x.c", "sentinel"))
        self.assertFalse(flow._is_existing_account)

    def test_passwordless_signup_is_a_NEW_account_without_password(self):
        """最关键的一条：passwordless 也是新账号。"""
        flow = _flow()
        with mock.patch.object(
            flow,
            "authorize_continue",
            return_value=_page("email_otp_verification", mode="passwordless_signup"),
        ):
            self.assertFalse(flow.signup("a@x.c", "sentinel"))
        self.assertFalse(
            flow._is_existing_account,
            "passwordless 新号被当成了已有账号 —— register_password 永远不会跑",
        )
        self.assertEqual(flow._existing_email_verification_mode, "passwordless_signup")

    def test_otp_login_page_is_an_existing_account(self):
        flow = _flow()
        with mock.patch.object(
            flow,
            "authorize_continue",
            return_value=_page("email_otp_verification", mode="login"),
        ):
            self.assertFalse(flow.signup("a@x.c", "sentinel"))
        self.assertTrue(flow._is_existing_account)

    def test_unknown_page_is_treated_as_existing(self):
        """未知分支按已有账号处理：误进 register_password 会 invalid_state。"""
        flow = _flow()
        with mock.patch.object(
            flow, "authorize_continue", return_value=_page("some_social_login")
        ):
            self.assertFalse(flow.signup("a@x.c", "sentinel"))
        self.assertTrue(flow._is_existing_account)

    def test_continue_url_fallback_recognises_the_password_page(self):
        """page_type 为空但 continue_url 指向 create-account/password 时也算新号。"""
        flow = _flow()
        with mock.patch.object(
            flow,
            "authorize_continue",
            return_value={
                "page": {"type": "", "payload": {}},
                "continue_url": "https://auth.openai.com/create-account/password",
            },
        ):
            self.assertTrue(flow.signup("a@x.c", "sentinel"))
        self.assertFalse(flow._is_existing_account)


if __name__ == "__main__":
    unittest.main()
