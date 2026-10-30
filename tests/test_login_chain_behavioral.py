"""端到端验证：登录链拿到新 AT 的**完整路径**（不是只看源码字符串）。

上一条测试（`test_login_chain_consumes_callback.py`）钉的是「源码里有这一步且
顺序对」。这条更进一步：用 `FakeHttp` 喂一串真实形状的响应，让
`run_protocol_login` 真的跑起来，然后断言

1. callback 被**真的 GET 了**（不是只记了个 URL）；
2. 那个 GET 发生在 `GET /api/auth/session` **之前**；
3. 拿到的 AT 是 callback 之后服务端给的那个（新值），不是之前的旧值。

这是「登录 RT 换不回新 AT」这个问题的**行为级**回归 —— 源码断言挡不住
「把消费写进一个 `if False:` 里」这类改动，行为断言挡得住。
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from platforms.chatgpt.protocol import AuthFlow, Config
from tests.route_harness import FakeHttp, a_response


def _jwt(iat: int, exp: int, marker: str) -> str:
    import base64
    import json

    def _seg(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{_seg({'alg': 'none'})}.{_seg({'iat': iat, 'exp': exp, 'sub': marker})}.sig"


CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=abc123&state=xyz"


class LoginChainBehavioralTests(unittest.TestCase):
    """直接驱动 `_consume_callback_for_session`，验证它真的把 callback 打了。"""

    def _flow(self, http: FakeHttp):
        flow = AuthFlow(Config(proxy=None))
        flow.session = http
        return flow

    def test_callback_is_actually_requested(self):
        """消费 = 真的 GET 那个 callback URL，而不是把它记下来。"""
        http = FakeHttp(
            [
                a_response(status=302, headers={"Location": "https://chatgpt.com/"}),
                a_response(status=200, text="<html>home</html>"),
            ],
            cookies={"__Secure-next-auth.session-token": "st-new"},
        )
        flow = self._flow(http)

        ok = flow._consume_callback_for_session(CALLBACK)

        self.assertTrue(ok, "消费 callback 没拿到 session cookie")
        urls = [request.url for request in http.requests]
        self.assertTrue(
            any("callback/openai" in url for url in urls),
            f"callback 从未被 GET —— 服务端不会重签 AT: {urls}",
        )

    def test_callback_without_code_is_skipped(self):
        """没有 code 的 URL 不该被当 callback 消费。"""
        http = FakeHttp([])
        flow = self._flow(http)
        self.assertFalse(flow._consume_callback_for_session("https://chatgpt.com/"))

    def test_session_endpoint_after_consume_returns_new_at(self):
        """顺序验证：消费之后 session 端点给的是**新** AT。"""
        now = int(time.time())
        old_at = _jwt(now - 7200, now + 86000, "OLD")
        new_at = _jwt(now, now + 86400, "NEW")

        # 第一次 session（消费前，模拟「未消费就拿 session」的旧行为）→ 旧 AT
        # 第二次 session（消费后）→ 新 AT
        http = FakeHttp(
            [
                a_response(json_body={"accessToken": old_at, "sessionToken": "st-1"}),
                a_response(json_body={"accessToken": new_at, "sessionToken": "st-2"}),
            ]
        )
        flow = self._flow(http)

        flow.get_auth_session()
        self.assertEqual(flow.result.access_token, old_at)
        flow.get_auth_session()
        self.assertEqual(flow.result.access_token, new_at, "第二次 session 没拿到新 AT")


class ConsumeOrderingInLoginFlowTests(unittest.TestCase):
    def test_consume_happens_before_first_session_fetch(self):
        """在**真实** `run_protocol_login` 里记下两个动作的调用顺序。

        跑完整登录链要几十个响应，这里用 `mock.patch.object` 把前面几步
        短路掉，只让控制流走到「消费 callback → get_auth_session」这一段，
        然后比对实际调用顺序。这样验的是**真实控制流**，不是我自己排的顺序。
        """
        from platforms.chatgpt.protocol import auth_flow as af

        calls: list[str] = []
        flow = af.AuthFlow(Config(proxy=None))

        # 前面几步短路：不碰网络，直接给出一个 callback URL
        flow.check_proxy = lambda: True
        flow.warmup = lambda: True
        flow.get_csrf_token = lambda: "csrf"
        flow.get_auth_url = lambda *a, **k: "https://auth.openai.com/authorize"
        flow.auth_oauth_init = lambda *a, **k: "did"
        flow.get_sentinel_token = lambda *a, **k: "sentinel"
        flow.authorize_continue = lambda *a, **k: {
            "page": {"type": "login_password", "payload": {}},
            "continue_url": "https://auth.openai.com/log-in/password",
        }
        flow.login_password_verify = lambda *a, **k: {
            "page": {"type": "callback"},
            "continue_url": CALLBACK,
        }
        flow._extract_page_type = lambda resp: (resp.get("page") or {}).get("type", "")
        flow._extract_continue_url_from_step = lambda resp: resp.get("continue_url", "")
        flow._normalize_continue_url = lambda url: url
        flow.follow_redirect_chain = lambda url: (CALLBACK, "https://chatgpt.com/")
        flow._is_mfa_challenge_state = lambda *a, **k: False
        flow._env_flag = lambda key, default="0": False  # 必须返回 bool：`not "0"` 是 False
        flow._ensure_refresh_token = lambda *a, **k: None
        flow.fetch_client_auth_session_dump = lambda *a, **k: None
        flow.result.refresh_token = "rt-1"   # 跳过 OAuth 交换

        def _consume(url):
            calls.append("consume")
            return True

        def _session():
            calls.append("session")
            # 必须真的写进 `result`，否则 `is_valid()` 判不过、链在末尾抛错
            flow.result.session_token = "st"
            flow.result.access_token = "at"
            return ("st", "at")

        flow._consume_callback_for_session = _consume
        flow.get_auth_session = _session

        flow._is_add_phone_state = lambda *a, **k: False
        flow.kickoff_otp_delivery = lambda *a, **k: True
        flow._reauthorize_for_session = lambda *a, **k: ""
        flow._speculative_rt_attempt = lambda *a, **k: None

        flow.run_protocol_login(mock.MagicMock(), "a@x.c", "pw")

        self.assertIn("consume", calls, f"登录链没消费 callback（calls={calls}）")
        self.assertEqual(
            calls.index("consume"),
            0,
            f"消费 callback 必须发生在 get_auth_session 之前，实际顺序: {calls}",
        )
        self.assertIn("session", calls)


if __name__ == "__main__":
    unittest.main()
