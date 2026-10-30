"""密码登录路线换 RT（platform client_id）。

背景（2026-10-26 实测）：Codex 路线（client_id=app_EMoamEEZ73f0CkXaXp7hrann）
在新号上卡在 choose-an-account / session.select，拿不到 RT；而 platform 路线
（client_id=app_2SKx67EdpoN0G6j64rFvigXD + password/verify + mfa/verify）能在
**不需要手机号**的前提下拿到 RT —— 新号连续 3 次、存量号 1 次成功。

本文件按 TDD 先写测试。参照实现：`/home/chatgpt2api/internal/service/password_login.go`。
"""

from __future__ import annotations

import unittest
from urllib.parse import parse_qs, urlencode, urlparse

from platforms.chatgpt.protocol import AuthFlow, Config

PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
PLATFORM_REDIRECT_URI = "https://platform.openai.com/auth/callback"


class PlatformAuthorizeUrlTests(unittest.TestCase):
    """`_build_platform_authorize` 必须产出 platform 侧的授权 URL。"""

    def test_url_carries_platform_client_and_redirect(self):
        flow = AuthFlow(Config())
        # 真实流程在构造 authorize 之前已经 warmup + seed 过设备标识；
        # 空 device_id 会让 `parse_qs`（默认丢空值）看不到这个参数，
        # 那是断言写法的问题，不是 URL 的问题。
        flow.seed_device_id("11111111-2222-3333-4444-555555555555")
        auth_url, state, verifier, redirect_uri, client_id = flow._build_platform_authorize(
            email="probe@example.com"
        )

        parsed = urlparse(auth_url)
        self.assertEqual(parsed.netloc, "auth.openai.com")
        self.assertTrue(parsed.path.endswith("/api/accounts/authorize"),
                        f"authorize 路径不对: {parsed.path}")

        qs = parse_qs(parsed.query)
        self.assertEqual(qs["client_id"][0], PLATFORM_CLIENT_ID)
        self.assertEqual(qs["redirect_uri"][0], PLATFORM_REDIRECT_URI)
        self.assertEqual(qs["response_type"][0], "code")
        self.assertEqual(qs["code_challenge_method"][0], "S256")
        self.assertIn("offline_access", qs["scope"][0],
                      "没有 offline_access 服务端不会发 refresh_token")
        self.assertEqual(qs["device_id"][0], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(qs["login_hint"][0], "probe@example.com")

        # 返回值要与 URL 自洽 —— 后面换 token 要用同一组值
        self.assertEqual(client_id, PLATFORM_CLIENT_ID)
        self.assertEqual(redirect_uri, PLATFORM_REDIRECT_URI)
        self.assertTrue(state)
        self.assertTrue(verifier)
        self.assertIn(qs["state"][0], {state})


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else ("{}" if payload is None else "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _RecordingSession:
    """记下请求，按 URL 回一个固定响应。"""

    def __init__(self, response):
        self._response = response
        self.calls = []

    def post(self, url, headers=None, data=None, json=None, timeout=None, **kwargs):
        self.calls.append({"url": url, "headers": headers or {}, "data": data, "json": json})
        return self._response


class PlatformTokenExchangeTests(unittest.TestCase):
    """`_exchange_platform_code` 必须打到 platform 端点、带 platform 参数。"""

    def _flow(self, response):
        flow = AuthFlow(Config())
        flow.session = _RecordingSession(response)
        flow._trace_http = lambda *a, **k: None
        return flow

    def test_posts_to_platform_token_endpoint_with_matching_params(self):
        payload = {"access_token": "at-1", "refresh_token": "rt-1", "id_token": "idt-1"}
        flow = self._flow(_Resp(200, payload))

        ok = flow._exchange_platform_code(
            code="the-code", verifier="the-verifier",
            redirect_uri=PLATFORM_REDIRECT_URI, client_id=PLATFORM_CLIENT_ID,
        )

        self.assertTrue(ok, "换 token 应当成功")
        self.assertEqual(len(flow.session.calls), 1)
        call = flow.session.calls[0]
        self.assertEqual(call["url"], "https://auth.openai.com/api/accounts/oauth/token",
                         "端点必须是 platform 那个，不是 Codex 的 /oauth/token")
        body = call["data"] if isinstance(call["data"], str) else urlencode(call["data"] or {})
        sent = parse_qs(body)
        self.assertEqual(sent["grant_type"][0], "authorization_code")
        self.assertEqual(sent["code"][0], "the-code")
        self.assertEqual(sent["code_verifier"][0], "the-verifier")
        self.assertEqual(sent["client_id"][0], PLATFORM_CLIENT_ID,
                         "client_id 必须与产出 code 的那个一致，否则 invalid_grant")
        self.assertEqual(sent["redirect_uri"][0], PLATFORM_REDIRECT_URI)

        self.assertEqual(flow.result.refresh_token, "rt-1")
        self.assertEqual(flow.result.access_token, "at-1")
        self.assertEqual(flow.result.id_token, "idt-1")

    def test_non_200_is_reported_not_raised(self):
        flow = self._flow(_Resp(400, {"error": "invalid_grant"},
                                text='{"error":"invalid_grant"}'))
        ok = flow._exchange_platform_code(
            code="c", verifier="v",
            redirect_uri=PLATFORM_REDIRECT_URI, client_id=PLATFORM_CLIENT_ID,
        )
        self.assertFalse(ok, "失败要返回 False，不该抛异常炸掉整条链")
        self.assertEqual(flow.result.refresh_token, "")


class PlatformLoginRouteTests(unittest.TestCase):
    """`acquire_rt_via_platform_login` 的编排：authorize → 密码 → 2FA → code → token。

    重点钉住复审指出的**授权码消费陷阱**：`follow_redirect_chain` 只拒绝消费
    `/api/auth/callback/openai` 那个回调；platform 的重定向若被跟随，一次性 code
    会被用掉，后面交换必然 invalid_grant。所以本路线必须**从响应里直接读 code**，
    绝不跟随 platform 回调。
    """

    def _flow(self, *, page_after_authorize="login_password", after_password=None,
              after_mfa=None):
        flow = AuthFlow(Config())
        flow._ua = "UA"
        flow._trace_http = lambda *a, **k: None
        flow.result.email = "user@example.com"
        flow.seed_device_id("dev-1")

        steps = {
            "authorize_continue": {"page": {"type": page_after_authorize},
                                   "continue_url": "https://auth.openai.com/log-in/password"},
        }
        if after_password is not None:
            steps["password_verify"] = after_password
        if after_mfa is not None:
            steps["mfa_verify"] = after_mfa

        calls = []

        def _authorize_continue(email, sentinel_token, **kw):
            calls.append(("authorize_continue", email))
            return steps["authorize_continue"]

        def _login_password_verify(password):
            calls.append(("password_verify", password))
            return steps["password_verify"]

        def _submit_mfa_totp(code, challenge_id):
            calls.append(("mfa_verify", challenge_id))
            return steps["mfa_verify"]

        flow.authorize_continue = _authorize_continue
        flow.login_password_verify = _login_password_verify
        flow.submit_mfa_totp = _submit_mfa_totp
        flow.get_sentinel_token = lambda device_id: "sentinel-1"
        flow.auth_oauth_init = lambda url: "dev-1"
        flow._calls = calls
        return flow

    def test_full_route_yields_refresh_token(self):
        mfa = {"continue_url": "https://platform.openai.com/auth/callback?code=CODE-1&state=s"}
        flow = self._flow(after_password={"page": {"type": "mfa_challenge"},
                                          "continue_url": "https://auth.openai.com/mfa-challenge/FID-1"},
                          after_mfa=mfa)
        exchanged = {}

        def _exchange(code, verifier, redirect_uri, client_id):
            exchanged.update(code=code, redirect_uri=redirect_uri, client_id=client_id)
            flow.result.refresh_token = "rt-final"
            return True

        flow._exchange_platform_code = _exchange

        ok = flow.acquire_rt_via_platform_login(
            email="user@example.com", password="pw-1", totp_secret="JBSWY3DPEHPK3PXP",
        )

        self.assertTrue(ok)
        self.assertEqual(exchanged["code"], "CODE-1",
                         "必须从响应里直接读 code")
        self.assertEqual(exchanged["client_id"], PLATFORM_CLIENT_ID)
        self.assertEqual(exchanged["redirect_uri"], PLATFORM_REDIRECT_URI)
        self.assertEqual([c[0] for c in flow._calls],
                         ["authorize_continue", "password_verify", "mfa_verify"])

    def test_no_redirect_following_so_code_is_not_burned(self):
        """绝不调用 follow_redirect_chain —— 那会把 platform 回调的 code 用掉。"""
        mfa = {"continue_url": "https://platform.openai.com/auth/callback?code=CODE-2"}
        flow = self._flow(after_password={"continue_url": "https://auth.openai.com/mfa-challenge/FID-1",
                                          "page": {"type": "mfa_challenge"}},
                          after_mfa=mfa)
        followed = []
        flow.follow_redirect_chain = lambda url, *a, **k: followed.append(url) or (None, None)
        flow._follow_authorize_for_callback = lambda *a, **k: followed.append("authorize_cb") or ("", "")
        flow._exchange_platform_code = lambda **k: True
        flow.result.refresh_token = ""
        flow._exchange_platform_code = lambda *a, **k: (setattr(flow.result, "refresh_token", "rt") or True)

        flow.acquire_rt_via_platform_login(
            email="user@example.com", password="pw", totp_secret="JBSWY3DPEHPK3PXP")

        self.assertEqual(followed, [],
                         f"跟随了重定向，code 会被消费掉: {followed}")

    def test_missing_totp_when_server_demands_mfa_fails_loudly(self):
        """服务端要 mfa 但本地没有 TOTP 密钥 → 明确失败，且不去猜密码。"""
        flow = self._flow(after_password={"page": {"type": "mfa_challenge"},
                                          "continue_url": "https://auth.openai.com/mfa-challenge/FID-1"})
        ok = flow.acquire_rt_via_platform_login(
            email="user@example.com", password="pw", totp_secret="",
        )
        self.assertFalse(ok)
        self.assertNotIn(("mfa_verify", "FID-1"), flow._calls,
                         "没有密钥时不该去提交 mfa")
        self.assertIn("totp", (flow.last_platform_route_error or "").lower(),
                      f"要给出可辨认的原因，实际: {flow.last_platform_route_error!r}")

    def test_missing_password_fails_before_any_request(self):
        flow = self._flow()
        ok = flow.acquire_rt_via_platform_login(
            email="user@example.com", password="", totp_secret="JBSWY3DPEHPK3PXP")
        self.assertFalse(ok)
        self.assertEqual(flow._calls, [], "没有密码不该发任何请求")
        self.assertIn("密码", flow.last_platform_route_error or "")


class RtRouteSelectionTests(unittest.TestCase):
    """`chatgpt_rt_route` 决定换来 RT 走哪条路：codex（可能要接码）/ login（不要接码）。"""

    def test_defaults_to_login(self):
        """默认是**密码登录**。

        2026-10-27 改的：实测 Codex 路线在新号上卡 choose-an-account
        （拿不到 RT），密码登录路线新号老号都能拿到。默认值必须与「Token 方案」
        的声明默认（`DEFAULT_CHATGPT_TOKEN_PLAN`）一致，否则设置页显示的和
        实际跑的不是一回事。
        """
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            RT_ROUTE_LOGIN, resolve_chatgpt_rt_route,
        )
        self.assertEqual(resolve_chatgpt_rt_route({}), RT_ROUTE_LOGIN)

    def test_accepts_login_and_aliases(self):
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            RT_ROUTE_LOGIN, resolve_chatgpt_rt_route,
        )
        for value in ("login", "LOGIN", "password", "password_login", " login ", 1):
            self.assertEqual(resolve_chatgpt_rt_route({"chatgpt_rt_route": value}),
                             RT_ROUTE_LOGIN, f"{value!r} 应识别为登录路线")

    def test_unknown_value_falls_back_to_codex(self):
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            RT_ROUTE_CODEX, resolve_chatgpt_rt_route,
        )
        self.assertEqual(resolve_chatgpt_rt_route({"chatgpt_rt_route": "banana"}),
                         RT_ROUTE_CODEX)

    def test_rt_route_is_reachable_from_the_settings_ui(self):
        """路线必须能从设置界面选到 —— 但**不再**是独立下拉。

        2026-10-26 起「RT 获取方式」合并进「Token 方案」三选一（用户要求）：
        设置页只渲染一个 `chatgpt_registration_mode`，两个路线取值是它的选项。
        所以这里断言的是「路线能从声明里选到」，而不是「有一个独立键」。

        独立键的断言搬到了 `tests/test_chatgpt_token_plan_settings.py`
        （`test_rt_route_is_no_longer_a_separate_field`）。
        """
        from platforms.chatgpt.plugin import ChatGPTPlatform

        keys = {m["key"]: m for m in ChatGPTPlatform.registration_modes}
        self.assertNotIn("chatgpt_rt_route", keys,
                         "「RT 获取方式」仍单独声明 —— 应已合并进 Token 方案")
        values = [o["value"] for o in keys["chatgpt_registration_mode"]["options"]]
        self.assertIn("refresh_token_codex", values, "Codex 路线在声明里选不到")
        self.assertIn("refresh_token_login", values, "密码登录路线在声明里选不到")
        joined = keys["chatgpt_registration_mode"].get("desc", "")
        self.assertTrue(any(w in joined for w in ("接码", "手机")),
                        f"文案要说明与接码的关系，实际: {joined!r}")

    def test_saved_in_config_whitelist(self):
        """不在 CONFIG_KEYS 里的话 PUT /api/config 会静默丢掉它。"""
        from api.config import CONFIG_KEYS
        self.assertIn("chatgpt_rt_route", CONFIG_KEYS,
                      "没进白名单，面板保存会被静默丢弃")


class RegistrationRouteWiringTests(unittest.TestCase):
    """注册链按 `chatgpt_rt_route` 选路，且 Codex 失败要回退到登录链。"""

    def _engine(self, extra, *, codex_ok=False, login_ok=True):
        from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine
        eng = ChatGPTRegistrationEngine(
            mailbox=object(), mode="refresh_token",
            extra_config=dict(extra),
        )
        calls = {"codex": 0, "login": 0}
        eng._run_codex_rt = lambda flow, **kw: (calls.__setitem__("codex", calls["codex"] + 1) or codex_ok)
        eng._run_login_rt = lambda flow, **kw: (calls.__setitem__("login", calls["login"] + 1) or login_ok)
        eng._calls = calls
        return eng

    def test_login_route_skips_codex_entirely(self):
        """选了登录路线就不该再跑 Codex —— 那条路在这个场景是白花钱的。"""
        eng = self._engine({"chatgpt_rt_route": "login"})
        eng._acquire_refresh_token(flow=object(), email="a@b.c", password="pw", totp_secret="S")
        self.assertEqual(eng._calls, {"codex": 0, "login": 1})

    def test_codex_route_falls_back_to_login_when_codex_fails(self):
        """用户要求：Codex 失败 → 回退到登录链。"""
        eng = self._engine({"chatgpt_rt_route": "codex"}, codex_ok=False, login_ok=True)
        ok = eng._acquire_refresh_token(flow=object(), email="a@b.c", password="pw", totp_secret="S")
        self.assertEqual(eng._calls, {"codex": 1, "login": 1})
        self.assertTrue(ok, "回退成功后应报成功")

    def test_codex_success_does_not_run_login(self):
        sup = self._engine({"chatgpt_rt_route": "codex"}, codex_ok=True)
        sup._acquire_refresh_token(flow=object(), email="a@b.c", password="pw", totp_secret="S")
        self.assertEqual(sup._calls, {"codex": 1, "login": 0},
                         "Codex 成功了不该再多打一次登录链")

    def test_access_token_only_mode_skips_both(self):
        """无 RT 模式两条路都不该跑。"""
        eng = self._engine({"chatgpt_rt_route": "login"})
        eng.mode = "access_token_only"
        eng._acquire_refresh_token(flow=object(), email="a@b.c", password="pw", totp_secret="S")
        self.assertEqual(eng._calls, {"codex": 0, "login": 0})


class BackfillLoginRouteTests(unittest.TestCase):
    """补 RT 也要能走登录路线（现在那条「协议重登」的终点其实还是 Codex 交换）。"""

    def _backfill(self, *, totp="SECRET", password="pw", login_ok=True):
        from platforms.chatgpt.rt_backfill import STRATEGY_PLATFORM_LOGIN, RefreshTokenBackfiller
        bf = RefreshTokenBackfiller(
            email="a@b.c", password=password, totp_secret=totp,
            log_fn=lambda _m: None,
        )
        calls = {"platform": 0}
        def _fake(flow_self):
            calls["platform"] += 1
            if login_ok:
                flow_self.result.refresh_token = "rt-from-login"
            return login_ok
        bf._run_platform_login = _fake
        bf._calls = calls
        return bf, STRATEGY_PLATFORM_LOGIN

    def test_login_strategy_uses_password_route(self):
        bf, strategy = self._backfill()
        bf._try_platform_login()
        self.assertEqual(bf._calls["platform"], 1,
                         "登录策略必须真的调用密码登录路线，而不是又去跑 Codex")

    def test_missing_totp_does_not_skip(self):
        """缺 `totp_secret` 不该整条跳过密码登录路线。

        2FA 只在服务端真要时才需要，而「有没有绑 2FA」是账号属性 —— 本地没存
        密钥推不出来。原来的写法等于：配置选了「密码登录」，库里没 totp 就把
        整条路线跳过，退回 session + codex 白烧两轮 OTP（实测 2026-10-30
        skyward-31.drums：那个号压根不需要 2FA，OTP 验完就过了）。
        """
        bf, strategy = self._backfill(totp="")
        self.assertEqual(bf._skip_reason(strategy), "")

    def test_missing_password_skips_with_reason(self):
        bf, strategy = self._backfill(password="")
        reason = bf._skip_reason(strategy)
        self.assertIn("密码", reason)

    def test_has_credentials_runs(self):
        bf, strategy = self._backfill()
        self.assertEqual(bf._skip_reason(strategy), "")

    def test_strategy_is_registered_in_run_order(self):
        """策略要在 run() 的尝试顺序里，否则永远不会被执行。

        顺序**跟着路线设置走**（2026-10-27）：默认「密码登录」把它排在最前，
        后面才是 Codex 链路（复用会话 → 协议重登）作为回退。
        """
        from platforms.chatgpt import rt_backfill as mod
        bf, _ = self._backfill()
        order = bf._strategy_order()
        self.assertIn(mod.STRATEGY_PLATFORM_LOGIN, order)
        self.assertEqual(order[0], mod.STRATEGY_PLATFORM_LOGIN,
                         f"默认路线是密码登录，它该排第一，实际 {order}")
        self.assertEqual(order[1:], [mod.STRATEGY_SESSION, mod.STRATEGY_LOGIN],
                         f"回退链路应保持 复用会话 → 协议重登，实际 {order}")


class StrategyBindingTests(unittest.TestCase):
    """策略必须是**绑定**方法 —— 裸函数引用会丢 self，调用即 TypeError。

    这个 bug 实际发生过：`(STRATEGY_SESSION, RefreshTokenBackfiller._try_session)`
    让 run() 里每条策略都 TypeError，17 条既有测试一起红。
    """

    def test_all_strategies_are_bound(self):
        from platforms.chatgpt.rt_backfill import RefreshTokenBackfiller
        bf = RefreshTokenBackfiller(email="a@b.c")
        for strategy, runner in bf._strategy_runners():
            self.assertTrue(callable(runner), f"{strategy} 不是可调用对象")
            # 绑定方法的 __self__ 指向实例；裸函数的 __self__ 不存在
            self.assertIs(getattr(runner, "__self__", None), bf,
                          f"{strategy} 的策略不是绑定方法（调用会丢 self）")

    def test_run_reaches_every_strategy_without_type_error(self):
        """run() 里任何策略抛 TypeError 都说明绑定又坏了。"""
        from platforms.chatgpt.rt_backfill import RefreshTokenBackfiller
        logs = []
        bf = RefreshTokenBackfiller(
            email="a@b.c", password="pw", totp_secret="S",
            session_token="st", access_token="at",
            log_fn=logs.append,
        )
        bf._try_session = lambda: None
        bf._try_login = lambda: None
        bf._try_platform_login = lambda: None
        bf.run()
        joined = "\n".join(logs)
        self.assertNotIn("missing 1 required positional argument", joined,
                         f"策略绑定又坏了: {joined[:200]}")


class BackfillClearsPartialFlagTests(unittest.TestCase):
    """补到 RT 之后要把 `partial` / `missing` 清掉。

    这两个键是注册时「RT 没拿到」打的标记。补 RT 成功后不清，账号会一直
    显示成「凭证不全」，而实际它已经有 RT 了 —— 界面与事实相反。
    """

    def _patch_for(self, *, rt="rt-ok"):
        from platforms.chatgpt.rt_backfill import BackfillResult
        from services.chatgpt_rt_backfill import build_extra_patch
        result = BackfillResult(success=True, email="a@b.c")
        result.refresh_token = rt
        return build_extra_patch(result)

    def test_successful_backfill_marks_partial_resolved(self):
        patch = self._patch_for()
        self.assertIs(patch.get("partial"), False,
                      "补到 RT 后 partial 必须变 False，否则账号一直被当成凭证不全")
        self.assertEqual(patch.get("missing"), "",
                         "missing 也要清掉，否则界面上仍显示缺 refresh_token")

    def test_failed_backfill_does_not_claim_resolution(self):
        from platforms.chatgpt.rt_backfill import BackfillResult
        from services.chatgpt_rt_backfill import build_extra_patch
        result = BackfillResult(success=False, email="a@b.c")
        patch = build_extra_patch(result)
        self.assertNotIn("partial", patch,
                         "没拿到 RT 就不能声称问题已解决")


if __name__ == "__main__":
    unittest.main()


class BackfillHonoursRtRouteSettingTests(unittest.TestCase):
    """补 RT 的策略**顺序**必须跟着 `chatgpt_rt_route` 设置走。

    实测（用户 2026-10-27 报）：设置里选「密码登录」，补 RT 却仍先跑 Codex
    授权（撞 add-phone、白等 30 秒），最后才走密码登录。

    这里直接测 `_strategy_runners()` 的顺序 —— 那是顺序的唯一定义处。
    不模拟 `run()` 的完整循环：那需要伪造 `_absorb`/`_active_flow` 才能让
    循环跳出，桩一多就测不到顺序本身了（第一版就栽在这）。
    """

    def _order(self, config: dict) -> list[str]:
        from platforms.chatgpt.rt_backfill import RefreshTokenBackfiller

        bf = RefreshTokenBackfiller(
            email="a@b.c", password="pw", totp_secret="SECRET",
            session_token="st", access_token="at",
            extra_config=dict(config), log_fn=lambda _m: None,
        )
        return [strategy for strategy, _runner in bf._strategy_runners()]

    def test_login_route_puts_password_route_first(self):
        from platforms.chatgpt.rt_backfill import (
            STRATEGY_LOGIN, STRATEGY_PLATFORM_LOGIN, STRATEGY_SESSION,
        )

        order = self._order({"chatgpt_rt_route": "login"})
        self.assertEqual(order[0], STRATEGY_PLATFORM_LOGIN,
                         f"设置选了密码登录，第一条却是 {order[0]}")
        # 后面回退 Codex 链路（复用会话 → 协议重登）
        self.assertEqual(order[1:], [STRATEGY_SESSION, STRATEGY_LOGIN])

    def test_codex_route_keeps_the_legacy_order(self):
        from platforms.chatgpt.rt_backfill import (
            STRATEGY_LOGIN, STRATEGY_PLATFORM_LOGIN, STRATEGY_SESSION,
        )

        self.assertEqual(
            self._order({"chatgpt_rt_route": "codex"}),
            [STRATEGY_SESSION, STRATEGY_LOGIN, STRATEGY_PLATFORM_LOGIN],
        )

    def test_default_route_is_login(self):
        """没有设置时跟随合并后的默认（密码登录）。"""
        from platforms.chatgpt.rt_backfill import STRATEGY_PLATFORM_LOGIN

        self.assertEqual(self._order({})[0], STRATEGY_PLATFORM_LOGIN)

    def test_merged_mode_value_also_works(self):
        """设置页现在写的是合并值 `refresh_token_login`，也要认。"""
        from platforms.chatgpt.rt_backfill import STRATEGY_PLATFORM_LOGIN

        self.assertEqual(
            self._order({"chatgpt_registration_mode": "refresh_token_login"})[0],
            STRATEGY_PLATFORM_LOGIN,
        )
        from platforms.chatgpt.rt_backfill import STRATEGY_SESSION
        self.assertEqual(
            self._order({"chatgpt_registration_mode": "refresh_token_codex"})[0],
            STRATEGY_SESSION,
        )


class SuccessMessageNamesTheActualStrategyTests(unittest.TestCase):
    """成功消息要说**真正成功的那条**策略。

    实测（用户 2026-10-27 报）：密码登录成功，日志却写「补 RT 成功（协议重登）」
    —— `summary()` 的三元表达式只区分「复用会话」与「其余」，把
    `platform_login` 归成了协议重登。
    """

    def test_platform_login_is_named_correctly(self):
        from platforms.chatgpt.rt_backfill import (
            STRATEGY_PLATFORM_LOGIN, BackfillResult,
        )

        result = BackfillResult(success=True, email="a@b.c")
        result.strategy = STRATEGY_PLATFORM_LOGIN
        self.assertIn("密码登录", result.summary(),
                      f"消息没说对策略: {result.summary()!r}")

    def test_other_strategies_unchanged(self):
        from platforms.chatgpt.rt_backfill import (
            STRATEGY_LOGIN, STRATEGY_SESSION, BackfillResult,
        )

        for strategy, expected in ((STRATEGY_SESSION, "复用会话"), (STRATEGY_LOGIN, "协议重登")):
            r = BackfillResult(success=True, email="a@b.c")
            r.strategy = strategy
            self.assertIn(expected, r.summary())
