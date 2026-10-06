"""Grok 浏览器注册链路的回归测试（不联网）。

把本机实测确认的关键约束钉死，避免以后被「顺手改回去」：
  1. 引擎必须是 camoufox —— chromium 在 x.ai 上会被 CF 403
  2. 验证码要用键盘逐字输入（页面会自动格式化去掉连字符）
  3. 提交按钮无文本，按 Enter 比按文本匹配稳
  4. 域名被拒 / 限流要给出可区分的错误
"""

import unittest
from unittest.mock import MagicMock, patch

from platforms.grok.register_browser import (
    CODE_RE,
    _gen_password,
    register_grok_via_browser,
)


class PasswordTests(unittest.TestCase):
    def test_password_meets_strength_requirements(self):
        """x.ai 要求大小写+数字+符号，生成器必须满足。"""
        for _ in range(20):
            pw = _gen_password()
            self.assertGreaterEqual(len(pw), 12, pw)
            self.assertTrue(any(c.islower() for c in pw), pw)
            self.assertTrue(any(c.isupper() for c in pw), pw)
            self.assertTrue(any(c.isdigit() for c in pw), pw)
            self.assertTrue(any(not c.isalnum() for c in pw), pw)


class CodeRegexTests(unittest.TestCase):
    def test_matches_xai_code_format(self):
        """x.ai 的码是 XXX-XXX。"""
        m = CODE_RE.search("SpaceXAI confirmation code: 333-389")
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "333-389")

    def test_ignores_unrelated_digits(self):
        self.assertIsNone(CODE_RE.search("your order 12345678 shipped"))


class AlreadyRegisteredPatternTests(unittest.TestCase):
    """「该邮箱已注册」的判定必须够宽 —— 漏判会让死号被反复重领。

    实测文案（注册时真遇到的）：
      "Existing account found — An account already exists which is
       associated with this email address. Please login using the login ..."
    但 x.ai 的文案会变，参考实现（reference/grok/grok-register）为此维护了
    11 条模式（含中文）。这里钉住覆盖面，别退化成两个字的字面量匹配。
    """

    def test_matches_observed_xai_wording(self):
        from platforms.grok.register_browser import _looks_already_registered

        observed = (
            "You are signing into\nGrok\nExisting account found\n\n"
            "An account already exists which is associated with this email "
            "address. Please login using the login"
        )
        self.assertTrue(_looks_already_registered(observed),
                        "实测遇到的文案必须被识别")

    def test_matches_reference_variants(self):
        from platforms.grok.register_browser import _looks_already_registered

        for text in (
            "This email is already registered",
            "An account with this email already exists",
            "User already exists",
            "This email address is unavailable",
            "email already in use",
            "此邮箱已被注册",
            "账号已存在",
            "找到现有账号",
        ):
            self.assertTrue(_looks_already_registered(text),
                            f"参考实现的模式应覆盖: {text!r}")

    def test_does_not_match_normal_pages(self):
        """正常页面不能误判 —— 误判会把可用的号标成已消耗。"""
        from platforms.grok.register_browser import _looks_already_registered

        for text in (
            "We've emailed a code to your address",
            "Create your account\nSign up with email",
            "",
            "Check your inbox for the verification code",
        ):
            self.assertFalse(_looks_already_registered(text),
                             f"正常文案不该误判: {text!r}")

    def test_checked_at_send_code_stage_too(self):
        """发码阶段就要认出「已有账号」，不能只等验证码环节。

        评审发现：若 x.ai 在发码阶段就回「Existing account found」，旧实现会
        落到「页面未确认发信」分支 → 别名记 failed → 放回 available → 反复重领。
        """
        import inspect

        from platforms.grok import register_browser as rb

        src = inspect.getsource(rb.register_grok_via_browser)
        # 发码后那段（在 _read_code 调用之前）必须有 already_registered 判定
        before_read = src.split("_read_code(")[0]
        self.assertIn("_looks_already_registered", before_read,
                      "发码阶段就该检查「已有账号」，否则会误记为失败")


class RegisterFlowTests(unittest.TestCase):
    """用桩浏览器验证控制流（不真起浏览器）。"""

    def _fake_camoufox(self, page_text: str, inputs=None, cookies=None):
        """构造一个假的 Camoufox 上下文管理器。"""
        page = MagicMock()
        page.evaluate.side_effect = lambda js, *a: (
            page_text if "innerText" in str(js) else (inputs if inputs is not None else [])
        )
        page.locator.return_value.first.fill.return_value = None
        page.locator.return_value.first.click.return_value = None
        page.locator.return_value.first.count.return_value = 1
        page.locator.return_value.first.is_visible.return_value = True
        ctx = MagicMock()
        ctx.cookies.return_value = cookies or []
        page.context = ctx

        browser = MagicMock()
        browser.new_page.return_value = page

        cm = MagicMock()
        cm.__enter__.return_value = browser
        cm.__exit__.return_value = None
        return cm, page

    def test_missing_camoufox_reports_clearly(self):
        with patch.dict("sys.modules", {"camoufox": None, "camoufox.sync_api": None}):
            res = register_grok_via_browser("a@b.com")
        self.assertFalse(res["ok"])
        self.assertTrue(res["error"], "必须给出失败原因")

    def test_rate_limit_is_distinguishable(self):
        cm, _page = self._fake_camoufox(
            "Too many code requests. Please wait a few minutes before requesting another code."
        )
        with patch("camoufox.sync_api.Camoufox", return_value=cm):
            res = register_grok_via_browser("a@icloud.com")
        self.assertFalse(res["ok"])
        self.assertIn("限流", res["error"])

    def test_invalid_domain_is_distinguishable(self):
        cm, _page = self._fake_camoufox(
            "Your email address is invalid. Please use a different email address."
        )
        with patch("camoufox.sync_api.Camoufox", return_value=cm):
            res = register_grok_via_browser("x@tiny.example")
        self.assertFalse(res["ok"])
        self.assertIn("拒收", res["error"])
        self.assertIn("tiny.example", res["error"], "错误里要带上是哪个域名")

    def test_unexpected_page_is_reported(self):
        cm, _page = self._fake_camoufox("Something completely different")
        with patch("camoufox.sync_api.Camoufox", return_value=cm):
            res = register_grok_via_browser("a@icloud.com")
        self.assertFalse(res["ok"])
        self.assertIn("未确认发信", res["error"])

    def test_proxy_config_is_passed_to_camoufox(self):
        """代理必须经过 build_playwright_proxy_config（socks5 认证要降级）。"""
        cm, _page = self._fake_camoufox("Something different")
        captured = {}

        def _factory(**kwargs):
            captured.update(kwargs)
            return cm

        with patch("camoufox.sync_api.Camoufox", side_effect=_factory):
            register_grok_via_browser(
                "a@icloud.com", proxy="socks5://user:pw@9.9.9.9:6740"
            )
        self.assertIn("proxy", captured, "代理必须传给 camoufox")
        self.assertEqual(captured["proxy"]["server"], "http://9.9.9.9:6740",
                         "带认证的 socks5 必须降级成 http")
        self.assertTrue(captured.get("geoip"), "有代理时应对齐 geoip")

    def test_sso_is_read_from_cookies(self):
        """拿到 SSO 才算成功 —— 从浏览器 cookie 里取。"""
        cm, _page = self._fake_camoufox(
            "Complete your sign up",
            inputs=[{"t": "password", "n": "password"}],
            cookies=[{"name": "sso", "value": "eyJhbGciOi.session.token"}],
        )
        with patch("camoufox.sync_api.Camoufox", return_value=cm):
            res = register_grok_via_browser("a@icloud.com")
        # 桩里 evaluate 的返回值是固定文本，因此这里只断言「读 cookie」这条路被走到
        self.assertIn("sso", res, "返回结构里必须有 sso 字段")


class CodeReadingTests(unittest.TestCase):
    """验证码读取必须支持两条链路 —— iCloud 号池 + 通用渠道。

    评审发现：`_read_code` 原先**只**读 iCloud，而 docstring 声称其它渠道
    走 `wait_for_code`。后果是非 iCloud 渠道（Outlook/tempmail）在浏览器
    路径下永远收不到码，只能干等到超时。
    """

    def test_uses_channel_wait_for_code_when_no_alias_id(self):
        from platforms.grok.register_browser import _read_code

        calls = {}

        class FakeMailbox:
            def get_current_ids(self, account):
                calls["ids"] = True
                return {"old-1"}

            def wait_for_code(self, account, **kwargs):
                calls["wait"] = kwargs
                calls["account"] = account
                return "123-456"

        account = object()
        code = _read_code(None, 5, None, mailbox=FakeMailbox(), mailbox_account=account)
        self.assertEqual(code, "123-456", "应走渠道的 wait_for_code 拿码")
        self.assertIs(calls.get("account"), account, "必须把账号对象传下去")
        self.assertTrue(calls.get("ids"), "应先取已有邮件 id 避免误用旧码")
        self.assertIn("before_ids", calls.get("wait") or {},
                      "要把旧 id 传给 wait_for_code 做增量判断")

    def test_returns_empty_when_no_source_given(self):
        """两个来源都没给时必须返回空（上层据此报错），不能静默当成功。"""
        from platforms.grok.register_browser import _read_code

        self.assertEqual(_read_code(None, 1, None), "")

    def test_channel_error_is_logged_not_raised(self):
        """渠道读码抛异常不能把整个注册炸掉 —— 记日志后返回空。"""
        from platforms.grok.register_browser import _read_code

        logs = []

        class BrokenMailbox:
            def wait_for_code(self, account, **kwargs):
                raise RuntimeError("imap down")

        code = _read_code(
            None, 2, logs.append,
            mailbox=BrokenMailbox(), mailbox_account=object(),
        )
        self.assertEqual(code, "")
        self.assertTrue(logs, "读码失败必须留下日志（否则无从排查）")

    def test_task_control_interrupts_polling(self):
        """等码期间必须能被打断 —— 默认要等 240 秒，不给停止入口等于卡死面板。

        评审发现：这条路径原先裸 `time.sleep(10)` 轮询，用户点「停止」也得
        干等满超时。
        """
        from platforms.grok.register_browser import _read_code

        class StopNow(Exception):
            pass

        class Control:
            def __init__(self):
                self.calls = 0

            def checkpoint(self):
                self.calls += 1
                raise StopNow()

        ctl = Control()
        with patch("services.icloud_service.fetch_alias_messages",
                   return_value=[]):
            with self.assertRaises(StopNow):
                _read_code(42, 30, None, task_control=ctl)
        self.assertGreaterEqual(ctl.calls, 1,
                                "轮询必须调用 checkpoint，否则无法停止")

    def test_task_control_optional(self):
        """不传 task_control 时不能崩（单测/脚本调用场景）。"""
        from platforms.grok.register_browser import _read_code

        with patch("services.icloud_service.fetch_alias_messages",
                   return_value=[]):
            code = _read_code(42, 1, None)
        self.assertEqual(code, "")

    def test_reads_code_from_body_not_just_subject(self):
        """验证码在**正文**里也必须读到。

        评审发现：代码读的是 `m.text` / `m.preview`，而 iCloud 的
        `MailMessage` 只有 `snippet` / `text_body` / `html_body` ——
        全部落到空串，等于只搜了主题。x.ai 一旦把码移出主题就静默读不到。
        """
        import platforms.grok.register_browser as rb
        from platforms.icloud.models import MailMessage

        # 主题没有码，码在正文里
        msg = MailMessage(
            provider_message_id="m1",
            mailbox="INBOX",
            subject="Your SpaceXAI verification",
            snippet="",
            text_body="Your confirmation code is 333-389. It expires soon.",
        )

        class FakeICloudService:
            @staticmethod
            def fetch_alias_messages(alias_id, limit=15):
                return [msg]

        logs = []
        with patch.dict("sys.modules", {}), \
             patch("services.icloud_service", FakeICloudService, create=True), \
             patch.object(rb.time, "sleep", lambda *_a: None):
            code = rb._read_code(42, 1, logs.append)
        self.assertEqual(code, "333-389",
                         "正文里的验证码必须能读到（属性名要对齐 MailMessage）")


class TaskInterruptionPassthroughTests(unittest.TestCase):
    """控制流异常（停止/跳过）不能被吞成普通失败。

    评审实测复现：`StopTaskRequested` 继承自 RuntimeError，被
    `_read_code` 里的 `except Exception` 捕获后变成「未收到验证码」——
    用户点了停止，任务却报注册失败，还继续跑完整个浏览器流程。
    """

    def test_read_code_does_not_swallow_task_interruption(self):
        from core.task_runtime import StopTaskRequested
        from platforms.grok.register_browser import _read_code

        class Control:
            def checkpoint(self):
                raise StopTaskRequested()

        with patch("services.icloud_service.fetch_alias_messages",
                   return_value=[]):
            with self.assertRaises(StopTaskRequested):
                _read_code(42, 30, None, task_control=Control())

    def test_channel_interruption_passes_through(self):
        """渠道 wait_for_code 抛的控制流异常同样要放行。"""
        from core.task_runtime import SkipCurrentAttemptRequested
        from platforms.grok.register_browser import _read_code

        class Mailbox:
            def wait_for_code(self, account, **kwargs):
                raise SkipCurrentAttemptRequested()

        with self.assertRaises(SkipCurrentAttemptRequested):
            _read_code(None, 5, None, mailbox=Mailbox(), mailbox_account=object())

    def test_ordinary_channel_error_still_swallowed(self):
        """普通异常仍要吞掉（读码失败不该炸掉整个注册）。"""
        from platforms.grok.register_browser import _read_code

        class Mailbox:
            def wait_for_code(self, account, **kwargs):
                raise RuntimeError("imap down")

        logs = []
        code = _read_code(None, 2, logs.append,
                          mailbox=Mailbox(), mailbox_account=object())
        self.assertEqual(code, "")
        self.assertTrue(logs)


class CookieChainPollingTests(unittest.TestCase):
    """提交后必须**轮询**等 sso 落地，不能只等固定时长。

    出处：reference/grok/grok-hub-clean/patched/grok_register/register.py:845-860
    （`COOKIE_CHAIN_WAIT_SECONDS` 默认 30 秒）。
    """

    def test_source_polls_for_sso_instead_of_fixed_wait(self):
        """提交后必须调轮询函数，而不是只等一个固定时长。

        轮询实现在 `_wait_for_sso_cookie`（行为测试见下两条）；这里钉住
        `register_grok_via_browser` **确实用了它**，且没退回固定等待。
        """
        import inspect

        from platforms.grok import register_browser as rb

        src = inspect.getsource(rb.register_grok_via_browser)
        self.assertIn(
            "_wait_for_sso_cookie(", src,
            "提交后必须调轮询函数等 sso 落地",
        )
        self.assertNotIn(
            "wait_for_timeout(15000)", src,
            "固定 15 秒等待会在跳链完成前返回 —— 把成功的注册误判成失败",
        )

    def test_polling_helper_has_a_timeout_ceiling(self):
        """轮询必须有超时上限（否则 x.ai 不下发 sso 时会挂死整条任务）。"""
        import inspect

        from platforms.grok.register_browser import _wait_for_sso_cookie

        src = inspect.getsource(_wait_for_sso_cookie)
        self.assertIn("while True:", src)
        self.assertIn("deadline", src, "必须有超时上限")

    def test_polls_until_sso_appears(self):
        """sso 第 3 次读取才出现 → 返回它（而不是判失败）。"""
        from platforms.grok.register_browser import _wait_for_sso_cookie

        cookies_seq = [
            {},                    # 跳链还没跑完
            {},                    # 还在跳
            {"sso": "x" * 152},    # 落地
        ]
        state = {"n": 0}

        class Ctx:
            def cookies(self):
                i = min(state["n"], len(cookies_seq) - 1)
                state["n"] += 1
                return [{"name": k, "value": v} for k, v in cookies_seq[i].items()]

        class Page:
            context = Ctx()

            def wait_for_timeout(self, ms):
                pass

        sso = _wait_for_sso_cookie(Page(), timeout=5)
        self.assertEqual(sso, "x" * 152)
        self.assertEqual(state["n"], 3, "应该读到第 3 次才拿到 sso")

    def test_returns_empty_on_timeout_without_hanging(self):
        """一直拿不到 sso → 超时返回空串（不能挂死）。"""
        import time as _t

        from platforms.grok.register_browser import _wait_for_sso_cookie

        class Ctx:
            def cookies(self):
                return []

        class Page:
            context = Ctx()

            def wait_for_timeout(self, ms):
                pass

        t0 = _t.time()
        sso = _wait_for_sso_cookie(Page(), timeout=0.3, poll_ms=10)
        elapsed = _t.time() - t0
        self.assertEqual(sso, "")
        self.assertLess(elapsed, 3, f"超时必须按时返回，实际 {elapsed:.1f}s")


class CookieBannerTests(unittest.TestCase):
    """cookie 同意横幅：文案各家不同、还会变（实测真机 OneTrust 是 'Allow All'）。

    实测（2026-10-06）：真机 DOM 里横幅按钮是 'Allow All' / 'Reject All' /
    'Confirm My Choices'，旧实现只试 'Accept All Cookies' / 'Close' /
    'Accept all' —— 全部落空，横幅遮住按钮时点击落空，表象是
    「找不到邮箱输入框」。
    """

    def test_observed_wording_is_covered(self):
        from platforms.grok import register_browser as rb

        for observed in ("Allow All", "Accept All Cookies", "Accept all"):
            self.assertIn(
                observed, rb._COOKIE_BANNER_BUTTON_NAMES,
                f"真机文案 {observed!r} 没进兜底清单",
            )

    def test_dismiss_clicks_the_observed_button(self):
        from platforms.grok.register_browser import _dismiss_cookie_banner

        clicks = []
        page = MagicMock()

        def _role(role, name=None):
            if name == "Allow All":
                btn = MagicMock()
                btn.first.click.side_effect = lambda timeout=None: clicks.append(name)
                return btn
            raise RuntimeError(f"no button named {name}")

        page.get_by_role.side_effect = _role
        page.locator.return_value.first.count.return_value = 0  # OneTrust id 选择器不命中
        _dismiss_cookie_banner(page)
        self.assertEqual(clicks, ["Allow All"], "真机文案 'Allow All' 必须被点到")


class SignupFormFillTests(unittest.TestCase):
    """建号表单填充要读回校验 + 多选择器兜底。

    实测（2026-10-06）：填充静默失败（选择器没命中/受控输入拒收）时照样
    提交，表象是「未拿到 SSO」，排查方向全错。
    """

    def _page(self, *, count=1, readback="James"):
        page = MagicMock()
        page.locator.return_value.first.count.return_value = count
        page.locator.return_value.first.fill.return_value = None
        page.evaluate.return_value = readback
        return page

    def test_all_fields_filled_returns_empty_missing(self):
        from platforms.grok.register_browser import _fill_signup_form

        missing = _fill_signup_form(self._page(), "Pw123!")
        self.assertEqual(missing, [], "正常填充不应报缺字段")

    def test_no_selector_match_reports_fields(self):
        from platforms.grok.register_browser import _fill_signup_form

        page = self._page(count=0)
        page.get_by_placeholder.side_effect = RuntimeError("no placeholder")
        missing = _fill_signup_form(page, "Pw123!")
        self.assertIn("givenName", missing)
        self.assertIn("password", missing)

    def test_readback_empty_flags_field(self):
        """fill 静默落空（读回为空）→ 字段被判 missing，不盲提交。"""
        from platforms.grok.register_browser import _fill_signup_form

        page = self._page(readback="")
        page.get_by_placeholder.side_effect = RuntimeError("no placeholder")
        missing = _fill_signup_form(page, "Pw123!")
        self.assertIn("password", missing, "读回为空必须报出字段，不能盲提交")


class SignupSubmitTests(unittest.TestCase):
    """提交按钮文案会变（实测 'Complete your sign up'）；多策略兜底。"""

    def test_prefers_observed_wording(self):
        from platforms.grok.register_browser import _submit_signup_form

        clicked = []
        page = MagicMock()

        def _role(role, name=None):
            if name == "Complete your sign up":
                btn = MagicMock()
                btn.last.click.side_effect = lambda timeout=None: clicked.append(name)
                return btn
            raise RuntimeError(name)

        page.get_by_role.side_effect = _role
        _submit_signup_form(page)
        self.assertEqual(clicked, ["Complete your sign up"])

    def test_falls_back_to_form_submit_button(self):
        from platforms.grok.register_browser import _submit_signup_form

        page = MagicMock()
        page.get_by_role.side_effect = RuntimeError("none")
        submitted = []
        page.locator.return_value.last.click.side_effect = (
            lambda timeout=None: submitted.append("form")
        )
        _submit_signup_form(page)
        self.assertEqual(submitted, ["form"])

    def test_last_resort_is_enter(self):
        from platforms.grok.register_browser import _submit_signup_form

        page = MagicMock()
        page.get_by_role.side_effect = RuntimeError("none")
        page.locator.return_value.last.click.side_effect = RuntimeError("no form button")
        _submit_signup_form(page)
        page.keyboard.press.assert_called_once_with("Enter")


if __name__ == "__main__":
    unittest.main()
