"""Grok 注册只产出 Web SSO（grok2api），不再「注册 build」。

用户要求（2026-10-23）：「注册 grok 时不需要注册 build 了，只要 websso
（上传 grok2api）那个。」

「注册 build」在代码里的形态是注册末尾那次 **OAuth Device Flow 换取**
（`exchange_oauth_via_browser` → AT/RT）。它产出的正是 CPA / Build 用的
凭据，而这条链在实测里**每次都白烧 4 分钟**（线上日志：
`点击授权按钮 → 授权未确认，仍尝试取 token → token 轮询失败: ` →
`OAuth 未成功（SSO 仍可用）`，随后 grok2api 接入成功）。也就是说：
build 侧从来没成功过，成功的是 Web SSO 那一段。

所以默认改为**只做 Web SSO**：

1. 不给 `grok_oauth_exchange` 时不发起 Device Flow（省 4 分钟/号）；
2. 账号上不写 `access_token` / `refresh_token` / `cpa_record`（没有 build 凭据）；
3. grok2api 接入照旧（SSO 上传 + NSFW）；
4. 显式 `grok_oauth_exchange=1` 时仍可换取（保留手动要 build 的入口）。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.base_platform import RegisterConfig


def _platform(extra: dict):
    import platforms.grok.plugin as plugin_mod

    return plugin_mod.GrokPlatform(RegisterConfig(executor_type="browser", extra=extra))


def _run(extra: dict):
    """跑一次浏览器注册路径，返回 (account, 日志, mock 句柄)。"""
    from core.base_mailbox import MailboxAccount

    logs: list = []

    class Mb:
        def set_account_status(self, account, status):
            pass

        def get_email(self):
            return MailboxAccount(email="t@icloud.com", extra={"icloud_alias_id": 42})

    plat = _platform({**extra, "grok_probe_after_register": "0"})
    plat.mailbox = Mb()
    plat._log_fn = logs.append

    result = {"ok": True, "sso": "sso-token", "password": "pw",
              "email": "t@icloud.com", "error": ""}

    with patch("platforms.grok.register_browser.register_grok_via_browser",
               return_value=result), \
         patch("platforms.grok.register_browser.exchange_oauth_via_browser") as exchange, \
         patch("platforms.grok.grok2api.Grok2ApiClient") as g2a_cls:
        g2a_cls.from_config.return_value.configured = True
        g2a_cls.from_config.return_value.ingest_sso.return_value = (True, "已接入（web + NSFW）")
        account = plat._register_via_browser(dict(plat.config.extra), None, logs.append, "")
    return account, logs, exchange, g2a_cls


class RegisterSkipsBuildByDefaultTests(unittest.TestCase):
    def test_oauth_exchange_is_not_called_by_default(self):
        """默认不换 OAuth —— 那就是「注册 build」，用户明确不要了。"""
        _account, logs, exchange, _g2a = _run({})
        exchange.assert_not_called()
        self.assertTrue(
            any("跳过" in str(line) for line in logs),
            f"跳过要留痕，否则用户以为还在换（实测每次都白等 4 分钟）；日志: {logs}",
        )

    def test_account_has_no_build_credentials(self):
        """没有 build 凭据：AT/RT/cpa_record 都不该出现。"""
        account, _logs, _exchange, _g2a = _run({})
        for key in ("access_token", "refresh_token", "cpa_record", "cpa_auth_filename"):
            self.assertNotIn(key, account.extra, f"{key} 属于 build 凭据，不该落库")
        self.assertEqual(account.extra.get("sso"), "sso-token")
        self.assertEqual(account.token, "sso-token", "token 列是 SSO 镜像")

    def test_grok2api_web_sso_upload_still_happens(self):
        """Web SSO 上传（grok2api）必须照旧 —— 这是用户要保留的那一段。"""
        _account, _logs, _exchange, g2a_cls = _run({})
        client = g2a_cls.from_config.return_value
        client.ingest_sso.assert_called_once()
        args, kwargs = client.ingest_sso.call_args
        self.assertEqual(args[0], "sso-token")
        self.assertEqual(args[1], "t@icloud.com")

    def test_explicit_opt_in_still_exchanges(self):
        """显式 `grok_oauth_exchange=1` 时仍换 —— 手动要 build 的入口保留。"""
        with patch("platforms.grok.register_browser.exchange_oauth_via_browser",
                   return_value={"access_token": "at", "refresh_token": "rt",
                                 "expires_in": 3600}) as exchange, \
             patch("platforms.grok.grok2api.Grok2ApiClient") as g2a_cls, \
             patch("platforms.grok.register_browser.register_grok_via_browser",
                   return_value={"ok": True, "sso": "sso-token", "password": "pw",
                                 "email": "t@icloud.com", "error": ""}), \
             patch("platforms.grok.plugin.GrokPlatform._maybe_write_cpa", return_value=None):
            from core.base_mailbox import MailboxAccount

            class Mb:
                def set_account_status(self, account, status):
                    pass

                def get_email(self):
                    return MailboxAccount(email="t@icloud.com", extra={"icloud_alias_id": 42})

            g2a_cls.from_config.return_value.configured = False
            plat = _platform({"grok_oauth_exchange": "1", "grok_probe_after_register": "0"})
            plat.mailbox = Mb()
            plat._log_fn = lambda _m: None
            account = plat._register_via_browser(
                dict(plat.config.extra), None, lambda _m: None, ""
            )

        exchange.assert_called_once()
        self.assertEqual(account.extra.get("access_token"), "at")
        self.assertIn("cpa_record", account.extra)


if __name__ == "__main__":
    unittest.main()


class BuildCredentialReachabilityTests(unittest.TestCase):
    """注册不再产 build 后，补 build 的两条路必须真的能落盘/上传。

    回归（独立评审发现）：`_maybe_write_cpa` 唯一的调用点在注册的 opt-in 分支里，
    「刷新 Token（登录协议）」只把 cpa_record 塞进 patch、从不调用它 —— 于是
    新号想补本地 `cpa_auth_dir` 写入或自动传 CPA，按钮点了也没反应，而文档把
    那条路写成「可用」。
    """

    def _account(self, **extra):
        from core.base_platform import Account, AccountStatus

        data = {"sso": "sso-1", "refresh_token": "old-rt"}
        data.update(extra)
        return Account(
            platform="grok", email="a@b.com", password="p",
            status=AccountStatus.REGISTERED, extra=data,
        )

    def test_refresh_token_action_writes_cpa_record_to_local_dir(self):
        """刷新 Token 成功后要把 CPA 记录写进 cpa_auth_dir（否则这条路是死的）。"""
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        tokens = {"access_token": "fresh-at", "refresh_token": "fresh-rt",
                  "expires_in": 21600, "token_type": "Bearer"}
        with tempfile.TemporaryDirectory() as tmp:
            plat = _platform({"cpa_auth_dir": tmp})
            plat._log_fn = lambda _m: None
            with _patch("platforms.grok.token_refresh.refresh_via_device_flow",
                        return_value=RefreshResult(ok=True, tokens=tokens, method="device")):
                result = plat.execute_action("refresh_token", self._account(), {})
            self.assertTrue(result["ok"], result)
            written = list(Path(tmp).glob("*.json"))
            self.assertEqual(len(written), 1, f"cpa_auth_dir 里没有落盘文件: {list(Path(tmp).iterdir())}")

    def test_refresh_token_action_uploads_to_cpa_when_enabled(self):
        """开着 `cpa_upload_grok_enabled` 时，刷新 Token 也要真的推到远端 CPA。"""
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        tokens = {"access_token": "fresh-at", "refresh_token": "fresh-rt",
                  "expires_in": 21600, "token_type": "Bearer"}
        plat = _platform({})
        plat._log_fn = lambda _m: None
        cfg = {"cpa_upload_grok_enabled": "1", "cpa_api_url": "http://cpa.test",
               "cpa_api_key": "k"}
        with _patch("platforms.grok.token_refresh.refresh_via_device_flow",
                    return_value=RefreshResult(ok=True, tokens=tokens, method="device")), \
             _patch("core.config_store.config_store.get",
                    side_effect=lambda k, d="": cfg.get(k, d)), \
             _patch("platforms.grok.upload.upload_to_cpa",
                    return_value=(True, "uploaded")) as up:
            result = plat.execute_action("refresh_token", self._account(), {})
        self.assertTrue(result["ok"], result)
        up.assert_called_once()
        self.assertTrue(result["account_extra_patch"].get("cpa_uploaded"))

    def test_probe_action_without_at_uses_sso_instead_of_hard_error(self):
        """没有 AT（新号的常态）时「测活」不能直接报缺 AT —— SSO 探活能用。"""
        plat = _platform({})
        plat._log_fn = lambda _m: None
        result = plat.execute_action("probe", self._account(access_token=""), {})
        self.assertTrue(result["ok"], f"无 AT 时应回落 SSO 探活，实际: {result}")
        self.assertIn("SSO", str(result["data"].get("summary") or ""))


class OptInIsReachableFromSettingsTests(unittest.TestCase):
    """`grok_oauth_exchange` 必须能从「注册设置」里设 —— 否则文档写的入口是假的。

    回归（独立评审发现）：文档让用户「显式给 grok_oauth_exchange=1」，但这个键
    不在 `CONFIG_KEYS` 里 → `PUT /api/config` 直接丢进 ignored，注册页也从不提交它。
    能改的只有原始任务 extra（`POST /tasks/register`），对面板用户等于不存在。
    """

    def test_key_is_in_config_whitelist(self):
        from api.config import CONFIG_KEYS

        self.assertIn(
            "grok_oauth_exchange", CONFIG_KEYS,
            "不在白名单 → 面板保存被静默丢弃（返回 ignored），文档说的入口不可达",
        )

    def test_platform_declares_it_as_a_registration_setting(self):
        """要能在「注册设置 → 各平台设置 → Grok」卡片里看到并改。"""
        from platforms.grok.plugin import GrokPlatform

        keys = {item["key"] for item in GrokPlatform.registration_modes}
        self.assertIn("grok_oauth_exchange", keys)

    def test_saved_setting_actually_reaches_the_register_path(self):
        """存进配置后，注册链路必须读到它（`RegistrationContextBuilder` 合并）。"""
        from modules.config.context import RegistrationContextBuilder

        ctx = RegistrationContextBuilder({"grok_oauth_exchange": "1"}).build(None)
        self.assertEqual(ctx.settings.get("grok_oauth_exchange"), "1")
