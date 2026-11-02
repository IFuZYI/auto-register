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
        """默认关的是**可注册性**预检（打真实发码接口那层），连通性层不受它影响。"""
        from services.task_runners import _pick_usable_exit

        called = []
        with patch("platforms.grok.exit_probe.probe_exit", lambda p: called.append(p) or {"ok": False}), \
                patch("platforms.grok.exit_probe.probe_reachable",
                      lambda p, **kw: {"ok": True, "detail": "出口连通（HTTP 200）", "status": "200"}):
            out = _pick_usable_exit(
                "p0", task_id="t", extra={}, log=lambda *a: None,
                get_next=lambda: "p1", report_bad=lambda p: None, normalize=lambda v: v,
            )
        self.assertEqual(out, "p0")
        self.assertEqual(called, [], "默认不该打真实发码接口（那层信号归属有争议）")


class ProbeReachableTests(unittest.TestCase):
    """连通性检查：传输层失败＝出口确定坏，HTTP 4xx/5xx 一律放行。

    背景（2026-11-02 实测）：两条 `9.142.*` 出口周期性地整条连不上 —— curl 回
    `000`，同一时刻浏览器里就是 `net::ERR_CONNECTION_CLOSED`，Turnstile 铸币
    三次全挂、白烧一个别名加两三分钟。而 `accounts.x.ai` 对裸 curl 回
    Cloudflare 403 是**常态**（浏览器能过挑战），不能据此摘出口。
    """

    def test_http_200_is_reachable(self):
        out = exit_probe.probe_reachable(
            "socks5h://u:p@1.2.3.4:1080",
            runner=lambda *a, **k: _proc("200 0.42"),
        )
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "200")

    def test_slow_exit_is_rejected(self):
        """curl 都慢到 12 秒的出口，浏览器一定 `Page.goto` 超时（实测两轮都栽）。"""
        out = exit_probe.probe_reachable(
            "socks5h://u:p@1.2.3.4:1080",
            runner=lambda *a, **k: _proc("200 12.5"),
        )
        self.assertFalse(out["ok"])
        self.assertIn("太慢", out["detail"])

    def test_status_without_timing_is_treated_as_fast_enough(self):
        """老式 `-w %{http_code}` 输出（没有耗时）不能因为解析不出时间就判死。"""
        out = exit_probe.probe_reachable(
            "socks5h://u:p@1.2.3.4:1080",
            runner=lambda *a, **k: _proc("200"),
        )
        self.assertTrue(out["ok"])

    def test_http_403_is_still_reachable(self):
        """Cloudflare 403 是挑战页，不是出口坏 —— 误杀会摘掉好出口。"""
        out = exit_probe.probe_reachable(
            "socks5h://u:p@1.2.3.4:1080",
            runner=lambda *a, **k: _proc("403"),
        )
        self.assertTrue(out["ok"], "4xx 不能判死")

    def test_http_000_is_unreachable(self):
        out = exit_probe.probe_reachable(
            "socks5h://u:p@1.2.3.4:1080",
            runner=lambda *a, **k: _proc("000", code=7),
        )
        self.assertFalse(out["ok"], "连不上就是死出口")
        self.assertFalse(out["skipped"])

    def test_empty_status_with_zero_exit_code_is_unreachable(self):
        out = exit_probe.probe_reachable(
            "socks5h://u:p@1.2.3.4:1080",
            runner=lambda *a, **k: _proc(""),
        )
        self.assertFalse(out["ok"])

    def test_timeout_is_unreachable(self):
        def _boom(*a, **k):
            raise subprocess.TimeoutExpired("curl", 17)

        out = exit_probe.probe_reachable("socks5h://u:p@1.2.3.4:1080", runner=_boom)
        self.assertFalse(out["ok"], "超时的出口同样不能用（就是要避免白烧别名）")

    def test_no_proxy_is_a_skip(self):
        out = exit_probe.probe_reachable("")
        self.assertTrue(out["ok"])
        self.assertTrue(out["skipped"])


class PickUsableExitTests(unittest.TestCase):
    """`_pick_usable_exit` 的连通性层：**不依赖** `grok_exit_preflight` 开关。"""

    def _call(self, *, reach_results, extra=None):
        from services import task_runners

        logs: list[str] = []
        bads: list[str] = []
        counter = {"n": 0}
        popped: list[str] = []

        def _next() -> str:
            counter["n"] += 1
            value = f"socks5h://cand{counter['n']}"
            popped.append(value)
            return value

        with patch.object(
            exit_probe, "probe_reachable", side_effect=reach_results
        ):
            out = task_runners._pick_usable_exit(
                "socks5h://bad0",
                task_id="t",
                extra=dict(extra or {}),
                log=lambda _tid, msg: logs.append(msg),
                get_next=_next,
                report_bad=bads.append,
                normalize=lambda value: value,
            )
        return out, logs, bads

    def test_dead_exit_is_rotated_even_with_preflight_off(self):
        out, logs, bads = self._call(
            reach_results=[
                {"ok": False, "detail": "出口连不上（curl rc=7 status=000）"},
                {"ok": True, "detail": "出口连通（HTTP 200）", "status": "200"},
            ]
        )
        self.assertEqual(out, "socks5h://cand1")
        self.assertEqual(bads, ["socks5h://bad0"], "死出口必须被摘掉")
        self.assertTrue(any("连通性检查" in line for line in logs), logs)

    def test_all_exits_dead_gives_up_instead_of_using_unverified_one(self):
        out, logs, _bads = self._call(
            reach_results=[{"ok": False, "detail": "连不上"}] * 5,
            extra={"grok_exit_preflight": "0"},
        )
        self.assertEqual(out, "", "全连不上时本轮放弃，别拿未验证的出口烧别名")
        self.assertTrue(any("本轮放弃" in line for line in logs), logs)


class NoUsableExitGuardTests(unittest.TestCase):
    """出口全被否时**不许退化成直连**。

    本机直连 x.ai 一样被拒（`accounts.x.ai` 对本机 IP 回 Cloudflare 403），
    只把「明确的出口问题」换成「看不懂的失败」，还白烧一个别名。
    """

    def test_register_flow_gives_up_with_a_clear_reason(self):
        import inspect

        from services import task_runners

        src = inspect.getsource(task_runners._run_register)
        self.assertIn("没有可用出口（连通性/延迟检查全部未通过），本轮放弃", src)
        self.assertIn("没有可用出口（连通性检查未通过）", src)
