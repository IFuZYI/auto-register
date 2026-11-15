"""基线降级：读不到初始邮件时必须让上层知道，而不是假装「收件箱是空的」。

问题
----
`get_current_ids()` 在首读失败时 `return set()` 并只打一行「忽略」。看起来
无害，实际后果很危险：`wait_for_code` 会把 `before_ids` 当成「注册前就存在
的信」，而空集意味着**收件箱里现有的每一封信都算新到的**。

于是服务抖动一下就会发生：

- 收件箱里躺着上一位用户的验证码 → 被当成本轮的码去填 → 注册失败；
- 或者更糟，用了别人的/过期的码，让账号状态变得难以解释。

正确做法：失败时保留「基线不可信」这个事实，让上层用**时间窗**
（`otp_sent_at`）兜底，而不是拿空集当基线。
"""
from __future__ import annotations

import unittest


class BaselineDegradedFlagTests(unittest.TestCase):
    """两个 iCloud 渠道都要暴露 `_baseline_degraded`。"""

    def _box(self, module_path, class_name, log_name):
        import importlib

        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
        box = object.__new__(cls)
        box._baseline_degraded = False
        return box

    def test_local_channel_flags_degraded_when_first_read_fails(self):
        from modules.mail.icloud_local import ICloudLocalMailbox
        from core.mailboxes.base import MailboxAccount

        box = ICloudLocalMailbox.__new__(ICloudLocalMailbox)
        box._baseline_degraded = False
        box._task_control = None
        box._log_fn = None
        box._extra = {}
        logs: list[str] = []
        box._log = logs.append

        def boom(*args, **kwargs):
            raise RuntimeError("IMAP 挂了")

        box._messages = boom
        box._resolve = lambda account: (1, "alias@icloud.com")

        ids = box.get_current_ids(MailboxAccount(email="alias@icloud.com"))
        self.assertEqual(ids, set())
        self.assertTrue(
            box._baseline_degraded,
            "首读失败后没有标记基线降级 —— 上层会把历史邮件当新邮件",
        )
        self.assertTrue(
            any("基线" in line or "降级" in line for line in logs),
            f"日志没有说清后果: {logs}",
        )

    def test_remote_channel_flags_degraded_when_first_read_fails(self):
        from modules.mail.icloud_remote import ICloudRemoteMailbox
        from core.mailboxes.base import MailboxAccount

        box = ICloudRemoteMailbox.__new__(ICloudRemoteMailbox)
        box._baseline_degraded = False
        box._task_control = None
        box._log_fn = None
        box._extra = {}
        logs: list[str] = []
        box._log = logs.append

        def boom(*args, **kwargs):
            raise RuntimeError("远程服务不可达")

        box._messages = boom
        box._resolve = lambda account: (1, "alias@icloud.com")

        ids = box.get_current_ids(MailboxAccount(email="alias@icloud.com"))
        self.assertEqual(ids, set())
        self.assertTrue(box._baseline_degraded)

    def test_successful_read_clears_the_flag(self):
        """读到之后要把标记清掉 —— 否则一次抖动会永久降级。"""
        from modules.mail.icloud_local import ICloudLocalMailbox
        from core.mailboxes.base import MailboxAccount

        box = ICloudLocalMailbox.__new__(ICloudLocalMailbox)
        box._baseline_degraded = True
        box._task_control = None
        box._log_fn = None
        box._extra = {}
        box._log = lambda msg: None
        box._messages = lambda account, limit=50: [
            type("M", (), {"provider_message_id": "id-1"})()
        ]
        box._resolve = lambda account: (1, "alias@icloud.com")

        ids = box.get_current_ids(MailboxAccount(email="alias@icloud.com"))
        self.assertEqual(ids, {"id-1"})
        self.assertFalse(box._baseline_degraded, "读到邮件后没清掉降级标记")


class DegradedBaselineDoesNotGuessTests(unittest.TestCase):
    """基线不可信又没时间窗时，不能把收件箱里的旧信当成本轮的码。"""

    def _box_with(self, mailbox_cls, messages):
        from core.mailboxes.base import MailboxAccount

        box = mailbox_cls.__new__(mailbox_cls)
        box._baseline_degraded = True          # 首读失败过
        box._task_control = None
        box._log_fn = None
        box._extra = {}
        box._alias_id = None
        box._account_id = ""
        logs: list[str] = []
        box._log = logs.append
        box._messages = lambda account, limit=50: messages
        box._resolve = lambda account: (1, "alias@icloud.com")
        return box, logs, MailboxAccount(email="alias@icloud.com")

    def test_no_time_window_means_no_code_is_returned(self):
        from modules.mail.icloud_local import ICloudLocalMailbox

        stale = type(
            "M",
            (),
            {
                "provider_message_id": "old-1",
                "received_at": None,
                "subject": "验证码",
                "preview": "你的验证码是 123456",
                "body": "你的验证码是 123456",
            },
        )()
        box, logs, account = self._box_with(ICloudLocalMailbox, [stale])
        box._message_text = lambda message: "你的验证码是 123456"

        with self.assertRaises(TimeoutError):
            box.wait_for_code(account, timeout=1, otp_sent_at=None)

        self.assertTrue(
            any("基线不可信" in line for line in logs),
            f"日志没有说明为什么没猜码: {logs[:5]}",
        )


if __name__ == "__main__":
    unittest.main()