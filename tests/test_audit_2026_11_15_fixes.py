"""2026-11-15 全量检测的回归测试（L04 / L06 两条 P0 + 两条 P1）。

每个用例都对应一条**已复现**的缺陷；断言的是缺陷的判别式，不是"没崩"。
"""
from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class ProxyRedactionTests(unittest.TestCase):
    """L04-3：无 scheme 的带认证写法原样回吐，打码等于没打。"""

    def test_no_scheme_userinfo_is_masked(self):
        from core.proxy_utils import redact_proxy_url

        out = redact_proxy_url("u:p@h:8080")
        self.assertNotIn("u:p@", out, f"userinfo 原样泄漏: {out}")
        self.assertIn("h:8080", out, "打码不该把主机端口抹掉")
        self.assertTrue(out.startswith("***:***@"), out)

    def test_legacy_colon_credentials_are_masked(self):
        from core.proxy_utils import redact_proxy_url

        out = redact_proxy_url("h:8080:u:p:ss")
        self.assertNotIn("u:p:ss", out, f"凭据原样泄漏: {out}")
        self.assertEqual(out, "h:8080:***")

    def test_positive_control_host_port_survives(self):
        """正控：不能因为加严而把 host:port 也抹掉（否则日志失去可用性）。"""
        from core.proxy_utils import redact_proxy_url

        self.assertEqual(redact_proxy_url("1.2.3.4:8080"), "1.2.3.4:8080")


class AddProxyResponseTests(unittest.TestCase):
    """L04-2：POST /api/proxies 回原始记录，`user:pass@` 整串进响应。"""

    def test_created_proxy_is_masked(self):
        from api import proxies
        from core.models.proxy import ProxyRecord

        record = ProxyRecord(url="socks5h://probe-user:probe-pass-9f3a@192.0.2.10:6740")
        body = proxies.ProxyCreate(url=record.url, region="US", tags=["bind"])
        with mock.patch.object(proxies.proxy_repository, "find_by_url", return_value=None), \
             mock.patch.object(proxies.proxy_repository, "add", return_value=record):
            payload = proxies.add_proxy(body, session=None)

        self.assertNotIn("probe-pass-9f3a", payload["url"], payload)
        self.assertNotIn("probe-user", payload["url"], payload)
        self.assertIn("192.0.2.10:6740", payload["url"])


class OutlookRegisteredEmailsTests(unittest.TestCase):
    """L06-1：JSON 后端下裸 Session 查 `accounts` 表 → no such table。"""

    def test_reads_through_repository_with_platform(self):
        from core.mailboxes.channels.outlook.mailbox import OutlookMailbox

        rows = [SimpleNamespace(email="A@B.com"), SimpleNamespace(email=""), SimpleNamespace(email="c@d.com")]
        box = OutlookMailbox.__new__(OutlookMailbox)
        box._platform_name = "ChatGPT"
        with mock.patch(
            "core.db.account_repository.list_all_accounts", return_value=rows
        ) as list_all:
            got = box._registered_emails_for_platform()

        self.assertEqual(got, {"a@b.com", "c@d.com"})
        # 正控：真的按平台查了仓储（否则这条断言可以靠"没调用"通过）
        list_all.assert_called_once_with(platform="chatgpt")


class ImportAccountsToPoolTests(unittest.TestCase):
    """L06-2：导入邮箱池走裸 SQL，JSON 部署下 no such table: outlook_accounts。"""

    def test_only_unpooled_rows_flip_to_available(self):
        from core.mailboxes.channels.outlook.mailbox import OutlookMailbox

        rows = [
            {"id": 1, "status": "unpooled"},
            {"id": 2, "status": "available"},
            {"id": 3, "status": "unpooled"},
        ]
        with mock.patch(
            "core.db.outlook_repository.outlook_repository.list_all", return_value=rows
        ), mock.patch(
            "core.db.outlook_repository.outlook_repository.set_fields",
            return_value={},
        ) as set_fields:
            result = OutlookMailbox.import_accounts_to_pool([1, 2, 3])

        self.assertEqual(result["changed"], 2)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["remaining_unpooled"], 2)
        touched = sorted(c.args[0] for c in set_fields.call_args_list)
        self.assertEqual(touched, [1, 3], "只有 unpooled 行该被改成 available")


if __name__ == "__main__":
    unittest.main()
