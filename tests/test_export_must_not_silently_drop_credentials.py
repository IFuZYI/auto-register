"""导出必须**要么给对数据，要么明确报错**，不能静默给空。

2026-11-04 独立审查（切片 D）：`services/account_export._extra_of()` 把
`get_extra()` 的任何异常都吞成 `{}`。而 `get_extra()` 对脏 JSON **自己就返回
`{}`**（`models_account.py` 内部已处理），所以能从这里抛出来的基本只有
**解密失败**（`CredentialKeyError`）—— 注释声称的「脏 JSON 不该让整次导出失败」
这个场景根本不经过这里。

后果：密钥文件丢失（`core/secret_box.py` 在缺失时会**静默新建**一把）/
被替换 / `CREDENTIAL_ENCRYPTION_KEY` 写错时，导出仍返回 200 与格式合法的文本，
只是凭证列全空。用户据此做迁移 → 目标环境拿到的是一批「有邮箱密码但没 AT/2FA」
的号，而他不知道数据已经丢了。

这里钉的契约：**解密失败必须炸出来**，不能变成「导出来是空的」。
"""

from __future__ import annotations

import unittest
from unittest import mock

from core.db.models_account import AccountModel
from core.secret_box import CredentialKeyError
from services.account_export import render_accounts


class ExportMustNotSilentlyDropCredentialsTests(unittest.TestCase):
    def _account(self) -> AccountModel:
        model = AccountModel(
            platform="chatgpt",
            email="export-keyfail@example.com",
            password="pw-1",
        )
        model.set_extra({
            "access_token": "eyJhbGciOiJSUzI1NiJ9.at.signature",
            "totp_secret": "JBSWY3DPEHPK3PXP",
        })
        return model

    def _render_with_broken_key(self) -> str:
        account = self._account()
        with mock.patch.object(
            AccountModel, "get_extra",
            side_effect=CredentialKeyError("凭据加密密钥必须是 base64 或 hex 编码的 32 字节"),
        ):
            return render_accounts([account], "email_pw_2fa_at")

    def test_broken_key_raises_instead_of_exporting_empty_credentials(self):
        with self.assertRaises(CredentialKeyError) as ctx:
            self._render_with_broken_key()
        message = str(ctx.exception)
        self.assertIn("export-keyfail@example.com", message, "报错要说清是哪个账号")
        self.assertIn("解密", message, f"报错要指向真正的原因（密钥），实际：{message}")

    def test_healthy_account_still_exports_the_credentials(self):
        """对照组：正常账号必须照常导出（别把这条修复做成「一律拒绝导出」）。"""
        account = self._account()
        out = render_accounts([account], "email_pw_2fa_at")
        self.assertIn("export-keyfail@example.com", out)
        self.assertIn("eyJhbGciOiJSUzI1NiJ9.at.signature", out, "AT 必须出现在导出里")
        self.assertIn("JBSWY3DPEHPK3PXP", out, "2FA 密钥必须出现在导出里")
