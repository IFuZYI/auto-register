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


class _FakeMailbox:
    """最小邮箱渠道：注册路径只用到 `get_email` 与 `set_account_status`。

    做成模块级单一定义 —— 此前每个用例里各写一份匿名 `Mb` 类，三份重复。
    """

    def set_account_status(self, account, status):
        pass

    def get_email(self):
        from core.base_mailbox import MailboxAccount

        return MailboxAccount(email="t@icloud.com", extra={"icloud_alias_id": 42})


def _account(**extra):
    """构造一个 grok 账号（默认带 SSO、无 AT —— 与默认注册产物一致）。"""
    from core.base_platform import Account, AccountStatus

    data = {"sso": "sso-1"}
    data.update(extra)
    return Account(
        platform="grok", email="a@b.com", password="p",
        status=AccountStatus.REGISTERED, extra=data,
    )


def _run(extra: dict):
    """跑一次浏览器注册路径，返回 (account, 日志, mock 句柄)。"""
    logs: list = []
    plat = _platform({**extra, "grok_probe_after_register": "0"})
    plat.mailbox = _FakeMailbox()
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
            g2a_cls.from_config.return_value.configured = False
            plat = _platform({"grok_oauth_exchange": "1", "grok_probe_after_register": "0"})
            plat.mailbox = _FakeMailbox()
            plat._log_fn = lambda _m: None
            account = plat._register_via_browser(
                dict(plat.config.extra), None, lambda _m: None, ""
            )

        exchange.assert_called_once()
        self.assertEqual(account.extra.get("access_token"), "at")
        self.assertIn("cpa_record", account.extra)



class BuildCredentialReachabilityTests(unittest.TestCase):
    """注册不再产 build 后，补 build 的两条路必须真的能落盘/上传。

    回归（独立评审发现）：`_maybe_write_cpa` 唯一的调用点在注册的 opt-in 分支里，
    「刷新 Token（登录协议）」只把 cpa_record 塞进 patch、从不调用它 —— 于是
    新号想补本地 `cpa_auth_dir` 写入或自动传 CPA，按钮点了也没反应，而文档把
    那条路写成「可用」。
    """

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
                result = plat.execute_action("refresh_token", _account(), {})
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
            result = plat.execute_action("refresh_token", _account(), {})
        self.assertTrue(result["ok"], result)
        up.assert_called_once()
        self.assertTrue(result["account_extra_patch"].get("cpa_uploaded"))

    def test_probe_action_without_at_uses_sso_instead_of_hard_error(self):
        """没有 AT（新号的常态）时「测活」不能直接报缺 AT —— 回落 SSO 探活。

        SSO 探活本身要打网络，这里只钉**动作层的接线**：不硬报错、走 detail、
        把 sso_status 带进 patch。判定逻辑由 `SsoStatusTests` 单独覆盖。
        """
        from unittest.mock import patch as _patch

        plat = _platform({})
        plat._log_fn = lambda _m: None
        detail = {"code": None, "summary": "SSO 探测：alive", "usable": True,
                  "sso_status": "alive"}
        with _patch.object(plat, "probe_account_detail", return_value=detail) as detail_fn:
            result = plat.execute_action("probe", _account(access_token=""), {})
        detail_fn.assert_called_once()
        self.assertTrue(result["ok"], f"无 AT 时应回落 SSO 探活，实际: {result}")
        self.assertIn("SSO", str(result["data"].get("summary") or ""))
        self.assertEqual(result["account_extra_patch"].get("sso_status"), "alive",
                         "sso_status 要落进 patch，否则 api/actions 落状态时拿不到")

    def test_probe_action_reports_rejected_sso_as_not_ok(self):
        """SSO 被上游拒绝时「测活」必须是失败（不能一律 alive）。"""
        from unittest.mock import patch as _patch

        plat = _platform({})
        plat._log_fn = lambda _m: None
        detail = {"code": None, "summary": "SSO 探测：rejected", "usable": False,
                  "sso_status": "rejected"}
        with _patch.object(plat, "probe_account_detail", return_value=detail):
            result = plat.execute_action("probe", _account(access_token=""), {})
        self.assertFalse(result["ok"], f"rejected 不能算有效: {result}")


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


class SsoStatusTests(unittest.TestCase):
    """`_sso_status` 必须真的能判出 rejected。

    回归（独立评审发现）：实现读 `getattr(r, "url", "")` 判断是否被重定向到
    登录页，但 `core.base_executor.Response` 根本没有 `url` 字段（只有
    status_code / text / headers / cookies）—— 于是 `final` 恒为空串，
    **任何** SSO 都判 `alive`，包括一个乱填的。`check_valid` 与「测活」按钮
    都建在它上面，等于失效判定永远不生效。
    """

    def _platform(self):
        return _platform({})

    def test_response_has_no_url_so_redirect_must_be_read_elsewhere(self):
        """钉住根因：Response 没有 url 字段（改实现的人得知道这一点）。"""
        from core.base_executor import Response

        r = Response(status_code=200, text="<html>sign-in</html>")
        self.assertFalse(hasattr(r, "url"))

    def test_redirect_to_sign_in_is_rejected(self):
        """响应内容/头里出现登录页特征 → rejected。"""
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        from core.base_executor import Response

        fake = MagicMock()
        fake.get.return_value = Response(
            status_code=200,
            text='<html><body>Sign in to your account</body></html>',
        )
        fake.get_cookies.return_value = {}
        with _patch("modules.execution.BrowserExecutorFactory") as factory:
            factory.return_value.create.return_value = fake
            self.assertEqual(self._platform()._sso_status("garbage"), "rejected")

    def test_healthy_page_is_alive(self):
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        from core.base_executor import Response

        fake = MagicMock()
        fake.get.return_value = Response(status_code=200, text="<html>Grok console</html>")
        fake.get_cookies.return_value = {}
        with _patch("modules.execution.BrowserExecutorFactory") as factory:
            factory.return_value.create.return_value = fake
            self.assertEqual(self._platform()._sso_status("good"), "alive")

    def test_network_error_is_unknown(self):
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        fake = MagicMock()
        fake.get.side_effect = RuntimeError("boom")
        fake.get_cookies.return_value = {}
        with _patch("modules.execution.BrowserExecutorFactory") as factory:
            factory.return_value.create.return_value = fake
            self.assertEqual(self._platform()._sso_status("x"), "unknown")


class RejectedSsoLandsStatusTests(unittest.TestCase):
    """被拒的 SSO 必须在账号状态上落「失效」—— 不能只报 ok=False。

    回归（独立评审发现）：`api/actions.py` 的 probe 分支只把 `probe_code` /
    `probe_summary` 交给 `apply_grok_status_policy`，而 SSO 探活的 code 是 None、
    summary 是「SSO 探测：rejected」—— 判定函数认的是 `sso_rejected` 标志，
    于是状态一个都不落，账号停在「正常」。
    """

    def test_actions_passes_sso_rejected_for_probe(self):
        import inspect

        from api import actions as actions_mod

        src = inspect.getsource(actions_mod._apply_action_result)
        probe_block = src.split('if action_id == "probe":', 1)[1].split('elif action_id == "probe_refresh"', 1)[0]
        self.assertIn("sso_rejected", probe_block,
                      "probe 分支必须把 sso_status 传给 apply_grok_status_policy，"
                      "否则被拒的 SSO 不落状态")

    def test_rejected_probe_patch_carries_sso_status_that_policy_understands(self):
        """patch 里的 sso_status 值必须是判定函数认得的那三个之一。"""
        from unittest.mock import patch as _patch

        plat = _platform({})
        plat._log_fn = lambda _m: None
        detail = {"code": None, "summary": "SSO 探测：rejected", "usable": False,
                  "sso_status": "rejected"}
        with _patch.object(plat, "probe_account_detail", return_value=detail):
            result = plat.execute_action("probe", _account(access_token=""), {})

        status = result["account_extra_patch"]["sso_status"]
        from services.grok_account_state import apply_grok_status_policy

        account = type("A", (), {"status": "registered"})()
        reason = apply_grok_status_policy(account, sso_rejected=(status == "rejected"))
        self.assertEqual(reason, "sso_rejected")
        self.assertEqual(account.status, "invalid")


class ProbeAfterRegisterSkipIsExplainedTests(unittest.TestCase):
    """开着 `grok_probe_after_register` 但没有 AT 时要说明为什么跳过。

    回归（独立评审 Minor）：默认不换 OAuth 后这个开关必然空转，而它此前
    **静默** no-op —— 用户打开开关却看不到 probe_status，只会以为测活坏了。
    """

    def test_skip_is_logged(self):
        from unittest.mock import patch as _patch

        logs: list = []
        plat = _platform({"grok_probe_after_register": "1"})
        plat.mailbox = _FakeMailbox()
        plat._log_fn = logs.append
        with _patch("platforms.grok.register_browser.register_grok_via_browser",
                    return_value={"ok": True, "sso": "sso-token", "password": "pw",
                                  "email": "t@icloud.com", "error": ""}), \
             _patch("platforms.grok.grok2api.Grok2ApiClient") as g2a:
            g2a.from_config.return_value.configured = False
            plat._register_via_browser(dict(plat.config.extra), None, logs.append, "")

        self.assertTrue(
            any("跳过注册后测活" in str(line) for line in logs),
            f"跳过要留痕，否则用户以为测活坏了；日志: {logs}",
        )


class OptInPlusIngestTogetherTests(unittest.TestCase):
    """显式换 build + grok2api 接入必须能同一次跑完（两条路互不吞掉）。

    回归（独立评审 Minor）：两个用例各自单独覆盖 —— opt-in 用例把
    `configured` 设成 False（跳过接入），接入用例不设 opt-in。组合起来
    （换到 AT/RT 的同时把 SSO 传上去）没有测试守着。
    """

    def test_exchange_and_ingest_in_one_run(self):
        from unittest.mock import patch as _patch

        logs: list = []
        plat = _platform({"grok_oauth_exchange": "1"})
        plat.mailbox = _FakeMailbox()
        plat._log_fn = logs.append
        tokens = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

        with _patch("platforms.grok.register_browser.register_grok_via_browser",
                    return_value={"ok": True, "sso": "sso-token", "password": "pw",
                                  "email": "t@icloud.com", "error": ""}), \
             _patch("platforms.grok.register_browser.exchange_oauth_via_browser",
                    return_value=tokens) as exchange, \
             _patch.object(plat, "_maybe_write_cpa", return_value=None), \
             _patch("platforms.grok.grok2api.Grok2ApiClient") as g2a:
            g2a.from_config.return_value.configured = True
            g2a.from_config.return_value.ingest_sso.return_value = (True, "已接入（web + NSFW）")
            account = plat._register_via_browser(
                dict(plat.config.extra), None, logs.append, ""
            )

        exchange.assert_called_once()
        self.assertEqual(account.extra.get("access_token"), "at")
        self.assertIn("cpa_record", account.extra)
        client = g2a.from_config.return_value
        client.ingest_sso.assert_called_once()
        self.assertEqual(client.ingest_sso.call_args.args[0], "sso-token")
        self.assertTrue(account.extra.get("grok2api_ingested"))


if __name__ == "__main__":
    unittest.main()
