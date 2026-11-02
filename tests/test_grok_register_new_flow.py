"""新版 Grok 注册流程（2026-11 accounts.x.ai Next.js 前端）的回归测试（不联网）。

线上实测（2026-11-01，camoufox + 住宅代理）钉死的事实：

  1. 发码改成了 `POST /api/auth/send-verification-code`，body 带 Castle 令牌。
     三种结局：
       200 {"ok":true}                                          → 正常
       400 code=account:email-signup-unavailable                → 出口 IP 被判定不可注册
       400 code=email:send-validation-code-rate-limited         → 按邮箱限流
     **两种失败都没有把地址用掉**（换 IP 后同一地址还能成功），所以都不能
     `consumes_address=True`（审计 R-22：旧实现的兜底正好相反）。
  2. 验证码页：`input[name=code]`、maxlength=6、不带连字符、满 6 位自动提交。
  3. 资料页：givenName/familyName/password + **Cloudflare Turnstile widget**
     （sitekey 0x4AAAAAAAhr9JGVDZbrZOo0，widget id 形如 cf-chl-widget-xxxx）。
     令牌没拿到就提交，页面报 "Complete the verification challenge, then try again."
     （前端 `error_type: "turnstile_failed", step: "credentials"`）。
  4. 页面级第三态："Something went wrong. Refresh the page and try again."
     —— 连发码请求都没发出去，同样可重试、不烧地址。
"""

import time
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from platforms.grok import signup_pages as sp


class SelectorPinningTests(unittest.TestCase):
    """新版页面的选择器必须钉死 —— 旧实现用的是一批过时的 role/text 定位。"""

    def test_code_input_selector(self):
        self.assertEqual(sp.CODE_INPUT, "input[name=code]")

    def test_email_input_selector(self):
        self.assertEqual(sp.EMAIL_INPUT, "input#email")

    def test_profile_inputs(self):
        self.assertEqual(sp.GIVEN_NAME_INPUT, "input#givenName")
        self.assertEqual(sp.FAMILY_NAME_INPUT, "input#familyName")
        self.assertEqual(sp.PASSWORD_INPUT, "input#password")

    def test_turnstile_sitekey_is_the_observed_one(self):
        """资料页 Turnstile 的 sitekey（从 challenge-platform 请求 URL 里读出来的）。"""
        self.assertEqual(sp.TURNSTILE_SITEKEY, "0x4AAAAAAAhr9JGVDZbrZOo0")


class SendCodeResultTests(unittest.TestCase):
    """API 响应的判定 —— 决定地址要不要标成 used。"""

    def test_ok_200_is_sent(self):
        v = sp.classify_send_code_result(200, '{"ok":true}')
        self.assertEqual(v["kind"], sp.SEND_RESULT_SENT)
        self.assertFalse(v["retryable"])
        self.assertFalse(v["consumes_address"])

    def test_email_signup_unavailable_is_environment_side(self):
        """实测：同一个地址换住宅代理就能发码 —— 这不是地址的错。"""
        v = sp.classify_send_code_result(
            400,
            '{"error":"Failed to send verification code",'
            '"code":"account:email-signup-unavailable"}',
        )
        self.assertEqual(v["kind"], sp.SEND_RESULT_EMAIL_UNAVAILABLE)
        self.assertTrue(v["retryable"], "换出口 IP 还能用，必须可重试")
        self.assertFalse(v["consumes_address"], "地址没被用掉，不能烧掉")

    def test_rate_limited_code_is_retryable_without_burning_address(self):
        v = sp.classify_send_code_result(
            400,
            '{"error":"Failed to send verification code",'
            '"code":"email:send-validation-code-rate-limited"}',
        )
        self.assertEqual(v["kind"], sp.SEND_RESULT_RATE_LIMITED)
        self.assertTrue(v["retryable"])
        self.assertFalse(v["consumes_address"])
        self.assertTrue(v["retry_after_hint"], "限流要给出等待提示")

    def test_existing_email_consumes_the_address(self):
        """只有「这个邮箱已经有账号」才算地址被用掉。"""
        v = sp.classify_send_code_result(
            400, '{"error":"An account with this email already exists.",'
                 '"code":"account:existing-email"}'
        )
        self.assertEqual(v["kind"], sp.SEND_RESULT_ALREADY_REGISTERED)
        self.assertTrue(v["consumes_address"])
        self.assertFalse(v["retryable"])

    def test_bodyless_failure_falls_back_to_page_text(self):
        v = sp.classify_send_code_result(
            500, "", "Too many code requests. Please wait a few minutes before requesting another code."
        )
        self.assertEqual(v["kind"], sp.SEND_RESULT_RATE_LIMITED)
        self.assertFalse(v["consumes_address"])

    def test_unknown_failure_is_retryable_and_safe(self):
        v = sp.classify_send_code_result(503, "", "")
        self.assertTrue(v["retryable"])
        self.assertFalse(v["consumes_address"])


class SendCodePageTests(unittest.TestCase):
    """页面文案判定（API 响应拿不到时的兜底）。"""

    def test_typographic_apostrophe_wording(self):
        """实测页面用的是排版撇号 ’，参考实现里写的 ASCII ' 会漏。"""
        v = sp.classify_send_code_page(
            "Email sign-up isn\u2019t available right now. Sign up another way."
        )
        self.assertEqual(v["kind"], sp.SEND_RESULT_EMAIL_UNAVAILABLE)

    def test_ascii_apostrophe_wording_too(self):
        v = sp.classify_send_code_page(
            "Email sign-up isn't available right now. Sign up another way."
        )
        self.assertEqual(v["kind"], sp.SEND_RESULT_EMAIL_UNAVAILABLE)

    def test_rate_limit_wording(self):
        v = sp.classify_send_code_page(
            "Too many code requests. Please wait a few minutes before requesting another code."
        )
        self.assertEqual(v["kind"], sp.SEND_RESULT_RATE_LIMITED)
        self.assertFalse(v["consumes_address"])

    def test_something_went_wrong_is_transient(self):
        v = sp.classify_send_code_page(
            "Something went wrong. Refresh the page and try again. "
            "Still stuck? Contact support@x.ai."
        )
        self.assertEqual(v["kind"], sp.SEND_RESULT_TRANSIENT)
        self.assertTrue(v["retryable"])
        self.assertFalse(v["consumes_address"])

    def test_cloudflare_challenge_is_environment_problem(self):
        for text in (
            "Just a moment...",
            "Verifying you are human. This may take a few seconds.",
            "Enable JavaScript and cookies to continue",
            "cf-chl-widget-abc123_response",
        ):
            with self.subTest(text=text):
                v = sp.classify_send_code_page(text)
                self.assertEqual(v["kind"], sp.SEND_RESULT_CF_CHALLENGE, text)
                self.assertFalse(v["consumes_address"])

    def test_unknown_text_does_not_consume_the_address(self):
        """审计 R-22：兜底把「任何没认出来的文本」当地址已消耗，会白烧号。"""
        v = sp.classify_send_code_page("Something completely different")
        self.assertEqual(v["kind"], sp.SEND_RESULT_NO_CONFIRMATION)
        self.assertTrue(v["retryable"], "没确认不等于不可重试")
        self.assertFalse(v["consumes_address"])

    def test_verify_page_text_is_no_confirmation(self):
        v = sp.classify_send_code_page(
            "Verify your email We've emailed a one time security code to a@b.com."
        )
        self.assertEqual(v["kind"], sp.SEND_RESULT_NO_CONFIRMATION)


class BlockingStateTests(unittest.TestCase):
    """只有「认出来是失败态」才在加载后立刻放弃；良性页面不能误伤。"""

    def test_normal_entry_page_is_not_a_blocking_state(self):
        for text in (
            "SpaceXSI You are signing into Grok Create your account "
            "Sign up with X Sign up with email Sign up with Apple",
            "Sign up with your email Email Sign up Go back",
            "Complete your sign up First name Last name Password Complete sign up",
        ):
            with self.subTest(text=text[:40]):
                self.assertIsNone(sp.detect_blocking_state(text))

    def test_unknown_text_is_not_a_blocking_state(self):
        self.assertIsNone(sp.detect_blocking_state("Something completely different"))

    def test_error_states_are_detected(self):
        cases = {
            "Email sign-up isn\u2019t available right now.": sp.SEND_RESULT_EMAIL_UNAVAILABLE,
            "Too many code requests. Please wait a few minutes.": sp.SEND_RESULT_RATE_LIMITED,
            "Something went wrong. Refresh the page and try again.": sp.SEND_RESULT_TRANSIENT,
            "Just a moment...": sp.SEND_RESULT_CF_CHALLENGE,
        }
        for text, kind in cases.items():
            with self.subTest(kind=kind):
                v = sp.detect_blocking_state(text)
                self.assertIsNotNone(v, text)
                self.assertEqual(v["kind"], kind)


class SendCodeErrorMessageTests(unittest.TestCase):
    """错误文案要能让人一眼看出「该换 IP」还是「该等一会」还是「这个号废了」。"""

    def test_unavailable_points_at_the_exit_ip(self):
        msg = sp.send_code_error_text(sp.classify_send_code_result(
            400, '{"code":"account:email-signup-unavailable"}'), "a@b.com")
        self.assertIn("a@b.com", msg)
        self.assertIn("出口", msg)

    def test_rate_limited_says_wait(self):
        msg = sp.send_code_error_text(
            sp.classify_send_code_result(
                400, '{"code":"email:send-validation-code-rate-limited"}'), "a@b.com")
        self.assertIn("限流", msg)

    def test_unknown_says_unconfirmed(self):
        msg = sp.send_code_error_text(
            sp.classify_send_code_page("Something completely different"), "a@b.com")
        self.assertIn("未确认发信", msg)


class TurnstileGateTests(unittest.TestCase):
    """资料页提交被 Turnstile 挡住时必须能认出来。"""

    def test_observed_wording(self):
        self.assertTrue(sp.looks_turnstile_gated(
            "Complete the verification challenge, then try again."
        ))

    def test_normal_profile_page_is_not_gated(self):
        self.assertFalse(sp.looks_turnstile_gated(
            "Complete your sign up a@b.com First name Last name Password Complete sign up"
        ))


class TurnstileTokenTests(unittest.TestCase):
    """资料页 Turnstile 令牌：先等 widget 自己过，过不了再用外挂铸币注入。"""

    def test_read_token_reads_the_widget_input(self):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: "tok-abc"
        self.assertEqual(sp.read_turnstile_token(page), "tok-abc")

    def test_read_token_pins_the_response_selector(self):
        seen = {}
        page = MagicMock()

        def _ev(js, *a):
            seen["js"] = str(js)
            return ""

        page.evaluate.side_effect = _ev
        self.assertEqual(sp.read_turnstile_token(page), "")
        self.assertIn("cf-turnstile-response", seen["js"])

    def test_read_token_blank_is_empty(self):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: "   "
        self.assertEqual(sp.read_turnstile_token(page), "")

    def test_hook_wraps_render_to_capture_the_callback(self):
        seen = {}
        page = MagicMock()

        def _ev(js, *a):
            seen["js"] = str(js)
            return True

        page.evaluate.side_effect = _ev
        self.assertTrue(sp.install_turnstile_hook(page))
        self.assertIn("turnstile", seen["js"])
        self.assertIn("render", seen["js"])

    def test_inject_calls_the_captured_callback_with_the_token(self):
        seen = {}
        page = MagicMock()

        def _ev(js, *a):
            seen["js"] = str(js)
            return True

        page.evaluate.side_effect = _ev
        self.assertTrue(sp.inject_turnstile_token(page, "MINTED-TOKEN"))
        self.assertIn("MINTED-TOKEN", seen["js"])

    def test_ensure_returns_widget_token_when_it_solves_by_itself(self):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: "WIDGET-TOKEN"
        minted = []
        token = sp.ensure_turnstile_token(
            page, mint=lambda **kw: minted.append(kw) or "SHOULD-NOT-BE-USED",
            timeout=1.0, poll_seconds=0.01, sleep=lambda s: None,
        )
        self.assertEqual(token, "WIDGET-TOKEN")
        self.assertEqual(minted, [], "widget 自己能过就不该去铸币")

    def test_ensure_mints_and_injects_when_widget_stays_empty(self):
        calls = []

        def _ev(js, *a):
            calls.append(str(js))
            if "cf-turnstile-response" in str(js):
                return ""          # widget 永远不过
            return True

        page = MagicMock()
        page.evaluate.side_effect = _ev
        seen_kwargs = {}

        def _mint(**kw):
            seen_kwargs.update(kw)
            return "MINTED-TOKEN"

        token = sp.ensure_turnstile_token(
            page, mint=_mint, timeout=0.05, poll_seconds=0.01,
            sleep=lambda s: None, page_url=sp.PROFILE_PAGE_URL,
        )
        self.assertEqual(token, "MINTED-TOKEN")
        self.assertEqual(seen_kwargs.get("site_key") or seen_kwargs.get("sitekey"),
                         sp.TURNSTILE_SITEKEY)
        self.assertTrue(any("MINTED-TOKEN" in js for js in calls), "铸出来的令牌要注入回调")

    def test_ensure_returns_empty_when_minting_fails(self):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: "" if "cf-turnstile-response" in str(js) else True
        token = sp.ensure_turnstile_token(
            page, mint=lambda **kw: "", timeout=0.05, poll_seconds=0.01, sleep=lambda s: None,
        )
        self.assertEqual(token, "")

    def test_ensure_respects_the_timeout(self):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: ""
        started = time.monotonic()
        sp.ensure_turnstile_token(
            page, mint=lambda **kw: "", timeout=0.05, poll_seconds=0.01, sleep=lambda s: None,
        )
        self.assertLess(time.monotonic() - started, 2.0, "别把调用方挂死")

    def test_mint_exception_is_not_fatal(self):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: ""

        def _boom(**kw):
            raise RuntimeError("所有 Turnstile 方案均失败")

        token = sp.ensure_turnstile_token(
            page, mint=_boom, timeout=0.05, poll_seconds=0.01, sleep=lambda s: None,
        )
        self.assertEqual(token, "", "铸币失败要降级成空令牌，不能炸掉整条链路")


class CodeEntryTests(unittest.TestCase):
    """验证码输入：新版是 input[name=code] + maxlength=6，满位自动提交。"""

    def _page(self, after_text="Complete your sign up"):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: after_text
        page.locator.return_value.first = MagicMock()
        return page

    def test_strips_the_dash(self):
        page = self._page()
        sp.enter_code(page, "764-275", sleep=lambda s: None)
        types = [c.args[0] for c in page.keyboard.type.call_args_list]
        self.assertEqual(types, ["764275"])

    def test_types_character_by_character(self):
        """页面会自动格式化，必须逐字打（旧实现踩过）。"""
        page = self._page()
        sp.enter_code(page, "764-275", sleep=lambda s: None)
        kwargs = page.keyboard.type.call_args_list[0].kwargs
        self.assertGreaterEqual(kwargs.get("delay", 0), 50)

    def test_uses_the_new_code_input(self):
        page = self._page()
        sp.enter_code(page, "764-275", sleep=lambda s: None)
        sels = [c.args[0] for c in page.locator.call_args_list]
        self.assertIn("input[name=code]", sels)

    def test_reports_success_when_the_page_advanced(self):
        page = self._page(after_text="Complete your sign up a@b.com First name Last name")
        self.assertTrue(sp.enter_code(page, "764-275", sleep=lambda s: None))
        page.keyboard.press.assert_not_called()

    def test_presses_enter_when_it_did_not_advance(self):
        page = self._page(after_text="Verify your email enter it below Confirm")
        sp.enter_code(page, "764-275", sleep=lambda s: None)
        pressed = [c.args[0] for c in page.keyboard.press.call_args_list]
        self.assertIn("Enter", pressed)

    def test_missing_input_is_reported(self):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: "Verify your email"
        page.locator.return_value.first = None
        self.assertFalse(sp.enter_code(page, "764-275", sleep=lambda s: None))


class ProfileFillTests(unittest.TestCase):
    def test_fills_the_three_fields(self):
        seen = []
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: seen.append(str(js)) or True
        sp.fill_profile(page, given_name="Neo", family_name="Lin", password="Pw1!aaaa")
        joined = "\n".join(seen)
        self.assertIn("givenName", joined)
        self.assertIn("familyName", joined)
        self.assertIn("password", joined)
        self.assertIn("Neo", joined)


# ── 整条链路的控制流（桩浏览器，不联网） ──────────────────────────────


class _FakeCamoufox:
    """最小假 camoufox：所有 innerText 返回同一段文本。

    默认在最前面垫上入口页那排按钮文案 —— 真页面永远先过入口页，
    否则会命中「入口页没渲染 → 判定出口被半拦」的快速失败路径（2026-11-01 起）。
    """

    _ENTRY_BANNER = ("SpaceXSI You are signing into Grok Sign up with your email "
                     "Email Sign up Go back ")

    def __init__(self, text="", inputs=None, cookies=None, with_entry=True):
        if with_entry:
            text = self._ENTRY_BANNER + text
        self.page = MagicMock()
        self.page.evaluate.side_effect = lambda js, *a: (
            text if "innerText" in str(js)
            else (inputs if inputs is not None else [])
        )
        self.page.locator.return_value.first.count.return_value = 1
        self.page.locator.return_value.first.is_visible.return_value = True
        self.page.locator.return_value.count.return_value = 1
        ctx = MagicMock()
        ctx.cookies.return_value = cookies or []
        self.page.context = ctx
        browser = MagicMock()
        browser.new_page.return_value = self.page
        self.cm = MagicMock()
        self.cm.__enter__.return_value = browser
        self.cm.__exit__.return_value = None

    def __call__(self, **kwargs):
        return self.cm


class AddressRecyclingPolicyTests(unittest.TestCase):
    """这些用例的页面文案里没有入口按钮，真等入口页要 40 秒（20s × 刷新一次），
    把等待设为 0，只验证后面的发码判定。"""

    def setUp(self):
        patcher = patch("platforms.grok.register_browser.ENTRY_WAIT_TIMEOUT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    """地址回收策略：只有「已注册 / 域名被拒」才允许烧地址。"""

    def test_email_signup_unavailable_keeps_the_address(self):
        page = _FakeCamoufox("Email sign-up isn\u2019t available right now. Sign up another way.")
        with patch("camoufox.sync_api.Camoufox", side_effect=page):
            from platforms.grok.register_browser import register_grok_via_browser

            res = register_grok_via_browser("a@icloud.com")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("email_unavailable"), "要让上层知道是出口 IP 的问题")
        self.assertFalse(res.get("consumes_address"), "地址没被用掉，不能标 used")
        self.assertTrue(res.get("retryable"), "换 IP 后还能重试")
        self.assertIn("出口", res["error"])

    def test_unknown_page_does_not_burn_the_address(self):
        """审计 R-22：兜底把未识别文本当「地址已消耗」会白烧号。"""
        page = _FakeCamoufox("Something completely different")
        with patch("camoufox.sync_api.Camoufox", side_effect=page), \
             patch("platforms.grok.register_browser.SEND_CODE_SETTLE_TIMEOUT", 0.05):
            from platforms.grok.register_browser import register_grok_via_browser

            res = register_grok_via_browser("a@icloud.com")
        self.assertFalse(res["ok"])
        self.assertIn("未确认发信", res["error"])
        self.assertFalse(res.get("consumes_address"))
        self.assertTrue(res.get("retryable"))

    def test_existing_account_still_consumes_the_address(self):
        page = _FakeCamoufox(
            "Existing account found An account with this email already exists"
        )
        with patch("camoufox.sync_api.Camoufox", side_effect=page):
            from platforms.grok.register_browser import register_grok_via_browser

            res = register_grok_via_browser("a@icloud.com")
        self.assertFalse(res["ok"])
        self.assertTrue(res.get("already_registered"))
        self.assertTrue(res.get("consumes_address"))

    def test_domain_rejection_reports_the_domain(self):
        page = _FakeCamoufox(
            "Your email address is invalid. Please use a different email address."
        )
        with patch("camoufox.sync_api.Camoufox", side_effect=page):
            from platforms.grok.register_browser import register_grok_via_browser

            res = register_grok_via_browser("x@tiny.example")
        self.assertFalse(res["ok"])
        self.assertIn("拒收", res["error"])
        self.assertIn("tiny.example", res["error"])


class TaskInterruptionPassthroughTests(unittest.TestCase):
    """「停止」不能被最外层 catch-all 吞成普通失败（审计 R-07）。"""

    def test_stop_requested_propagates_out_of_register(self):
        from core.task_runtime import StopTaskRequested
        from platforms.grok import signup_pages

        page = _FakeCamoufox("Create your account Sign up with your email")
        with patch("camoufox.sync_api.Camoufox", side_effect=page), \
             patch.object(signup_pages, "detect_blocking_state",
                          side_effect=StopTaskRequested()):
            from platforms.grok.register_browser import register_grok_via_browser

            with self.assertRaises(StopTaskRequested):
                register_grok_via_browser("a@icloud.com")


class ProfileStageTests(unittest.TestCase):
    """资料页：填三件套 → 拿 Turnstile 令牌 → 提交。"""

    def _page(self, texts):
        seq = list(texts)
        page = MagicMock()

        def _ev(js, *a):
            js = str(js)
            # 只有**真正读页面文案**的那次才从队列里取。
            #
            # 踩过：这段原来是「JS 里含 innerText 就取一次」，而按文案点击的
            # 打分脚本、读「提交按钮是否禁用」的脚本里也都有 innerText —— 于是
            # 点一次按钮顺手吃掉一份文案，队列提前耗尽，后面的判定读到别的
            # 页面的文案，测试红在完全不相干的地方（加了「等按钮可用再点」之后
            # `TopLevelResultHonestyTests` 就是这么红的）。
            if "aria-disabled" in js:          # 读提交按钮状态
                return {"label": "Sign up", "disabled": False, "found": True}
            if "bestScore" in js:              # 按文案点击（不改变页面文案）
                return "Sign up"
            if "innerText" in js:
                if len(seq) > 1:
                    return seq.pop(0)
                return seq[0]
            return ""

        page.evaluate.side_effect = _ev
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.count.return_value = 1
        return page

    def test_turnstile_gate_is_flagged_and_keeps_the_address(self):
        from platforms.grok import register_browser as rb
        from platforms.grok import signup_pages

        profile = "Complete your sign up a@b.com First name Last name Password Complete sign up"
        gate = "Complete the verification challenge, then try again."
        page = self._page([profile, gate, gate])
        with patch.object(signup_pages, "set_field_value", return_value=True), \
             patch.object(signup_pages, "ensure_turnstile_token", return_value=""), \
             patch.object(signup_pages, "click_profile_submit", return_value="Complete sign up"):
            out = rb._profile_stage(page, "Pw1!aaaa")
        self.assertFalse(out["ok"])
        self.assertTrue(out["turnstile_blocked"], "必须认出是 Turnstile 挡住了")
        self.assertIn("验证挑战", out["error"])
        self.assertTrue(out["retryable"])
        self.assertFalse(out["consumes_address"])

    def test_success_when_token_is_available(self):
        from platforms.grok import register_browser as rb
        from platforms.grok import signup_pages

        profile = "Complete your sign up a@b.com First name Last name Password Complete sign up"
        after = "You are signing into Grok"
        page = self._page([profile, after, after])
        with patch.object(signup_pages, "set_field_value", return_value=True), \
             patch.object(signup_pages, "ensure_turnstile_token", return_value="TOKEN-X"), \
             patch.object(signup_pages, "click_profile_submit", return_value="Complete sign up"):
            out = rb._profile_stage(page, "Pw1!aaaa")
        self.assertTrue(out["ok"], out)
        self.assertFalse(out.get("turnstile_blocked"))

    def test_missing_fields_are_reported(self):
        from platforms.grok import register_browser as rb
        from platforms.grok import signup_pages

        page = self._page(["Complete your sign up First name Last name Password"])
        with patch.object(signup_pages, "set_field_value", return_value=False), \
             patch.object(signup_pages, "click_profile_submit", return_value=""):
            out = rb._profile_stage(page, "Pw1!aaaa")
        self.assertFalse(out["ok"])
        self.assertIn("givenName", out["error"])


class SendCodeStageTests(unittest.TestCase):
    """发码阶段：从 API 响应 + 页面文案判定结局。"""

    def _page(self, texts):
        seq = list(texts)
        page = MagicMock()
        handlers = {}

        def _ev(js, *a):
            js = str(js)
            # 只有**真正读页面文案**的那次才从队列里取。
            #
            # 踩过：这段原来是「JS 里含 innerText 就取一次」，而按文案点击的
            # 打分脚本、读「提交按钮是否禁用」的脚本里也都有 innerText —— 于是
            # 点一次按钮顺手吃掉一份文案，队列提前耗尽，后面的判定读到别的
            # 页面的文案，测试红在完全不相干的地方（加了「等按钮可用再点」之后
            # `TopLevelResultHonestyTests` 就是这么红的）。
            if "aria-disabled" in js:          # 读提交按钮状态
                return {"label": "Sign up", "disabled": False, "found": True}
            if "bestScore" in js:              # 按文案点击（不改变页面文案）
                return "Sign up"
            if "innerText" in js:
                if len(seq) > 1:
                    return seq.pop(0)
                return seq[0]
            return ""

        page.evaluate.side_effect = _ev
        page.on.side_effect = lambda event, handler: handlers.setdefault(event, handler)
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.count.return_value = 1
        page._handlers = handlers
        return page

    def test_200_ok_means_sent(self):
        from platforms.grok import register_browser as rb

        page = self._page(["Sign up with your email Email Sign up Go back",
                           "Verify your email We've emailed a one time security code"])
        resp = MagicMock()
        resp.url = "https://accounts.x.ai/api/auth/send-verification-code"
        resp.status = 200
        resp.text.return_value = '{"ok":true}'
        # 先让阶段把响应监听挂上，再把响应喂进去（真实场景是提交后异步到达）
        out = rb._send_code_stage(page, "a@b.com", _test_responses=[resp])
        self.assertTrue(out["ok"], out)

    def test_verification_page_is_recognized_even_without_api_response(self):
        from platforms.grok import register_browser as rb

        page = self._page(["Sign up with your email Email Sign up Go back",
                           "Verify your email We've emailed a one time security code to a@b.com"])
        out = rb._send_code_stage(page, "a@b.com")
        self.assertTrue(out["ok"], out)

    def test_api_code_wins_over_ambiguous_page_text(self):
        from platforms.grok import register_browser as rb

        page = self._page(["Sign up with your email Email Sign up Go back", "Something else"])
        resp = MagicMock()
        resp.url = "https://accounts.x.ai/api/auth/send-verification-code"
        resp.status = 400
        resp.text.return_value = ('{"error":"Failed to send verification code",'
                                  '"code":"account:email-signup-unavailable"}')
        out = rb._send_code_stage(page, "a@b.com", _test_responses=[resp])
        self.assertFalse(out["ok"])
        self.assertTrue(out["retryable"])
        self.assertFalse(out["consumes_address"])
        self.assertTrue(out.get("email_unavailable"))
        self.assertIn("出口", out["error"])


class ProxyGeoipTests(unittest.TestCase):
    """有代理时要把出口 IP 对齐给 camoufox —— 但绝不能让启动失败。

    线上实测（2026-11-01）：camoufox 的 `geoip=True` 会自己用 6 个公有 IP
    探测服务反查出口（各 5s 超时），住宅代理上抛
    `camoufox.exceptions.InvalidIP: Failed to get IP address: … read timeout=5`，
    浏览器根本起不来。所以：自己解析出口 IP（拿不到就算了），把解析结果当
    `geoip` 传进去 —— 既对齐了时区/语言，又不会因为探测超时炸启动。
    """

    def _capture(self, exit_ip: str) -> dict:
        captured: dict = {}

        fake = _FakeCamoufox("Sign up with your email Email Sign up Go back")

        def _factory(**kwargs):
            captured.update(kwargs)
            return fake.cm

        with patch("platforms.grok.register_browser._resolve_exit_ip", return_value=exit_ip), \
             patch("camoufox.sync_api.Camoufox", side_effect=_factory), \
             patch("platforms.grok.register_browser.SEND_CODE_SETTLE_TIMEOUT", 0.05):
            from platforms.grok.register_browser import register_grok_via_browser

            register_grok_via_browser("a@icloud.com", proxy="socks5://u:p@9.9.9.9:6740")
        return captured

    def test_geoip_aligned_to_resolved_exit_ip(self):
        captured = self._capture("203.0.113.7")
        self.assertEqual(captured.get("geoip"), "203.0.113.7",
                         "解析到出口 IP 时应传给 camoufox 做时区/语言对齐")

    def test_geoip_disabled_when_resolution_fails(self):
        captured = self._capture("")
        self.assertIs(captured.get("geoip"), False,
                      "解析不到出口 IP 时必须退化成 False —— 不能因为探测超时起不来")


class TopLevelResultHonestyTests(unittest.TestCase):
    """顶层结果不许把「走到资料页」当成注册成功。

    线上实测（2026-11-01，GB 住宅出口）：发码 200 → 收到码 → 码通过 → 资料页填好，
    但 Turnstile 没拿到令牌，页面回 `Complete the verification challenge`。
    那一轮顶层返回了 `ok=True` —— 因为发码阶段的判定字典里 `ok=True`（「已发信」）
    被 `result.update(stage)` 覆盖进了最终结果。**上报成功但没有 SSO** 是最坏的
    一种错：任务会把这号当成功入库，实际是个空壳。
    """

    def _page(self, final_text):
        """按**阶段**给页面文案，不按「第几次读」给。

        踩过两次：这段原先按「第 N 次读页面文案就返回第 N 个字符串」造，于是
        任何**读次数**的变化都会让后面每个阶段读到上一页的文案 —— 加了「等按钮
        可用再点」之后（点击本身不再消费队列），资料页读到的是验证码页的文案，
        Turnstile 门禁判定整个失效，测试红在一个看起来毫不相干的地方。

        按阶段给就没这个问题：入口点开没有、提交点过没有、码进去没有，这三件
        事决定看到哪一页。点击文案靠 `labels` 区分 —— 入口那次带
        `sign up with email`，提交那次是 `_EMAIL_SUBMIT_LABELS`。
        """
        entry = ("SpaceXSI You are signing into Grok Sign up with your email "
                 "Email Sign up Go back")
        code_page = ("Verify your email We've emailed a one time security code "
                     "Please enter it below: Resend (27) Confirm")
        st = {"email_view": False, "submitted": False, "code_done": False}

        def _ev(js, *a):
            js = str(js)
            # 顺序要紧：提交脚本里也有 `bestScore` 这个变量名，先判它就会
            # 把提交当成「入口点击」，页面永远停在邮箱表单（踩过）。
            if "aria-disabled" in js:          # 提交发码脚本（三档梯子 / dryRun 只读状态）
                args = a[0] if a else {}
                if args.get("dryRun"):
                    return {"label": "Sign up", "disabled": False, "found": True, "reason": ""}
                st["submitted"] = True
                return {"how": "button", "label": "Sign up", "reason": "", "found": True,
                        "disabled": False, "hasInput": True, "inputOk": True, "diag": {}}
            if "bestScore" in js:              # 按文案点击（入口那次「Sign up with email」）
                st["email_view"] = True
                return "Sign up with email"
            if "innerText" in js:              # 读页面文案
                if not st["submitted"]:
                    return entry
                if not st["code_done"]:
                    return code_page
                return final_text
            return ""

        page = MagicMock()
        page.evaluate.side_effect = _ev
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.count.return_value = 1
        # `_wait_for_sso_cookie` 是「轮询到 35 秒」的忙等（`wait_for_timeout`
        # 在桩上是空操作）。让它推动一个假时钟 —— 走到的分支和真机一样，
        # 但不用真等 35 秒（不推的话这条用例要跑半分钟，失败还看不出原因）。
        page._clock = {"t": 1_000_000.0}
        page.wait_for_timeout.side_effect = (
            lambda ms=0: page._clock.__setitem__("t", page._clock["t"] + (ms or 0) / 1000.0)
        )
        page._st = st
        return page

    def _run(self, final_text):
        from platforms.grok import register_browser as rb
        from platforms.grok import signup_pages

        page = self._page(final_text)

        def _enter(_page, _code, **_kw):
            # 码进去了才轮到资料页 —— 这是阶段之间的最后一跳。
            page._st["code_done"] = True
            return True

        fake = _FakeCamoufox("")
        fake.cm.__enter__.return_value.new_page.return_value = page

        with patch.object(signup_pages, "set_field_value", return_value=True), \
             patch.object(signup_pages, "enter_code", side_effect=_enter), \
             patch.object(signup_pages, "ensure_turnstile_token", return_value=""), \
             patch.object(signup_pages, "click_profile_submit", return_value="Complete sign up"), \
             patch.object(rb, "_read_code", return_value="863-689"), \
             patch.object(rb.time, "time", lambda: page._clock["t"]), \
             patch.object(rb.time, "sleep", lambda *a: None), \
             patch.object(signup_pages.time, "sleep", lambda *a: None), \
             patch("camoufox.sync_api.Camoufox", return_value=fake.cm):
            return rb.register_grok_via_browser("a@icloud.com")

    def test_blocked_profile_stage_is_not_a_success(self):
        gate = "Complete the verification challenge, then try again."
        res = self._run(gate)
        self.assertFalse(res["ok"], f"没拿到 SSO 就不能报成功: {res}")
        self.assertEqual(res.get("sso"), "")
        self.assertTrue(res.get("turnstile_blocked"))


class ChromeDiscoveryTests(unittest.TestCase):
    """Turnstile 铸币要能找到 Playwright 自带的 Chromium。

    线上实测（2026-11-01）：本机没有任何系统 Chrome（`/usr/bin/google-chrome`
    等全都不存在），而 `find_chrome()` 走 Playwright 兜底分支时要去起一次
    driver 进程读 `p.chromium.executable_path`，那个进程实测被杀掉
    （`TargetClosedError`）→ 返回空串 → 铸币三连失败 `chrome not found`，
    而真正可用的二进制就躺在 `~/.cache/ms-playwright/chromium-*/chrome-linux64/chrome`。
    """
    def setUp(self):
        import tempfile

        from platforms.grok import turnstile_mint

        self.mod = turnstile_mint
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.binary = root / "chromium-1248" / "chrome-linux64" / "chrome"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_text("#!/bin/sh\n")
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root)
        self.addCleanup(os.environ.pop, "PLAYWRIGHT_BROWSERS_PATH", None)
        self.mod._find_chrome_cached.cache_clear()
        self.addCleanup(self.mod._find_chrome_cached.cache_clear)

    def test_playwright_cache_is_scanned(self):
        found = self.mod._playwright_chromium_candidates()
        self.assertIn(str(self.binary), found)

    def test_find_chrome_uses_playwright_cache_without_driver(self):
        with patch.object(self.mod, "_SYSTEM_CHROME_CANDIDATES", ()), \
             patch("playwright.sync_api.sync_playwright",
                   side_effect=AssertionError("不该为了读路径去起 playwright driver")):
            self.assertEqual(self.mod.find_chrome(), str(self.binary))


class VirtualDisplayTests(unittest.TestCase):
    """屏外 mint 需要一块 X display。

    线上实测（2026-11-01）：offscreen Chrome 路线拉起的是 headed 浏览器，
    本机没有 X server，Playwright 直接报
    `Looks like you launched a headed browser without having a XServer running.`
    而 Playwright 随即把进程关掉，错误退化成 `TargetClosedError: BrowserType.launch`。
    本机 `/usr/bin/Xvfb` 与 `/usr/bin/xvfb-run` 都在，所以没有 DISPLAY 时
    应该自动套 `xvfb-run`。
    """

    def setUp(self):
        from platforms.grok import turnstile_mint

        self.mod = turnstile_mint

    def test_prefix_used_when_display_missing(self):
        with patch.dict(os.environ, {"DISPLAY": ""}, clear=False), \
             patch.object(self.mod.shutil, "which",
                          side_effect=lambda n: "/usr/bin/xvfb-run" if n == "xvfb-run" else None):
            prefix = self.mod._virtual_display_prefix()
        self.assertEqual(prefix[:2], ["xvfb-run", "-a"])
        self.assertIn("-screen 0 1280x800x24", prefix)

    def test_no_prefix_when_display_works(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            sock = Path(tmp) / "X77"
            sock.write_text("")
            with patch.dict(os.environ, {"DISPLAY": ":77"}, clear=False), \
                 patch.object(self.mod, "_X11_SOCKET_DIR", tmp):
                self.assertEqual(self.mod._virtual_display_prefix(), [])

    def test_no_prefix_when_xvfb_absent(self):
        with patch.dict(os.environ, {"DISPLAY": ""}, clear=False), \
             patch.object(self.mod.shutil, "which", return_value=None):
            self.assertEqual(self.mod._virtual_display_prefix(), [])


class EntryPageWaitTests(unittest.TestCase):
    """入口页要等按钮渲染出来再点。

    线上实测（2026-11-01，e2e_loop 第 1 次）：`goto` 之后入口页有时是白屏/还在跑
    Cloudflare jsd 探针，6 次盲点全部「未命中」，白等 72 秒才放弃 —— 而同一个
    出口下一次启动就正常了。所以先轮询等 `Sign up with email` 出现。
    """

    def _page(self, texts):
        seq = list(texts)
        page = MagicMock()

        def _ev(js, *a):
            js = str(js)
            # 只有**真正读页面文案**的那次才从队列里取。
            #
            # 踩过：这段原来是「JS 里含 innerText 就取一次」，而按文案点击的
            # 打分脚本、读「提交按钮是否禁用」的脚本里也都有 innerText —— 于是
            # 点一次按钮顺手吃掉一份文案，队列提前耗尽，后面的判定读到别的
            # 页面的文案，测试红在完全不相干的地方（加了「等按钮可用再点」之后
            # `TopLevelResultHonestyTests` 就是这么红的）。
            if "aria-disabled" in js:          # 读提交按钮状态
                return {"label": "Sign up", "disabled": False, "found": True}
            if "bestScore" in js:              # 按文案点击（不改变页面文案）
                return "Sign up"
            if "innerText" in js:
                if len(seq) > 1:
                    return seq.pop(0)
                return seq[0]
            return ""

        page.evaluate.side_effect = _ev
        return page

    def test_waits_until_buttons_render(self):
        from platforms.grok import signup_pages

        page = self._page(["", "Just a moment...", "Sign up with your email Email Sign up Go back"])
        slept = []
        self.assertTrue(
            signup_pages.wait_for_entry(page, timeout=30, poll=2, sleep=slept.append)
        )
        self.assertEqual(len(slept), 2, "应该每轮睡一次，直到按钮出现")

    def test_cf_challenge_is_not_ready(self):
        from platforms.grok import signup_pages

        self.assertFalse(
            signup_pages.wait_for_entry(self._page(["Just a moment..."]), timeout=0, sleep=lambda s: None)
        )

    def test_times_out_without_buttons(self):
        from platforms.grok import signup_pages

        self.assertFalse(
            signup_pages.wait_for_entry(self._page([""]), timeout=0, sleep=lambda s: None)
        )


class OpenEntryPageTests(unittest.TestCase):
    """入口页没就绪时刷新一次再等（只刷一次，别把整轮时间都花在刷新上）。"""

    def test_reloads_once_when_entry_missing(self):
        from platforms.grok import register_browser as rb

        page = MagicMock()
        with patch("platforms.grok.register_browser.signup_pages.wait_for_entry",
                   side_effect=[False, True]) as wait:
            self.assertTrue(rb._open_entry_page(page, log=lambda m: None))
        self.assertEqual(wait.call_count, 2)
        page.reload.assert_called_once()

    def test_no_reload_when_entry_ready(self):
        from platforms.grok import register_browser as rb

        page = MagicMock()
        with patch("platforms.grok.register_browser.signup_pages.wait_for_entry",
                   return_value=True) as wait:
            self.assertTrue(rb._open_entry_page(page, log=lambda m: None))
        self.assertEqual(wait.call_count, 1)
        page.reload.assert_not_called()


class LocalizedEntryTests(unittest.TestCase):
    """出口 IP 不在英语国家时，入口页会被本地化 —— 不能只认英文。

    线上实测（2026-11-01，批量跑 5 个号里 3 个挂在「入口页没渲染」）：JP 出口
    （`183.77.203.114`）返回的是日语页 `メールで登録` / `アカウントを作成`，
    而 GB 出口才是 `Sign up with email`。当时只认英文文案，好页面被判成坏出口，
    白白浪费 5-6 个出口和 5 分钟。修法两条腿：钉住浏览器语言（en-US），
    同时给文案表补多语言兜底 —— 站点以后按 IP 本地化也不至于瞎。
    """

    _JP = ("SpaceXSI\nGrok\nにログインしています\nアカウントを作成\n\n"
           "X で登録\nメールで登録\nApple で登録\nGoogle で登録\nSign up with GitHub\n\n"
           "アカウントをお持ちですか？ ログイン")
    _DE = ("SpaceXSI\nDu meldest dich bei Grok an\nKonto erstellen\n\nMit X registrieren\n"
           "Mit E-Mail registrieren\nMit Apple registrieren\n\nDu hast schon ein Konto? Anmelden")
    _ES = ("SpaceXSI\nEstás iniciando sesión en Grok\nCrear cuenta\n\nRegistrarse con X\n"
           "Registrarse con correo electrónico\nRegistrarse con Apple\n\n"
           "¿Ya tienes una cuenta? Iniciar sesión")

    def _page(self, text):
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: text if "innerText" in str(js) else ""
        return page

    def _click_page(self, text):
        seen: dict = {}
        page = MagicMock()

        def _ev(js, *a):
            # 读页面文本的 JS 也含 innerText，用打分脚本特有的 bestScore 区分
            if "bestScore" in str(js):
                seen.update(a[0] if a else {})
                return "hit"
            return text

        page.evaluate.side_effect = _ev
        return page, seen

    def test_japanese_entry_page_counts_as_rendered(self):
        from platforms.grok import signup_pages

        self.assertTrue(
            signup_pages.wait_for_entry(self._page(self._JP), timeout=0, sleep=lambda s: None),
            "日语入口页是正常页面（JP 出口实测），不能再判成「没渲染」",
        )

    def test_german_entry_page_counts_as_rendered(self):
        from platforms.grok import signup_pages

        self.assertTrue(
            signup_pages.wait_for_entry(self._page(self._DE), timeout=0, sleep=lambda s: None)
        )

    def test_spanish_entry_page_counts_as_rendered(self):
        from platforms.grok import signup_pages

        self.assertTrue(
            signup_pages.wait_for_entry(self._page(self._ES), timeout=0, sleep=lambda s: None)
        )

    def test_localized_email_button_is_clickable(self):
        from platforms.grok import signup_pages

        for text, want in (
            (self._JP, "メールで登録"),
            (self._DE, "mit e-mail registrieren"),
            (self._ES, "registrarse con correo electrónico"),
        ):
            page, seen = self._click_page(text)
            self.assertTrue(signup_pages.click_email_signup(page), f"没点到邮箱按钮: {want}")
            labels = [str(x).lower() for x in seen.get("labels", [])]
            self.assertIn(want, labels, f"按钮文案表里缺 {want}")


class BrowserLocaleTests(unittest.TestCase):
    """把浏览器语言钉成英文，别让出口国家决定页面文案。

    camoufox 会按 `geoip` 对齐 `navigator.language` / `Accept-Language`
    （`.venv/.../camoufox/fingerprints.py:1775 context_options['locale'] = nav_lang`），
    于是 JP 出口自动出日语页面（2026-11-01 实测）。我们显式传 `locale` 覆盖它，
    让文案表与选择器不依赖出口国家。
    """

    def _capture(self, *, proxy: str) -> dict:
        captured: dict = {}
        fake = _FakeCamoufox("Sign up with your email Email Sign up Go back")

        def _factory(**kwargs):
            captured.update(kwargs)
            return fake.cm

        with patch("camoufox.sync_api.Camoufox", side_effect=_factory), \
             patch("platforms.grok.register_browser.SEND_CODE_SETTLE_TIMEOUT", 0.05):
            from platforms.grok.register_browser import register_grok_via_browser

            register_grok_via_browser("a@icloud.com", **({"proxy": proxy} if proxy else {}))
        return captured

    def test_locale_pinned_to_english_with_proxy(self):
        captured = self._capture(proxy="socks5://u:p@9.9.9.9:6740")
        locale = captured.get("locale")
        self.assertTrue(locale, "必须显式传 locale，否则 camoufox 按出口 IP 对齐语言")
        self.assertEqual(list(locale)[0], "en-US", f"首选英文，实际 {locale}")

    def test_locale_pinned_without_proxy(self):
        captured = self._capture(proxy="")
        locale = captured.get("locale")
        self.assertTrue(locale, "无代理时也要钉语言，别跟着宿主 OS 走")
        self.assertEqual(list(locale)[0], "en-US", f"首选英文，实际 {locale}")


class NavigationFailureTests(unittest.TestCase):
    """出口烂掉时 `page.goto` 会抛 70 秒的 TimeoutError —— 不能让它原样冒到上层。

    线上实测（2026-11-01 批量跑第 2 轮第 5 个号）：SE 出口 `goto` 抛
    `TimeoutError: Page.goto: Timeout 70000ms exceeded`，结果字典里
    `kind=None`、错误是一坨 Playwright 调用日志 —— 上层既不知道这是「出口问题」
    还是「流程问题」，也没法据此换出口重试。导航失败必须和入口页没渲染一样被
    明确归类：可重试、不消耗地址、错误文案说清怎么办。
    """

    def _result(self, **kw):
        from platforms.grok.register_browser import register_grok_via_browser
        fake = _FakeCamoufox("")
        fake.page.goto.side_effect = TimeoutError("Page.goto: Timeout 70000ms exceeded")
        with patch("camoufox.sync_api.Camoufox", side_effect=fake):
            return register_grok_via_browser("a@icloud.com", **kw)

    def test_navigation_timeout_is_classified(self):
        res = self._result()
        self.assertIs(res["ok"], False)
        self.assertIs(res["retryable"], True, res.get("error"))
        self.assertIs(res["consumes_address"], False, res.get("error"))
        self.assertIs(res["nav_failed"], True, res.get("error"))
        self.assertIn("出口", str(res["error"]))
        self.assertIn("地址仍然可用", str(res["error"]))

    def test_navigation_error_does_not_leak_playwright_call_log(self):
        res = self._result()
        err = str(res["error"])
        self.assertNotIn("Call log", err, err)
        self.assertNotIn("ms exceeded", err, err)


class SendCodeMessageTests(unittest.TestCase):
    """错误文案只许包一层：线上实测把「未确认发信」套了两层，读起来像机器故障。"""

    def test_no_confirmation_detail_is_not_doubled(self):
        from platforms.grok import register_browser as rb
        page = _FakeCamoufox("SpaceXSI\nGrok\nSign up with email", with_entry=False).page
        stage = rb._send_code_stage(page, "a@icloud.com", timeout=0)
        err = str(stage.get("error") or "")
        self.assertEqual(stage["kind"], "no_confirmation", stage)
        self.assertEqual(err.count("未确认发信"), 1, err)
        self.assertIn("页面未给出理由", err, err)


class CaptchaSolverWiringTests(unittest.TestCase):
    """R-21 剩下的一半：用户配的验证码解决器必须真的进铸币链。

    之前 `signup_pages._default_mint` 里 `mint_turnstile(...)` 从不传 `captcha`，
    于是 `RegisterConfig.captcha_solver`（界面上的「验证码解决器」）配了也白配 ——
    浏览器流程只会走页面 widget / 屏外 Chrome / 本地 solver。线上实测
    （2026-11-01）资料页卡死时，配好的外部解决器一次都没被试过。
    """

    def _cfg(self, solver, **extra):
        from core.base_platform import RegisterConfig
        return RegisterConfig(captcha_solver=solver, extra=dict(extra))

    def test_yescaptcha_with_key_is_built(self):
        from platforms.grok.register_browser import configured_captcha
        cap = configured_captcha(self._cfg("yescaptcha", yescaptcha_key="k123"))
        self.assertEqual(type(cap).__name__, "YesCaptcha", cap)

    def test_yescaptcha_without_key_is_skipped(self):
        from platforms.grok.register_browser import configured_captcha
        self.assertIsNone(
            configured_captcha(self._cfg("yescaptcha")),
            "没 key 就不能接：每次注册先白等一次外部 API 更亏",
        )

    def test_local_solver_uses_configured_url(self):
        from platforms.grok.register_browser import configured_captcha
        cap = configured_captcha(self._cfg("local_solver", solver_url="http://127.0.0.1:9999/"))
        self.assertIsNotNone(cap)
        self.assertIn("9999", str(getattr(cap, "solver_url", "")))

    def test_manual_solver_is_not_used_in_browser_flow(self):
        from platforms.grok.register_browser import configured_captcha
        self.assertIsNone(
            configured_captcha(self._cfg("manual")),
            "浏览器流程里没人手工点，manual 不能接",
        )

    def test_unknown_or_missing_solver_is_none(self):
        from platforms.grok.register_browser import configured_captcha
        self.assertIsNone(configured_captcha(None))
        self.assertIsNone(configured_captcha(self._cfg("2captcha")))

    def test_default_mint_passes_captcha_and_prefers_it(self):
        from platforms.grok import signup_pages

        sentinel = object()
        with patch("platforms.grok.turnstile_mint.mint_turnstile", return_value="t" * 20) as m:
            token = signup_pages._default_mint(
                "sitekey", "https://accounts.x.ai/sign-up", proxy="", log=None, captcha=sentinel,
            )
        self.assertEqual(token, "t" * 20)
        self.assertIs(m.call_args.kwargs.get("captcha"), sentinel)
        self.assertEqual(
            m.call_args.kwargs.get("prefer"), "captcha",
            "配了解决器就先走它，别先干等 45 秒页面 widget",
        )

    def test_default_mint_without_captcha_keeps_default_order(self):
        from platforms.grok import signup_pages

        with patch("platforms.grok.turnstile_mint.mint_turnstile", return_value="t" * 20) as m:
            signup_pages._default_mint("sitekey", "u", proxy="", log=None)
        self.assertIsNone(m.call_args.kwargs.get("captcha"))
        self.assertEqual(m.call_args.kwargs.get("prefer"), "")

    def test_profile_stage_forwards_captcha_to_mint(self):
        from platforms.grok import register_browser as rb

        sentinel = object()
        seen = {}
        fake = _FakeCamoufox("Create Your Grok Account")

        def _ensure(page, **kw):
            seen.update(kw)
            return "tok" * 20

        with patch("platforms.grok.signup_pages.fill_profile", return_value=[]), \
             patch("platforms.grok.signup_pages.ensure_turnstile_token", side_effect=_ensure):
            rb._profile_stage(fake.page, "pw", captcha=sentinel)

        self.assertIs(seen.get("captcha"), sentinel, "资料页阶段没把解决器传下去")


class EntryPageFailFastTests(unittest.TestCase):
    """入口页没渲染就别再盲点按钮 —— 省时间，也省出口。

    线上实测（2026-11-01，e2e_loop 第 2 次）：某些出口 IP 上 sign-up 页只返回壳
    （CF jsd 半拦），`Sign up with email` 6 次全部「未命中」，连等带点白烧 80 秒，
    最后报的错却是「找不到邮箱输入框（新版入口页未出现 Sign up with email）」
    —— 看着像选择器坏了，其实是出口 IP 的事，换出口立刻就好。
    """

    def test_no_click_loop_when_entry_never_renders(self):
        from platforms.grok import register_browser as rb
        from platforms.grok import signup_pages

        fake = _FakeCamoufox("grok")
        with patch.object(signup_pages, "wait_for_entry", return_value=False), \
             patch.object(signup_pages, "open_email_form") as open_form, \
             patch("platforms.grok.register_browser.ENTRY_WAIT_TIMEOUT", 0), \
             patch("camoufox.sync_api.Camoufox", return_value=fake.cm):
            res = rb.register_grok_via_browser("a@icloud.com")
        self.assertFalse(res["ok"])
        self.assertFalse(open_form.called, "页面都没渲染出来，点按钮是白点")
        self.assertTrue(res["retryable"])
        self.assertFalse(res["consumes_address"])
        self.assertIn("出口", res["error"])
        self.assertTrue(res.get("entry_not_rendered"))


class _MailStub:
    """最小 MailMessage 替身（只带 _read_code 真正会读的字段）。"""

    def __init__(self, subject, body="", sender="", received_at=None):
        self.provider_message_id = f"m-{abs(hash(subject)) % 10000}"
        self.mailbox = "INBOX"
        self.subject = subject
        self.snippet = ""
        self.text_body = body
        self.html_body = ""
        self.sender = sender
        self.received_at = received_at


def _xai_mail(code, received_at):
    return _MailStub(
        f"Grok confirmation code: {code}",
        f"Validate your email Thank you for creating a Grok Account {code}",
        "noreply_at_x_ai_62d2mv6z8v9034_b18de71c@icloud.com",
        received_at,
    )


def _marketing_mail():
    """真实样本：Tasklet 的推广信，正文里有 `for-ios`。"""
    return _MailStub(
        "The New Tasklet iOS App \U0001F4F2",
        '<a href="https://tasklet.ai/download-for-ios">Download for-ios now</a>',
        "news_at_tasklet_ai_s8dh5x@icloud.com",
        None,
    )


class CodeMailFilteringTests(unittest.TestCase):
    """只有 x.ai 的确认码信才配当验证码 —— 别把营销信里的 `for-ios` 填进页面。

    线上实测（2026-11-01 05:55，别名 535 `smogs-bicep.12@icloud.com`）：
    邮箱里躺着一封 Tasklet 推广信（主题 `The New Tasklet iOS App 📲`），正文含
    `for-ios`；`CODE_RE_ANY` 是 `re.IGNORECASE` 的 `[A-Z0-9]{3}-[A-Z0-9]{3}`，
    正好把它当验证码。真码信（`Grok confirmation code: 166-500`）当时刚投递、
    远端还没索引到，于是脚本读到 `for-ios` 填进去，页面停在
    `Verify your email`，整轮白跑还差点把真码作废。
    """

    def _run(self, msgs, *, timeout=1, after=None, logs=None):
        import platforms.grok.register_browser as rb

        class FakeICloud:
            @staticmethod
            def fetch_alias_messages(alias_id, limit=15):
                return list(msgs)

        logs = logs if logs is not None else []
        with patch("services.icloud_service", FakeICloud, create=True), \
             patch.object(rb.time, "sleep", lambda *_a: None):
            return rb._read_code(42, timeout, logs.append, after=after), logs

    def test_marketing_mail_code_shaped_word_is_never_used(self):
        now = time.time()
        # 营销信排在前面：必须靠内容过滤挡住，而不是靠「新信在前」这种巧合
        code, logs = self._run([_marketing_mail(), _xai_mail("166-500", now - 2)],
                               after=now - 30)
        self.assertEqual(code, "166-500", "必须取真码，不能取 for-ios")

    def test_only_marketing_mail_times_out_without_using_it(self):
        code, logs = self._run([_marketing_mail()])
        self.assertEqual(code, "", "没有真码信就该返回空（上层报未收到），绝不能填 for-ios")
        self.assertTrue(any("跳过" in l for l in logs),
                        "跳过的可疑码要留痕，否则改版排查方向全错")

    def test_stale_code_from_an_earlier_attempt_is_not_reused(self):
        """同一地址重试时，上一轮留在邮箱里的旧码不能拿来用。"""
        now = time.time()
        old = _xai_mail("742-956", now - 600)
        new = _xai_mail("166-500", now - 2)
        code, _ = self._run([new, old], after=now - 30)
        self.assertEqual(code, "166-500")
        code, _ = self._run([old], after=now - 30)
        self.assertEqual(code, "", "只看到旧码时要继续等，别填过期码")

    def test_mail_without_received_at_is_still_usable(self):
        """上游没给 received_at 时不能因为「时间未知」就拒收（宁可读到，也别读不到）。"""
        code, _ = self._run([_xai_mail("333-389", None)], after=time.time() - 30)
        self.assertEqual(code, "333-389")


class CodeFreshnessWiringTests(unittest.TestCase):
    """发码时刻必须一路传到读码：同地址重试时邮箱里还躺着上一轮的码。

    线上实测（2026-11-01 05:55，别名 535）：真码信 `166-500` 和上一轮的旧码
    混在一起，读码不知道「哪封是这次发的」，只能靠瞎猜。
    """

    def setUp(self):
        patcher = patch("platforms.grok.register_browser.ENTRY_WAIT_TIMEOUT", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_send_stage_records_the_send_moment(self):
        from platforms.grok import register_browser as rb

        # 注意取 `.page`：`_FakeCamoufox` 本身是「camoufox 工厂」替身，不是页面
        page = _FakeCamoufox("Verify your email We've emailed a one time security code").page
        before = time.time()
        out = rb._send_code_stage(page, "a@b.com")
        self.assertTrue(out["ok"], out)
        self.assertGreaterEqual(out.get("sent_at", 0), before - 1,
                               "发码成功必须记下时刻，供读码做新旧判断")

    def test_top_level_forwards_send_moment_to_code_reader(self):
        from platforms.grok import register_browser as rb

        seen = {}
        page = _FakeCamoufox("Verify your email We've emailed a one time security code")

        def _fake_read(alias_id, timeout, log, **kwargs):
            seen.update(kwargs)
            return ""

        with patch("camoufox.sync_api.Camoufox", side_effect=page), \
             patch.object(rb, "_read_code", _fake_read):
            res = rb.register_grok_via_browser("a@icloud.com", alias_id=99)
        self.assertFalse(res["ok"])
        self.assertIn("after", seen, "必须把发码时刻传给 _read_code")
        self.assertTrue(seen["after"], "after 不能是 None/0")


class EntryPageDiagnosticsTests(unittest.TestCase):
    """入口页没渲染时，错误信息必须说清「是被 CF 挑战挡了」还是「只返回了外壳」。

    为什么：批量跑的时候一连几个出口都报同一句「疑似出口 IP 被半拦」，
    没有页面内容就只能猜（是 CF 挑战？还是前端 JS 没加载？还是文案变了？
    2026-11-01 批量实测就这么卡住了一轮）。把页面开头带进错误里，判据立刻可查。
    """

    def setUp(self):
        p = patch("platforms.grok.register_browser.ENTRY_WAIT_TIMEOUT", 0)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, page_text):
        from platforms.grok import register_browser as rb

        page = _FakeCamoufox(page_text, with_entry=False)
        with patch("camoufox.sync_api.Camoufox", side_effect=page):
            return rb.register_grok_via_browser("a@icloud.com")

    def test_cloudflare_challenge_is_named(self):
        res = self._run("Just a moment... Verifying you are human. cf-chl-widget-abcd")
        self.assertFalse(res["ok"])
        self.assertIn("Cloudflare", res["error"])
        self.assertIn("Just a moment", res["error"], "要把页面开头带出来当证据")
        self.assertFalse(res["consumes_address"])

    def test_shell_page_reports_page_head(self):
        res = self._run("grok")
        self.assertFalse(res["ok"])
        self.assertIn("外壳", res["error"])
        self.assertIn("grok", res["error"], "没有 CF 特征时也要给出页面开头")


class EmailPageTurnstileGateTests(unittest.TestCase):
    """邮箱页「可提交」的口径：以**按钮**为准，不把令牌当硬门槛。

    2026-11-02 真机 A/B（本次修复的现场证据）：同一出口、`令牌 0 字符` 的提交
    直接拿到验证码并注册成功 —— 所以「空票必被通用拒绝」不成立。曾经按这个
    假设改成「有令牌字段就必须有票」，反而会拦掉本来能过的提交。
    真正需要补票的情形是**按钮禁用**（页面自己在说还没准备好）。
    """

    class _Page:
        def __init__(self, *, token_len, input_exists, button_disabled=False, button_found=True):
            self.token_len = token_len
            self.input_exists = input_exists
            self.button_disabled = button_disabled
            self.button_found = button_found

        def evaluate(self, script, *args):
            if "tokenLen" in str(script):
                return {"tokenLen": self.token_len, "inputExists": self.input_exists}
            return {
                "label": "Sign up",
                "disabled": self.button_disabled,
                "found": self.button_found,
                "reason": "",
            }

        def wait_for_timeout(self, ms):
            return None

    def test_empty_token_with_clickable_button_is_still_submittable(self):
        """真机证据：空票 + 按钮可点 → 提交，并且确实能发码成功。"""
        page = self._Page(token_len=0, input_exists=True, button_disabled=False)
        state = sp.wait_until_submittable(page, timeout=0.05, poll=0.05)
        self.assertTrue(state["ready"])

    def test_disabled_button_is_not_ready(self):
        page = self._Page(token_len=0, input_exists=True, button_disabled=True)
        state = sp.wait_until_submittable(page, timeout=0.05, poll=0.05)
        self.assertFalse(state["ready"])

    def test_token_makes_it_ready(self):
        page = self._Page(token_len=64, input_exists=True, button_disabled=False)
        state = sp.wait_until_submittable(page, timeout=0.05, poll=0.05)
        self.assertTrue(state["ready"])
        self.assertEqual(state["token_len"], 64)

    def test_page_without_token_field_keeps_the_button_only_rule(self):
        page = self._Page(token_len=0, input_exists=False, button_disabled=False)
        state = sp.wait_until_submittable(page, timeout=0.05, poll=0.05)
        self.assertTrue(state["ready"])


class EmailPageMintWithoutSolverTests(unittest.TestCase):
    """按钮禁用且页面上有令牌字段时，必须补票，且补票不依赖用户配 solver。

    旧代码：`if not gate.token_len and captcha is not None:` —— 没配 solver 时
    直接跳过铸币、只干等 widget，等不到就永远卡在「按钮禁用」。
    内建铸币不需要 solver：`mint_turnstile` 在 captcha=None 时跳过 captcha
    直接走 offscreen。
    """

    def test_stage_mints_when_button_is_disabled(self):
        from platforms.grok import register_browser as rb
        from platforms.grok import signup_pages as pages

        page = _FakeCamoufox("Sign up with email Email Sign up", with_entry=False).page

        def evaluate(script, *args):
            text = str(script)
            if "tokenLen" in text:
                return {"tokenLen": 0, "inputExists": True}
            if "document.body" in text:
                return "Sign up with email Email Sign up"
            # 按钮禁用 = 页面在说「还没准备好」
            return {"label": "Sign up", "disabled": True, "found": True, "reason": ""}

        page.evaluate.side_effect = evaluate
        calls = []

        def fake_ensure(page_arg, **kw):
            calls.append(kw.get("captcha"))
            return ""

        with patch.object(pages, "ensure_turnstile_token", side_effect=fake_ensure):
            stage = rb._send_code_stage(page, "a@icloud.com", proxy="", captcha=None, timeout=0)

        self.assertTrue(calls, "按钮禁用 + 有令牌字段时必须尝试补票（captcha=None 也走 offscreen）")
        self.assertIsNone(calls[0])
        self.assertTrue(stage.get("antibot_not_ready"), stage)
        self.assertFalse(stage.get("consumes_address"), "没提交就谈不上消耗地址")

    def test_stage_does_not_mint_when_button_is_clickable(self):
        """按钮可点时不补票：真机实测空票也能成功，白等一次铸币不划算。"""
        from platforms.grok import register_browser as rb
        from platforms.grok import signup_pages as pages

        page = _FakeCamoufox("Sign up with email Email Sign up", with_entry=False).page

        def evaluate(script, *args):
            text = str(script)
            if "tokenLen" in text:
                return {"tokenLen": 0, "inputExists": True}
            if "document.body" in text:
                return "Sign up with email Email Sign up"
            return {"label": "Sign up", "disabled": False, "found": True, "reason": ""}

        page.evaluate.side_effect = evaluate
        calls = []

        with patch.object(
            pages, "ensure_turnstile_token", side_effect=lambda *a, **kw: calls.append(1) or ""
        ):
            rb._send_code_stage(page, "a@icloud.com", proxy="", captcha=None, timeout=0)

        self.assertFalse(calls, "按钮可点就不该去铸币")


class CookieBannerBeforeSubmitTests(unittest.TestCase):
    """提交前若页面还停在 cookie 横幅上，必须再关一次。

    线上实测（2026-11-02 18:13，出口 9.142.37.16）：`发码结局 no_confirmation:
    未确认发信（页面未给出理由: Cookie notice. Essential cookies keep the site
    working…）` —— 横幅晚渲染盖住提交按钮，点了等于没点。
    """

    def _run(self, *, text):
        from platforms.grok import register_browser as rb
        from platforms.grok import signup_pages as pages

        page = _FakeCamoufox(text, with_entry=False).page

        def evaluate(script, *args):
            js = str(script)
            if "tokenLen" in js:
                return {"tokenLen": 0, "inputExists": True}
            if "document.body" in js:
                return text
            return {"label": "Sign up", "disabled": True, "found": True, "reason": ""}

        page.evaluate.side_effect = evaluate
        logs: list[str] = []
        dismissed: list[int] = []
        with patch.object(rb, "_dismiss_cookie_banner", side_effect=lambda p: dismissed.append(1)), \
                patch.object(pages, "ensure_turnstile_token", side_effect=lambda *a, **k: ""):
            rb._send_code_stage(page, "a@icloud.com", proxy="", captcha=None, timeout=0, log=lambda m: logs.append(str(m)))
        return dismissed, logs

    def test_banner_present_triggers_redismiss(self):
        dismissed, logs = self._run(
            text="Cookie notice. Essential cookies keep the site working and stay on. "
                 "Sign up with email Email Sign up"
        )
        self.assertTrue(dismissed, "页面还停在 cookie 横幅时必须再关一次")
        self.assertTrue(any("cookie 横幅" in line for line in logs), logs)

    def test_no_banner_does_not_spend_time(self):
        dismissed, _logs = self._run(text="Sign up with email Email Sign up")
        self.assertFalse(dismissed, "没有横幅就别白等按钮超时")
