"""出口预检：Grok 注册前先问一句「这个出口能不能注册」。

背景（线上实测 2026-11-01）：两轮注册都在提交邮箱后拿到
`400 {"code":"account:email-signup-unavailable"}` —— 请求确实发出去了（上一版
修好了点击），但上游按**出口 IP 声誉**拒绝。而原来的顺序是「先领 iCloud 别名 →
起浏览器 → 填表 → 提交 → 才知道出口不行」，一次白烧一个别名 + 半分钟。

预检用同一个接口 + 一个永远收不到信的地址（`@example.invalid`，RFC 2606 保留），
在**领别名之前**就问出来。
"""
from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from platforms.grok import exit_probe


def _proc(stdout: str, code: int = 0):
    p = MagicMock()
    p.stdout = stdout
    p.returncode = code
    return p


class ProbeExitTests(unittest.TestCase):
    def test_unavailable_code_means_bad_exit(self):
        body = '{"code":"account:email-signup-unavailable"}'
        out = exit_probe.probe_exit(
            "socks5h://u:p@1.2.3.4:1080",
            runner=lambda *a, **k: _proc(body + "\n__STATUS__400"),
        )
        self.assertFalse(out["ok"], "被拒必须如实说不能用")
        self.assertEqual(out["code"], exit_probe.UNAVAILABLE_CODE)

    def test_other_errors_are_fail_open(self):
        """别的错误码**不能**证明出口坏 —— 预检只认那一个码。"""
        for body in ('{"code":"email:invalid"}', '{"code":"email:send-validation-code-rate-limited"}',
                     '{"ok":true}', 'not json at all'):
            out = exit_probe.probe_exit(
                "socks5h://u:p@1.2.3.4:1080",
                runner=lambda *a, _b=body, **k: _proc(_b + "\n__STATUS__200"),
            )
            self.assertTrue(out["ok"], f"{body} 不该被判成坏出口")

    def test_plain_text_rejection_still_counts(self):
        """上游偶尔回纯文本，不含 JSON —— 也要认出来。"""
        out = exit_probe.probe_exit(
            "socks5h://u:p@1.2.3.4:1080",
            runner=lambda *a, **k: _proc("account:email-signup-unavailable\n__STATUS__400"),
        )
        self.assertFalse(out["ok"])

    def test_probe_failure_is_fail_open(self):
        """预检自己炸了（超时 / 网络错）不能把注册挡住。"""
        def _boom(*a, **k):
            raise subprocess.TimeoutExpired("curl", 20)

        out = exit_probe.probe_exit("socks5h://u:p@1.2.3.4:1080", runner=_boom)
        self.assertTrue(out["ok"])
        self.assertTrue(out["skipped"])

    def test_no_proxy_is_skipped(self):
        out = exit_probe.probe_exit("")
        self.assertTrue(out["ok"])
        self.assertTrue(out["skipped"])

    def test_uses_the_same_endpoint_as_registration(self):
        """预检必须打**注册那个接口**，换个接口探出来的结论就不作数了。"""
        captured = {}

        def _run(cmd, **k):
            captured["cmd"] = cmd
            return _proc('{"ok":true}\n__STATUS__200')

        exit_probe.probe_exit("socks5h://u:p@1.2.3.4:1080", runner=_run)
        cmd = captured["cmd"]
        self.assertIn(exit_probe.SEND_CODE_URL, cmd)
        self.assertIn("socks5h://u:p@1.2.3.4:1080", cmd)
        # 地址必须是永远收不到信的那个，不能拿真地址去探
        self.assertTrue(any(exit_probe.PROBE_ADDRESS in str(x) for x in cmd))
        self.assertTrue(exit_probe.PROBE_ADDRESS.endswith(".invalid"))


class PickUsableExitTests(unittest.TestCase):
    """注册前的换出口循环。"""

    def _pick(self, probes, nexts):
        from services.task_runners import _pick_usable_exit

        seq = list(probes)
        reported = []

        def _probe(_proxy):
            return seq.pop(0) if len(seq) > 1 else seq[0]

        with patch("platforms.grok.exit_probe.probe_exit", _probe):
            out = _pick_usable_exit(
                "p0",
                task_id="t",
                extra={"grok_exit_preflight": "1"},
                log=lambda *a: None,
                get_next=lambda: nexts.pop(0) if nexts else "px",
                report_bad=reported.append,
                normalize=lambda v: v,
            )
        return out, reported

    def test_returns_the_first_good_exit(self):
        out, reported = self._pick([{"ok": True}], [])
        self.assertEqual(out, "p0")
        self.assertEqual(reported, [], "好出口不该被摘")

    def test_skips_bad_exits_and_reports_them(self):
        out, reported = self._pick(
            [{"ok": False}, {"ok": False}, {"ok": True}],
            ["p1", "p2"],
        )
        self.assertEqual(out, "p2")
        self.assertEqual(reported, ["p0", "p1"], "坏出口要摘掉，否则下一轮又撞上")

    def test_gives_up_after_the_budget(self):
        out, reported = self._pick([{"ok": False}], ["p1", "p2", "p3", "p4", "p5", "p6"])
        self.assertEqual(out, "", "全都不行就返回空，让本轮直接放弃")
        self.assertEqual(len(reported), 5, "只试固定个数，不能无限换")

    def test_switch_off_returns_the_original_proxy(self):
        from services.task_runners import _pick_usable_exit

        called = []
        with patch("platforms.grok.exit_probe.probe_exit", lambda p: called.append(p) or {"ok": False}):
            out = _pick_usable_exit(
                "p0", task_id="t", extra={"grok_exit_preflight": "0"}, log=lambda *a: None,
                get_next=lambda: "p1", report_bad=lambda p: None, normalize=lambda v: v,
            )
        self.assertEqual(out, "p0")
        self.assertEqual(called, [], "关掉时不该发任何探测请求")


class PreflightDefaultTests(unittest.TestCase):
    """预检默认**关**。

    2026-11-01 更正：`email-signup-unavailable` 主要是**邮箱域名配额**（同一域名
    一个窗口 3~4 个），不是出口 IP。用 `@example.invalid` 探出口探的是另一条
    判定分支，结论不可信，还会把好出口误摘 —— 所以默认不开。
    """

    def test_default_is_off(self):
        from services.task_runners import _pick_usable_exit

        called = []
        with patch("platforms.grok.exit_probe.probe_exit", lambda p: called.append(p) or {"ok": False}):
            out = _pick_usable_exit(
                "p0", task_id="t", extra={}, log=lambda *a: None,
                get_next=lambda: "p1", report_bad=lambda p: None, normalize=lambda v: v,
            )
        self.assertEqual(out, "p0")
        self.assertEqual(called, [], "默认不该发任何探测请求")
