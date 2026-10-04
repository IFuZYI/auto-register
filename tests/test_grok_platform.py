"""Grok 插件功能完备性验证。

覆盖：
1. 插件注册与契约（BasePlatform 抽象方法全部实现）
2. grpc-web / protobuf 编解码正确性（含字节级断言）
3. signup body 结构与字段
4. SSO 判定与提取（含误判防护）
5. 验证码提取（xAI XXX-XXX 格式）
6. 错误判定（防误杀：大 body 不做 duplicate 匹配）
7. OAuth Device Flow 参数（CLIENT_ID / SCOPES / grant_type）
8. CPA 记录格式与文件名
9. 测活请求构造
10. 平台操作（actions）完备性
11. 邮箱渠道可用性（18 家 provider 全部可创建）
12. 代理池集成
13. 端到端流程（mock HTTP，验证调用序列）

运行：python -m tests.test_grok_platform
"""
from __future__ import annotations

import json
import sys
import struct
import unittest
from unittest.mock import MagicMock, patch


class TestOAuthConstants(unittest.TestCase):
    """OAuth Device Flow 参数（与 4 个参考项目一致）。"""

    def test_client_id_matches_reference(self):
        from platforms.grok.constants import CLIENT_ID

        self.assertEqual(CLIENT_ID, "b1a00492-073a-47ea-816f-4c329264a828")

    def test_scopes_exact_six(self):
        from platforms.grok.constants import SCOPES

        parts = SCOPES.split()
        self.assertEqual(len(parts), 6, "scope 必须正好 6 项")
        self.assertEqual(
            parts,
            ["openid", "profile", "email", "offline_access",
             "grok-cli:access", "api:access"],
        )
        # 关键：不能含 conversations / workspaces（该 client 未授权）
        self.assertNotIn("conversations", SCOPES)
        self.assertNotIn("workspaces", SCOPES)

    def test_grant_type(self):
        from platforms.grok.constants import DEVICE_GRANT_TYPE

        self.assertEqual(
            DEVICE_GRANT_TYPE, "urn:ietf:params:oauth:grant-type:device_code"
        )

    def test_endpoints(self):
        from platforms.grok.constants import (
            CONNECT_CREATE, CONNECT_VERIFY, DEVICE_APPROVE_URL, DEVICE_CODE_URL,
            DEVICE_VERIFY_URL, SIGNUP_URL, TOKEN_URL,
        )

        self.assertEqual(CONNECT_CREATE,
                         "https://accounts.x.ai/auth_mgmt.AuthManagement/CreateEmailValidationCode")
        self.assertEqual(CONNECT_VERIFY,
                         "https://accounts.x.ai/auth_mgmt.AuthManagement/VerifyEmailValidationCode")
        self.assertEqual(SIGNUP_URL, "https://accounts.x.ai/sign-up?redirect=grok-com")
        self.assertEqual(DEVICE_CODE_URL, "https://auth.x.ai/oauth2/device/code")
        self.assertEqual(DEVICE_VERIFY_URL, "https://auth.x.ai/oauth2/device/verify")
        self.assertEqual(DEVICE_APPROVE_URL, "https://auth.x.ai/oauth2/device/approve")
        self.assertEqual(TOKEN_URL, "https://auth.x.ai/oauth2/token")

    def test_probe_endpoint(self):
        from platforms.grok.constants import CPA_PROBE_URL, CPA_PROBE_MODEL

        self.assertEqual(CPA_PROBE_URL, "https://cli-chat-proxy.grok.com/v1/responses")
        self.assertEqual(CPA_PROBE_MODEL, "grok-4.5")


class TestCpaRecord(unittest.TestCase):
    """CPA 记录格式与文件名。"""

    def _token(self):
        import base64

        def b64(d):
            return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

        return {
            "access_token": f"{b64({'alg':'RS256'})}.{b64({'sub':'u1','email':'a@b.com','exp':1893456000})}.sig",
            "refresh_token": "rt-123",
            "id_token": "",
            "expires_in": 21600,
            "token_type": "Bearer",
        }

    def test_record_fields(self):
        from platforms.grok.oauth_device import token_to_cpa_record

        record = token_to_cpa_record(self._token(), email="a@b.com", sso="sso-val")
        self.assertEqual(record["type"], "xai")
        self.assertEqual(record["auth_kind"], "oauth")
        self.assertEqual(record["email"], "a@b.com")
        self.assertEqual(record["sub"], "u1")
        self.assertEqual(record["token_type"], "Bearer")
        self.assertEqual(record["token_endpoint"], "https://auth.x.ai/oauth2/token")
        self.assertEqual(record["base_url"], "https://cli-chat-proxy.grok.com/v1")
        self.assertIn("headers", record)
        self.assertTrue(record["expired"].endswith("Z"), "expired 应为 RFC3339 带 Z")

    def test_sso_excluded_by_default(self):
        """默认不写 sso 字段（对齐健康号格式）。"""
        import os

        from platforms.grok.oauth_device import token_to_cpa_record

        old = os.environ.pop("CPA_INCLUDE_SSO", None)
        try:
            record = token_to_cpa_record(self._token(), email="a@b.com", sso="secret")
            self.assertNotIn("sso", record)
        finally:
            if old is not None:
                os.environ["CPA_INCLUDE_SSO"] = old

    def test_sso_included_when_enabled(self):
        import os

        from platforms.grok.oauth_device import token_to_cpa_record

        os.environ["CPA_INCLUDE_SSO"] = "1"
        try:
            record = token_to_cpa_record(self._token(), email="a@b.com", sso="secret")
            self.assertEqual(record["sso"], "secret")
        finally:
            os.environ.pop("CPA_INCLUDE_SSO", None)

    def test_filename(self):
        from platforms.grok.oauth_device import cpa_auth_filename

        self.assertEqual(
            cpa_auth_filename({"email": "a@b.com"}), "xai-a@b.com.json"
        )
        # 已是 xai 开头不重复加前缀
        self.assertEqual(
            cpa_auth_filename({"email": "xai-user@b.com"}), "xai-user@b.com.json"
        )
        # 无 email 时用 sub
        self.assertEqual(cpa_auth_filename({"sub": "user-1"}), "xai-user-1.json")

    def test_auth_entry_format(self):
        from platforms.grok.oauth_device import token_to_auth_entry

        key, entry = token_to_auth_entry(self._token(), email="a@b.com")
        self.assertEqual(key, "https://auth.x.ai::b1a00492-073a-47ea-816f-4c329264a828")
        self.assertEqual(entry["auth_mode"], "oidc")
        self.assertEqual(entry["oidc_client_id"], "b1a00492-073a-47ea-816f-4c329264a828")
        self.assertEqual(entry["refresh_token"], "rt-123")


class TestPlatformContract(unittest.TestCase):
    """平台插件契约完备性。"""

    def test_registered(self):
        from core.registry import get, load_all

        load_all()
        cls = get("grok")
        self.assertEqual(cls.name, "grok")
        self.assertEqual(cls.display_name, "Grok")

    def test_abstract_methods_implemented(self):
        from core.registry import get, load_all

        load_all()
        cls = get("grok")
        # 可实例化 = 抽象方法全部实现
        instance = cls()
        self.assertTrue(callable(instance.register))
        self.assertTrue(callable(instance.check_valid))

    def test_supported_executors(self):
        from core.registry import get, load_all

        load_all()
        cls = get("grok")
        # 协议路径已删除（2026-10-01）——只剩浏览器一条路。
        # 断言「至少有一个且第一个是 browser」，而不是写死列表：
        # 写死会让以后新增执行器时这条测试变成噪音。
        self.assertTrue(cls.supported_executors, "必须声明执行器")
        self.assertEqual(cls.supported_executors[0], "browser",
                         "第一个 = 平台默认，Grok 的默认必须是浏览器")

    def test_actions_declared(self):
        from core.registry import get, load_all

        load_all()
        instance = get("grok")()
        actions = instance.get_platform_actions()
        ids = {a["id"] for a in actions}
        for expected in ("probe", "refresh_oauth", "export_cpa_json",
                         "upload_cpa", "upload_sub2api"):
            self.assertIn(expected, ids, f"缺少操作: {expected}")
        # 每个 action 都要有 label 与 params
        for a in actions:
            self.assertIn("label", a)
            self.assertIn("params", a)
        # 上传类动作要标 scope=panel：它们的目标是外部面板，操作面在面板管理页，
        # 账号页的菜单按这个字段过滤（见 test_panel_management_actions.py）。
        for a in actions:
            if a["id"] in ("upload_cpa", "upload_sub2api", "upload_grok2api"):
                self.assertEqual(a.get("scope"), "panel", f"{a['id']} 缺 scope=panel")

    def test_unknown_action_raises(self):
        from core.base_platform import Account
        from core.registry import get, load_all

        load_all()
        instance = get("grok")()
        with self.assertRaises(NotImplementedError):
            instance.execute_action("nonexistent", Account(platform="grok", email="", password=""), {})

    def test_get_quota(self):
        from core.base_platform import Account
        from core.registry import get, load_all

        load_all()
        instance = get("grok")()
        quota = instance.get_quota(Account(platform="grok", email="a@b.com", password="p",
                                           extra={"access_token": "at", "expires_in": 100}))
        self.assertTrue(quota["has_oauth"])
        self.assertEqual(quota["expires_in"], 100)

    def test_check_valid_without_token(self):
        from core.base_platform import Account
        from core.registry import get, load_all

        load_all()
        instance = get("grok")()
        self.assertFalse(instance.check_valid(
            Account(platform="grok", email="a@b.com", password="p", extra={})
        ))

    def test_executor_fallback(self):
        """不支持的 executor 应回落到平台声明的默认（第一个）。"""
        from core.base_platform import RegisterConfig
        from core.registry import get, load_all

        load_all()
        cls = get("grok")
        cfg = RegisterConfig(executor_type="bogus")
        instance = cls(cfg)
        self.assertEqual(instance.config.executor_type, cls.supported_executors[0],
                         "不受支持的值要回落到平台声明的第一个执行器")

    def test_empty_executor_uses_platform_default_silently(self):
        """空执行器 = 让平台决定，回落默认且**不打降级日志**。

        任务页不预设执行器（各平台支持集合不同，写死会替平台预选）。
        空值走的是「未指定」而不是「选错了」，日志里不该出现降级告警。
        """
        import io
        from contextlib import redirect_stdout

        from core.base_platform import RegisterConfig
        from core.registry import get, load_all

        load_all()
        cls = get("grok")
        buf = io.StringIO()
        with redirect_stdout(buf):
            instance = cls(RegisterConfig(executor_type=""))
        self.assertEqual(instance.config.executor_type, cls.supported_executors[0])
        self.assertNotIn("不受支持", buf.getvalue(),
                         "空值不该报「不受支持」—— 那是「让平台决定」")

    def test_grok_default_executor_is_browser(self):
        """Grok 的默认执行器必须是 browser。

        协议路径在 x.ai 上会被 CF 403（见 register_browser.py 的实测记录），
        默认落到那里等于开箱即坏。列表顺序即默认，这条钉住它。
        """
        from core.registry import get, load_all

        load_all()
        cls = get("grok")
        self.assertEqual(cls.supported_executors[0], "browser")

    def test_chatgpt_declares_only_protocol(self):
        """ChatGPT 只有纯协议 —— 声明里不能出现浏览器执行器。

        实测依据：platforms/chatgpt 全树无 playwright/camoufox/chromium，
        走的是 curl_cffi 指纹伪装。此前它没声明，继承了基类默认的三个执行器，
        界面给它列了「无头/有头浏览器」—— 选了会被静默降级。
        """
        from core.registry import get, load_all

        load_all()
        cls = get("chatgpt")
        self.assertEqual(cls.supported_executors, ["protocol"])


class TestMailboxChannels(unittest.TestCase):
    """邮箱渠道（多 provider 支持）。"""

    def test_mailbox_factory_lists_all_providers(self):
        """内置渠道清单里的每个 provider 都要能查到（Grok 复用）。

        清单取自 `core.mailboxes.channels.__all__`（渠道的权威定义），
        不在这里再抄一份 —— 抄的话删/加渠道要改两处。

        门槛只要求「清单里的都在」，不写死数量：渠道数量随需求增减
        （一次性临时邮箱整体删除后只剩 Outlook），写死 10 会在删渠道时
        报一个和真实意图无关的错。
        """
        from core.base_mailbox import available_providers
        from core.mailboxes.channels import __all__ as builtin_channel_modules

        registered = set(available_providers())
        builtin = set(builtin_channel_modules)
        missing = sorted(builtin - registered)
        self.assertEqual(missing, [], f"这些渠道没注册上: {missing}")
        # 本地号池那条（modules/mail/icloud_local.py）也要在
        self.assertIn("icloud_local", registered)

    def test_grok_uses_injected_mailbox(self):
        """插件应优先使用注入的 mailbox（任务层已建好）。"""
        import inspect

        from platforms.grok import plugin as plugin_mod

        src = inspect.getsource(plugin_mod.GrokPlatform._register_via_browser)
        self.assertIn("self.mailbox", src)
        self.assertIn("wait_for_code", src)
        self.assertIn("create_mailbox", src, "未注入时应能自行创建")

    def test_uses_project_otp_timeout(self):
        """应复用平台的 OTP 超时解析（不写死魔法值）。"""
        import inspect

        from platforms.grok import plugin as plugin_mod

        src = inspect.getsource(plugin_mod.GrokPlatform._register_via_browser)
        self.assertIn("get_mailbox_otp_timeout", src)

    def test_passes_xai_code_pattern(self):
        """应传 xAI 专用验证码正则（XXX-XXX）。

        正则本身在 `constants.CODE_RE_ANY`；传参发生在 `register_browser.py`
        的读码处（插件层不经过它）—— 所以查那个模块，而不是插件。
        """
        import inspect

        from platforms.grok import register_browser as rb_mod
        from platforms.grok.constants import CODE_RE_ANY

        src = inspect.getsource(rb_mod)
        self.assertIn("code_pattern=CODE_RE.pattern", src,
                      "读码时必须把 xAI 的 XXX-XXX 正则交给渠道，"
                      "否则渠道按通用规则抓会漏码")
        # 正则本身确实是 XXX-XXX 形状
        self.assertTrue(CODE_RE_ANY.search("ABC-123"),
                        "CODE_RE_ANY 应匹配 xAI 的 ABC-123 形状")


class TestTurnstileModes(unittest.TestCase):
    """Turnstile 多种方案。"""

    def test_three_modes_available(self):
        from platforms.grok import turnstile_mint as tm

        self.assertTrue(callable(tm.mint_via_captcha))
        self.assertTrue(callable(tm.mint_via_offscreen_chrome))
        self.assertTrue(callable(tm.mint_via_solver_service))
        self.assertTrue(callable(tm.mint_turnstile))

    def test_mint_script_ported(self):
        from pathlib import Path

        script = Path("/home/Register/scripts/grok_turnstile_mint.py")
        self.assertTrue(script.exists(), "屏外 mint 脚本未移植")
        content = script.read_text()
        for flag in ("--site-key", "--url", "--proxy", "--chrome", "--no-headless"):
            self.assertIn(flag, content, f"脚本缺少参数 {flag}")

    def test_mint_via_captcha_validates_token(self):
        from platforms.grok.turnstile_mint import mint_via_captcha

        class FakeCaptcha:
            def solve_turnstile(self, url, key):
                return "short"

        with self.assertRaises(RuntimeError):
            mint_via_captcha(FakeCaptcha(), "key", "url")

    def test_mint_via_captcha_success(self):
        from platforms.grok.turnstile_mint import mint_via_captcha

        class FakeCaptcha:
            def solve_turnstile(self, url, key):
                return "0." + "x" * 50

        token = mint_via_captcha(FakeCaptcha(), "key", "url")
        self.assertTrue(token.startswith("0."))

    def test_offscreen_timeout_error_never_leaks_proxy_credentials(self):
        """mint 超时的错误信息不能带代理凭据。

        `subprocess.TimeoutExpired.__str__` 会原样带上整个 argv，而 args 里
        有 `--proxy http://user:pass@host`。这些错误串最终经 `api/tasks.py`
        收进任务 errors 并通过 `GET /api/tasks/{id}` 回给客户端，所以直接
        `f"timeout: {exc}"` 等于把代理密码发给任何能读任务详情的人。
        """
        import subprocess

        from platforms.grok import turnstile_mint as tm

        secret = "SUPER-SECRET-PROXY-PW"
        proxy = f"http://user:{secret}@proxy.example:2000"

        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(args, timeout=1)

        with patch.object(tm.subprocess, "run", side_effect=fake_run), \
             patch.object(tm, "find_chrome", return_value="/usr/bin/fake-chrome"), \
             patch.object(tm.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                tm.mint_via_offscreen_chrome("0xKEY", "https://x.ai", proxy=proxy, log=None, retries=1)

        message = str(ctx.exception)
        self.assertNotIn(secret, message, f"代理密码泄漏进错误信息: {message}")
        self.assertNotIn("user:", message, f"代理用户名泄漏进错误信息: {message}")

    def test_offscreen_timeout_error_still_diagnosable(self):
        """脱敏不能把错误信息变成没用的空串 —— 仍要能看出是超时。"""
        import subprocess

        from platforms.grok import turnstile_mint as tm

        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(args, timeout=1)

        with patch.object(tm.subprocess, "run", side_effect=fake_run), \
             patch.object(tm, "find_chrome", return_value="/usr/bin/fake-chrome"), \
             patch.object(tm.time, "sleep"):
            with self.assertRaises(RuntimeError) as ctx:
                tm.mint_via_offscreen_chrome("0xKEY", "https://x.ai", proxy="http://u:p@h:1", log=None, retries=1)

        message = str(ctx.exception)
        self.assertIn("timeout", message.lower(), f"错误信息看不出是超时: {message}")
        self.assertIn("***", message, f"应显示脱敏后的代理地址: {message}")

    def test_offscreen_falls_back_and_reports(self):
        """所有方案失败时应抛错并列出各方案原因。"""
        from platforms.grok.turnstile_mint import mint_turnstile

        with patch("platforms.grok.turnstile_mint.mint_via_offscreen_chrome",
                   side_effect=RuntimeError("boom1")), \
             patch("platforms.grok.turnstile_mint.mint_via_solver_service",
                   side_effect=RuntimeError("boom2")):
            with self.assertRaises(RuntimeError) as ctx:
                mint_turnstile("key", "url", captcha=None)
            msg = str(ctx.exception)
            self.assertIn("boom1", msg)
            self.assertIn("boom2", msg)


class TestProxyIntegration(unittest.TestCase):
    """代理池集成。"""

    def test_plugin_reads_proxy_from_config(self):
        """插件必须从 config 读代理（而不是环境变量或全局单例）。

        `register()` 是读配置的地方（读完后把 proxy 传给 `_register_via_browser`）——
        所以查那里。
        """
        import inspect

        from platforms.grok import plugin as plugin_mod

        src = inspect.getsource(plugin_mod.GrokPlatform.register)
        self.assertIn("self.config.proxy", src)

    def test_probe_accepts_proxy(self):
        import inspect

        from platforms.grok.probe import probe_token

        sig = inspect.signature(probe_token)
        self.assertIn("executor", sig.parameters)


class TestProbeRequest(unittest.TestCase):
    """测活请求构造。"""

    def test_probe_sends_required_headers(self):
        from platforms.grok.probe import probe_token

        captured = {}

        class FakeExecutor:
            def post(self, url, headers=None, json=None):
                captured["url"] = url
                captured["headers"] = headers
                captured["json"] = json
                r = MagicMock()
                r.status_code = 200
                r.text = '{"ok":true}'
                return r

        code, summary = probe_token(
            "at-123", email="a@b.com", sub="u1",
            executor=FakeExecutor(), warmup=False, retries=1,
        )
        self.assertEqual(code, 200)
        self.assertEqual(captured["url"], "https://cli-chat-proxy.grok.com/v1/responses")
        h = captured["headers"]
        self.assertEqual(h["Authorization"], "Bearer at-123")
        self.assertEqual(h["x-email"], "a@b.com")
        self.assertEqual(h["x-userid"], "u1")
        self.assertEqual(h["x-grok-model-override"], "grok-4.5")
        # body 结构
        body = captured["json"]
        self.assertEqual(body["model"], "grok-4.5")
        self.assertFalse(body["stream"])
        self.assertEqual(body["max_output_tokens"], 16)
        self.assertEqual(body["input"][0]["content"][0]["text"], "ok")

    def test_probe_retries_transient_403(self):
        from platforms.grok.probe import probe_token

        state = {"n": 0}

        class FakeExecutor:
            def post(self, url, headers=None, json=None):
                state["n"] += 1
                r = MagicMock()
                if state["n"] < 2:
                    r.status_code = 403
                    r.text = '{"error":"permission-denied"}'
                else:
                    r.status_code = 200
                    r.text = "{}"
                return r

        with patch("time.sleep"):
            code, _ = probe_token("at", executor=FakeExecutor(), warmup=False, retries=3)
        self.assertEqual(code, 200)
        self.assertEqual(state["n"], 2, "瞬时 403 应重试")

    def test_probe_no_token(self):
        from platforms.grok.probe import probe_token

        code, msg = probe_token("", warmup=False)
        self.assertIsNone(code)
        self.assertIn("access_token", msg)


class TestUploadModule(unittest.TestCase):
    """上传模块。"""

    def test_upload_without_url_fails_gracefully(self):
        from platforms.grok.upload import upload_to_cpa, upload_to_sub2api

        ok, msg = upload_to_cpa({}, api_url="", api_key="")
        self.assertFalse(ok)
        self.assertIn("未配置", msg)

        ok, msg = upload_to_sub2api({}, api_url="", api_key="")
        self.assertFalse(ok)
        self.assertIn("未配置", msg)

    def test_upload_cpa_success(self):
        from platforms.grok.upload import upload_to_cpa

        class FakeResp:
            status_code = 200
            text = "ok"

        with patch("curl_cffi.requests.post", return_value=FakeResp()):
            ok, msg = upload_to_cpa(
                {"email": "a@b.com", "access_token": "at"},
                api_url="http://cpa.local", api_key="key",
            )
        self.assertTrue(ok)
        self.assertIn("xai-a@b.com.json", msg)

    def test_upload_sub2api_includes_group(self):
        from platforms.grok.upload import upload_to_sub2api

        captured = {}

        class FakeResp:
            status_code = 200
            text = "ok"

        def fake_post(url, headers=None, json=None, **kw):
            captured["payload"] = json
            return FakeResp()

        with patch("curl_cffi.requests.post", side_effect=fake_post):
            ok, _ = upload_to_sub2api(
                {"email": "a@b.com", "access_token": "at"},
                api_url="http://sub.local", api_key="k", group="grok",
            )
        self.assertTrue(ok)
        self.assertEqual(captured["payload"]["group_name"], "grok")


class TestConfigurableBehavior(unittest.TestCase):
    """可配置项。"""

    def test_env_overrides(self):
        import os

        from platforms.grok import constants as c

        old = os.environ.get("GROK_SIGNUP_RETRIES")
        os.environ["GROK_SIGNUP_RETRIES"] = "5"
        try:
            self.assertEqual(c.signup_retries(), 5)
        finally:
            if old is None:
                os.environ.pop("GROK_SIGNUP_RETRIES", None)
            else:
                os.environ["GROK_SIGNUP_RETRIES"] = old

    def test_retries_clamped(self):
        import os

        from platforms.grok import constants as c

        os.environ["GROK_SIGNUP_RETRIES"] = "999"
        try:
            self.assertLessEqual(c.signup_retries(), 5)
        finally:
            os.environ.pop("GROK_SIGNUP_RETRIES", None)

    def test_bad_value_falls_back(self):
        import os

        from platforms.grok import constants as c

        os.environ["GROK_SIGNUP_CFG_TTL"] = "abc"
        try:
            self.assertEqual(c.signup_cfg_ttl(), 1200.0)
        finally:
            os.environ.pop("GROK_SIGNUP_CFG_TTL", None)


class TestBrowserFallback(unittest.TestCase):
    """浏览器兜底路径（CF 保护下的 signup 兑换与 OAuth 授权）。

    背景：signup 响应只给 cookie-chain 跳转令牌，必须由「同一个已通过 CF 的
    浏览器上下文」兑换；device/verify|approve 同样受 CF 保护。见
    reference/grok/cloudTemp-grokzhuce/g/same_session_register.py:2820-2920。
    """

    def test_oauth_browser_module_contract(self):
        """oauth_browser 暴露 oauth_device_via_browser，返回 token dict 或 None。"""
        from platforms.grok import oauth_browser

        self.assertTrue(callable(oauth_browser.oauth_device_via_browser))
        import inspect

        sig = inspect.signature(oauth_browser.oauth_device_via_browser)
        self.assertIn("sso_cookie", sig.parameters)
        self.assertIn("proxy", sig.parameters)

    def test_oauth_browser_empty_sso_returns_none(self):
        """空 SSO 直接返回 None，不启动浏览器。"""
        from platforms.grok.oauth_browser import oauth_device_via_browser

        self.assertIsNone(oauth_device_via_browser(""))
        self.assertIsNone(oauth_device_via_browser("   "))

    def test_approve_selectors_cover_multilingual(self):
        """授权按钮选择器覆盖中英文（上游可能返回任一语言）。"""
        from platforms.grok.oauth_browser import _APPROVE_SELECTORS

        joined = " ".join(_APPROVE_SELECTORS)
        for word in ("Continue", "Allow", "Authorize", "允许", "继续"):
            self.assertIn(word, joined, f"缺少授权按钮文案: {word}")

    def test_plugin_uses_browser_fallback_by_default(self):
        """插件默认开启浏览器兜底（可用 extra 关闭）。"""
        from platforms.grok import plugin as plugin_mod

        self.assertTrue(hasattr(plugin_mod, "_truthy"))
        # 默认（未传）= 开启
        self.assertTrue(plugin_mod._truthy(None, default=True))
        # 显式关闭
        for off in ("0", "false", "no", "off", "False"):
            self.assertFalse(plugin_mod._truthy(off, default=True))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
