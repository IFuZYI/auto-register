"""SessionMixin 指纹轮换：UA 与 client hints 必须同进同退。

这段代码有真实事故背景：`_rotate_impersonate_session` 曾只更新 `self._ua` 和
session，而 `sec-ch-ua*` 头全从 `self._fingerprint` 取 —— 换完指纹后变成
「UA 说 Chrome/136、client hints 说 v=146」的自相矛盾组合，是 CF 一抓一个准
的特征。`_sync_fingerprint_to` 的「UA 从新指纹里取」也是修过的坑：自己再调
`ua_for_impersonate` 会重新随机系统版本，造出「会话 UA 14_4 / 指纹 14_5」。

这些行为没有专门的测试守 —— 之前的测试只验证「patch 点还在」。
"""

from __future__ import annotations

import unittest
from unittest import mock

from platforms.chatgpt.protocol.fingerprint import (
    family_impersonates,
    fingerprint_for_impersonate,
    generate_fingerprint,
)
from platforms.chatgpt.protocol.mixins import session as session_mixin_module
from platforms.chatgpt.protocol.mixins.session import SessionMixin


class _Flow(SessionMixin):
    """最小宿主：只带轮换逻辑需要的字段。"""

    def __init__(self, impersonate="chrome136"):
        self.config = mock.Mock(proxy="http://proxy.example:8080")
        self._fingerprint = fingerprint_for_impersonate(impersonate, generate_fingerprint())
        self._ua = self._fingerprint["user_agent"]
        self._impersonate_candidates = family_impersonates(impersonate)
        self._impersonate_idx = 0
        self.session = None


class RotateImpersonateSessionTests(unittest.TestCase):
    def test_returns_false_at_last_candidate(self):
        flow = _Flow()
        flow._impersonate_idx = len(flow._impersonate_candidates) - 1

        self.assertFalse(flow._rotate_impersonate_session())

    def test_rotation_advances_and_rebuilds_session(self):
        flow = _Flow()
        fake_session = mock.Mock()

        with mock.patch.object(
            session_mixin_module, "create_http_session", return_value=fake_session
        ) as factory:
            self.assertTrue(flow._rotate_impersonate_session())

        self.assertEqual(flow._impersonate_idx, 1)
        self.assertIs(flow.session, fake_session)
        # 新会话必须用新 impersonate 和同步后的 UA 建
        _, kwargs = factory.call_args
        self.assertEqual(kwargs["impersonate"], flow._impersonate_candidates[1])
        self.assertEqual(kwargs["user_agent"], flow._ua)
        self.assertEqual(kwargs["proxy"], "http://proxy.example:8080")

    def test_rotation_keeps_ua_and_client_hints_consistent(self):
        """换指纹后 UA 与 sec-ch-ua 必须来自同一个指纹（事故回归）。"""
        flow = _Flow()
        with mock.patch.object(
            session_mixin_module, "create_http_session", return_value=mock.Mock()
        ):
            flow._rotate_impersonate_session()

        fp = flow._fingerprint
        self.assertEqual(flow._ua, fp.get("user_agent"),
                         "UA 必须与新指纹同步（否则 UA 和 client hints 自相矛盾）")
        sec_ch = fp.get("sec_ch_ua", "")
        if sec_ch:
            # chrome 家族：UA 里的版本要与 sec-ch-ua 的 v= 对得上
            import re
            ua_version = re.search(r"Chrome/(\d+)", flow._ua or "")
            sec_version = re.search(r'v="(\d+)"', sec_ch)
            if ua_version and sec_version:
                self.assertEqual(
                    ua_version.group(1), sec_version.group(1),
                    f"UA 版本 {ua_version.group(1)} 与 sec-ch-ua {sec_version.group(1)} 不一致",
                )

    def test_sync_failure_keeps_flow_alive(self):
        """client hints 同步失败时沿用旧指纹（不能让流程崩）。"""
        flow = _Flow()
        old_ua = flow._ua

        with mock.patch.object(
            session_mixin_module,
            "fingerprint_for_impersonate",
            side_effect=RuntimeError("boom"),
        ), mock.patch.object(
            session_mixin_module, "create_http_session", return_value=mock.Mock()
        ):
            self.assertTrue(flow._rotate_impersonate_session())

        self.assertTrue(flow._ua, "UA 不能变空")
        self.assertEqual(flow._fingerprint.get("user_agent"), old_ua,
                         "兜底路径下指纹未被破坏")


class SwitchBrowserFamilyTests(unittest.TestCase):
    def test_switch_updates_candidates_and_resets_index(self):
        flow = _Flow("chrome136")
        flow._impersonate_idx = 2

        flow._switch_browser_family("safari18_0")

        self.assertEqual(flow._impersonate_idx, 0)
        self.assertEqual(flow._impersonate_candidates, family_impersonates("safari18_0"))
        # UA 跟着新家族走
        self.assertIn("Safari", flow._ua)


class SentinelFpKwargsTests(unittest.TestCase):
    def test_kwargs_carry_full_fingerprint(self):
        flow = _Flow()
        kwargs = flow._sentinel_fp_kwargs()

        # 关键字段全在（漏一个就会让 sentinel 的画像与 HTTP 头不一致）
        for key in (
            "user_agent", "sec_ch_ua", "sec_ch_ua_platform", "screen",
            "lang", "browser_type", "navigator_platform", "timezone",
            "hardware_concurrency", "device_pixel_ratio",
        ):
            self.assertIn(key, kwargs, f"sentinel kwargs 缺 {key}")

        self.assertEqual(kwargs["user_agent"], flow._ua)

    def test_missing_fingerprint_fields_default_safely(self):
        flow = _Flow()
        flow._fingerprint = {}
        kwargs = flow._sentinel_fp_kwargs()

        self.assertEqual(kwargs["user_agent"], flow._ua)
        self.assertEqual(kwargs["sec_ch_ua"], "")
        self.assertEqual(kwargs["hardware_concurrency"], 0)
        self.assertIsNone(kwargs["device_memory"])


class IsTlsErrorTests(unittest.TestCase):
    def test_markers_detected(self):
        for message in (
            "curl: (35) TLS connect error",
            "TLS connect error ... OPENSSL_internal",
            "ssl error: SSLError",
        ):
            self.assertTrue(
                SessionMixin._is_tls_error(RuntimeError(message)), message
            )

    def test_other_errors_not_detected(self):
        for message in ("HTTP 409", "timeout", "connection refused"):
            self.assertFalse(SessionMixin._is_tls_error(RuntimeError(message)), message)


if __name__ == "__main__":
    unittest.main()
