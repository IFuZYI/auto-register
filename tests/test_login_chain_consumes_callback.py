"""登录链必须消费 callback，否则拿到的 AT 是**旧的**（用户问题 2026-10-30）。

## 根因

`run_register` 里有这一步，`run_protocol_login`（登录链）里**没有**：

```python
if (not refresh_only_mode) and callback_url:
    self._consume_callback_for_session(callback_url)
```

`follow_redirect_chain` 故意**不消费** callback（要把 code 留给 OAuth token
exchange）。而 NextAuth 只在 callback 真正被 GET 时才签发新 session cookie 并
**重新签发 access_token**。跳过这步时，紧接着的 `GET /api/auth/session` 拿的是
上一次会话残留的 AT —— 令牌未到期，服务端原样返回旧值（`token_refresh.py` 的
注释里早有实测记录 2026-10-06）。

于是「登录链跑完了、AT 也有了」，但那个 AT 是旧的。用户看到的现象就是
「登录 RT 换不回新 AT」。

## 这条测试钉什么

不测 HTTP 细节，只测**顺序契约**：在 `run_protocol_login` 里，消费 callback
必须发生在第一次 `get_auth_session()` **之前** —— 顺序反了就还是拿到旧 AT。
"""

from __future__ import annotations

import inspect
import unittest


class LoginChainConsumesCallbackTests(unittest.TestCase):
    def test_consume_callback_runs_before_get_auth_session(self):
        from platforms.chatgpt.protocol import auth_flow as af

        source = inspect.getsource(af.AuthFlow.run_protocol_login)
        consume_at = source.find("_consume_callback_for_session")
        self.assertGreater(consume_at, -1, "登录链没有消费 callback —— 拿到的 AT 会是旧的")

        session_at = source.find("self.get_auth_session()")
        self.assertGreater(session_at, -1)
        self.assertLess(
            consume_at,
            session_at,
            "消费 callback 必须发生在 get_auth_session 之前，否则服务端返回旧 AT",
        )

    def test_consume_is_guarded_by_refresh_only_mode(self):
        """`OAUTH_REFRESH_ONLY` 只要 RT 时不该去消费 callback（会把 code 用掉）。"""
        from platforms.chatgpt.protocol import auth_flow as af

        source = inspect.getsource(af.AuthFlow.run_protocol_login)
        start = source.find("_consume_callback_for_session")
        window = source[max(0, start - 400) : start]
        self.assertIn("refresh_only_mode", window, "消费 callback 没有 refresh_only 守卫")

    def test_register_chain_also_consumes(self):
        """注册链本来就有这一步 —— 这条防的是「改登录链时把注册链的删了」。"""
        from platforms.chatgpt.protocol import auth_flow as af

        source = inspect.getsource(af.AuthFlow.run_register)
        self.assertIn("_consume_callback_for_session", source)


class AtProvenanceStampingTests(unittest.TestCase):
    """AT 换了 → 记录必须跟着对齐到 AT 自身的 iat（不是「动作发生的时刻」）。"""

    def _jwt(self, iat: int, exp: int, marker: str = "") -> str:
        import base64
        import json

        def _seg(payload: dict) -> str:
            raw = json.dumps(payload, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        return f"{_seg({'alg': 'none'})}.{_seg({'iat': iat, 'exp': exp, 'sub': marker or 'x'})}.sig"

    def test_restamp_aligns_to_at_iat(self):
        import time

        from services.action_execution import _restamp_at_provenance

        now = int(time.time())
        at = self._jwt(now - 86400, now + 86400)   # 一天前签发的
        extra: dict = {"chatgpt_token_refresh": {"ok": True, "refreshed": True, "at": "2026-10-24T20:00:13"}}

        _restamp_at_provenance(extra, {"access_token": at})

        stamp = extra["chatgpt_token_refresh"]
        self.assertEqual(stamp["at_iat"], now - 86400, "记录没对齐到 AT 的签发时间")
        self.assertEqual(stamp["at_exp"], now + 86400)
        self.assertEqual(stamp["at_source"], "credential_write")
        # 「上一次刷新动作」的结论不能被别的路径冒充
        self.assertTrue(stamp["ok"])
        self.assertTrue(stamp["refreshed"])

    def test_restamp_does_not_touch_flags(self):
        from services.action_execution import _restamp_at_provenance

        extra = {"chatgpt_token_refresh": {"ok": False, "message": "刷新失败", "at": "x"}}
        _restamp_at_provenance(extra, {"access_token": self._jwt(1000, 90000)})
        stamp = extra["chatgpt_token_refresh"]
        self.assertFalse(stamp["ok"], "对齐时间不该改写上一次的刷新结论")
        self.assertEqual(stamp["message"], "刷新失败")

    def test_no_at_write_is_a_noop(self):
        from services.action_execution import _restamp_at_provenance

        extra: dict = {"chatgpt_token_refresh": {"at": "2026-10-24T20:00:13"}}
        _restamp_at_provenance(extra, {"refresh_token": "rt-new"})
        self.assertEqual(extra["chatgpt_token_refresh"]["at"], "2026-10-24T20:00:13")

    def test_non_jwt_at_does_not_fake_a_time(self):
        """解不出 iat 时**不要**冒充签发时间（宁可留空也不写假的）。"""
        from services.action_execution import _restamp_at_provenance

        extra: dict = {}
        _restamp_at_provenance(extra, {"access_token": "not-a-jwt"})
        self.assertNotIn("chatgpt_token_refresh", extra)


class PluginStampCarriesAtProvenanceTests(unittest.TestCase):
    def test_refresh_stamp_records_at_iat(self):
        from platforms.chatgpt import plugin as mod

        source = inspect.getsource(mod.ChatGPTPlatform.execute_action)
        self.assertIn("at_iat", source, "刷新 stamp 没记 AT 的签发时间")
        self.assertIn("_access_token_claims", source)


if __name__ == "__main__":
    unittest.main()
