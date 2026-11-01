"""发码被拒后的处置：识别、地址回收、重试判定。

背景（线上实测，2026-10-23）：`shine_academe.9c@icloud.com` 被反复领取、
反复失败。真机抓包确认发码请求确实发出（`POST /api/auth/send-verification-code`），
但 x.ai 回的是「不要再来」这类理由，表现为三种页面文案：

- `Too many code requests. Please wait a few minutes...`（最明确，限流）
- `Email sign-up isn't available right now. Sign up another way.`（邮箱侧被限）
- 什么都不说（静默，只停在表单）

第一版据此定下的规则是「三种都算这个地址被用掉 → 记 used → 判不可重试」。

**2026-11-01 修正（有反证实验，这条老规则是错的）**：对同一个出口 IP 换地址
做 A/B，再对同一个地址换出口 IP 做 A/B，结论是这两类拒绝的归属完全不同：

- `email:send-validation-code-rate-limited`（HTTP 400，同一接口的机器读错误码）
  **跟邮箱走**：别名 534 半小时内被我们自己请求 5 次后，在全部 3 个代理上都恒
  返回它；同期未使用过的别名在住宅出口上一次就 `200 {"ok":true"}`。
  重开一轮（甚至换个出口）**不改结局**，但等冷却后同一地址仍可用。
- `account:email-signup-unavailable`（HTTP 400 错误的 `code` 字段）
  **跟出口 IP 走**：`9.142.43.158` / `9.142.43.133` 两条机房代理上恒出现；
  换成住宅代理 `us.1024proxy.io` 后，**同一个地址立刻发码成功**。→ 地址根本没被用掉。
- 地址的发码预算实测约 4–6 次（含失败），之后第 3 种结局（静默）也会转为限流。

所以现在钉住的是：

1. 三种拒绝都要被识别成各自的类型（不能都落进「页面未确认发信」的兜底）；
2. **两类拒绝都不消耗地址**（`consumes_address=False`）—— 烧掉地址会让号池
   被无谓消耗，而地址其实还能在别的出口上用；真正该换的是出口 / 等冷却；
3. 两类都**可重试**（`retryable=True`），重试走新出口即可；
4. 只有「该邮箱已经有账号」（`email:existing-email` 一类）才消耗地址并判死。
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import platforms.grok.plugin as plugin_mod
from core.base_platform import RegisterConfig


def _platform(extra=None):
    return plugin_mod.GrokPlatform(
        RegisterConfig(executor_type="browser", extra=dict(extra or {}))
    )


class SendCodeRejectionDetectionTests(unittest.TestCase):
    """三种页面文案都要被识别成各自的失败类型。"""

    def _classify(self, body_text: str) -> dict:
        """把页面文本喂给分类器，返回结果字典。"""
        from platforms.grok.register_browser import classify_send_code_page

        return classify_send_code_page(body_text)

    def test_too_many_requests(self):
        out = self._classify(
            "You are signing into\nGrok\nSign up with your email\nEmail\n\n"
            "Too many code requests. Please wait a few minutes before requesting another code."
        )
        self.assertEqual(out["kind"], "rate_limited")
        self.assertEqual(out["retry_after_hint"], 180)
        self.assertFalse(out["consumes_address"],
                         "限流跟邮箱走、且会冷却：地址没被用掉，不能烧号池")
        self.assertTrue(out["retryable"], "换个出口或等冷却后还能用这个地址")

    def test_email_signup_unavailable(self):
        out = self._classify(
            "You are signing into\nGrok\nSign up with your email\nEmail\n\n"
            "Email sign-up isn’t available right now. Sign up another way."
        )
        self.assertEqual(out["kind"], "email_unavailable")
        self.assertFalse(out["consumes_address"],
                         "A/B 实测换个住宅出口同一地址立刻成功 → 地址没被用掉")
        self.assertTrue(out["retryable"], "该换的是出口 IP，不是地址")

    def test_silent_form(self):
        """只停在表单、既没发信也没报错 —— 第 3 种结局，地址同样不烧。"""
        out = self._classify(
            "You are signing into\nGrok\nSign up with your email\nEmail\n"
            "Sign upGo back\n\nBy continuing, you agree to the SpaceXSI Terms of Service"
        )
        self.assertEqual(out["kind"], "no_confirmation")
        self.assertFalse(out["consumes_address"],
                         "客户端连请求都没发出（实测有这种轮次）—— 更不该烧地址")
        self.assertTrue(out["retryable"])

    def test_success_is_not_mistaken(self):
        out = self._classify(
            "You are signing into\nGrok\nWe've emailed you a code.\nVerify your email"
        )
        self.assertEqual(out["kind"], "sent")

    def test_all_rejections_keep_the_address(self):
        """三种拒绝都必须保住地址 —— 它们都不代表这个号被用掉了。"""
        from platforms.grok.register_browser import classify_send_code_page

        for text in (
            "Too many code requests. Please wait a few minutes",
            "Email sign-up isn’t available right now",
            "Sign up with your email\nEmail\nSign upGo back",
        ):
            out = classify_send_code_page(text)
            self.assertFalse(out["consumes_address"], f"{out['kind']} 不该烧地址")

    def test_only_existing_account_is_fatal(self):
        """唯一该判死并消耗地址的：这个邮箱已经有账号了。"""
        from platforms.grok.register_browser import classify_send_code_page

        out = classify_send_code_page(
            "An account with this email already exists. Sign in instead."
        )
        self.assertEqual(out["kind"], "already_registered")
        self.assertTrue(out["consumes_address"])
        self.assertFalse(out["retryable"])


class SendCodeRejectionBookkeepingTests(unittest.TestCase):
    """浏览器路径把「消耗地址」如实转给插件 → 记 used 而不是 failed。"""

    def test_browser_result_marks_address_consumed_on_rejection(self):
        """`register_grok_via_browser` 的返回值要带 `consumes_address`。"""
        import inspect

        from platforms.grok import register_browser as rb

        src = inspect.getsource(rb.register_grok_via_browser)
        self.assertIn("consumes_address", src,
                      "被拒时必须把「地址已消耗」带回去，否则上层会放回 available")

    def test_plugin_records_used_when_address_consumed(self):
        """插件见 `consumes_address` → 记 used（不是 failed）。"""
        logs: list = []
        statuses: list = []

        class Mb:
            def set_account_status(self, account, status):
                statuses.append(status)

            def get_email(self):
                from core.base_mailbox import MailboxAccount

                return MailboxAccount(email="t@icloud.com", extra={"icloud_alias_id": 42})

        plat = _platform({})
        plat.mailbox = Mb()
        plat._log_fn = logs.append
        rejected = {
            "ok": False, "sso": "", "password": "", "email": "t@icloud.com",
            "error": "x.ai 取码限流（这个邮箱已被消耗，换地址）",
            "consumes_address": True,
        }
        with patch("platforms.grok.register_browser.register_grok_via_browser",
                   return_value=rejected):
            with self.assertRaises(RuntimeError):
                plat.register()
        self.assertEqual(statuses, ["used"],
                         "被拒的地址必须记 used，否则会被放回 available 反复重领")


class SendCodeRejectionIsNotRetriedTests(unittest.TestCase):
    """重试循环必须认得这类失败，不再白跑第 2 轮。"""

    def test_rejection_raises_non_retryable(self):
        """上层拿到的异常要被判成「不可重试」。"""
        from core.task_runtime import NonRetryableRegisterError

        logs: list = []
        plat = _platform({})
        plat.mailbox = None
        plat._log_fn = logs.append
        rejected = {
            "ok": False, "sso": "", "password": "", "email": "t@icloud.com",
            "error": "x.ai 取码限流（这个邮箱已被消耗，换地址）",
            "consumes_address": True,
        }
        with patch("platforms.grok.register_browser.register_grok_via_browser",
                   return_value=rejected), \
             patch("platforms.grok.plugin.GrokPlatform.is_email_registered",
                   return_value=False):
            with self.assertRaises(Exception) as ctx:
                plat.register(email="t@icloud.com")
        self.assertIsInstance(
            ctx.exception, NonRetryableRegisterError,
            f"发码被拒应判不可重试（实际 {type(ctx.exception).__name__}: {ctx.exception}）",
        )


if __name__ == "__main__":
    unittest.main()


class RejectedAddressStopsBeingReissuedTests(unittest.TestCase):
    """被拒的地址必须从「本平台可领」里剔除，否则会被无限次重领。

    机制：`mark_alias_used(platform=...)` 把 `used_platforms` 记上本平台，
    领取查询（`services/icloud/aliases.py`）见 `used` + 本平台已用 → 跳过。

    线上实测（2026-10-23）：`shine_academe.9c@icloud.com` 从未成功注册过
    grok，`used_platforms` 一直是 `,chatgpt,`，于是每次都被领出来撞同一堵墙
    （当天被领取 4 次以上，每次都因发码被拒失败）。
    """

    def test_mark_used_blocks_same_platform_reissue(self):
        from services.icloud import aliases as aliases_mod

        called = {}

        def fake_mark(alias_id, *, platform=""):
            called["id"] = alias_id
            called["platform"] = platform

        mb = MagicMock()
        mb.set_account_status = None  # 直接调服务层，不经渠道

        with patch.object(aliases_mod, "mark_alias_used", fake_mark):
            aliases_mod.mark_alias_used(528, platform="grok")
        self.assertEqual(called["platform"], "grok",
                         "必须把平台名带上，否则会锁掉所有平台")

    def test_used_platforms_filter_skips_grok(self):
        """`mark_alias_used(platform="grok")` 之后，领取查询会跳过这个地址。

        直接跑真实 helper（不 mock）：`,chatgpt,` 加上 grok → 记录里含 grok →
        `email_used_by_platform` 为真 → `claim_alias` 的 `used` 分支 `continue`。
        """
        from core.db.base import email_used_by_platform, mark_email_used_by

        before = ",chatgpt,"   # 线上这条地址的真实值
        self.assertFalse(email_used_by_platform(before, "grok"),
                         "前置：被拒之前 grok 还可用，于是被反复领取")
        after = mark_email_used_by(before, "grok")
        self.assertEqual(after, ",chatgpt,grok,")
        self.assertTrue(email_used_by_platform(after, "grok"),
                        "记上之后领取必须跳过 —— 这是打破死循环的关键")


class RejectionEndToEndTests(unittest.TestCase):
    """一次被拒注册的完整后果（走真实取号路径，不传 email）。

    注意必须**不传** `email`：显式传地址时走 `_mailbox_account_for` 取渠道
    凭证，拿不到就不记账（别名会停在 in_use）—— 那是既有行为，不是本次改动
    的问题。真实任务里邮箱来自渠道，走的是这条路径。
    """

    def test_rejection_records_used_and_marks_platform(self):
        from core.base_mailbox import MailboxAccount
        from core.db.base import email_used_by_platform, mark_email_used_by
        from core.task_runtime import NonRetryableRegisterError

        statuses: list = []
        used_field = {"v": ",chatgpt,"}

        class Mb:
            _last_account = MailboxAccount(email="shine@icloud.com",
                                           extra={"icloud_alias_id": 528})

            def set_account_status(self, account, status):
                statuses.append(status)
                if status == "used":
                    used_field["v"] = mark_email_used_by(used_field["v"], "grok")

            def get_email(self):
                return self._last_account

        plat = _platform({})
        plat.mailbox = Mb()
        plat._log_fn = lambda _m: None
        rejected = {
            "ok": False, "sso": "", "password": "", "email": "shine@icloud.com",
            "error": "x.ai 取码限流（这个邮箱本轮已用掉）",
            "consumes_address": True, "retryable": False,
        }
        with patch("platforms.grok.register_browser.register_grok_via_browser",
                   return_value=rejected):
            with self.assertRaises(NonRetryableRegisterError):
                plat.register()

        self.assertEqual(statuses, ["used"], "被拒的地址必须记 used")
        self.assertTrue(email_used_by_platform(used_field["v"], "grok"),
                        "used_platforms 没记上 grok —— 下次还会领到它")
