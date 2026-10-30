"""注册主链的**第一段**必须真跑（tests-quality 审计 P0 的第一步）。

`AuthFlow.run_register`（约 400 行）至今没有任何测试执行过 —— mutation 实测：
在函数体首行插 `raise`，注册相关 83 条测试全绿。

整条链要喂的响应太多，一次啃不动；这里先把**最容易坏、也最好验证**的三步接上
真实调用链（`FakeHttp` 只替网络出口，不 mock 实现）：

    warmup()        GET  https://chatgpt.com/           ← 必须有 oai-did cookie
    get_csrf_token()GET  https://chatgpt.com/api/auth/csrf
    get_auth_url()  POST https://chatgpt.com/api/auth/signin/openai

这三步错了后面必然 409 invalid_state —— 而 409 在真机上表现为「注册全失败」，
排查要花几小时。钉住它们 = 钉住整条链的入口。
"""

from __future__ import annotations

import unittest
from unittest import mock

from tests.route_harness import FakeHttp, a_response


class RegisterFirstLegTests(unittest.TestCase):
    def _flow(self, http: FakeHttp):
        from platforms.chatgpt.protocol import AuthFlow, Config

        flow = AuthFlow(Config(proxy=None))
        flow.session = http
        return flow

    def test_warmup_csrf_and_auth_url_are_wired_end_to_end(self):
        from platforms.chatgpt.protocol import auth_flow as af

        http = FakeHttp(
            [
                a_response(status=200, text="<html></html>"),
                a_response(json_body={"csrfToken": "csrf-1"}),
                a_response(
                    json_body={"url": "https://auth.openai.com/authorize?client_id=app_x"}
                ),
            ],
            cookies={"oai-did": "did-1"},
        )
        flow = self._flow(http)

        with mock.patch.object(af, "create_http_session", return_value=http), mock.patch.object(
            af.time, "sleep", lambda *_: None
        ):
            self.assertTrue(flow.warmup(), "warmup 拿不到 oai-did 就该 False")
            csrf = flow.get_csrf_token()
            auth_url = flow.get_auth_url(csrf, email="new@x.c")

        self.assertEqual(csrf, "csrf-1")
        self.assertTrue(
            auth_url.startswith("https://auth.openai.com/authorize"),
            f"授权地址不对: {auth_url}",
        )
        urls = [request.url for request in http.requests]
        self.assertTrue(any(u.rstrip("/") == "https://chatgpt.com" for u in urls), urls)
        self.assertTrue(any("/api/auth/csrf" in u for u in urls), urls)
        signin = [u for u in urls if "/api/auth/signin/openai" in u]
        self.assertTrue(signin, urls)
        # 入口三连必须**按顺序**发生：顺序错了（比如先 signin 再 csrf）服务端必 409
        self.assertLess(
            next(i for i, u in enumerate(urls) if "/api/auth/csrf" in u),
            next(i for i, u in enumerate(urls) if "/api/auth/signin/openai" in u),
        )

    def test_warmup_without_oai_did_is_false(self):
        """CF 403（没种上 oai-did）必须如实失败 —— 继续注册只会换来 409。"""
        from platforms.chatgpt.protocol import auth_flow as af

        http = FakeHttp(
            [a_response(status=403, text="cf")] * 8,
            cookies={},
        )
        flow = self._flow(http)

        with mock.patch.object(af, "create_http_session", return_value=http), mock.patch.object(
            af.time, "sleep", lambda *_: None
        ):
            self.assertFalse(flow.warmup())

    def test_csrf_token_failure_raises_readable_error(self):
        from platforms.chatgpt.protocol import auth_flow as af

        http = FakeHttp([a_response(status=403, text="forbidden")] * 4)
        flow = self._flow(http)

        with mock.patch.object(af, "create_http_session", return_value=http), mock.patch.object(
            af.time, "sleep", lambda *_: None
        ):
            with self.assertRaises(Exception):
                flow.get_csrf_token()


if __name__ == "__main__":
    unittest.main()
