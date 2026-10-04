"""Grok 号池记账语义 + grok2api 接入的行为回归。

背景（实测）：
- 浏览器路径的记账：x.ai 报「已有账号」（`already_registered`）必须记 `used`
  而不是 `failed` —— iCloud 的 `failed` 会把地址放回池子，下次又被领出来
  撞同一堵墙（实测 `bleaker.gills7f@icloud.com` 被反复领出）。
- grok2api 上传文件名必须是 `grok-web-sso-tokens.txt`，否则报
  `Cannot read properties of undefined`；重复上传是幂等的。

注：协议注册路径（含它的「换邮箱重试」）已于 2026-10-01 整体删除 ——
x.ai 对协议层发码「假接受」（grpc-status:0 但零投递）、协议验码恒为 grpc=3。
"""

import unittest
from unittest.mock import MagicMock, patch


class BrowserPathBookkeepingTests(unittest.TestCase):
    """浏览器路径的号池记账：三种落定（成功 / 已消耗 / 失败）。"""

    def _run(self, *, browser_result: dict):
        """跑一次 `_register_via_browser`，记录记账调用。"""
        from core.base_mailbox import MailboxAccount
        from core.base_platform import RegisterConfig
        import platforms.grok.plugin as plugin_mod

        statuses: list = []
        logs: list = []

        class Mb:
            def set_account_status(self, account, status):
                statuses.append(str(status).strip().lower())

            def get_email(self):
                return MailboxAccount(
                    email="t@icloud.com", extra={"icloud_alias_id": 42}
                )

            def wait_for_code(self, account, **kwargs):
                return "123456"

        plugin = plugin_mod.GrokPlatform.__new__(plugin_mod.GrokPlatform)
        plugin.config = RegisterConfig(
            executor_type="browser",
            extra={
                "grok_oauth_exchange": "0",
                "grok_probe_after_register": "0",
                "grok_grok2api_ingest": "0",
            },
            proxy="",
        )
        plugin.mailbox = Mb()
        plugin._log_fn = logs.append
        plugin._task_control = None

        # 成功路径会继续做 OAuth 兑换与 CPA 落盘 —— 那些都要打网络，
        # 单测里必须一并 mock，否则整个测试文件挂住（实测踩过）。
        # extra 必须显式关掉 OAuth / 测活 / grok2api 接入：
        # `_truthy(key, default=True)` 让**空 dict** 把它们全打开，
        # 于是成功路径会真去打网络（实测挂住 60 秒+）。
        extra = {
            "grok_oauth_exchange": "0",
            "grok_probe_after_register": "0",
            "grok_grok2api_ingest": "0",
        }
        with patch("platforms.grok.register_browser.register_grok_via_browser",
                   return_value=browser_result), \
             patch.object(plugin, "_maybe_write_cpa", return_value=None):
            try:
                plugin._register_via_browser(extra, None, logs.append, "")
            except Exception as exc:
                logs.append(f"RAISED {type(exc).__name__}: {exc}")
        return statuses, logs

    def test_success_records_used(self):
        statuses, logs = self._run(browser_result={
            "ok": True, "sso": "sso-token", "password": "pw",
            "email": "t@icloud.com", "error": "",
        })
        self.assertEqual(statuses, ["used"], f"成功必须记 used；日志: {logs[-5:]}")

    def test_already_registered_records_used_not_failed(self):
        """x.ai 报「已有账号」→ 记 used（地址已消耗，别再发）。

        这是实测踩过的坑：记 failed 会把它放回 available，下一轮又被领出来
        撞同一堵墙。
        """
        statuses, logs = self._run(browser_result={
            "ok": False, "sso": "", "password": "",
            "email": "t@icloud.com",
            "error": "该邮箱已有 x.ai 账号（别名已消耗）",
            "already_registered": True,
        })
        self.assertEqual(
            statuses, ["used"],
            f"已消耗的别名必须记 used，否则会被反复重领；实际: {statuses}",
        )

    def test_ordinary_failure_records_failed(self):
        """普通失败（地址没问题，本轮废了）→ 记 failed（放回池子下次可用）。"""
        statuses, logs = self._run(browser_result={
            "ok": False, "sso": "", "password": "",
            "email": "t@icloud.com", "error": "页面未确认发信",
        })
        self.assertEqual(statuses, ["failed"], f"实际: {statuses}")


class Grok2ApiClientTests(unittest.TestCase):
    """grok2api 客户端的请求形态（不联网）。"""

    def _client(self):
        from platforms.grok.grok2api import Grok2ApiClient

        c = Grok2ApiClient("http://g2a.test", "admin", "pw")
        c._token = "tok"
        c._token_exp = 10**12
        return c

    def test_import_uses_fixed_filename(self):
        """文件名必须是 grok-web-sso-tokens.txt（grok2api 靠它识别类型）。"""
        c = self._client()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = 'data: {"created":1}\n'
        with patch.object(c, "_request", return_value=resp) as req:
            ok, msg = c.import_sso("sso-value")
        self.assertTrue(ok, msg)
        files = req.call_args.kwargs["files"]
        self.assertIn("file", files)
        filename = files["file"][0]
        self.assertEqual(filename, "grok-web-sso-tokens.txt")
        self.assertIn(b"sso-value", files["file"][1])

    def test_duplicate_import_counts_as_success(self):
        """重复上传返回 created:0/updated:1 —— 也算成功（幂等）。"""
        c = self._client()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = 'data: {"created":0,"updated":1}\n'
        with patch.object(c, "_request", return_value=resp):
            ok, _ = c.import_sso("sso-value")
        self.assertTrue(ok)

    def test_failed_import_reports_body(self):
        c = self._client()
        resp = MagicMock()
        resp.status_code = 500
        resp.text = "Cannot read properties of undefined (reading 'trim')"
        with patch.object(c, "_request", return_value=resp):
            ok, msg = c.import_sso("sso-value")
        self.assertFalse(ok)
        self.assertIn("trim", msg)

    def test_account_setup_order_is_terms_birthdate_nsfw(self):
        """NSFW 三步顺序不能换（grok2api 侧有依赖）。"""
        c = self._client()
        resp = MagicMock()
        resp.status_code = 200
        calls: list = []

        def _req(method, path, **kwargs):
            calls.append(path)
            return resp

        with patch.object(c, "_request", side_effect=_req):
            ok, done, failed = c.account_setup(42)
        self.assertTrue(ok, failed)
        self.assertEqual(
            [p.rsplit("/", 1)[-1] for p in calls], ["accept-terms", "birth-date", "nsfw"]
        )
        self.assertEqual(done, ["accept-terms", "birth-date", "nsfw"])

    def test_derive_posts_ids_and_strategy(self):
        c = self._client()
        resp = MagicMock()
        resp.status_code = 200
        resp.text = 'data: {"created":1}\n'
        with patch.object(c, "_request", return_value=resp) as req:
            ok, _ = c.sync_to_console([7])
        self.assertTrue(ok)
        body = req.call_args.kwargs["json_body"]
        self.assertEqual(body["ids"], ["7"], "id 要转成字符串（grok2api 按字符串比）")
        self.assertEqual(body["strategy"], "all")

    def test_unconfigured_client_reports_clearly(self):
        from platforms.grok.grok2api import Grok2ApiClient, Grok2ApiError

        c = Grok2ApiClient("", "", "")
        self.assertFalse(c.configured)
        with self.assertRaises(Grok2ApiError):
            c.login()

    def test_ingest_flow_reports_derived_formats(self):
        """完整接入：上传 → 派生 → NSFW，摘要里带出每一步。

        派生用 Web 号池的 id（`sync-to-console` / `convert-to-build` 只认
        provider=grok_web 的账号，见 `ingest_sso` 里的说明）。
        """
        c = self._client()
        with patch.object(c, "import_sso", return_value=(True, "created/updated")), patch.object(
            c, "find_web_account_by_email", return_value={"id": 5, "email": "a@b.com", "provider": "grok_web"}
        ), patch.object(c, "sync_to_console", return_value=(True, "ok")), patch.object(
            c, "convert_to_build", return_value=(True, "ok")
        ), patch.object(
            c, "account_setup", return_value=(True, ["accept-terms", "birth-date", "nsfw"], [])
        ):
            ok, summary = c.ingest_sso("sso", "a@b.com")
        self.assertTrue(ok)
        for part in ("web", "console", "build", "NSFW"):
            self.assertIn(part, summary)

    def test_ingest_upload_failure_is_fatal(self):
        c = self._client()
        with patch.object(c, "import_sso", return_value=(False, "HTTP 500")):
            ok, summary = c.ingest_sso("sso", "a@b.com")
        self.assertFalse(ok)
        self.assertIn("上传失败", summary)


if __name__ == "__main__":
    unittest.main()
