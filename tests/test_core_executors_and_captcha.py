"""执行器基类 / 协议执行器 / 验证码基类的单元测试。

覆盖三个此前零覆盖的模块：

- ``core/base_executor.py``   —— ``Response`` dataclass 与 ``BaseExecutor`` ABC 契约
  （抽象方法集、上下文管理器语义、proxy 默认值）；
- ``core/executors/protocol.py`` —— ``ProtocolExecutor`` 的构造（impersonate / UA /
  代理配置）、``_wrap`` 字段映射、get/post 透传、cookie 读写、close；
- ``core/base_captcha.py``    —— ``_default_solver_url`` 的环境变量优先级、
  ``YesCaptcha`` / ``ManualCaptcha`` / ``LocalSolverCaptcha`` 的成功、错误、
  超时三条路径（网络全部 mock，不打真实请求）。

约定：协议执行器的 curl_cffi Session 与验证码的 requests 调用都在**方法内**创建/
导入，所以这里 patch 的是模块属性（``core.executors.protocol.curl_requests.Session``、
``requests.post`` / ``requests.get``）—— 与仓库其它测试的做法一致。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from core.base_captcha import (
    BaseCaptcha,
    LocalSolverCaptcha,
    ManualCaptcha,
    YesCaptcha,
    _default_solver_url,
)
from core.base_executor import BaseExecutor, Response
from core.executors.protocol import ProtocolExecutor
from core.proxy_utils import build_requests_proxy_config

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# --------------------------------------------------------------- 假 curl_cffi Session


class _FakeCookies:
    """模仿 curl_cffi 的 Cookies：``jar`` 是可迭代的 cookie 对象列表。"""

    def __init__(self) -> None:
        self.jar: list = []
        self._store: dict[str, str] = {}

    def set(self, name: str, value: str) -> None:
        self._store[name] = value
        self.jar = [
            SimpleNamespace(name=k, value=v) for k, v in self._store.items()
        ]


class _FakeCurlResponse:
    def __init__(self, status_code: int = 200, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


class _FakeCurlSession:
    """假 Session：记录调用参数、可配置返回值，不触网。"""

    def __init__(self) -> None:
        self.impersonate = None
        self.proxies: dict = {}
        self.headers: dict = {}
        self.cookies = _FakeCookies()
        self.closed = False
        self.calls: list = []
        self.response = _FakeCurlResponse(
            status_code=200,
            text='{"ok": true}',
            headers={"content-type": "application/json"},
        )

    def get(self, url, *, headers=None, params=None):
        self.calls.append(("get", url, {"headers": headers, "params": params}))
        return self.response

    def post(self, url, *, headers=None, params=None, data=None, json=None):
        self.calls.append(
            ("post", url, {"headers": headers, "params": params, "data": data, "json": json})
        )
        return self.response

    def close(self) -> None:
        self.closed = True


# ------------------------------------------------------------------ Response / ABC


class ResponseTests(unittest.TestCase):
    """``Response`` dataclass：json() 与默认字段的隔离性。"""

    def test_json_parses_text(self):
        resp = Response(status_code=200, text='{"token": "abc", "n": 3}')
        self.assertEqual(resp.json(), {"token": "abc", "n": 3})

    def test_json_invalid_text_raises(self):
        resp = Response(status_code=500, text="<html>error</html>")
        with self.assertRaises(ValueError):
            resp.json()

    def test_default_headers_and_cookies_are_per_instance(self):
        """默认值必须各自独立（default_factory）——共享一份可变字典会串数据。"""
        first = Response(status_code=200, text="{}")
        second = Response(status_code=200, text="{}")
        first.headers["x-a"] = "1"
        first.cookies["sid"] = "1"
        self.assertEqual(second.headers, {})
        self.assertEqual(second.cookies, {})

    def test_explicit_headers_and_cookies_are_kept(self):
        resp = Response(
            status_code=302,
            text="",
            headers={"location": "/next"},
            cookies={"a": "1"},
        )
        self.assertEqual(resp.headers, {"location": "/next"})
        self.assertEqual(resp.cookies, {"a": "1"})


class _MiniExecutor(BaseExecutor):
    """最小可用执行器：只为实现 ABC 契约，不触网。"""

    def __init__(self, proxy=None):
        super().__init__(proxy)
        self.closed = False

    def get(self, url, *, headers=None, params=None) -> Response:
        return Response(status_code=200, text="{}")

    def post(self, url, *, headers=None, params=None, data=None, json=None) -> Response:
        return Response(status_code=200, text="{}")

    def get_cookies(self) -> dict:
        return {}

    def set_cookies(self, cookies: dict) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class BaseExecutorTests(unittest.TestCase):
    """``BaseExecutor`` 抽象契约：不可直接实例化、子类可用、上下文管理器关闭。"""

    def test_cannot_instantiate_abstract_base(self):
        with self.assertRaises(TypeError):
            BaseExecutor()

    def test_subclass_missing_any_abstract_method_is_rejected(self):
        class _Incomplete(BaseExecutor):
            def get(self, url, *, headers=None, params=None):
                return Response(status_code=200, text="{}")

            def post(self, url, *, headers=None, params=None, data=None, json=None):
                return Response(status_code=200, text="{}")

            def get_cookies(self):
                return {}

            def set_cookies(self, cookies):
                pass
            # 故意缺 close()

        with self.assertRaises(TypeError):
            _Incomplete()

    def test_proxy_defaults_to_none(self):
        self.assertIsNone(_MiniExecutor().proxy)

    def test_explicit_proxy_is_stored(self):
        self.assertEqual(_MiniExecutor("http://127.0.0.1:1080").proxy, "http://127.0.0.1:1080")

    def test_context_manager_returns_self_and_closes(self):
        executor = _MiniExecutor()
        with executor as entered:
            self.assertIs(entered, executor)
            self.assertFalse(executor.closed)
        self.assertTrue(executor.closed, "__exit__ 必须调用 close()")

    def test_context_manager_closes_on_exception(self):
        executor = _MiniExecutor()
        with self.assertRaises(RuntimeError):
            with executor:
                raise RuntimeError("boom")
        self.assertTrue(executor.closed)


# ------------------------------------------------------------- ProtocolExecutor


class ProtocolExecutorTests(unittest.TestCase):
    """协议执行器：构造配置、_wrap 映射、请求透传、cookie、close。"""

    def _make(self, **kwargs):
        fake = _FakeCurlSession()
        patcher = mock.patch(
            "core.executors.protocol.curl_requests.Session", return_value=fake
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return ProtocolExecutor(**kwargs), fake

    def test_constructor_defaults(self):
        executor, fake = self._make()
        self.assertIs(executor.s, fake)
        self.assertEqual(fake.impersonate, "chrome124")
        self.assertEqual(fake.headers["user-agent"], DEFAULT_UA)
        # 没给代理时不碰 proxies（保持 session 原值）
        self.assertEqual(fake.proxies, {})
        self.assertIsNone(executor.proxy)

    def test_constructor_sets_proxy_config(self):
        proxy = "proxy.example:2000:user:pass"
        executor, fake = self._make(proxy=proxy)
        self.assertEqual(executor.proxy, proxy)
        self.assertEqual(
            fake.proxies,
            {"http": "http://user:pass@proxy.example:2000",
             "https": "http://user:pass@proxy.example:2000"},
        )
        # 与共享件口径一致（socks5 会被规范化为 socks5h）
        self.assertEqual(fake.proxies, build_requests_proxy_config(proxy))

    def test_constructor_custom_impersonate(self):
        _, fake = self._make(impersonate="safari17_0")
        self.assertEqual(fake.impersonate, "safari17_0")

    def test_wrap_maps_status_text_headers_cookies(self):
        executor, fake = self._make()
        fake.response = _FakeCurlResponse(
            status_code=401, text="nope", headers={"content-type": "text/plain"}
        )
        fake.cookies.set("sid", "s-1")
        fake.cookies.set("oai-did", "d-1")

        resp = executor._wrap(fake.response)

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.text, "nope")
        self.assertEqual(resp.headers, {"content-type": "text/plain"})
        self.assertEqual(resp.cookies, {"sid": "s-1", "oai-did": "d-1"})

    def test_get_passes_kwargs_and_wraps_response(self):
        executor, fake = self._make()
        resp = executor.get(
            "https://example.com/a", headers={"x-h": "1"}, params={"q": "2"}
        )
        self.assertEqual(
            fake.calls,
            [("get", "https://example.com/a", {"headers": {"x-h": "1"}, "params": {"q": "2"}})],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"ok": True})

    def test_post_passes_data_and_json(self):
        executor, fake = self._make()
        executor.post(
            "https://example.com/b",
            headers={"x-h": "2"},
            params={"p": "1"},
            data={"form": "1"},
            json={"j": 2},
        )
        self.assertEqual(
            fake.calls,
            [("post", "https://example.com/b",
              {"headers": {"x-h": "2"}, "params": {"p": "1"},
               "data": {"form": "1"}, "json": {"j": 2}})],
        )

    def test_get_cookies_reads_session_jar(self):
        executor, fake = self._make()
        fake.cookies.set("a", "1")
        fake.cookies.set("b", "2")
        self.assertEqual(executor.get_cookies(), {"a": "1", "b": "2"})

    def test_set_cookies_writes_each_pair(self):
        executor, fake = self._make()
        executor.set_cookies({"a": "1", "b": "2"})
        self.assertEqual(executor.get_cookies(), {"a": "1", "b": "2"})

    def test_close_closes_session(self):
        executor, fake = self._make()
        executor.close()
        self.assertTrue(fake.closed)


# ----------------------------------------------------------- _default_solver_url


class DefaultSolverUrlTests(unittest.TestCase):
    """环境变量优先级：LOCAL_SOLVER_URL > SOLVER_PORT > 8889。"""

    def test_defaults_to_8889(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_default_solver_url(), "http://127.0.0.1:8889")

    def test_solver_port_env_is_used(self):
        with mock.patch.dict(os.environ, {"SOLVER_PORT": "9999"}, clear=True):
            self.assertEqual(_default_solver_url(), "http://127.0.0.1:9999")

    def test_local_solver_url_wins_over_port(self):
        with mock.patch.dict(
            os.environ,
            {"LOCAL_SOLVER_URL": "http://solver.local:1234", "SOLVER_PORT": "9999"},
            clear=True,
        ):
            self.assertEqual(_default_solver_url(), "http://solver.local:1234")


# --------------------------------------------------------------------- BaseCaptcha


class BaseCaptchaContractTests(unittest.TestCase):
    def test_base_captcha_is_abstract(self):
        with self.assertRaises(TypeError):
            BaseCaptcha()

    def test_subclass_missing_a_method_is_rejected(self):
        class _Half(BaseCaptcha):
            def solve_turnstile(self, page_url, site_key):
                return "t"
            # 故意缺 solve_image()

        with self.assertRaises(TypeError):
            _Half()

    def test_minimal_subclass_works(self):
        class _Both(BaseCaptcha):
            def solve_turnstile(self, page_url, site_key):
                return "turnstile-token"

            def solve_image(self, image_b64):
                return "image-text"

        captcha = _Both()
        self.assertEqual(captcha.solve_turnstile("https://p", "k"), "turnstile-token")
        self.assertEqual(captcha.solve_image("b64"), "image-text")


# ---------------------------------------------------------------------- YesCaptcha


class YesCaptchaTests(unittest.TestCase):
    """YesCaptcha 的三条路径：成功 / 创建失败 / 轮询错误 / 超时。"""

    def test_success_path_and_request_payload(self):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-1"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {"status": "ready", "solution": {"token": "TOKEN-X"}}

        with mock.patch("requests.post", side_effect=[create_resp, result_resp]) as post:
            with mock.patch("time.sleep"):
                token = YesCaptcha("KEY-1").solve_turnstile("https://page", "site-key")

        self.assertEqual(token, "TOKEN-X")
        self.assertEqual(post.call_count, 2)
        first, second = post.call_args_list
        self.assertEqual(first.args[0], "https://api.yescaptcha.com/createTask")
        self.assertEqual(
            first.kwargs["json"],
            {
                "clientKey": "KEY-1",
                "task": {
                    "type": "TurnstileTaskProxyless",
                    "websiteURL": "https://page",
                    "websiteKey": "site-key",
                },
            },
        )
        self.assertEqual(first.kwargs["timeout"], 30)
        self.assertIs(first.kwargs["verify"], False)
        self.assertEqual(second.args[0], "https://api.yescaptcha.com/getTaskResult")
        self.assertEqual(second.kwargs["json"], {"clientKey": "KEY-1", "taskId": "task-1"})

    def test_missing_task_id_raises_with_body(self):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"errorId": 1}
        create_resp.text = "invalid key"

        with mock.patch("requests.post", return_value=create_resp):
            with self.assertRaises(RuntimeError) as ctx:
                YesCaptcha("KEY").solve_turnstile("https://page", "site-key")

        self.assertIn("YesCaptcha 创建任务失败", str(ctx.exception))
        self.assertIn("invalid key", str(ctx.exception))

    def test_polling_error_raises(self):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-2"}
        result_resp = mock.Mock()
        result_resp.json.return_value = {"errorId": 12, "errorDescription": "bad"}

        with mock.patch("requests.post", side_effect=[create_resp, result_resp]):
            with mock.patch("time.sleep"):
                with self.assertRaises(RuntimeError) as ctx:
                    YesCaptcha("KEY").solve_turnstile("https://page", "site-key")

        self.assertIn("YesCaptcha 错误", str(ctx.exception))

    def test_timeout_after_60_polls(self):
        create_resp = mock.Mock()
        create_resp.json.return_value = {"taskId": "task-3"}
        pending = mock.Mock()
        pending.json.return_value = {"status": "processing", "errorId": 0}

        with mock.patch("requests.post", side_effect=[create_resp] + [pending] * 60) as post:
            with mock.patch("time.sleep") as sleep:
                with self.assertRaises(TimeoutError):
                    YesCaptcha("KEY").solve_turnstile("https://page", "site-key")

        self.assertEqual(post.call_count, 61, "1 次创建 + 60 次轮询")
        self.assertEqual(sleep.call_count, 60)

    def test_solve_image_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            YesCaptcha("KEY").solve_image("b64")


# ------------------------------------------------------------------- ManualCaptcha


class ManualCaptchaTests(unittest.TestCase):
    def test_turnstile_reads_stripped_input(self):
        with mock.patch("builtins.input", return_value="  manual-token  ") as fake_input:
            token = ManualCaptcha().solve_turnstile("https://page", "site-key")
        self.assertEqual(token, "manual-token")
        self.assertIn("https://page", fake_input.call_args.args[0])

    def test_image_reads_stripped_input(self):
        with mock.patch("builtins.input", return_value="  ab12  ") as fake_input:
            text = ManualCaptcha().solve_image("b64")
        self.assertEqual(text, "ab12")
        self.assertIn("图片验证码", fake_input.call_args.args[0])


# -------------------------------------------------------------- LocalSolverCaptcha


class LocalSolverCaptchaUrlTests(unittest.TestCase):
    def test_url_defaults_from_env_and_rstrips_slash(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                LocalSolverCaptcha().solver_url, "http://127.0.0.1:8889"
            )
        self.assertEqual(
            LocalSolverCaptcha("http://127.0.0.1:8889/").solver_url,
            "http://127.0.0.1:8889",
            "尾部斜杠必须去掉，否则拼出 //turnstile",
        )
        self.assertEqual(
            LocalSolverCaptcha("http://custom:1/").solver_url, "http://custom:1"
        )


class LocalSolverCaptchaSolveTests(unittest.TestCase):
    """本地 solver：成功 / 缺 taskId / CAPTCHA_FAIL / 非 200 跳过 / 超时。"""

    def _turnstile_resp(self, task_id="tid-1"):
        resp = mock.Mock()
        resp.json.return_value = {"taskId": task_id}
        resp.raise_for_status.return_value = None
        return resp

    def _result_resp(self, payload, status_code=200):
        resp = mock.Mock()
        resp.status_code = status_code
        resp.json.return_value = payload
        return resp

    def test_success_path_and_request_shape(self):
        first = self._turnstile_resp()
        ready = self._result_resp({"status": "ready", "solution": {"token": "T-1"}})

        with mock.patch("requests.get", side_effect=[first, ready]) as get:
            with mock.patch("time.sleep"):
                token = LocalSolverCaptcha("http://127.0.0.1:8889/").solve_turnstile(
                    "https://page", "site-key"
                )

        self.assertEqual(token, "T-1")
        self.assertEqual(get.call_count, 2)
        submit, poll = get.call_args_list
        self.assertEqual(submit.args[0], "http://127.0.0.1:8889/turnstile")
        self.assertEqual(submit.kwargs["params"], {"url": "https://page", "sitekey": "site-key"})
        self.assertEqual(submit.kwargs["timeout"], 15)
        self.assertEqual(poll.args[0], "http://127.0.0.1:8889/result")
        self.assertEqual(poll.kwargs["params"], {"id": "tid-1"})
        self.assertEqual(poll.kwargs["timeout"], 10)

    def test_missing_task_id_raises(self):
        first = mock.Mock()
        first.json.return_value = {"error": "nope"}
        first.text = "solver busy"
        first.raise_for_status.return_value = None

        with mock.patch("requests.get", return_value=first):
            with self.assertRaises(RuntimeError) as ctx:
                LocalSolverCaptcha("http://s:1").solve_turnstile("https://p", "k")

        self.assertIn("LocalSolver 未返回 taskId", str(ctx.exception))
        self.assertIn("solver busy", str(ctx.exception))

    def test_captcha_fail_raises(self):
        first = self._turnstile_resp()
        failed = self._result_resp({"status": "CAPTCHA_FAIL"})

        with mock.patch("requests.get", side_effect=[first, failed]):
            with mock.patch("time.sleep"):
                with self.assertRaises(RuntimeError) as ctx:
                    LocalSolverCaptcha("http://s:1").solve_turnstile("https://p", "k")

        self.assertIn("LocalSolver Turnstile 失败", str(ctx.exception))

    def test_non_200_result_is_skipped(self):
        """非 200 的轮询响应不算结果，跳过继续轮询，直到 ready。"""
        first = self._turnstile_resp()
        error = self._result_resp({"status": "ready", "solution": {"token": "x"}}, status_code=500)
        ready = self._result_resp({"status": "ready", "solution": {"token": "T-2"}})

        with mock.patch("requests.get", side_effect=[first, error, ready]) as get:
            with mock.patch("time.sleep"):
                token = LocalSolverCaptcha("http://s:1").solve_turnstile("https://p", "k")

        self.assertEqual(token, "T-2")
        self.assertEqual(get.call_count, 3)

    def test_ready_without_token_keeps_polling_until_timeout(self):
        """status=ready 但 solution 里没有 token —— 不能当成成功返回。"""
        first = self._turnstile_resp()
        empty_ready = self._result_resp({"status": "ready", "solution": {}})

        with mock.patch("requests.get", side_effect=[first] + [empty_ready] * 60) as get:
            with mock.patch("time.sleep") as sleep:
                with self.assertRaises(TimeoutError):
                    LocalSolverCaptcha("http://s:1").solve_turnstile("https://p", "k")

        self.assertEqual(get.call_count, 61)
        self.assertEqual(sleep.call_count, 60)

    def test_timeout_after_60_polls(self):
        first = self._turnstile_resp()
        pending = self._result_resp({"status": "processing"})

        with mock.patch("requests.get", side_effect=[first] + [pending] * 60) as get:
            with mock.patch("time.sleep"):
                with self.assertRaises(TimeoutError):
                    LocalSolverCaptcha("http://s:1").solve_turnstile("https://p", "k")

        self.assertEqual(get.call_count, 61)

    def test_solve_image_not_implemented(self):
        with self.assertRaises(NotImplementedError):
            LocalSolverCaptcha("http://s:1").solve_image("b64")


class LocalSolverStartTests(unittest.TestCase):
    """start_solver：命令行拼装、headless 开关、等待探测、启动超时。"""

    def test_builds_cmd_and_waits_for_service(self):
        with mock.patch("subprocess.Popen") as popen:
            with mock.patch("requests.get", return_value=mock.Mock()) as get:
                with mock.patch("time.sleep"):
                    LocalSolverCaptcha.start_solver(
                        headless=True, browser_type="camoufox", port=8890
                    )

        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("turnstile_solver/start.py"), cmd[1])
        self.assertEqual(cmd[cmd.index("--port") + 1], "8890")
        self.assertEqual(cmd[cmd.index("--browser_type") + 1], "camoufox")
        self.assertNotIn("--no-headless", cmd)
        self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
        get.assert_called_with("http://localhost:8890/", timeout=2)

    def test_headless_false_appends_flag(self):
        with mock.patch("subprocess.Popen") as popen:
            with mock.patch("requests.get", return_value=mock.Mock()):
                with mock.patch("time.sleep"):
                    LocalSolverCaptcha.start_solver(headless=False, port=8891)

        cmd = popen.call_args.args[0]
        self.assertIn("--no-headless", cmd)
        self.assertEqual(cmd[cmd.index("--port") + 1], "8891")

    def test_start_timeout_raises_after_20_probes(self):
        with mock.patch("subprocess.Popen"):
            with mock.patch("requests.get", side_effect=Exception("connection refused")) as get:
                with mock.patch("time.sleep") as sleep:
                    with self.assertRaises(RuntimeError) as ctx:
                        LocalSolverCaptcha.start_solver(port=8892)

        self.assertIn("LocalSolver 启动超时", str(ctx.exception))
        self.assertEqual(get.call_count, 20)
        self.assertEqual(sleep.call_count, 20)


if __name__ == "__main__":
    unittest.main()
