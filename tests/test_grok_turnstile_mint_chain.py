"""Grok Turnstile mint 的兜底链与 solver 轮询测试。

`turnstile_mint.py` 是 Grok 注册的守门环节：拿不到 Turnstile token 整条
注册就进不去。三条路线（captcha → 屏外 Chrome → Solver 服务）的**回退
顺序**与「哪个错误该重试/该放弃」直接决定用户看到的是「换条路继续」还是
「任务失败」。已有测试覆盖了 mint_via_captcha 与屏外超时的凭据脱敏，
这里补：统一入口的回退链、solver 服务轮询、snap 包装识别。
"""

from __future__ import annotations

import unittest
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
