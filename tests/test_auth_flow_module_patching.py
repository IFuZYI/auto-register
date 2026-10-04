"""拆 Mixin 后，测试对模块级名字的打桩必须仍然生效。

`tests/test_chatgpt_warmup_fingerprint.py` 用
`mock.patch.object(auth_flow_module.time, "sleep")` 与
`mock.patch.object(auth_flow_module, "create_http_session")` 打桩。
如果 `warmup` / `_rotate_impersonate_session` 被搬到别的 mixin，而那个模块
**没有**自己的 `time` / `create_http_session` 模块级名字，patch 就会失效 ——
测试会去发真实网络请求（表现为超时或 403，而不是断言失败），非常难排查。

因此本方案把 `warmup` / `check_proxy` 永久留在 `auth_flow.py`。
"""
import unittest
from unittest import mock

from platforms.chatgpt.protocol import auth_flow as auth_flow_module
from platforms.chatgpt.protocol.auth_flow import AuthFlow
from platforms.chatgpt.protocol.fingerprint import (
    family_impersonates,
    fingerprint_for_impersonate,
    generate_fingerprint,
)


class _Cookies:
    def get_dict(self):
        return {"__cf_bm": "x", "oai-did": "did-123"}


class _Session:
    cookies = _Cookies()

    def get(self, url, headers=None, timeout=None):
        return mock.Mock(status_code=200)


def _flow() -> AuthFlow:
    flow = AuthFlow.__new__(AuthFlow)
    flow.config = mock.Mock(proxy=None)
    flow._fingerprint = fingerprint_for_impersonate("chrome136", generate_fingerprint())
    flow._ua = flow._fingerprint["user_agent"]
    flow._impersonate_candidates = family_impersonates("chrome136")
    flow._impersonate_idx = 0
    return flow


class PatchingStillWorksTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(auth_flow_module.time, "sleep", lambda *_: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_warmup_and_check_proxy_stay_in_auth_flow_module(self):
        """这两个方法必须在 auth_flow 模块里定义，patch 才有效。"""
        self.assertEqual(
            auth_flow_module.AuthFlow.warmup.__module__,
            "platforms.chatgpt.protocol.auth_flow",
        )
        self.assertEqual(
            auth_flow_module.AuthFlow.check_proxy.__module__,
            "platforms.chatgpt.protocol.auth_flow",
        )

    def test_warmup_uses_the_patched_session_factory(self):
        flow = _flow()
        with mock.patch.object(
            auth_flow_module, "create_http_session", return_value=_Session()
        ) as factory:
            self.assertTrue(flow.warmup())
            factory.assert_called()


if __name__ == "__main__":
    unittest.main()
