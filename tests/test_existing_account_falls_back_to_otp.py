"""已有账号走到 `login_password` 分支时，密码 401 必须降级到 OTP。

## 实测的症状（用户日志 2026-10-28 01:39–02:08）

    [协议] authorize/continue 返回非标准注册页面: page_type=login_password
    [协议] 已有账号进入 login_password 分支，先走密码校验再 OTP
    [协议] 该号无已知密码，用默认规则猜一个试试（多半 401）
    [FAIL] 注册失败: 密码登录失败: 401 - invalid_username_or_password

**第 50/51/52/53 个账号全部失败**，而且每个都重试 2 轮 —— 因为
`login_password_verify` 在非 200 时直接 `raise RuntimeError`，上层
（`auth_flow` 的 OTP 分支）根本没机会跑。

## 为什么这必然发生

注册时邮箱是**新取的**，但 iCloud 隐私邮箱会**复用**（同一别名可以给不同
平台注册）。当这个地址在 OpenAI 侧已经存在，`authorize/continue` 返回的就是
`page_type=login_password` —— 此时：
- 密码是「猜的」（新号没设过密码）→ 必然 401；
- 正确路径是 **OTP 登录**（代码里 `WEBUI_ALLOW_LOGIN=1` 就是为这个）。

`raise` 把这条路堵死了。

## 正确行为

401 不是致命错误，是「这个号不能用密码登」的信号 —— 应当返回一个空 dict
让调用方继续走 OTP 分支，并**记一条日志**说明降级原因。
"""

from __future__ import annotations

import unittest


class PasswordVerifyDoesNotRaiseOn401Tests(unittest.TestCase):
    def _method_body(self) -> str:
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "platforms" / "chatgpt" / "protocol" / "mixins" / "signup.py"
        ).read_text(encoding="utf-8")
        start = src.index("def login_password_verify(")
        end = src.find("\n    def ", start + 10)
        return src[start:end]

    def test_does_not_raise_on_non_200(self):
        body = self._method_body()
        self.assertNotIn(
            "raise RuntimeError", body,
            "login_password_verify 在非 200 时 raise —— 已有账号的 OTP 降级路径"
            "永远走不到（用户日志里第 50-53 个账号全因此失败）",
        )

    def test_returns_empty_dict_on_rejection(self):
        body = self._method_body()
        self.assertIn(
            "return {}", body,
            "被拒时没有返回空 dict —— 调用方拿不到可继续的信号",
        )


class AuthFlowHasAnOtpFallbackForPasswordRejectionTests(unittest.TestCase):
    """`auth_flow` 在密码被拒时要有 OTP 分支。"""

    def test_password_branch_handles_rejection(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "platforms" / "chatgpt" / "protocol" / "auth_flow.py"
        ).read_text(encoding="utf-8")
        start = src.index('if page_type == "login_password":')
        end = src.find('if page_type ==', start + 10)
        block = src[start:] if end == -1 else src[start:end]
        self.assertTrue(
            "kickoff_otp_delivery" in block or "wait_for_otp" in block,
            "login_password 分支里没有 OTP 兜底 —— 密码被拒后无路可走",
        )


if __name__ == "__main__":
    unittest.main()
