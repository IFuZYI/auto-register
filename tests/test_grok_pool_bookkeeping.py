"""Grok 号池记账回归测试。

背景（实测 bug）：`set_account_status` 之前**只有 ChatGPT 引擎调用**，
Grok 两条注册路径一个调用点都没有。后果是领过的 iCloud 别名永远停在
`in_use` —— 池子只出不进。一次失败任务实测留下 2 个死号。

这组测试钉住三条：
  1. 注册成功 → 记 `used`
  2. 注册失败（返回 ok=False）→ 记 `failed`（放回 available，不标死）
  3. 注册抛异常 → 也要记 `failed`（异常路径最容易漏）
"""

import unittest
from unittest.mock import MagicMock, patch

import platforms.grok.plugin as plugin_mod
from core.base_platform import AccountStatus, RegisterConfig


def _make_platform(extra=None, mailbox=None):
    extra = dict(extra or {"grok_register_mode": "browser"})
    # 执行器现在**就是**注册路径（browser/protocol），且优先于遗留的
    # `grok_register_mode`。fixture 从同一个意图里取执行器 —— 否则浏览器
    # 路径的测试会因为默认执行器而跑到协议路径上（真打网络，直接挂住）。
    executor = str(extra.get("grok_register_mode") or "browser")
    cfg = RegisterConfig(executor_type=executor, extra=extra)
    return plugin_mod.GrokPlatform(cfg, mailbox=mailbox)


class _RecordingMailbox:
    """记录 set_account_status 调用，并提供一个假账号对象。"""

    def __init__(self):
        self.calls = []
        self._account = MagicMock()
        self._account.email = "t@icloud.com"
        self._account.extra = {"icloud_alias_id": 42}

    def get_email(self):
        return self._account

    def wait_for_code(self, account, **kwargs):
        """协议路径会调它收码；这里给一个码让流程走到 signup 阶段。"""
        return "123-456"

    def set_account_status(self, account, status):
        self.calls.append(status)


class BrowserPathBookkeepingTests(unittest.TestCase):
    """浏览器路径的记账。"""

    def test_success_records_used(self):
        mb = _RecordingMailbox()
        plat = _make_platform(mailbox=mb)
        ok_result = {"ok": True, "sso": "sso-token", "password": "pw",
                     "email": "t@icloud.com", "error": ""}
        # 成功路径会去换 OAuth + 接入 grok2api —— 单测里都必须 mock，
        # 否则会打真实网络（表现为整个测试文件卡住）。
        with patch("platforms.grok.register_browser.register_grok_via_browser",
                   return_value=ok_result), \
             patch("platforms.grok.register_browser.exchange_oauth_via_browser",
                   return_value={"access_token": "at", "refresh_token": "rt",
                                 "expires_in": 3600}), \
             patch("platforms.grok.grok2api.Grok2ApiClient") as g2a_cls, \
             patch.object(plat, "_maybe_write_cpa", return_value=None):
            g2a_cls.from_config.return_value.configured = False
            plat.register()
        self.assertEqual(mb.calls, ["used"],
                         "成功必须记 used，否则别名永远停在 in_use")

    def test_failed_result_records_failed(self):
        mb = _RecordingMailbox()
        plat = _make_platform(mailbox=mb)
        bad = {"ok": False, "sso": "", "password": "", "email": "t@icloud.com",
               "error": "x.ai 取码限流（稍后再试）"}
        with patch("platforms.grok.register_browser.register_grok_via_browser",
                   return_value=bad):
            with self.assertRaises(RuntimeError):
                plat.register()
        self.assertEqual(mb.calls, ["failed"],
                         "失败必须记账，否则号卡在 in_use")

    def test_exception_also_records_failed(self):
        """异常路径最容易被漏掉 —— 这里专门钉住。"""
        mb = _RecordingMailbox()
        plat = _make_platform(mailbox=mb)
        with patch("platforms.grok.register_browser.register_grok_via_browser",
                   side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                plat.register()
        self.assertEqual(mb.calls, ["failed"],
                         "抛异常也要记账（finally 之外的路径）")

    def test_already_registered_marks_used_not_failed(self):
        """别名已消耗（x.ai 报「已有账号」）→ 标 used。

        标 failed 会把它放回 available，下次又被领出来撞同一堵墙 ——
        实测：`bleaker.gills7f@icloud.com` 就是这样被反复领出的。
        """
        mb = _RecordingMailbox()
        plat = _make_platform(mailbox=mb)
        already = {"ok": False, "sso": "", "password": "",
                   "email": "t@icloud.com",
                   "error": "该邮箱已有 x.ai 账号（别名已消耗）",
                   "already_registered": True}
        with patch("platforms.grok.register_browser.register_grok_via_browser",
                   return_value=already):
            with self.assertRaises(RuntimeError):
                plat.register()
        self.assertEqual(mb.calls, ["used"],
                         "已消耗的别名必须标 used，否则会被反复重领")

class RecordHelperTests(unittest.TestCase):
    """_record_mailbox_status 本身的契约。"""

    def test_ignores_mailbox_without_hook(self):
        plat = _make_platform()
        plat._record_mailbox_status(MagicMock(spec=[]), MagicMock(), "used", None)

    def test_swallows_hook_errors(self):
        """记账失败不能影响注册结果 —— 异常必须被吞。"""
        plat = _make_platform()
        mb = MagicMock()
        mb.set_account_status.side_effect = RuntimeError("db locked")
        logs = []
        plat._record_mailbox_status(mb, MagicMock(), "used", logs.append)
        self.assertTrue(logs, "吞异常时应该留下一条日志")

    def test_none_account_is_noop(self):
        plat = _make_platform()
        mb = _RecordingMailbox()
        plat._record_mailbox_status(mb, None, "used", None)
        self.assertEqual(mb.calls, [], "没有账号对象时不该调用钩子")


if __name__ == "__main__":
    unittest.main()
