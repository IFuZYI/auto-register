"""导入必须把主凭据镜像进 `token` 列 —— 否则导入号对 4 处读它的人是空的。

2026-11-04 独立审查（切片 E，L3）提到「导入后 AT 只活在 extra 里，`account.token`
的读取者会看到空」。**实测确认是真缺陷**（不是理论风险）：

    用 JSON 导入建一个带 access_token 的账号 →
        token 列 = ''            ← 空
        extra.access_token 有值  ← 只活在这里

而 `account.token` 的读取者有 4 处以上：

- `platforms/chatgpt/plugin.py:131 / 375 / 606 / 632` —— `token=account.token`
- `services/task_runners.py:933` —— `"token": account.token`
- `api/actions.py:227` —— `self.token = model.token`

仓库里本来就有 `core.credential_fields.sync_token_column`
（规则：`token` 列 = 平台主凭据镜像，chatgpt → access_token、grok → sso，
没有对应值时不碰列），但**两条导入路径都没调它** —— 注册路径调了，所以真库里
1256 行 token 列 0 个空（那些是注册出来的），只有**导入**出来的号会是空。

这条直接导致「导出备份 → 换台机器导入」之后，导入进来的 ChatGPT 号在
探测/刷新/推送里读到空 token。
"""

from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

CHATGPT_EMAIL = "token-column-chatgpt@example.com"
GROK_EMAIL = "token-column-grok@example.com"
AT = "eyJhbGciOiJSUzI1NiJ9.token-column-at.sig"
SSO = "eyJhbGciOiJIUzI1NiJ9.token-column-sso.sig"


class ImportMirrorsThePrimaryCredentialIntoTokensColumnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import main as main_mod

        cls._ctx = TestClient(main_mod.app)
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def tearDown(self):
        from core.db import account_repository

        for platform, email in (("chatgpt", CHATGPT_EMAIL), ("grok", GROK_EMAIL)):
            row = account_repository.find_by_email(platform, email)
            if row is not None:
                account_repository.delete(row.id, platform)

    def _import_json(self, entry):
        return self.client.post("/api/accounts/import", json={
            "platform": entry["platform"],
            "lines": [json.dumps([entry])],
            "format": "json",
        })

    def test_chatgpt_import_mirrors_the_access_token_into_the_column(self):
        r = self._import_json({
            "platform": "chatgpt", "email": CHATGPT_EMAIL, "password": "pw-1",
            "access_token": AT, "refresh_token": "rt-1", "status": "registered",
        })
        self.assertEqual(r.status_code, 200, r.text[:200])

        from core.db import account_repository

        row = account_repository.find_by_email("chatgpt", CHATGPT_EMAIL)
        self.assertIsNotNone(row)
        self.assertEqual(
            row.token, AT,
            "导入的 ChatGPT 号 token 列必须是 AT 镜像 —— 否则 "
            "chatgpt/plugin.py 的 4 处 token=account.token 读到空串",
        )

    def test_grok_import_mirrors_the_sso_into_the_column(self):
        r = self._import_json({
            "platform": "grok", "email": GROK_EMAIL, "sso": SSO, "status": "registered",
        })
        self.assertEqual(r.status_code, 200, r.text[:200])

        from core.db import account_repository

        row = account_repository.find_by_email("grok", GROK_EMAIL)
        self.assertIsNotNone(row)
        self.assertEqual(row.token, SSO, "导入的 Grok 号 token 列必须是 SSO 镜像")

    def test_text_import_also_mirrors_the_access_token(self):
        """文本导入是同一条链的另一个入口 —— 两条路都要镜像。

        （上一轮修「明文落盘」时就吃过这个亏：只修了文本那条，
        测试却只驱动 JSON 那条，于是修复看起来有效其实没被覆盖。）
        """
        line = "----".join([CHATGPT_EMAIL, "pw-1", "JBSWY3DPEHPK3PXP", AT, "rt-1"])
        r = self.client.post("/api/accounts/import", json={
            "platform": "chatgpt",
            "lines": [line],
            "format": "text",
            "source_format": "email_pw_2fa_at_rt",
        })
        self.assertEqual(r.status_code, 200, r.text[:200])

        from core.db import account_repository

        row = account_repository.find_by_email("chatgpt", CHATGPT_EMAIL)
        self.assertIsNotNone(row)
        self.assertEqual(row.token, AT, "文本导入也必须把 AT 镜像进 token 列")

    def test_an_account_without_the_primary_credential_keeps_an_empty_column(self):
        """对照组：没有主凭据时**不许**凭空写一个值（沉默不等于清空，也不等于编造）。"""
        r = self._import_json({
            "platform": "chatgpt", "email": CHATGPT_EMAIL, "password": "pw-1",
            "status": "registered",
        })
        self.assertEqual(r.status_code, 200, r.text[:200])

        from core.db import account_repository

        row = account_repository.find_by_email("chatgpt", CHATGPT_EMAIL)
        self.assertIsNotNone(row)
        self.assertFalse(row.token, f"没有 AT 就不该有 token 镜像，实际 {row.token!r}")
