"""OTP 提取的防误判（实测踩过的两个坑）。

背景（生产日志实录）：iCloud 隐私邮箱注册 ChatGPT，连续四次都读到验证码
`202609`，而邮件里的真码是 `406553` / `670101`。`202609` 是**主号邮箱**
`sample123456@icloud.com` 本地部分的数字段 —— 它出现在邮件头的转发标记里：

    X-ICLOUD-HME: p=alias.sample@icloud.com; d=; f=sample123456@icloud.com; ...

兜底正则从那里截出了 6 位数字。后果是每轮都拿同一个错码去验，第二次直接撞上
服务端的 `max_check_attempts` 把整轮锁死。

同类坑还有两个，一起钉住：
- HTML 里的 hex 色值 `color: #202123` 会被当成验证码（`(?<!#)` 挡掉）
- 紧贴字母数字的串（`u24681357`）会被截出 6 位（字母数字边界挡掉）
"""

from __future__ import annotations

import unittest

from core.mailboxes.base import BaseMailbox
from platforms.chatgpt.protocol.mail_provider import extract_otp


class _Probe(BaseMailbox):
    """只为拿到 `_safe_extract`（抽象方法给最小实现）。"""

    def get_email(self):  # pragma: no cover - 不调用
        return None

    def get_current_ids(self, account):  # pragma: no cover - 不调用
        return set()

    def wait_for_code(self, *args, **kwargs):  # pragma: no cover - 不调用
        return ""


class SafeExtractHeaderTests(unittest.TestCase):
    """邮件头里的转发标记不能污染验证码提取。"""

    def setUp(self):
        self.probe = _Probe()

    def test_forwarding_marker_email_does_not_win(self):
        """真码在正文、主号邮箱数字段在头部时，要取正文那个。

        这是生产日志里那次的原始形态（`f=sample123456@icloud.com`）。
        """
        text = (
            "X-ICLOUD-HME: p=alias.sample@icloud.com; d=; "
            "f=sample123456@icloud.com; r=to; s=otp@tm1.openai.com\r\n\r\n"
            "<p>Enter this temporary verification code to continue:</p>"
            "<p>406553</p>"
        )
        self.assertEqual(self.probe._safe_extract(text), "406553")

    def test_email_local_part_with_digits_is_ignored(self):
        """即使正文里没有真码，也不能把邮箱本地部分的数字当验证码。"""
        text = "From: noreply@x.com\nX-HME: f=sample123456@icloud.com\nno code here"
        self.assertNotEqual(self.probe._safe_extract(text), "123456")

    def test_hex_color_is_not_a_code(self):
        """HTML 的 `#202123` 不能被当成验证码。"""
        text = '<td style="color: #202123">Enter code</td>'
        self.assertNotEqual(self.probe._safe_extract(text), "202123")

    def test_digits_glued_to_letters_are_not_a_code(self):
        """`u24681357` 这类紧贴字母数字的串不能截出 6 位。"""
        text = "token u24681357 end"
        self.assertNotEqual(self.probe._safe_extract(text), "246813")

    def test_semantic_anchor_still_wins(self):
        """带语义的锚点仍优先 —— 别为了防误判把正常路径弄丢。"""
        text = "Your verification code is 123456. Ignore #654321."
        self.assertEqual(self.probe._safe_extract(text), "123456")

    def test_real_code_in_plain_text_is_found(self):
        text = "Enter this code: 987654"
        self.assertEqual(self.probe._safe_extract(text), "987654")


class ExtractorAgreementTests(unittest.TestCase):
    """两条提取路径必须给出同一个答案。

    实际运行走 `_safe_extract`（core.mailboxes.base），而 RT 补号走
    `extract_otp`（platforms.chatgpt.protocol.mail_provider）。两边判据不一致时，
    「注册能过、补 RT 读不到码」这种问题会非常难查。
    """

    SAMPLES = [
        # (说明, 文本, 期望)
        ("真码 + 主号邮箱在头部",
         "X-HME: f=sample123456@icloud.com\r\n<p>code 406553</p>", "406553"),
        ("HTML 色值干扰",
         '<p style="color: #202123">Enter this code: 111222</p>', "111222"),
        ("纯文本",
         "Your verification code is 333444", "333444"),
    ]

    def test_both_paths_agree(self):
        probe = _Probe()
        for label, text, expected in self.SAMPLES:
            with self.subTest(case=label):
                mine = probe._safe_extract(text)
                theirs = extract_otp(text)
                self.assertEqual(mine, expected, f"_safe_extract 取错了：{label}")
                self.assertEqual(
                    mine, theirs,
                    f"两条提取路径不一致（_safe_extract={mine}, extract_otp={theirs}）",
                )


class MailProviderInterfaceTests(unittest.TestCase):
    """`wait_for_otp` 的 `exclude_codes` 参数：所有实现都要认。"""

    def test_base_signature_declares_exclude_codes(self):
        import inspect

        from platforms.chatgpt.protocol.mail_provider import MailProvider

        params = inspect.signature(MailProvider.wait_for_otp).parameters
        self.assertIn("exclude_codes", params)
        self.assertIsNone(params["exclude_codes"].default)

    def test_icloud_local_respects_exclude_codes(self):
        """`ICloudLocalMailbox.wait_for_code` 要能跳过已试过的码。"""
        import inspect

        from modules.mail.icloud_local import ICloudLocalMailbox

        src = inspect.getsource(ICloudLocalMailbox.wait_for_code)
        self.assertIn("exclude_codes", src)

    def test_icloud_local_respects_the_time_window(self):
        """`otp_sent_at` 必须被尊重 —— 否则补发后会读到上一封信。

        实测日志：'OTP 已重发' 4 秒后又读到同一个码。
        """
        import inspect

        from modules.mail.icloud_local import ICloudLocalMailbox

        src = inspect.getsource(ICloudLocalMailbox.wait_for_code)
        self.assertIn("otp_sent_at", src)
        self.assertIn("cutoff", src)


if __name__ == "__main__":
    unittest.main()
