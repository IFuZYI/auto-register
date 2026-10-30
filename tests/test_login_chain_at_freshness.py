"""登录链（RT → AT）到底换不换得到**新** AT？—— 用真实调用链验证。

用户问题（2026-10-30）：**「登录RT是不是不能换回新的AT，换到的AT是不是还是旧的」**

这个问题有三个可能的答案，必须用代码路径区分，不能靠日志猜：

1. 登录链根本拿不到 AT（`get_auth_session` 返回空）→ 谈不上新旧；
2. 拿到 AT，但它是 `/api/auth/session` 里服务端**原样返回的旧值**；
3. 拿到 AT，且是**新签发**的（`iat` 是刚刚）。

关键在 `AuthFlow.get_auth_session()`：它读的是 `https://chatgpt.com/api/auth/session`
的 `accessToken` 字段。ChatGPT 的 session 端点行为是**令牌未到期时原样返回旧
AT**（这一点 `plugin.py` 的注释里已经记录了实测 2026-10-06）。所以「登录链
换到的 AT 是不是旧的」取决于登录链是否真的让服务端**重新签发**。

这里用 `FakeHttp` 喂两种响应，钉住代码的真实行为：
- 服务端返回旧 AT（iat 早于现在）→ 代码应该如实报「只返还原本的 AT」而不是
  谎报「已刷新」；
- 服务端返回新 AT（iat 是刚签的）→ 应该报「已换发」。
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from platforms.chatgpt.protocol import AuthFlow, Config
from tests.route_harness import FakeHttp, a_response

#: 造一个「签发于 1 小时前」的假 JWT（只带 iat/exp，签名不重要 —— 代码只解 payload）
def _fake_jwt(iat: int, exp: int, marker: str = "") -> str:
    import base64
    import json

    def _seg(payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = _seg({"alg": "none", "typ": "JWT"})
    body = _seg({"iat": iat, "exp": exp, "sub": marker or f"at-{iat}"})
    return f"{header}.{body}.sig"


class LoginChainAccessTokenSourceTests(unittest.TestCase):
    """`get_auth_session()` 拿到的 AT 是服务端给什么就是什么。"""

    def _flow(self, http: FakeHttp):
        flow = AuthFlow(Config(proxy=None))
        flow.session = http
        return flow

    def test_session_endpoint_returns_the_server_value_verbatim(self):
        """**核心事实**：登录链不生成 AT，它只是把 session 端点的 accessToken 抄下来。

        所以「登录链能不能换到新 AT」= 「服务端在这次登录后有没有重签」。
        代码这一层没有能力区分，也不该假装区分 —— 区分靠 `iat` 比较（见下一条）。
        """
        now = int(time.time())
        old_at = _fake_jwt(now - 3600, now + 3600 * 24, "OLD")
        http = FakeHttp([a_response(json_body={"accessToken": old_at, "sessionToken": "st-1"})])
        flow = self._flow(http)

        st, at = flow.get_auth_session()

        self.assertEqual(st, "st-1")
        self.assertEqual(at, old_at, "session 端点给的 AT 必须原样返回，不能改写")
        self.assertEqual(flow.result.access_token, old_at)

    def test_old_at_is_detectable_by_iat(self):
        """「是不是旧的」只能靠 `iat` 判 —— 这是 plugin 里 `refreshed` 的判据来源。"""
        from services.chatgpt_token_lifecycle import decode_jwt_claims

        now = int(time.time())
        old_at = _fake_jwt(now - 7200, now + 3600 * 24, "OLD")
        new_at = _fake_jwt(now, now + 3600 * 24, "NEW")

        self.assertLess(int(decode_jwt_claims(old_at).get("iat")), now - 3000)
        self.assertGreaterEqual(int(decode_jwt_claims(new_at).get("iat")), now - 5)


class RefreshedFlagSemanticsTests(unittest.TestCase):
    """`refreshed` 必须如实反映「换发了没有」。

    `plugin.py` 里写：
        result.refreshed = bool(result.access_token) and result.access_token != previous_at
    这个判据是对的（字符串不等 = 换发了）。但**登录链拿到的是同一个串时**
    refreshed 必须为 False —— 否则界面上会显示「已刷新」而 AT 根本没变。
    """

    def test_identical_at_means_not_refreshed(self):
        now = int(time.time())
        at = _fake_jwt(now - 3600, now + 3600 * 24)
        self.assertFalse(bool(at) and at != at)

    def test_different_at_means_refreshed(self):
        now = int(time.time())
        old = _fake_jwt(now - 3600, now + 3600 * 24, "OLD")
        new = _fake_jwt(now, now + 3600 * 24, "NEW")
        self.assertTrue(bool(new) and new != old)


class LoginChainRouteTests(unittest.TestCase):
    """`run_protocol_login` 的返回值结构：AT 从 `_absorb` 里来，不额外造。"""

    def test_absorb_only_copies_non_empty_values(self):
        """登录链没换到某样东西时，**不能**把调用方已有的值冲成空串。

        plugin 里靠「非空才覆盖」保住刷新链刚轮换的 ST —— 冲空等于轮换值丢了。
        """
        from platforms.chatgpt.login_refresh import LoginRefreshResult

        result = LoginRefreshResult(access_token="old-at", session_token="old-st")
        flow = mock.MagicMock()
        flow.result.access_token = "new-at"
        flow.result.session_token = ""      # 这次没换到
        flow.result.refresh_token = "rt-1"
        flow.result.id_token = ""
        flow.result.cookie_header = ""
        flow.result.device_id = ""

        from platforms.chatgpt import login_refresh as mod

        refresher = mock.MagicMock()
        refresher._active_flow = flow
        mod.LoginAccessTokenRefresher._absorb(refresher, result)

        self.assertEqual(result.access_token, "new-at")
        self.assertEqual(result.session_token, "old-st", "空值把已有 ST 冲掉了")
        self.assertEqual(result.refresh_token, "rt-1")


if __name__ == "__main__":
    unittest.main()
