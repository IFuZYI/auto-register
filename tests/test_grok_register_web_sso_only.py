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


def _isolate_cpa_env(testcase):
    """清掉环境里的 `GROK_CPA_AUTH_DIR`，测试结束后恢复。

    `_maybe_write_cpa` 会读这个环境变量（文档推荐的唯一入口），开发机上常驻
    有值。不清的话：默认路径的用例会误报「未写入」，还会往那个目录真写文件
    （复审实测：`xai-a@b.com.json` 出现在外部目录里）。
    """
    import os
    from unittest.mock import patch as _patch

    patcher = _patch.dict(os.environ, {"GROK_CPA_AUTH_DIR": ""}, clear=False)
    patcher.start()
    testcase.addCleanup(patcher.stop)


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

    def setUp(self):
        _isolate_cpa_env(self)

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

    def test_probe_action_without_at_explains_how_to_get_one(self):
        """没有 AT（新号的常态）时「测活」要说清缺什么、怎么补 —— 不能静默失败。

        这里**不**回落 SSO 判定：那套「访问 accounts.x.ai 看是否跳登录页」的
        判定已知不可靠（响应没有 url、i18n 文案表里带 sign-in 字样、全新会话会
        把有效 SSO 判成未登录），拿它当测活结论会把好号标成「失效」。
        """
        plat = _platform({})
        plat._log_fn = lambda _m: None
        result = plat.execute_action("probe", _account(access_token=""), {})
        self.assertFalse(result["ok"])
        self.assertIn("access_token", result["error"])
        self.assertIn("刷新 Token", result["error"],
                      "只说缺 AT 不够 —— 要告诉用户怎么补（评审要求）")
        self.assertNotIn("sso_status", result.get("account_extra_patch") or {},
                         "不可靠的 SSO 判定不该参与测活结论")


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

    def test_saved_setting_actually_reaches_the_plugin(self):
        """存进配置后，**插件实例**必须真的会去换 —— 端到端，不是回显。

        复审指出原来的写法是恒真式：`RegistrationContextBuilder` 只是通用
        dict merge，把键名换成任意字符串也一样通过，等于什么都没证明。
        这里走真实链路：config_store → RegistrationContextBuilder →
        RegisterConfig(extra=…) → 插件 → `_register_via_browser` 是否调了
        `exchange_oauth_via_browser`。
        """
        from unittest.mock import patch as _patch

        from core.config_store import config_store
        from modules.config.context import RegistrationContextBuilder

        stored = {"grok_oauth_exchange": "1", "grok_probe_after_register": "0"}
        with _patch.object(config_store, "get_all", return_value=stored):
            merged = RegistrationContextBuilder(config_store.get_all()).build(None).settings

        plat = _platform(dict(merged))
        plat.mailbox = _FakeMailbox()
        plat._log_fn = lambda _m: None
        with _patch("platforms.grok.register_browser.register_grok_via_browser",
                    return_value={"ok": True, "sso": "sso-token", "password": "pw",
                                  "email": "t@icloud.com", "error": ""}), \
             _patch("platforms.grok.register_browser.exchange_oauth_via_browser",
                    return_value={"access_token": "at", "expires_in": 1}) as exchange, \
             _patch.object(plat, "_maybe_write_cpa", return_value=None), \
             _patch("platforms.grok.grok2api.Grok2ApiClient") as g2a:
            g2a.from_config.return_value.configured = False
            plat._register_via_browser(dict(plat.config.extra), None, lambda _m: None, "")

        exchange.assert_called_once()


class SsoStatusIsNotTrustedForProbingTests(unittest.TestCase):
    """`_sso_status` 是已知不可靠的弱信号，不能接进测活/状态判定。

    评审实测（2026-10-23）三条，逐条钉住，防止有人再把它当判据：

    1. `core.base_executor.Response` 没有 `url` —— 「是否跳登录页」看不出来；
    2. 改成看响应体也不成立：accounts.x.ai 每个页面都内嵌同一份 i18n 文案表，
       里面有 `Unable to add sign-in method` / `Create your account`；
    3. 全新会话即使带有效 sso cookie 也常被判未登录（`oauth_device.py:172-174`
       早已因此放弃这套校验）。

    所以：测活不回落它（`BuildCredentialReachabilityTests` 钉住），
    `api/actions.py` 也不拿它落状态（下面第一条钉住）。
    """

    def test_response_has_no_url_field(self):
        """根因：Response 没有 url（改实现的人得知道）。"""
        from core.base_executor import Response

        r = Response(status_code=200, text="<html>sign-in</html>")
        self.assertFalse(hasattr(r, "url"))

    def test_unreachable_executor_is_unknown(self):
        """`unknown` 也必须可达 —— 否则「永远 alive」和「健康」无法区分。

        终审 Minor 5：现有实现对任何输入都返回 alive（url 恒空），所以
        `test_healthy_page_would_be_misread_as_rejected` 在「实现整体坏掉」时也绿。
        这条用会抛异常的 executor 把第三态钉住。
        """
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        fake = MagicMock()
        fake.get.side_effect = RuntimeError("network down")
        fake.get_cookies.return_value = {}
        with _patch("modules.execution.BrowserExecutorFactory") as factory:
            factory.return_value.create.return_value = fake
            self.assertEqual(_platform({})._sso_status("any"), "unknown")

    def test_healthy_page_would_be_misread_as_rejected(self):
        """真实页面的 i18n 文案表里就带 sign-in 字样 —— 看响应体会误判。

        这条不是「期望的行为」，而是把**已知缺陷**钉成事实：任何改成看 body
        的实现都会在这里红。修它需要先解决 i18n 文案表的干扰（且第 3 条缺陷
        仍然存在），所以正确做法是不用它做判据。
        """
        from unittest.mock import MagicMock
        from unittest.mock import patch as _patch

        from core.base_executor import Response

        # 真实 accounts.x.ai 页面里内嵌的文案（实测抓取，健康页也有）
        healthy_with_catalog = (
            "<html><title>Privacy Request | SpaceXSI Accounts</title>"
            '<script>{"messages":{"signInMethod":"Unable to add sign-in method",'
            '"createAccount":"Create your account"}}</script></html>'
        )
        fake = MagicMock()
        fake.get.return_value = Response(status_code=200, text=healthy_with_catalog)
        fake.get_cookies.return_value = {}
        with _patch("modules.execution.BrowserExecutorFactory") as factory:
            factory.return_value.create.return_value = fake
            verdict = _platform({})._sso_status("valid-sso")
        self.assertEqual(verdict, "alive",
                         "健康页被判 rejected 会误杀好号 —— 若这里红了，"
                         "说明实现又开始看响应体了，请先读 _sso_status 的 docstring")

    def test_probe_401_still_lands_invalid(self):
        """正向：`probe` 的 401 必须仍然落「失效」—— 别把接线改成死分支。

        复审用变异证明：把 `api/actions.py` 里整个 probe 状态策略删掉，
        `test_actions_never_uses_sso_status_to_land_invalid` 仍然绿（它只断言
        负面方向）。这条补上正向，删掉接线就会红。
        """
        from unittest import mock

        from api.actions import _apply_action_result
        from core.db.models_account import AccountModel

        model = AccountModel(platform="grok", email="w@e.com", password="p",
                             status="registered")
        _apply_action_result(
            "grok", "probe", model,
            {"ok": False, "data": {"status": 401, "summary": "unauthorized"}},
            mock.Mock(),
        )
        self.assertEqual(model.status, "invalid",
                         "probe 401 没落失效 —— probe→状态的接线断了")

    def test_actions_never_uses_sso_status_to_land_invalid(self):
        """行为测试：SSO 判定不得让 probe 动作把账号标成「失效」。

        用真调 `_apply_action_result` 而不是源码扫描 —— 扫描对「接线被改成
        死分支」这类变异无感（本仓 `test_status_semantics.py` 的既定口径）。
        """
        from unittest import mock

        from api.actions import _apply_action_result
        from core.db.models_account import AccountModel

        model = AccountModel(platform="grok", email="w@e.com", password="p",
                             status="registered")
        _apply_action_result(
            "grok", "probe", model,
            {
                "ok": False,
                "data": {"status": None, "summary": "SSO 探测：rejected"},
                "account_extra_patch": {"sso_status": "rejected"},
            },
            mock.Mock(),
        )
        self.assertEqual(
            model.status, "registered",
            "不可靠的 SSO 判定把好号标成失效了 —— 它会误杀有效账号",
        )


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


class BuildCredentialsAreEffectivelyUnavailableTests(unittest.TestCase):
    """两条「补 build」的路其实是同一条，而这条实测从不成功 —— 文案不能吹。

    终审（2026-10-23）发现：`refresh_via_device_flow` 调的就是
    `register_browser.exchange_oauth_via_browser`（token_refresh.py:226-228），
    与注册末尾那次失败 100% 的 device flow 是**同一个函数**。而插件描述、
    测活报错、文档三处都把它当作「更划算的替代」推荐 —— 等于让用户再白等
    4 分钟走一条本仓日志证明走不通的路。
    """

    def test_refresh_path_uses_the_same_device_flow(self):
        """先钉住事实：两条路同源（改成行为测试 —— 真跑一遍看它调了谁）。

        用 mock 观察实际调用，而不是扫源码：源码扫描对「接线被改成死分支」
        无感（本仓 `test_status_semantics.py` 的既定口径）。
        """
        from unittest.mock import patch as _patch

        from platforms.grok import token_refresh

        with _patch("platforms.grok.register_browser.exchange_oauth_via_browser",
                    return_value={"access_token": "at"}) as exchange:
            token_refresh.refresh_via_device_flow("sso-cookie", proxy="socks5://p:1")
        exchange.assert_called_once()
        # 参数也要断言：cookie 传丢（空串）同样会「调用一次」，但那是「SSO 失效」
        # 与「链路不通」的分界，只看 call_count 抓不住（终审 Minor 6）。
        args, kwargs = exchange.call_args
        self.assertEqual(args[0], "sso-cookie", "SSO cookie 没传给 device flow")
        self.assertEqual(kwargs.get("proxy"), "socks5://p:1", "代理没传下去")

    def test_plugin_description_does_not_call_it_cheaper(self):
        from platforms.grok.plugin import GrokPlatform

        mode = next(m for m in GrokPlatform.registration_modes
                    if m["key"] == "grok_oauth_exchange")
        desc = mode["desc"]
        self.assertNotIn("划算", desc,
                         "两条路同源且实测失败，不能说「按号补更划算」")
        self.assertIn("同一个", desc,
                      "要说清两条路是同一个 device flow，否则用户以为换条路就能成")

    def test_probe_error_does_not_promise_it_will_work(self):
        plat = _platform({})
        plat._log_fn = lambda _m: None
        result = plat.execute_action("probe", _account(access_token=""), {})
        self.assertNotIn("划算", result["error"])
        self.assertIn("同一个", result["error"],
                      "报错里要说明它走的是同一条 device flow")


class RefreshMessageDoesNotOverclaimTests(unittest.TestCase):
    """`refresh_token` 落盘/上传失败时，消息不能无条件说「已保存」。

    终审发现：`_maybe_write_cpa` 内部把失败吞成日志，动作却一律返回
    「Token 已刷新（登录协议），新凭证已保存」—— 用户拿这句话当「成了」的
    唯一信号，而 CPA 落盘/上传可能一个都没发生。
    """

    def test_message_flags_cpa_failure(self):
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        tokens = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        plat = _platform({"cpa_auth_dir": "/proc/definitely/not/writable"})
        plat._log_fn = lambda _m: None
        with _patch("platforms.grok.token_refresh.refresh_via_device_flow",
                    return_value=RefreshResult(ok=True, tokens=tokens, method="device")):
            result = plat.execute_action("refresh_token", _account(), {})
        self.assertTrue(result["ok"], "凭证确实刷新了，ok 仍应为真")
        message = str(result["data"].get("message") or "")
        self.assertIn("CPA", message,
                      f"CPA 落盘失败要在消息里说出来，实际: {message!r}")


class CpaAuthDirIsReachableTests(unittest.TestCase):
    """`cpa_auth_dir` 必须在文档里给出**真能设**的入口。

    终审发现：文档把它当作一条「写本地目录」的路，但 `cpa_auth_dir` 不在
    `CONFIG_KEYS`（`PUT /api/config` 会丢进 ignored）、没有 registration_mode、
    注册页也不提交它 —— 唯一能用的入口是环境变量 `GROK_CPA_AUTH_DIR`。
    这与上一个提交刚给 `grok_oauth_exchange` 修的问题一模一样。
    """

    def test_env_var_really_is_the_only_surface(self):
        """`cpa_auth_dir` 确实没有面板入口 —— 这是「文档只能写环境变量」的依据。

        复审删掉了原来扫 `docs/FEATURES.md` 文本的写法：它依赖 CWD、改标题就
        IndexError，而且证明不了任何行为。这里改为断言**代码事实**：
        该键不在 CONFIG_KEYS、也不在 registration_modes，而
        `_maybe_write_cpa` 真的会读环境变量。
        """
        import inspect

        from api.config import CONFIG_KEYS
        from platforms.grok.plugin import GrokPlatform

        self.assertNotIn("cpa_auth_dir", CONFIG_KEYS,
                         "若已加入白名单，文档就该改成「面板可设」")
        mode_keys = {m["key"] for m in GrokPlatform.registration_modes}
        self.assertNotIn("cpa_auth_dir", mode_keys)
        src = inspect.getsource(GrokPlatform._maybe_write_cpa)
        self.assertIn("GROK_CPA_AUTH_DIR", src,
                      "环境变量是唯一入口 —— 代码必须真的读它")


class StaleActionNameInErrorsTests(unittest.TestCase):
    """缺 CPA 记录的报错不能把用户支去「重换 OAuth」—— 那个 id 已弃用。

    终审 Minor：`refresh_oauth` 的 label 是「已弃用」（plugin.py 的动作声明），
    文档也已改称「刷新 Token（登录协议）」。新号默认没有 cpa_record，所以这两条
    报错是它们最常看到的话。
    """

    def test_upload_errors_name_the_current_action(self):
        """真跑一次动作看报错文案 —— 不扫源码。"""
        plat = _platform({})
        plat._log_fn = lambda _m: None
        account = _account(access_token="at", refresh_token="rt")  # 无 cpa_record

        for action in ("upload_cpa", "upload_sub2api"):
            result = plat.execute_action(action, account, {})
            self.assertFalse(result["ok"])
            error = str(result["error"])
            self.assertIn("刷新 Token", error,
                          f"{action} 的报错应指向当前动作名，实际: {error!r}")
            self.assertNotIn("重换 OAuth", error,
                             f"{action} 的报错仍在指向已弃用的 refresh_oauth")


class DeclaredDefaultMatchesRuntimeTests(unittest.TestCase):
    """界面声明的默认值必须与运行时的兜底一致（否则「界面说 X、运行时做 Y」）。

    终审 Minor：`grok_oauth_exchange` 的 registration_modes 声明 `default: "0"`，
    运行时兜底在 `_truthy(..., default=False)` —— 此前没有任何断言把它们绑在一起。
    """

    def test_declared_default_maps_to_runtime_fallback(self):
        from platforms.grok.plugin import GrokPlatform, _truthy

        mode = next(m for m in GrokPlatform.registration_modes
                    if m["key"] == "grok_oauth_exchange")
        self.assertEqual(mode["default"], "0")
        # 声明 "0" ⇔ 运行时 default=False
        self.assertFalse(_truthy(mode["default"], default=False),
                         "声明值是「关」，运行时兜底却当成「开」")
        self.assertFalse(_truthy(None, default=False),
                         "缺省时必须与声明一致：不换")


class RefreshSurvivesRecordBuildFailureTests(unittest.TestCase):
    """`token_to_cpa_record` 抛异常时，刷新动作仍要正常返回。

    回归（自查发现，我自己在终审修复里引入）：新增的 `cpa_notes` 段在 `try`
    之外读 `settings`，而 `settings` 是在 `try` **里面**赋值的 —— 一旦
    `token_to_cpa_record` 先抛，外层就是 `UnboundLocalError`（未捕获），
    用户拿到 500 而不是「凭证已刷新、CPA 记录没生成」。
    """

    def test_record_failure_does_not_raise(self):
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        tokens = {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}
        plat = _platform({"cpa_auth_dir": "/tmp/x"})
        plat._log_fn = lambda _m: None
        with _patch("platforms.grok.token_refresh.refresh_via_device_flow",
                    return_value=RefreshResult(ok=True, tokens=tokens, method="device")), \
             _patch("platforms.grok.plugin.token_to_cpa_record",
                    side_effect=RuntimeError("boom")):
            result = plat.execute_action("refresh_token", _account(), {})

        self.assertTrue(result["ok"], "凭证确实刷新了，不该因为 CPA 记录生成失败而失败")
        self.assertIn("access_token", result["account_extra_patch"])


class RefreshMessageMatrixTests(unittest.TestCase):
    """六种配置组合下，消息与真实行为必须一致（不误报、不漏报）。

    这是 `_cpa_upload_decision()` 收敛（复审 N4）之后的护栏：消息侧与
    `_maybe_write_cpa` 共用同一处判定，任何一边改动都会让这张表里某行红。
    """

    _TOKENS = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}

    def _run(self, cfg, *, extra=None, env=None, upload_ok=(True, "ok"), return_patch=False):
        import os
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        plat = _platform(dict(extra or {}))
        plat._log_fn = lambda _m: None
        # 默认行必须显式清掉环境变量：`GROK_CPA_AUTH_DIR` 是文档推荐的入口，
        # 开发机上常驻有值 —— 不清的话默认行会误报「未写入」，还会往那个
        # 目录真写一个文件（复审实测）。
        ambient = dict(env or {})
        if "GROK_CPA_AUTH_DIR" not in ambient:
            ambient["GROK_CPA_AUTH_DIR"] = ""
        with _patch.dict(os.environ, ambient, clear=False), \
             _patch("platforms.grok.token_refresh.refresh_via_device_flow",
                    return_value=RefreshResult(ok=True, tokens=self._TOKENS, method="device")), \
             _patch("core.config_store.config_store.get",
                    side_effect=lambda k, d="": cfg.get(k, d)), \
             _patch("platforms.grok.upload.upload_to_cpa", return_value=upload_ok) as up:
            result = plat.execute_action("refresh_token", _account(), {})
        self._last_patch = result.get("account_extra_patch") or {}
        if return_patch:
            return result["data"]["message"], up.call_count
        return result["data"]["message"], up.call_count

    _ON = {"cpa_upload_grok_enabled": "1", "cpa_api_url": "http://x", "cpa_api_key": "k"}

    def test_nothing_configured_says_nothing_extra(self):
        msg, calls = self._run({})
        self.assertEqual(calls, 0)
        self.assertNotIn("CPA", msg, f"什么都没配时不该提 CPA: {msg!r}")

    def test_upload_success_is_quiet(self):
        """上传成功：不报错，**且** patch 里确实记了 `cpa_uploaded=True`。

        复审指出原来这行与「全空」行重复（都只断言「不提 CPA」+ calls）。
        补正向断言后它才独立成立：静默 + 真的传了，两者缺一都不算过。
        """
        msg, calls = self._run(self._ON, return_patch=True)
        self.assertEqual(calls, 1)
        self.assertNotIn("CPA", msg, f"上传成功时不该报错: {msg!r}")
        self.assertTrue(self._last_patch.get("cpa_uploaded"),
                        "上传成功必须记 cpa_uploaded=True（否则下游读不到）")

    def test_upload_failure_is_reported(self):
        msg, _ = self._run(self._ON, upload_ok=(False, "boom"))
        self.assertIn("CPA 上传失败", msg)

    def test_enabled_without_url_is_reported(self):
        msg, calls = self._run({"cpa_upload_grok_enabled": "1"})
        self.assertEqual(calls, 0)
        self.assertIn("未填 CPA 地址", msg)

    def test_bad_config_dir_is_reported(self):
        msg, _ = self._run({}, extra={"cpa_auth_dir": "/proc/nope/x"})
        self.assertIn("本地目录未写入", msg)

    def test_bad_env_dir_is_reported(self):
        msg, _ = self._run({}, env={"GROK_CPA_AUTH_DIR": "/proc/nope/x"})
        self.assertIn("本地目录未写入", msg)


class UserVisibleStringsHaveNoMarkdownTests(unittest.TestCase):
    """用户可见的字符串里不能出现 markdown —— 前端不渲染它。

    终审实测：`registration_modes` 的 desc 进 antd `Form.Item tooltip`、
    `probe` 的 error 进 `<pre>`，两处都不渲染 markdown，用户看到的是字面量
    `**同一个**`。这是本次改动的主要沟通面，星号是纯噪声。
    """

    def test_plugin_description_has_no_markdown(self):
        from platforms.grok.plugin import GrokPlatform

        mode = next(m for m in GrokPlatform.registration_modes
                    if m["key"] == "grok_oauth_exchange")
        self.assertNotIn("**", mode["desc"], f"desc 里有 markdown: {mode['desc']!r}")

    def test_probe_error_has_no_markdown(self):
        plat = _platform({})
        plat._log_fn = lambda _m: None
        error = plat.execute_action("probe", _account(access_token=""), {})["error"]
        self.assertNotIn("**", error, f"报错里有 markdown: {error!r}")

    def test_every_registration_mode_desc_is_plain(self):
        from platforms.grok.plugin import GrokPlatform

        for mode in GrokPlatform.registration_modes:
            self.assertNotIn("**", str(mode.get("desc") or ""),
                             f"{mode['key']} 的 desc 有 markdown")
            self.assertNotIn("`", str(mode.get("desc") or ""),
                             f"{mode['key']} 的 desc 有反引号（同样不渲染）")


class RefreshMessageCoversRecordBuildFailureTests(unittest.TestCase):
    """记录没生成时也要提示 —— 否则消息读起来像成了。

    终审 Minor 2：`token_to_cpa_record` 抛异常 + 开关开着 + 地址已填时，
    三条 note 全不触发（`_cpa_upload_skipped_for_no_url` 为 False，因为地址是有的），
    于是消息是光秃秃的「新凭证已保存」，而落盘/上传一次都没跑。
    """

    def test_record_failure_is_reported(self):
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        tokens = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        plat = _platform({})
        plat._log_fn = lambda _m: None
        cfg = {"cpa_upload_grok_enabled": "1", "cpa_api_url": "http://x", "cpa_api_key": "k"}
        with _patch("platforms.grok.token_refresh.refresh_via_device_flow",
                    return_value=RefreshResult(ok=True, tokens=tokens, method="device")), \
             _patch("core.config_store.config_store.get",
                    side_effect=lambda k, d="": cfg.get(k, d)), \
             _patch("platforms.grok.plugin.token_to_cpa_record",
                    side_effect=RuntimeError("boom")):
            result = plat.execute_action("refresh_token", _account(), {})

        self.assertTrue(result["ok"])
        message = str(result["data"].get("message") or "")
        self.assertIn("未生成", message,
                      f"记录没生成要在消息里说出来，实际: {message!r}")


class RecordFailureNoteIsSingleTests(unittest.TestCase):
    """记录没生成时只报「未生成」，不再附带「本地目录未写入」。

    回归（终审复审实测）：两条同时出现时，读起来像两个独立故障，而实际
    落盘/上传一次都没尝试 —— 「未写入」暗示「写过但失败」。
    """

    def test_only_one_note_when_record_fails(self):
        import tempfile
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        tokens = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        with tempfile.TemporaryDirectory() as tmp:
            plat = _platform({"cpa_auth_dir": tmp})
            plat._log_fn = lambda _m: None
            with _patch("platforms.grok.token_refresh.refresh_via_device_flow",
                        return_value=RefreshResult(ok=True, tokens=tokens, method="device")), \
                 _patch("platforms.grok.plugin.token_to_cpa_record",
                        side_effect=RuntimeError("boom")):
                result = plat.execute_action("refresh_token", _account(), {})

        message = str(result["data"].get("message") or "")
        self.assertIn("未生成", message)
        self.assertNotIn("本地目录未写入", message,
                         f"记录都没生成，不该报「未写入」: {message!r}")


class RefreshRejectsEmptyTokensTests(unittest.TestCase):
    """device flow 返回 ok 但 token 为空时，不能报成功、不能写空凭证。

    实测（本次多面检测发现）：`RefreshResult(ok=True, tokens={})` 会让动作
    返回 `ok=True` + 「新凭证已保存」，同时 patch 里 `access_token` 是空串、
    `probe_status` 被写成 `refresh_ok`、还生成一个 `access_token` 为空的
    `cpa_record`。上游偶发返回空 body 时，账号会被记成「刚刷新过、凭证正常」，
    而实际什么都没有 —— 这正是本分支反复在修的那类「消息说成了、实际没做」。
    """

    def _run(self, tokens):
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        plat = _platform({})
        plat._log_fn = lambda _m: None
        with _patch("platforms.grok.token_refresh.refresh_via_device_flow",
                    return_value=RefreshResult(ok=True, tokens=tokens, method="device")):
            return plat.execute_action("refresh_token", _account(), {})

    def test_empty_tokens_is_not_success(self):
        result = self._run({})
        self.assertFalse(result["ok"], f"空 token 不能算成功: {result}")
        self.assertIn("token", str(result.get("error") or "").lower())

    def test_empty_tokens_writes_no_credentials(self):
        result = self._run({})
        patch = result.get("account_extra_patch") or {}
        self.assertNotIn("access_token", patch, "空 AT 不该写进 patch")
        self.assertNotIn("cpa_record", patch, "空凭证不该生成 cpa_record")
        self.assertNotEqual(patch.get("probe_status"), "refresh_ok",
                            "没有新凭证却标成 refresh_ok")

    def test_missing_access_token_but_rt_present_is_not_success(self):
        """只有 RT、没有 AT 也算没换到 —— 下游全都要 AT。"""
        result = self._run({"refresh_token": "rt-only"})
        self.assertFalse(result["ok"], f"缺 AT 不能算成功: {result}")

class NoFalseAlarmOnSuccessfulWriteTests(unittest.TestCase):
    """写**成功**时绝不能报「未写入」—— 误报方向此前完全没有测试。

    测试质量复审用变异证明：把「写成功后记 `cpa_auth_path`」删掉、或把
    `"cpa_auth_path" not in patch` 这半条件去掉，40 条测试全绿。而这两处
    恰恰决定「消息是否撒谎」。本类补上正反两个方向。
    """

    def _run(self, *, writable: bool):
        import tempfile
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        tokens = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        auth_dir = tmp.name if writable else "/proc/definitely/not/writable"
        plat = _platform({"cpa_auth_dir": auth_dir})
        plat._log_fn = lambda _m: None
        with _patch("platforms.grok.token_refresh.refresh_via_device_flow",
                    return_value=RefreshResult(ok=True, tokens=tokens, method="device")):
            return plat.execute_action("refresh_token", _account(), {}), auth_dir

    def test_successful_write_emits_no_note(self):
        result, auth_dir = self._run(writable=True)
        message = str(result["data"].get("message") or "")
        self.assertNotIn("CPA", message,
                         f"写成功了却报 CPA 问题（误报）: {message!r}")
        # 正向确认：真的落盘了，且路径记进了 patch
        from pathlib import Path
        self.assertEqual(len(list(Path(auth_dir).glob("*.json"))), 1,
                         "临时目录里没有落盘文件 —— 这条测试的前提不成立")
        self.assertIn("cpa_auth_path", result["account_extra_patch"],
                      "写成功后必须记 cpa_auth_path，否则消息侧判不出「已写」")

    def test_unwritable_dir_emits_the_note(self):
        result, _ = self._run(writable=False)
        message = str(result["data"].get("message") or "")
        self.assertIn("本地目录未写入", message,
                      f"写失败却没报: {message!r}")


class EmptySettingMeansNoExchangeTests(unittest.TestCase):
    """配置里**没有**这个键时，注册绝不能换 —— 走真实链路验证默认值。

    复审指出：只测「设为 1 会换」抓不住「默认值被翻回 True」这个变异
    （设为 1 时 default 无关）。这里补上缺省方向的端到端断言。
    """

    def test_absent_setting_means_no_exchange(self):
        from unittest.mock import patch as _patch

        from core.config_store import config_store
        from modules.config.context import RegistrationContextBuilder

        with _patch.object(config_store, "get_all", return_value={}):
            merged = RegistrationContextBuilder(config_store.get_all()).build(None).settings

        plat = _platform(dict(merged))
        plat.mailbox = _FakeMailbox()
        plat._log_fn = lambda _m: None
        with _patch("platforms.grok.register_browser.register_grok_via_browser",
                    return_value={"ok": True, "sso": "sso-token", "password": "pw",
                                  "email": "t@icloud.com", "error": ""}), \
             _patch("platforms.grok.register_browser.exchange_oauth_via_browser") as exchange, \
             _patch("platforms.grok.grok2api.Grok2ApiClient") as g2a:
            g2a.from_config.return_value.configured = False
            account = plat._register_via_browser(dict(plat.config.extra), None, lambda _m: None, "")

        exchange.assert_not_called()
        self.assertNotIn("access_token", account.extra)


class NoAtAccountIsNotDeclaredValidTests(unittest.TestCase):
    """没有 AT 的账号，`check_valid` 不能判「有效」。

    正确性复审 C1（带执行验证）：`probe_account_detail` 在没有 AT 时回落
    `_sso_status`，而那个判定**永远**返回 `alive`（`Response` 没有 url，
    `rejected` 不可达）—— 于是 `check_valid` 恒为 True。调用方
    （`core/scheduler.py` / `api/accounts.py`）见 `valid=True` 就把
    `invalid` 改回 `registered`：**死号被复活**，重新进池、重新被领取。

    改动前新号总有 AT，这条只是罕见兜底；现在它是唯一分支，等于每个新号都走。
    """

    def _platform(self):
        plat = _platform({})
        plat._log_fn = lambda _m: None
        return plat

    def _account(self, status):
        from core.base_platform import Account, AccountStatus

        return Account(platform="grok", email="dead@b.com", password="p",
                       token="sso-dead", status=status, extra={"sso": "sso-dead"})

    def test_check_valid_is_false_without_access_token(self):
        plat = self._platform()
        account = self._account("invalid")
        with mock_patch_status(plat, "alive"):
            self.assertFalse(plat.check_valid(account),
                             "无 AT 时不能仅凭 SSO 判定就说账号有效")

    def test_invalid_stays_invalid_after_check(self):
        """端到端：invalid 账号过一遍「检测」后不能变成 registered。"""
        from core.db.models_account import AccountModel

        plat = self._platform()
        account = self._account("invalid")
        with mock_patch_status(plat, "alive"):
            valid = plat.check_valid(account)
            detail = plat.last_probe_detail
        model = AccountModel(platform="grok", email="dead@b.com", password="p",
                             status="invalid")
        # 复刻两个调用方的判定（scheduler / accounts 同一套）
        if valid and model.status in ("expired", "invalid"):
            model.status = "registered"
        elif not valid:
            plat.apply_probe_status_policy(model, detail=detail)
        self.assertEqual(model.status, "invalid",
                         "死号被复活了 —— 它会重新进池被领取")

    def test_at_present_still_uses_probe_code(self):
        """有 AT 时行为不变：200 判有效（别把正向路径也改坏）。"""
        plat = self._platform()
        account = self._account("registered")
        account.extra["access_token"] = "at"
        with mock_patch_probe(plat, (200, "{}")):
            self.assertTrue(plat.check_valid(account))


def mock_patch_status(plat, status):
    from unittest.mock import patch as _patch

    return _patch.object(plat, "_sso_status", return_value=status)


def mock_patch_probe(plat, ret):
    from unittest.mock import patch as _patch

    return _patch("platforms.grok.plugin.probe_token", return_value=ret)


class MessageCarriesTheRealReasonTests(unittest.TestCase):
    """消息里要给出**真实原因**，不能只指「详见任务日志」。

    正确性复审 I1：动作端点（`api/actions.py`）构造插件时从不设 `_log_fn`，
    `_maybe_write_cpa` 的失败原因只进日志 —— 而那条日志在按钮路径上不存在。
    用户看到「CPA 上传失败（详见任务日志）」却无处可看。
    """

    def test_upload_failure_message_contains_the_reason(self):
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        tokens = {"access_token": "a", "refresh_token": "r", "expires_in": 3600}
        plat = _platform({"cpa_upload_grok_enabled": "1"})
        plat._log_fn = None  # 与动作端点一致：没有日志接收方
        cfg = {"cpa_upload_grok_enabled": "1", "cpa_api_url": "http://x", "cpa_api_key": "k"}
        with _patch("platforms.grok.token_refresh.refresh_via_device_flow",
                    return_value=RefreshResult(ok=True, tokens=tokens, method="device")), \
             _patch("core.config_store.config_store.get",
                    side_effect=lambda k, d="": cfg.get(k, d)), \
             _patch("platforms.grok.upload.upload_to_cpa",
                    return_value=(False, "HTTP 401 unauthorized")):
            result = plat.execute_action("refresh_token", _account(), {})

        message = str(result["data"].get("message") or "")
        self.assertIn("401", message,
                      f"消息里要有真实原因（日志在动作路径上不存在）: {message!r}")


class ProbeRefreshAlsoWritesCpaTests(unittest.TestCase):
    """`probe_refresh` 换到新凭证后也要落盘/上传 —— 与 `refresh_token` 对齐。

    正确性复审 I2：两个动作都从 `token_to_cpa_record` 产出 AT/RT，但只有
    `refresh_token` 调了 `_maybe_write_cpa`。`probe_refresh` 走的是 refresh
    grant（比 device flow 便宜得多），用户更容易点它 —— 却不落盘。
    """

    def test_probe_refresh_writes_record(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch as _patch

        from platforms.grok.token_refresh import RefreshResult

        tokens = {"access_token": "fresh-at", "refresh_token": "fresh-rt",
                  "expires_in": 3600, "token_type": "Bearer"}
        with tempfile.TemporaryDirectory() as tmp:
            plat = _platform({"cpa_auth_dir": tmp})
            plat._log_fn = None
            with _patch("platforms.grok.token_refresh.refresh_via_grant",
                        return_value=RefreshResult(ok=True, tokens=tokens, method="grant")):
                result = plat.execute_action("probe_refresh", _account(refresh_token="old-rt"), {})
            self.assertTrue(result["ok"], result)
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 1,
                             "probe_refresh 换到凭证却没落盘")


if __name__ == "__main__":
    unittest.main()
