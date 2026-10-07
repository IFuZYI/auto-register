"""TLS 瞬断重试会话 + solver 进程管理 + 浏览器运行时开关的单元测试。

三个模块都在关键链路上但此前只有零散覆盖：

- `platforms/chatgpt/protocol/http_client.py` 的 `_TlsRetrySession`：
  代理链路 5.4% 概率的 TLS 瞬断靠它原 session 重试恢复（实测 8/8 一次成功）。
  重试语义错一处就是「本该恢复的注册全挂」或「把服务端拒绝也重试成异常流量」。
- `services/solver_manager.py`：Turnstile solver 的拉起/停止与环境开关。
  这里错的表现是「solver 静默不启动，注册全部卡在验证码」。
- `core/browser_runtime.py` 的 `parse_env_bool` / `resolve_browser_headless`：
  有头/无头解析优先级错会让 Docker 里弹出「未检测到 DISPLAY」。

全部用假 session/假进程驱动，不碰网络。
"""

from __future__ import annotations

import logging
import unittest
from unittest import mock

from core.browser_runtime import parse_env_bool, resolve_browser_headless


# ────────────────────────── _TlsRetrySession ──────────────────────────


class _FakeInnerSession:
    """模拟真 session：可配置 get/post 的失败序列，其余属性普通透传。"""

    def __init__(self, failures=None):
        self.failures = list(failures or [])
        self.calls = []
        self.attr_reads = 0
        self._items = ["cookie-a", "cookie-b"]

    def __iter__(self):
        return iter(self._items)

    def _do(self, method, *args, **kwargs):
        self.calls.append((method, args, kwargs))
        if self.failures:
            exc = self.failures.pop(0)
            if exc is not None:
                raise exc
        return f"resp-{method}"

    def get(self, *args, **kwargs):
        return self._do("get", *args, **kwargs)

    def post(self, *args, **kwargs):
        return self._do("post", *args, **kwargs)

    def put(self, *args, **kwargs):
        return self._do("put", *args, **kwargs)

    # 透传验证用的普通属性
    trust_env = True


class TlsRetrySessionTests(unittest.TestCase):
    def _make(self, failures=None, retries=2, backoff=0):
        from platforms.chatgpt.protocol.http_client import _TlsRetrySession

        inner = _FakeInnerSession(failures)
        wrapped = _TlsRetrySession(inner, retries=retries, backoff=backoff)
        return wrapped, inner

    def test_success_passes_through(self):
        wrapped, inner = self._make()
        self.assertEqual(wrapped.get("https://x"), "resp-get")
        self.assertEqual(inner.calls[0][0], "get")

    def test_tls_error_is_retried_on_same_session(self):
        """TLS 瞬断（curl: (35)）→ 原 session 重试并恢复。"""
        err = RuntimeError("curl: (35) TLS connect error ... OPENSSL_internal")
        wrapped, inner = self._make(failures=[err])

        result = wrapped.get("https://x")

        self.assertEqual(result, "resp-get")
        self.assertEqual(len(inner.calls), 2, "应在同一个 inner session 上重试")

    def test_non_tls_error_is_not_retried(self):
        """服务端明确拒绝（如 409）不得重试 —— 重试只会更像异常流量。"""
        err = RuntimeError("HTTP 409 invalid_state")
        wrapped, inner = self._make(failures=[err])

        with self.assertRaises(RuntimeError):
            wrapped.get("https://x")
        self.assertEqual(len(inner.calls), 1, "非 TLS 错误不能重试")

    def test_retries_are_capped(self):
        """重试次数用尽后仍失败 → 原样抛出（不无限循环）。"""
        err = "curl: (35) TLS connect error"
        wrapped, inner = self._make(
            failures=[RuntimeError(err), RuntimeError(err), RuntimeError(err)], retries=2
        )

        with self.assertRaises(RuntimeError):
            wrapped.post("https://x")
        self.assertEqual(len(inner.calls), 3, "1 次原始 + 2 次重试")

    def test_post_and_put_also_retry(self):
        err = "sslerror handshake"
        for method in ("post", "put"):
            wrapped, inner = self._make(failures=[RuntimeError(err)])
            getattr(wrapped, method)("https://x")
            self.assertEqual(len(inner.calls), 2, f"{method} 也应重试")

    def test_other_attributes_pass_through(self):
        """cookies / trust_env 这类属性读写直达真 session（包装透明）。"""
        wrapped, inner = self._make()
        self.assertTrue(wrapped.trust_env)
        wrapped.trust_env = False
        self.assertFalse(inner.trust_env)
        self.assertEqual(list(iter(wrapped)), ["cookie-a", "cookie-b"])

    def test_url_kwarg_is_used_in_retry_log(self):
        """日志里要能看见重试的是哪个 URL（kwargs 形态也不丢）。"""
        err = RuntimeError("curl: (35) tls connect error")
        wrapped, _inner = self._make(failures=[err])
        with self.assertLogs("platforms.chatgpt.protocol.http_client", level="WARNING") as logs:
            wrapped.get(url="https://example.com/a")
        self.assertIn("example.com", "\n".join(logs.output))


class CreateHttpSessionTests(unittest.TestCase):
    def test_socks5_is_normalized_to_socks5h(self):
        """socks5:// 必须升级成 socks5h:// —— 让 DNS 走代理端解析。"""
        from platforms.chatgpt.protocol import http_client

        fake = mock.MagicMock()
        fake_cffi = mock.MagicMock(return_value=fake)
        with (
            mock.patch.object(http_client, "_HAS_CFFI", True),
            mock.patch.object(http_client, "CffiSession", fake_cffi),
        ):
            http_client.create_http_session(proxy="socks5://1.2.3.4:1080")

        _, kwargs = fake_cffi.call_args
        self.assertIn("impersonate", kwargs)
        self.assertEqual(fake.proxies, {"https": "socks5h://1.2.3.4:1080", "http": "socks5h://1.2.3.4:1080"})
        self.assertFalse(fake.trust_env)

    def test_http_proxy_kept_verbatim(self):
        from platforms.chatgpt.protocol import http_client

        fake = mock.MagicMock()
        fake_cffi = mock.MagicMock(return_value=fake)
        with (
            mock.patch.object(http_client, "_HAS_CFFI", True),
            mock.patch.object(http_client, "CffiSession", fake_cffi),
        ):
            http_client.create_http_session(proxy="http://user:pw@1.2.3.4:8080")

        self.assertEqual(
            fake.proxies, {"https": "http://user:pw@1.2.3.4:8080", "http": "http://user:pw@1.2.3.4:8080"}
        )

    def test_no_proxy_sets_explicit_empty(self):
        """无代理时也要显式清空 —— 否则系统环境变量会隐式接管出口。"""
        from platforms.chatgpt.protocol import http_client

        fake = mock.MagicMock()
        fake_cffi = mock.MagicMock(return_value=fake)
        with (
            mock.patch.object(http_client, "_HAS_CFFI", True),
            mock.patch.object(http_client, "CffiSession", fake_cffi),
        ):
            http_client.create_http_session()

        self.assertEqual(fake.proxies, {"https": "", "http": ""})

    def test_cffi_session_is_wrapped_with_tls_retry(self):
        from platforms.chatgpt.protocol import http_client

        fake = mock.MagicMock()
        fake_cffi = mock.MagicMock(return_value=fake)
        with (
            mock.patch.object(http_client, "_HAS_CFFI", True),
            mock.patch.object(http_client, "CffiSession", fake_cffi),
        ):
            session = http_client.create_http_session()

        self.assertIsInstance(session, http_client._TlsRetrySession)


# ────────────────────────── solver_manager ──────────────────────────


class SolverEnvTests(unittest.TestCase):
    """环境变量 → 开关解析（错一个都会让 solver 静默不启动）。"""

    def test_enabled_defaults_to_true(self):
        from services import solver_manager

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(solver_manager._solver_enabled())

    def test_disabled_by_zero_false_no(self):
        from services import solver_manager

        for value in ("0", "false", "no", "FALSE"):
            with mock.patch.dict("os.environ", {"APP_ENABLE_SOLVER": value}):
                self.assertFalse(solver_manager._solver_enabled(), value)

    def test_port_and_url_resolution(self):
        from services import solver_manager

        with mock.patch.dict("os.environ", {"SOLVER_PORT": "9999"}, clear=False):
            self.assertEqual(solver_manager._solver_port(), 9999)
            self.assertEqual(solver_manager._solver_url(), "http://127.0.0.1:9999")

    def test_local_solver_url_wins_and_trailing_slash_stripped(self):
        from services import solver_manager

        with mock.patch.dict(
            "os.environ", {"LOCAL_SOLVER_URL": "http://solver.internal:7000/"}, clear=False
        ):
            self.assertEqual(solver_manager._solver_url(), "http://solver.internal:7000")

    def test_proxy_defaults_to_enabled(self):
        """解题浏览器默认走代理 —— Turnstile 判 IP 信誉，直连过不了 x.ai。"""
        from services import solver_manager

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(solver_manager._solver_proxy_enabled())
        with mock.patch.dict("os.environ", {"SOLVER_PROXY_ENABLED": "0"}):
            self.assertFalse(solver_manager._solver_proxy_enabled())

    def test_bind_host_and_browser_type_defaults(self):
        from services import solver_manager

        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(solver_manager._solver_bind_host(), "0.0.0.0")
            self.assertEqual(solver_manager._solver_browser_type(), "camoufox")


class SolverLifecycleTests(unittest.TestCase):
    def test_start_skips_when_disabled(self):
        from services import solver_manager

        with (
            mock.patch.dict("os.environ", {"APP_ENABLE_SOLVER": "0"}),
            mock.patch.object(solver_manager, "subprocess") as fake_subprocess,
        ):
            solver_manager.start()
        fake_subprocess.Popen.assert_not_called()

    def test_start_skips_when_already_running(self):
        from services import solver_manager

        with (
            mock.patch.object(solver_manager, "is_running", return_value=True),
            mock.patch.object(solver_manager, "subprocess") as fake_subprocess,
        ):
            solver_manager.start()
        fake_subprocess.Popen.assert_not_called()

    def test_start_launches_process_with_expected_flags(self):
        from services import solver_manager

        fake_proc = mock.MagicMock()
        fake_proc.pid = 12345
        # 第一次 is_running 假（未运行）→ 启动；之后返回 True 表示就绪
        running = {"count": 0}

        def _is_running():
            running["count"] += 1
            return running["count"] > 1

        with (
            mock.patch.object(solver_manager, "is_running", side_effect=_is_running),
            mock.patch.object(solver_manager, "subprocess") as fake_subprocess,
            mock.patch.object(solver_manager.time, "sleep"),
            mock.patch.dict("os.environ", {}, clear=True),
        ):
            fake_subprocess.Popen.return_value = fake_proc
            fake_subprocess.STDOUT = -2
            solver_manager.start()

        fake_subprocess.Popen.assert_called_once()
        cmd = fake_subprocess.Popen.call_args.args[0]
        self.assertIn("--browser_type", cmd)
        self.assertIn("camoufox", cmd)
        self.assertIn("--proxy", cmd, "默认带 --proxy（解题走代理）")
        self.assertIn("--port", cmd)

    def test_stop_terminates_process(self):
        from services import solver_manager

        fake_proc = mock.MagicMock()
        fake_proc.poll.return_value = None  # 还活着
        solver_manager._proc = fake_proc
        try:
            solver_manager.stop()
        finally:
            solver_manager._proc = None

        fake_proc.terminate.assert_called_once()
        fake_proc.wait.assert_called_once()

    def test_stop_without_process_is_noop(self):
        from services import solver_manager

        solver_manager._proc = None
        solver_manager.stop()  # 不抛异常即通过


# ────────────────────────── browser_runtime ──────────────────────────


class ParseEnvBoolTests(unittest.TestCase):
    def test_true_values(self):
        for value in ("1", "true", "YES", "On", " true "):
            with mock.patch.dict("os.environ", {"PROBE": value}):
                self.assertIs(parse_env_bool("PROBE"), True, value)

    def test_false_values(self):
        for value in ("0", "false", "NO", "off"):
            with mock.patch.dict("os.environ", {"PROBE": value}):
                self.assertIs(parse_env_bool("PROBE"), False, value)

    def test_unset_or_empty_is_none(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(parse_env_bool("PROBE"))
        with mock.patch.dict("os.environ", {"PROBE": "   "}):
            self.assertIsNone(parse_env_bool("PROBE"))

    def test_invalid_value_warns_and_returns_none(self):
        with mock.patch.dict("os.environ", {"PROBE": "banana"}):
            with self.assertLogs("core.browser_runtime", level="WARNING"):
                self.assertIsNone(parse_env_bool("PROBE"))


class ResolveBrowserHeadlessTests(unittest.TestCase):
    def test_env_override_wins_over_requested(self):
        with mock.patch.dict("os.environ", {"PLAYWRIGHT_HEADLESS": "0"}):
            headless, source = resolve_browser_headless(True)
        self.assertFalse(headless, "env 覆盖应压过请求值")
        self.assertTrue(source.startswith("env:"))

    def test_requested_wins_over_default(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            headless, source = resolve_browser_headless(False, default_headless=True)
        self.assertFalse(headless)
        self.assertTrue(source.startswith("requested:"))

    def test_default_used_when_nothing_set(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            headless, source = resolve_browser_headless(None, default_headless=False)
        self.assertFalse(headless)
        self.assertTrue(source.startswith("default:"))

    def test_second_env_name_is_consulted(self):
        with mock.patch.dict("os.environ", {"REGISTER_HEADLESS": "1"}, clear=True):
            headless, source = resolve_browser_headless(None)
        self.assertTrue(headless)
        self.assertIn("REGISTER_HEADLESS", source)


if __name__ == "__main__":
    unittest.main()
