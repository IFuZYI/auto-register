"""浏览器收尾必须**有超时**：`Browser.close()` 在浏览器进程已死时会永久空转。

## 事故（2026-11-03 08:59，线上，已用 py-spy 抓到栈）

一个 Grok 任务在第 3/5 个账号跑完流程后**静默卡了 20 分钟**，整批停摆、
连「停止任务」都不响应。py-spy dump：

```
Thread 182950 (active+gil): "ThreadPoolExecutor-0_0"
    SyncBase._sync (playwright/_impl/_sync_base.py:113)
    Browser.close (playwright/sync_api/_generated.py:16425)
    Camoufox.__exit__ (camoufox/sync_api.py:58)
    register_grok_via_browser (platforms/grok/register_browser.py:980)
```

同时 `pgrep -P <main.py>` 是**空的**：浏览器/driver 进程早就不在了（那台机器
2GB 内存，`dmesg` 里能看到 chrome 被 OOM 杀掉）。但 `_sync()` 里的
`while not task.done(): self._dispatcher_fiber.switch()` 永远不会退出 ——
它等的那个 driver 回应永远不来，于是 worker 线程 100% CPU 空转到底。

`Camoufox.__exit__` 自己 catch 不住这种情况（它只 try/finally 保证 base teardown
被调用，等的是**上层**能返回）。

## 修法

把 `with Camoufox(...)` 换成显式 `_open_browser()` + `finally: _close_browser(...)`：

- `_close_browser` 先看 `browser.is_connected()`；已断开就**根本不调 close**
  （这是零成本、最可靠的那条路）。
- 已连接也包一层 `threading.Thread` + `join(timeout)`：close 超时就**放弃等待**、
  记一条日志、把浏览器对象丢掉（进程由 OS 回收，泄漏一个死进程远比卡死整批好）。

代价是每次正常收尾多一个线程对象（毫秒级），收益是「一个号收尾失败」不再能
拖死整批任务。
"""
from __future__ import annotations

import inspect
import unittest
from unittest.mock import patch

from platforms.grok import register_browser as rb


class CloseBrowserTimeoutTests(unittest.TestCase):
    def test_close_is_not_called_when_browser_already_disconnected(self):
        """浏览器已断开就别调 close —— 正是这条会永久空转。"""
        calls = []

        class _Browser:
            def is_connected(self):
                return False

            def close(self):  # pragma: no cover - 不该被调用
                calls.append("close")

        rb._close_browser(_Browser(), log=lambda m: calls.append(m), timeout=0.05)
        self.assertNotIn("close", calls)

    def test_close_happens_when_still_connected(self):
        calls = []

        class _Browser:
            def is_connected(self):
                return True

            def close(self):
                calls.append("close")

        rb._close_browser(_Browser(), log=lambda m: calls.append(m), timeout=0.05)
        self.assertIn("close", calls)

    def test_hung_close_is_abandoned_instead_of_blocking_forever(self):
        """核心回归：close 永不返回时，必须在 timeout 内返回并告警。"""
        import time

        class _Browser:
            def is_connected(self):
                return True

            def close(self):
                time.sleep(30)  # 模拟 driver 已死、回应永不到来

        started = time.monotonic()
        logs = []
        rb._close_browser(_Browser(), log=logs.append, timeout=0.2)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, f"close 卡了 {elapsed:.1f}s，应当已被放弃")
        self.assertTrue(any("超时" in str(m) for m in logs), f"应当记一条超时告警: {logs}")

    def test_close_failure_does_not_raise(self):
        class _Browser:
            def is_connected(self):
                return True

            def close(self):
                raise RuntimeError("browser 已经崩了")

        # 不该抛：收尾路径绝不能再制造新的失败
        rb._close_browser(_Browser(), log=lambda m: None, timeout=0.05)


class RegisterUsesGuardedCloseTests(unittest.TestCase):
    """源码级钉住：注册主链必须走带超时的收尾，不能再用裸 `with Camoufox()`。

    裸 with 的 __exit__ 就是事故现场那一帧；只要有人改回去，这条就红。
    """

    def test_register_does_not_use_bare_camoufox_context_manager(self):
        """裸 `with Camoufox(...)` 的 __exit__ 就是事故现场那一帧 —— 不许改回去。

        注意：注释里会提到 `with Camoufox(...)`（解释为什么不用它），所以这里
        只看**代码行**（去掉注释与空行后逐行比对），不然会误伤。
        """
        src = inspect.getsource(rb.register_grok_via_browser)
        code_lines = [
            line.split("#", 1)[0].strip()
            for line in src.splitlines()
        ]
        self.assertFalse(
            [ln for ln in code_lines if ln.startswith("with Camoufox(")],
            "注册主链不能再用裸 with Camoufox(...)",
        )
        self.assertIn("camoufox_cm.__enter__()", src)
        self.assertIn("_close_browser(", src)

    def test_close_browser_is_called_in_a_finally_block(self):
        src = inspect.getsource(rb.register_grok_via_browser)
        self.assertIn("finally:", src)


class CamoufoxExitMustNotRecloseTests(unittest.TestCase):
    """Camoufox.__exit__ 会再 close 一次 —— 必须先摘掉 browser 引用。

    2026-11-03 09:53 二次踩到：`_close_browser` 超时收尾之后，
    `camoufox_cm.__exit__()` 又调 `self.browser.close()`，同一个 driver 死锁，
    py-spy 栈还是 `SyncBase._sync → Browser.close → Camoufox.__exit__`。
    """

    def test_browser_reference_is_cleared_before_context_exit(self):
        src = inspect.getsource(rb.register_grok_via_browser)
        self.assertIn("camoufox_cm.browser = None", src)
        self.assertLess(
            src.index("camoufox_cm.browser = None"),
            src.index("camoufox_cm.__exit__"),
            "必须先摘引用再调 __exit__",
        )


class SuccessPathMustReturnResultTests(unittest.TestCase):
    """成功路径必须真的返回 result —— 返回 None 会把成功记成失败。

    2026-11-03 线上事故：把 `with` 改成显式 open/close 时，成功分支丢掉了
    末尾统一的 `return result` → 函数返回 None → 插件里 `result.get("ok")`
    抛 `'NoneType' object has no attribute 'get'`，日志里明明有
    「注册成功，SSO len=152」，任务却记失败。
    """

    def _fake_result(self):
        return {"ok": True, "sso": "x" * 152, "password": "pw", "email": "a@b.c", "error": ""}

    def test_success_returns_the_result_dict_not_none(self):
        from unittest.mock import MagicMock

        rb_orig = rb.register_grok_via_browser
        # 直接驱动真正的函数体：把浏览器/页面全部替成桩
        import platforms.grok.signup_pages as sp

        page = MagicMock()
        page.url = "https://accounts.x.ai/sign-up"
        browser = MagicMock()
        browser.new_page.return_value = page
        browser.is_connected.return_value = False  # 跳过 close，避免真起浏览器
        cm = MagicMock()
        cm.__enter__.return_value = browser

        with patch.object(rb, "_open_entry_page", return_value=True), \
             patch.object(rb, "_send_code_stage", return_value={"ok": True, "sent_at": 1.0}), \
             patch.object(rb, "_read_code", return_value="ABC-123"), \
             patch.object(rb, "_profile_stage", return_value={"ok": True, "error": "", "retryable": False, "consumes_address": False, "turnstile_blocked": False}), \
             patch.object(rb, "_wait_for_sso_cookie", return_value="y" * 152), \
             patch.object(sp, "wait_for_code_or_profile", return_value="code"), \
             patch.object(sp, "enter_code", return_value=True), \
             patch.object(sp, "wait_for_profile_form", return_value=True), \
             patch.object(sp, "open_email_form", return_value=True), \
             patch.object(rb, "_dismiss_cookie_banner"), \
             patch.object(rb, "_resolve_exit_ip", return_value=""), \
             patch("camoufox.sync_api.Camoufox", return_value=cm):
            out = rb.register_grok_via_browser("a@b.c", alias_id=1)

        self.assertIsNotNone(out, "成功路径不能返回 None（会把成功记成失败）")
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(len(out.get("sso") or ""), 152)
