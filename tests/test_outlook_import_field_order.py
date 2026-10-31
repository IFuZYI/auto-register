"""Outlook 导入的字段顺序。

真实事故（2026-10-31）：用户导入 10 条 Outlook 账号，全部失败。
文件长这样：

    email@outlook.com----x----M.C538_BAY...（545 字符）----9e5f94bc-...（GUID）

也就是 `邮箱----密码----refresh_token----client_id`。而导入器按文档顺序
`邮箱----密码----client_id----refresh_token` 解析，把 refresh_token 当成
client_id 去换 token，微软返回：

    400 invalid_grant
    AADSTS7000012: The grant was obtained for a different tenant

**提示语里一个字都没提字段顺序** —— 用户只会以为 token 过期了或号被封了。

实测：按原顺序 0/10 可用；换序后 10/10 可用。

所以解析器按「谁像 GUID」自动纠正：微软的 client_id 一律是 GUID
（8-4-4-4-12），refresh_token 不是（MSA 的是 `M.C5xx_...` 一长串）。
"""

import unittest

from services.mail_imports.microsoft_import_rules import (
    ACCOUNT_TYPE_MAILAPI_URL,
    parse_microsoft_import_line,
)

CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
REFRESH_TOKEN = (
    "M.C538_BAY.0.U.MsaArtifacts.-Cs7Y6IQ4OCtvgLl3Q05UptBt1eWnefdTsYmApSDq7mi23Xu5qfY01"
    "uflEViUSkHjqIrdIAfdHZbiRyOV2lY2r6RdecorhKDKHSAY43eQTz7"
)


class OutlookImportFieldOrderTests(unittest.TestCase):
    def test_token_third_client_id_fourth_is_auto_corrected(self):
        """用户实际给的顺序（refresh_token 在第 3 段）。"""
        record = parse_microsoft_import_line(
            1, f"a@outlook.com----x----{REFRESH_TOKEN}----{CLIENT_ID}"
        )
        self.assertEqual(record.email, "a@outlook.com")
        self.assertEqual(record.client_id, CLIENT_ID)
        self.assertEqual(record.refresh_token, REFRESH_TOKEN)

    def test_documented_order_is_untouched(self):
        """文档顺序（client_id 在第 3 段）不能被「纠正」坏。"""
        record = parse_microsoft_import_line(
            1, f"b@outlook.com----pw----{CLIENT_ID}----{REFRESH_TOKEN}"
        )
        self.assertEqual(record.client_id, CLIENT_ID)
        self.assertEqual(record.refresh_token, REFRESH_TOKEN)

    def test_both_orders_yield_the_same_record(self):
        a = parse_microsoft_import_line(1, f"c@outlook.com----x----{REFRESH_TOKEN}----{CLIENT_ID}")
        b = parse_microsoft_import_line(2, f"c@outlook.com----x----{CLIENT_ID}----{REFRESH_TOKEN}")
        self.assertEqual(a.client_id, b.client_id)
        self.assertEqual(a.refresh_token, b.refresh_token)

    def test_does_not_guess_when_neither_side_looks_like_a_client_id(self):
        """两段都不像 GUID 时保持原样 —— 猜错比不猜更糟。

        这种情况多半是用户把别的格式（比如 `邮箱----密码----token----备注`）
        贴进来了，原样交给可用性探测，让微软给出真实错误。
        """
        record = parse_microsoft_import_line(
            1, "d@outlook.com----pw----some-token----a-note"
        )
        self.assertEqual(record.client_id, "some-token")
        self.assertEqual(record.refresh_token, "a-note")

    def test_does_not_swap_when_both_look_like_client_ids(self):
        """两段都像 GUID 时不动 —— 那是两个 client_id，不是本格式。"""
        other = "11111111-2222-3333-4444-555555555555"
        record = parse_microsoft_import_line(
            1, f"e@outlook.com----pw----{CLIENT_ID}----{other}"
        )
        self.assertEqual(record.client_id, CLIENT_ID)
        self.assertEqual(record.refresh_token, other)

    def test_two_part_mailapi_url_is_unaffected(self):
        record = parse_microsoft_import_line(1, "f@outlook.com----https://mail.example.com/x")
        self.assertEqual(record.account_type, ACCOUNT_TYPE_MAILAPI_URL)
        self.assertEqual(record.mailapi_url, "https://mail.example.com/x")
        self.assertEqual(record.client_id, "")


if __name__ == "__main__":
    unittest.main()
