"""Grok 测活链路的回归测试。

钉住本轮实测发现的两个 bug（都会让**所有**账号被判无效）：

  1. `probe_token` 没有 `proxy` 参数，但 `check_valid` 一直传 `proxy=` →
     每次 TypeError 被 `except Exception: return False` 吞掉。
  2. 客户端版本 `0.2.93` 被 x.ai 版本闸拦下（HTTP 426），实测
     `1.0.13+` 才是 200。

这两条都不会抛异常、只会静默返回 False —— 正是最难发现的一类。
"""

import re
import unittest
from unittest.mock import patch

import platforms.grok.plugin as plugin_mod
from core.base_platform import Account, AccountStatus, RegisterConfig
from platforms.grok.constants import CPA_GROK_HEADERS
from platforms.grok.probe import probe_token


class ProbeSignatureTests(unittest.TestCase):
    """probe_token 的签名必须与调用方一致。"""

    def test_accepts_proxy_kwarg(self):
        """check_valid 以 proxy= 调用；不接受就是 TypeError。"""
        import inspect

        params = inspect.signature(probe_token).parameters
        self.assertIn("proxy", params, "probe_token 必须接受 proxy 参数")

    def test_proxy_is_actually_used(self):
        """传了 proxy 就必须真的走代理（否则等于没传）。"""
        captured = {}

        class _FakeResp:
            status_code = 200
            text = '{"ok":true}'

        def _fake_post(url, **kwargs):
            captured.update(kwargs)
            return _FakeResp()

        with patch("curl_cffi.requests.post", _fake_post):
            code, _ = probe_token(
                "tok", proxy="http://u:p@1.2.3.4:8080", warmup=False, retries=1
            )
        self.assertEqual(code, 200)
        self.assertIn("proxies", captured, "必须把 proxy 传给 curl_cffi")
        self.assertEqual(captured["proxies"]["https"], "http://u:p@1.2.3.4:8080")

    def test_no_proxy_means_direct(self):
        captured = {}

        class _FakeResp:
            status_code = 200
            text = "{}"

        def _fake_post(url, **kwargs):
            captured.update(kwargs)
            return _FakeResp()

        with patch("curl_cffi.requests.post", _fake_post):
            probe_token("tok", warmup=False, retries=1)
        self.assertNotIn("proxies", captured, "不传 proxy 时应直连")


class ClientVersionTests(unittest.TestCase):
    """客户端版本必须 >= x.ai 的版本闸（实测 0.2.x → 426）。"""

    def test_version_meets_xai_floor(self):
        ver = CPA_GROK_HEADERS["x-grok-client-version"]
        # 容错解析：允许 "1.0.40-rc1" 这类带后缀的覆盖值，别让测试本身炸掉
        numeric = re.match(r"(\d+)\.(\d+)", str(ver))
        self.assertIsNotNone(numeric, f"版本号无法解析: {ver!r}")
        major, minor = int(numeric.group(1)), int(numeric.group(2))
        self.assertGreaterEqual(
            (major, minor), (1, 0),
            f"x.ai 要求 >= 1.0.13，当前 {ver} 会被 426 拦下",
        )

    def test_user_agent_matches_version(self):
        """UA 里的版本号必须与 header 一致（不一致本身就是可疑信号）。"""
        ver = CPA_GROK_HEADERS["x-grok-client-version"]
        self.assertIn(ver, CPA_GROK_HEADERS["User-Agent"])


class CheckValidTests(unittest.TestCase):
    """check_valid 的判定语义。"""

    def _plat(self):
        return plugin_mod.GrokPlatform(
            RegisterConfig(executor_type="protocol", extra={}), mailbox=None
        )

    def _acct(self):
        return Account(
            platform="grok", email="t@icloud.com", password="p",
            user_id="sub-1", token="sso", status=AccountStatus.REGISTERED,
            extra={"access_token": "at", "sso": "sso"},
        )

    def test_200_is_valid(self):
        with patch("platforms.grok.plugin.probe_token", return_value=(200, "{}")):
            self.assertTrue(self._plat().check_valid(self._acct()))

    def test_402_no_credits_is_still_valid(self):
        """凭证有效但没额度 —— 不能判死（实测新账号就是 402）。"""
        body = '{"code":"personal-team-blocked:spending-limit","error":"run out of credits"}'
        with patch("platforms.grok.plugin.probe_token", return_value=(402, body)):
            self.assertTrue(self._plat().check_valid(self._acct()))

    def test_401_is_invalid(self):
        with patch("platforms.grok.plugin.probe_token",
                   return_value=(401, "unauthorized")):
            self.assertFalse(self._plat().check_valid(self._acct()))

    def test_403_token_revoked_is_invalid(self):
        """403 但带 invalid 字样 = 凭证真废了，仍须判无效。"""
        with patch("platforms.grok.plugin.probe_token",
                   return_value=(403, '{"error":"token invalid"}'),
                   ):
            self.assertFalse(self._plat().check_valid(self._acct()))

    def test_426_version_gate_is_not_valid(self):
        """版本闸 426 不是「账号有效」——它意味着探测本身没生效。"""
        with patch("platforms.grok.plugin.probe_token",
                   return_value=(426, "CLI version outdated")):
            self.assertFalse(self._plat().check_valid(self._acct()))

    def test_check_valid_does_not_swallow_signature_errors(self):
        """回归：proxy 参数缺失时 check_valid 会静默返回 False。
        这里断言它确实把 proxy 传下去了（用真签名校验，不用 mock）。

        2026-10-06：探测主体抽到 `probe_account_detail`（check_valid 变薄
        包装，细节供状态落库用），pin 的位置跟着移。
        """
        import inspect

        from platforms.grok.probe import probe_token as real_probe

        params = inspect.signature(real_probe).parameters
        self.assertIn("proxy", params)
        # 且 plugin 里的调用确实带 proxy
        src = inspect.getsource(plugin_mod.GrokPlatform.probe_account_detail)
        self.assertIn("proxy=", src, "probe_account_detail 应当把 proxy 传下去")


class ProbeVerdictConsistencyTests(unittest.TestCase):
    """`_probe_verdict` 必须被所有调用点共用 —— 否则同一账号两种结论。

    实测踩过：`check_valid` 已按「402/403 无额度仍有效」判定，但
    `execute_action`（账号池的「测活」按钮）里还单独写着 `code == 200`，
    于是同一个账号在按钮下报失败、在有效性检查里却算通过。
    """

    def _plat(self):
        return plugin_mod.GrokPlatform(
            RegisterConfig(executor_type="protocol", extra={}), mailbox=None
        )

    def test_verdict_semantics(self):
        v = self._plat()._probe_verdict
        cases = [
            (200, "{}", True, "200 有效"),
            (402, '{"code":"personal-team-blocked:spending-limit"}', True,
             "402 无额度仍有效"),
            (403, "permission-denied", True, "403 权限类仍有效"),
            (403, '{"error":"token invalid"}', False, "403 带 invalid 判无效"),
            (401, "unauthorized", False, "401 无效"),
            (426, "CLI version outdated", False, "426 版本闸不算有效"),
            (None, "connection error", False, "请求失败无效"),
        ]
        for code, summary, expected, label in cases:
            ok, _reason = v(code, summary)
            self.assertEqual(ok, expected, f"{label}: 期望 {expected}，得到 {ok}")

    def test_execute_action_uses_shared_verdict(self):
        """execute_action 的「测活」不能再自己写 code == 200。"""
        import inspect

        src = inspect.getsource(plugin_mod.GrokPlatform.execute_action)
        self.assertIn("_probe_verdict", src,
                      "execute_action 必须复用 _probe_verdict，不能各判各的")
        self.assertNotIn('"ok": code == 200', src,
                         "不该保留独立的 code == 200 判定")

    def test_execute_action_reports_402_as_ok(self):
        """端到端：402 的账号在「测活」动作里必须返回 ok=True。"""
        plat = self._plat()
        account = Account(
            platform="grok", email="t@icloud.com", password="p",
            user_id="sub-1", token="sso", status=AccountStatus.REGISTERED,
            extra={"access_token": "at", "sso": "sso"},
        )
        with patch("platforms.grok.plugin.probe_token",
                   return_value=(402, '{"code":"personal-team-blocked:spending-limit"}')):
            out = plat.execute_action("probe", account, {})
        self.assertTrue(out["ok"], f"402 应算有效，实际 error={out.get('error')!r}")
        self.assertEqual(out["data"]["status"], 402)


if __name__ == "__main__":
    unittest.main()
