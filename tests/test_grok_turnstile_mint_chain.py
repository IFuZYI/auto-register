"""Grok Turnstile mint 的兜底链与 solver 轮询测试。

`turnstile_mint.py` 是 Grok 注册的守门环节：拿不到 Turnstile token 整条
注册就进不去。三条路线（captcha → 屏外 Chrome → Solver 服务）的**回退
顺序**与「哪个错误该重试/该放弃」直接决定用户看到的是「换条路继续」还是
「任务失败」。已有测试覆盖了 mint_via_captcha 与屏外超时的凭据脱敏，
这里补：统一入口的回退链、solver 服务轮询、snap 包装识别。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch
from unittest import mock

import platforms.grok.turnstile_mint as mint_mod


class MintTurnstileFallbackChainTests(unittest.TestCase):
    def test_captcha_success_short_circuits(self):
        """captcha 可用时不再尝试其它路线。"""
        captcha = mock.MagicMock()
        captcha.solve_turnstile.return_value = "token-1234567890abc"

        with (
            mock.patch.object(mint_mod, "mint_via_offscreen_chrome") as offscreen,
            mock.patch.object(mint_mod, "mint_via_solver_service") as solver,
        ):
            token = mint_mod.mint_turnstile(
                "site-key", "https://page", captcha=captcha
            )

        self.assertEqual(token, "token-1234567890abc")
        offscreen.assert_not_called()
        solver.assert_not_called()

    def test_falls_back_to_offscreen_when_captcha_fails(self):
        captcha = mock.MagicMock()
        captcha.solve_turnstile.side_effect = RuntimeError("solver down")

        with (
            mock.patch.object(mint_mod, "mint_via_offscreen_chrome", return_value="token-abc1234567890") as offscreen,
            mock.patch.object(mint_mod, "mint_via_solver_service") as solver,
        ):
            token = mint_mod.mint_turnstile("site-key", "https://page", captcha=captcha)

        self.assertEqual(token, "token-abc1234567890")
        offscreen.assert_called_once()
        solver.assert_not_called()

    def test_falls_back_to_solver_service_when_offscreen_fails(self):
        captcha = mock.MagicMock()
        captcha.solve_turnstile.side_effect = RuntimeError("solver down")

        with (
            mock.patch.object(mint_mod, "mint_via_offscreen_chrome", side_effect=RuntimeError("chrome missing")),
            mock.patch.object(mint_mod, "mint_via_solver_service", return_value="token-final12345") as solver,
        ):
            token = mint_mod.mint_turnstile("site-key", "https://page", captcha=captcha)

        self.assertEqual(token, "token-final12345")
        solver.assert_called_once()

    def test_all_failures_raise_with_each_reason(self):
        """全失败时错误要带每条路线的失败原因（排查时一眼看出卡在哪）。"""
        captcha = mock.MagicMock()
        captcha.solve_turnstile.side_effect = RuntimeError("captcha boom")

        with (
            mock.patch.object(mint_mod, "mint_via_offscreen_chrome", side_effect=RuntimeError("chrome boom")),
            mock.patch.object(mint_mod, "mint_via_solver_service", side_effect=RuntimeError("solver boom")),
        ):
            with self.assertRaises(RuntimeError) as ctx:
                mint_mod.mint_turnstile("site-key", "https://page", captcha=captcha)

        message = str(ctx.exception)
        self.assertIn("captcha boom", message)
        self.assertIn("chrome boom", message)
        self.assertIn("solver boom", message)

    def test_prefer_puts_a_route_first(self):
        """prefer 指定的路线先跑；成功即返回。"""
        with (
            mock.patch.object(mint_mod, "mint_via_solver_service", return_value="token-solver12345") as solver,
            mock.patch.object(mint_mod, "mint_via_offscreen_chrome") as offscreen,
        ):
            token = mint_mod.mint_turnstile(
                "site-key", "https://page", prefer="solver"
            )

        self.assertEqual(token, "token-solver12345")
        solver.assert_called_once()
        offscreen.assert_not_called()

    def test_no_captcha_skips_captcha_route(self):
        """没传 captcha 时 captcha 路线直接跳过（不报错）。"""
        with (
            mock.patch.object(mint_mod, "mint_via_offscreen_chrome", return_value="token-abc1234567890"),
        ):
            token = mint_mod.mint_turnstile("site-key", "https://page", captcha=None)

        self.assertEqual(token, "token-abc1234567890")


class MintViaSolverServiceTests(unittest.TestCase):
    """外部 solver 服务的轮询：ready 返回 / fail 抛出 / 超时 TimeoutError。"""

    def _fake_response(self, payload, status_code=200):
        response = mock.MagicMock()
        response.status_code = status_code
        response.json.return_value = payload
        response.text = str(payload)
        return response

    def test_ready_token_is_returned(self):
        created = self._fake_response({"taskId": "task-1"})
        pending = self._fake_response({"status": "pending"})
        ready = self._fake_response({"status": "ready", "solution": {"token": "tok-ready-123456"}})

        with (
            mock.patch("requests.get", side_effect=[created, pending, ready]),
            mock.patch.object(mint_mod.time, "sleep"),
        ):
            token = mint_mod.mint_via_solver_service("site-key", "https://page")

        self.assertEqual(token, "tok-ready-123456")

    def test_fail_status_raises_immediately(self):
        created = self._fake_response({"taskId": "task-1"})
        failed = self._fake_response({"status": "CAPTCHA_FAIL"})

        with (
            mock.patch("requests.get", side_effect=[created, failed]),
            mock.patch.object(mint_mod.time, "sleep"),
        ):
            with self.assertRaises(RuntimeError):
                mint_mod.mint_via_solver_service("site-key", "https://page")

    def test_missing_task_id_raises(self):
        created = self._fake_response({"nope": True})

        with mock.patch("requests.get", return_value=created):
            with self.assertRaises(RuntimeError) as ctx:
                mint_mod.mint_via_solver_service("site-key", "https://page")

        self.assertIn("taskId", str(ctx.exception))

    def test_timeout_raises_timeout_error(self):
        """deadline 过后仍未 ready → TimeoutError（调用方据此走下一路线）。"""
        created = self._fake_response({"taskId": "task-1"})
        pending = self._fake_response({"status": "pending"})

        clock = {"t": 1000.0}

        def _now():
            clock["t"] += 100  # 每查一次时间就跳 100 秒 → 立即过期
            return clock["t"]

        with (
            mock.patch("requests.get", side_effect=[created] + [pending] * 50),
            mock.patch.object(mint_mod.time, "sleep"),
            mock.patch.object(mint_mod.time, "time", side_effect=_now),
        ):
            with self.assertRaises(TimeoutError):
                mint_mod.mint_via_solver_service("site-key", "https://page", timeout=30)

    def test_url_defaults_to_local_solver_port(self):
        """默认地址必须指向本项目 solver 端口（8889），不能是参考项目的 5072。"""
        created = self._fake_response({"taskId": "task-1"})
        ready = self._fake_response({"status": "ready", "solution": {"token": "tok-ready-123456"}})

        with (
            mock.patch("requests.get", side_effect=[created, ready]) as get,
            mock.patch.object(mint_mod.time, "sleep"),
            mock.patch.dict("os.environ", {}, clear=True),
        ):
            mint_mod.mint_via_solver_service("site-key", "https://page")

        first_url = get.call_args_list[0].args[0]
        self.assertIn("8889", first_url)
        self.assertNotIn("5072", first_url)


class SnapWrapperDetectionTests(unittest.TestCase):
    """snap 转发脚本识别 —— 认错会让 root 下的浏览器启动报一句看不懂的错。"""

    def test_small_script_pointing_at_snap_is_detected(self, ):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            wrapper = Path(tmp) / "chromium-browser"
            wrapper.write_text("#!/bin/sh\nexec /snap/bin/chromium \"$@\"\n")
            self.assertTrue(mint_mod._is_snap_wrapper(str(wrapper)))

    def test_real_binary_is_not_snap_wrapper(self):
        """真实二进制（体积大 / 无 snap 字样）不被误判。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "chrome"
            binary.write_bytes(b"\x7fELF" + b"\x00" * 5000)
            self.assertFalse(mint_mod._is_snap_wrapper(str(binary)))

    def test_missing_path_is_not_snap_wrapper(self):
        self.assertFalse(mint_mod._is_snap_wrapper("/nonexistent/chrome"))


if __name__ == "__main__":
    unittest.main()


class OffscreenMintResourceGuardTests(unittest.TestCase):
    """铸币要串行 + 内存不足就跳过。

    事故（2026-11-02 18:25）：3 个 Grok 注册并发跑，每个都要起一个屏外 Chromium
    铸币，2GB 内存的机器直接把主进程打爆 —— 服务无 traceback 消失，日志停在
    「已进入邮箱注册视图」，任务全部变孤儿。
    """

    def test_low_memory_skips_without_spawning_chrome(self):
        from platforms.grok import turnstile_mint as tm

        with patch.object(tm, "_available_mb", return_value=10), patch.object(
            tm, "find_chrome", side_effect=AssertionError("内存不足时不该去找浏览器")
        ):
            with self.assertRaises(RuntimeError) as excinfo:
                tm.mint_via_offscreen_chrome("0xKEY", "https://accounts.x.ai/sign-up")
        self.assertIn("内存不足", str(excinfo.exception))

    def test_enough_memory_proceeds_to_locked_impl(self):
        from platforms.grok import turnstile_mint as tm

        calls = []
        with patch.object(tm, "_available_mb", return_value=9999), patch.object(
            tm, "find_chrome", return_value="/usr/bin/google-chrome"
        ), patch.object(
            tm, "_mint_offscreen_locked", side_effect=lambda *a, **k: calls.append(a) or "TOKEN"
        ), patch.object(tm, "_SCRIPT_PATH") as script:
            script.exists.return_value = True
            out = tm.mint_via_offscreen_chrome("0xKEY", "https://accounts.x.ai/sign-up")
        self.assertEqual(out, "TOKEN")
        self.assertEqual(len(calls), 1)

    def test_mint_is_serialised_across_threads(self):
        """两个线程同时铸币时，锁内的实现不得重叠执行。"""
        import threading as _threading
        import time as _time

        from platforms.grok import turnstile_mint as tm

        active = {"now": 0, "max": 0}
        guard = _threading.Lock()

        def fake_locked(*_a, **_k):
            with guard:
                active["now"] += 1
                active["max"] = max(active["max"], active["now"])
            _time.sleep(0.15)
            with guard:
                active["now"] -= 1
            return "TOKEN"

        with patch.object(tm, "_available_mb", return_value=9999), patch.object(
            tm, "find_chrome", return_value="/usr/bin/google-chrome"
        ), patch.object(tm, "_mint_offscreen_locked", side_effect=fake_locked), patch.object(
            tm, "_SCRIPT_PATH"
        ) as script:
            script.exists.return_value = True
            threads = [
                _threading.Thread(
                    target=tm.mint_via_offscreen_chrome,
                    args=("0xKEY", "https://accounts.x.ai/sign-up"),
                )
                for _ in range(3)
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join()

        self.assertEqual(active["max"], 1, "铸币必须串行（同时起多个 Chromium 会 OOM）")


class MemoryGateMustRunInsideTheMintLockTests(unittest.TestCase):
    """内存闸门必须在**持锁之后**判定。

    2026-11-04 独立审查（切片 A1）：`mint_via_offscreen_chrome` 先读
    `_available_mb()` 判断「内存够不够」，**然后**才 `with _MINT_LOCK`。于是 N 个
    并发注册在锁外**同时**看到「内存够」全部通过闸门，接着排队进锁逐个起
    Chromium —— 闸门保护的正是「同时起几个 Chromium」，锁外判定等于没判定
    （2026-11-02 18:25 那次 2GB 机器被 3 个并发铸币打爆，就是这个形状）。

    这里不靠线程调度（那要靠运气），而是用一个记录「何时被持有」的假锁 +
    一个记录「调用时锁是否被持有」的假内存探针，**确定性地**断言顺序。
    """

    class _Lock:
        def __init__(self):
            self.held = False

        def __enter__(self):
            self.held = True
            return self

        def __exit__(self, *exc):
            self.held = False
            return False

    class _ScriptPath:
        def exists(self):
            return True

    def test_memory_is_probed_while_the_lock_is_held(self):
        from platforms.grok import turnstile_mint as tm

        lock = self._Lock()
        probes: list[bool] = []

        def _probe():
            probes.append(lock.held)
            return 10**6  # 内存充足，放行到下一关

        def _stop(*_a, **_k):
            raise RuntimeError("到这里就够了（只想看顺序）")

        with patch.object(tm, "_MINT_LOCK", lock), \
                patch.object(tm, "_SCRIPT_PATH", self._ScriptPath()), \
                patch.object(tm, "_available_mb", _probe), \
                patch.object(tm, "find_chrome", lambda: "/usr/bin/chrome"), \
                patch.object(tm, "_mint_offscreen_locked", _stop):
            with self.assertRaises(RuntimeError):
                tm.mint_via_offscreen_chrome("site-key", "https://example.com/page")

        self.assertEqual(probes, [True], "内存闸门必须在**持锁之后**判定，否则并发全都能过")

    def test_low_memory_still_refuses_inside_the_lock(self):
        """对照组：闸门本身还在 —— 内存不足时照样拒绝，且拒绝时已经放锁。"""
        from platforms.grok import turnstile_mint as tm

        lock = self._Lock()
        with patch.object(tm, "_MINT_LOCK", lock), \
                patch.object(tm, "_SCRIPT_PATH", self._ScriptPath()), \
                patch.object(tm, "_available_mb", lambda: 1), \
                patch.object(tm, "find_chrome", lambda: "/usr/bin/chrome"):
            with self.assertRaises(RuntimeError) as ctx:
                tm.mint_via_offscreen_chrome("site-key", "https://example.com/page")

        self.assertIn("内存不足", str(ctx.exception))
        self.assertFalse(lock.held, "抛异常时锁必须已经放掉，否则后续铸币全被卡死")
