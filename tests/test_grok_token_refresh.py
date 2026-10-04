"""Grok token 刷新与有效性检测的契约。

背景（实测确认的三条事实）：

1. **RT 会轮换**：x.ai 的 OAuth refresh grant 每次成功都会返回**新的
   refresh_token**，旧值随即失效（实测：浏览器换到新 RT 后，新 RT 走
   refresh grant 200；本地存的旧 RT 全部 `invalid_grant`）。
2. **协议 device flow 被 CF 挡**：device/verify / device/approve 纯 HTTP
   返回 403（实测），只有真实浏览器里完成授权才走得通 —— 浏览器 flow
   实测成功（拿到全新 AT + 轮换 RT）。
3. **错误码要分类**（参考 grok2api 的 `IsPermanentCredentialRefreshErrorCode`）：
   `invalid_grant` 等是**永久失效**（账号需要重新授权）；
   `invalid_client` 等是**网关配置问题**（不该标账号失效）；
   `rate_limited` 等是**瞬时**（应重试，不该误杀账号）。

因此：
- `refresh_via_grant`：RT → 新 token（纯协议快路径），错误分类；
- `refresh_via_device_flow`：SSO → 登录协议换新 token（浏览器完成授权）；
- 两个动作：`probe_refresh`（检测有效性，顺带刷新轮换后的凭证）、
  `refresh_token`（走登录协议重新获取 token）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class ClassifyRefreshErrorTests(unittest.TestCase):
    """错误码分类与 grok2api 对齐（永久 / 配置 / 瞬时 / 未知）。"""

    def test_invalid_grant_is_permanent(self):
        from platforms.grok.token_refresh import classify_refresh_error

        self.assertEqual(classify_refresh_error(400, "invalid_grant"), "permanent")

    def test_all_permanent_codes(self):
        from platforms.grok.token_refresh import classify_refresh_error

        for code in (
            "invalid_grant",
            "invalid_refresh_token",
            "refresh_token_invalid",
            "refresh_token_expired",
            "refresh_token_revoked",
            "refresh_token_reused",
            "expired_token",
            "revoked_token",
            "missing_refresh_token",
        ):
            self.assertEqual(
                classify_refresh_error(400, code), "permanent", f"{code} 应为永久失效"
            )

    def test_invalid_client_is_configuration(self):
        """网关配置问题不该标账号失效（grok2api 同款口径）。"""
        from platforms.grok.token_refresh import classify_refresh_error

        for code in ("invalid_client", "unauthorized_client", "invalid_scope"):
            self.assertEqual(
                classify_refresh_error(400, code), "configuration", f"{code} 应为配置类"
            )

    def test_rate_limited_is_retryable(self):
        from platforms.grok.token_refresh import classify_refresh_error

        for code in ("rate_limited", "too_many_requests", "temporarily_unavailable"):
            self.assertEqual(
                classify_refresh_error(429, code), "retryable", f"{code} 应为瞬时"
            )

    def test_unknown_400_is_unknown(self):
        from platforms.grok.token_refresh import classify_refresh_error

        self.assertEqual(classify_refresh_error(400, "some_new_error"), "unknown")

    def test_http_error_body_without_code_uses_status(self):
        from platforms.grok.token_refresh import classify_refresh_error

        # 无错误码时按状态码粗判：401 → unknown（保守，不误杀）
        self.assertEqual(classify_refresh_error(401, ""), "unknown")
        # 5xx 属瞬时
        self.assertEqual(classify_refresh_error(503, ""), "retryable")


class RefreshViaGrantTests(unittest.TestCase):
    """RT → 新 token 的纯协议快路径。"""

    def setUp(self):
        from platforms.grok import token_refresh

        self.mod = token_refresh

    def _patch_post(self, response):
        return mock.patch.object(
            self.mod, "_post_form", return_value=response
        )

    def test_success_returns_tokens(self):
        resp = _FakeResponse(200, {
            "access_token": "new-at", "refresh_token": "new-rt",
            "id_token": "new-idt", "expires_in": 21600, "token_type": "Bearer",
        })
        with self._patch_post(resp):
            result = self.mod.refresh_via_grant("old-rt")
        self.assertTrue(result.ok)
        self.assertEqual(result.tokens["access_token"], "new-at")
        self.assertEqual(result.tokens["refresh_token"], "new-rt")
        self.assertEqual(result.method, "grant")

    def test_success_without_rotated_rt_keeps_old(self):
        """x.ai 未轮换时（response 无 refresh_token）保留原值。"""
        resp = _FakeResponse(200, {"access_token": "new-at", "expires_in": 3600})
        with self._patch_post(resp):
            result = self.mod.refresh_via_grant("old-rt")
        self.assertTrue(result.ok)
        self.assertEqual(result.tokens["refresh_token"], "old-rt")

    def test_invalid_grant_is_permanent_failure(self):
        resp = _FakeResponse(400, {
            "error": "invalid_grant", "error_description": "Invalid or unknown refresh token",
        })
        with self._patch_post(resp):
            result = self.mod.refresh_via_grant("dead-rt")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, "permanent")
        self.assertEqual(result.error_code, "invalid_grant")

    def test_missing_refresh_token_fails_without_request(self):
        with mock.patch.object(self.mod, "_post_form") as post:
            result = self.mod.refresh_via_grant("")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "missing_refresh_token")
        self.assertEqual(result.kind, "permanent")
        post.assert_not_called()

    def test_network_error_is_retryable(self):
        with mock.patch.object(self.mod, "_post_form", side_effect=OSError("connection reset")):
            result = self.mod.refresh_via_grant("some-rt")
        self.assertFalse(result.ok)
        self.assertEqual(result.kind, "retryable")

    def test_request_uses_refresh_grant_form(self):
        """请求体必须是 refresh_token grant（client_id 与注册一致）。"""
        captured = {}

        def fake_post(url, form, **kwargs):
            captured["url"] = url
            captured["form"] = form
            return _FakeResponse(200, {"access_token": "at", "expires_in": 3600})

        with mock.patch.object(self.mod, "_post_form", side_effect=fake_post):
            self.mod.refresh_via_grant("rt-1")

        from platforms.grok.constants import CLIENT_ID, TOKEN_URL

        self.assertEqual(captured["url"], TOKEN_URL)
        self.assertEqual(captured["form"]["grant_type"], "refresh_token")
        self.assertEqual(captured["form"]["client_id"], CLIENT_ID)
        self.assertEqual(captured["form"]["refresh_token"], "rt-1")


class RefreshViaDeviceFlowTests(unittest.TestCase):
    """SSO → 登录协议换新 token（浏览器完成授权，协议版被 CF 挡）。"""

    def setUp(self):
        from platforms.grok import token_refresh

        self.mod = token_refresh

    def test_success_via_browser(self):
        tokens = {"access_token": "at", "refresh_token": "rt", "expires_in": 21600}
        with mock.patch(
            "platforms.grok.register_browser.exchange_oauth_via_browser",
            return_value=tokens,
        ) as browser_flow:
            result = self.mod.refresh_via_device_flow("sso-cookie", proxy="socks5://p:1")
        self.assertTrue(result.ok)
        self.assertEqual(result.tokens["access_token"], "at")
        self.assertEqual(result.method, "device")
        browser_flow.assert_called_once()
        # 代理必须传给浏览器 flow（x.ai 按 IP 判风险）
        _, kwargs = browser_flow.call_args
        self.assertEqual(kwargs.get("proxy"), "socks5://p:1")

    def test_browser_failure_is_reported(self):
        with mock.patch(
            "platforms.grok.register_browser.exchange_oauth_via_browser",
            return_value={},
        ):
            result = self.mod.refresh_via_device_flow("sso-cookie")
        self.assertFalse(result.ok)
        self.assertEqual(result.method, "device")

    def test_empty_sso_fails_without_launching_browser(self):
        with mock.patch(
            "platforms.grok.register_browser.exchange_oauth_via_browser"
        ) as browser_flow:
            result = self.mod.refresh_via_device_flow("")
        self.assertFalse(result.ok)
        browser_flow.assert_not_called()


class PlatformActionTests(unittest.TestCase):
    """平台动作：probe_refresh（检测）与 refresh_token（登录协议刷新）。"""

    def setUp(self):
        from core.registry import get, load_all

        load_all()
        self.platform = get("grok")()

    def _account(self, **extra):
        from core.base_platform import Account

        return Account(
            platform="grok", email="a@b.com", password="p",
            token=extra.get("sso", ""), extra=extra,
        )

    def test_probe_refresh_success_updates_credentials(self):
        """检测成功 = RT 还能换（顺带保存轮换后的新凭证）。"""
        from platforms.grok.token_refresh import RefreshResult

        account = self._account(refresh_token="old-rt", sso="sso-1")
        fake = RefreshResult(ok=True, tokens={
            "access_token": "new-at", "refresh_token": "new-rt",
            "id_token": "new-idt", "expires_in": 21600,
        }, method="grant")
        with mock.patch("platforms.grok.token_refresh.refresh_via_grant", return_value=fake):
            result = self.platform.execute_action("probe_refresh", account, {})
        self.assertTrue(result["ok"])
        patch = result["account_extra_patch"]
        self.assertEqual(patch["access_token"], "new-at")
        self.assertEqual(patch["refresh_token"], "new-rt")
        self.assertEqual(patch["probe_status"], "refresh_ok")

    def test_probe_refresh_permanent_failure_marks_invalid(self):
        from platforms.grok.token_refresh import RefreshResult

        account = self._account(refresh_token="dead-rt", sso="sso-1")
        fake = RefreshResult(
            ok=False, error_code="invalid_grant", error="Invalid or unknown refresh token",
            kind="permanent", status=400,
        )
        with mock.patch("platforms.grok.token_refresh.refresh_via_grant", return_value=fake):
            result = self.platform.execute_action("probe_refresh", account, {})
        self.assertFalse(result["ok"])
        patch = result["account_extra_patch"]
        self.assertEqual(patch["probe_status"], "refresh_invalid")
        self.assertIn("invalid_grant", result["error"])

    def test_probe_refresh_without_rt_reports_actionable_hint(self):
        account = self._account(sso="sso-1")  # 无 RT
        result = self.platform.execute_action("probe_refresh", account, {})
        self.assertFalse(result["ok"])
        self.assertIn("refresh_token", result["error"])
        # 提示用「刷新 Token」动作（走登录协议）
        self.assertIn("刷新", result["error"])

    def test_probe_refresh_configuration_error_does_not_blame_account(self):
        """网关配置类错误不该标账号失效（提示可重试）。"""
        from platforms.grok.token_refresh import RefreshResult

        account = self._account(refresh_token="rt-1", sso="sso-1")
        fake = RefreshResult(
            ok=False, error_code="invalid_client", error="client misconfigured",
            kind="configuration", status=400,
        )
        with mock.patch("platforms.grok.token_refresh.refresh_via_grant", return_value=fake):
            result = self.platform.execute_action("probe_refresh", account, {})
        self.assertFalse(result["ok"])
        patch = result["account_extra_patch"]
        # 不写 refresh_invalid —— 账号没错，是网关的问题
        self.assertNotEqual(patch.get("probe_status"), "refresh_invalid")
        self.assertIn("重试", result["error"])

    def test_refresh_token_action_uses_device_flow_and_saves(self):
        """刷新 Token：走登录协议（SSO），保存全新凭证。"""
        from platforms.grok.token_refresh import RefreshResult

        account = self._account(sso="sso-1", refresh_token="old-rt")
        fake = RefreshResult(ok=True, tokens={
            "access_token": "fresh-at", "refresh_token": "fresh-rt",
            "id_token": "fresh-idt", "expires_in": 21600, "token_type": "Bearer",
        }, method="device")
        with mock.patch(
            "platforms.grok.token_refresh.refresh_via_device_flow", return_value=fake
        ) as flow:
            result = self.platform.execute_action("refresh_token", account, {})
        self.assertTrue(result["ok"])
        flow.assert_called_once()
        patch = result["account_extra_patch"]
        self.assertEqual(patch["access_token"], "fresh-at")
        self.assertEqual(patch["refresh_token"], "fresh-rt")
        self.assertEqual(patch["id_token"], "fresh-idt")
        # CPA 记录同步更新（下游上传用）
        self.assertIn("cpa_record", patch)

    def test_refresh_token_action_without_sso_fails(self):
        account = self._account(refresh_token="rt-1")  # 无 SSO
        result = self.platform.execute_action("refresh_token", account, {})
        self.assertFalse(result["ok"])
        self.assertIn("SSO", result["error"])

    def test_refresh_token_action_device_failure_reported(self):
        from platforms.grok.token_refresh import RefreshResult

        account = self._account(sso="sso-1")
        fake = RefreshResult(ok=False, error="browser flow failed", method="device")
        with mock.patch(
            "platforms.grok.token_refresh.refresh_via_device_flow", return_value=fake
        ):
            result = self.platform.execute_action("refresh_token", account, {})
        self.assertFalse(result["ok"])
        self.assertIn("browser flow failed", result["error"])

    def test_actions_declared_with_labels(self):
        ids = {a["id"]: a for a in self.platform.get_platform_actions()}
        self.assertIn("probe_refresh", ids)
        self.assertIn("refresh_token", ids)
        self.assertTrue(ids["probe_refresh"]["label"])
        self.assertTrue(ids["refresh_token"]["label"])

    def test_deprecated_refresh_oauth_forwards_to_refresh_token(self):
        """旧动作 `refresh_oauth`（协议 device flow，被 CF 挡死从未成功过）
        必须转发到新实现，而不是继续走那条死路。"""
        from platforms.grok.token_refresh import RefreshResult

        account = self._account(sso="sso-1")
        fake = RefreshResult(ok=True, tokens={
            "access_token": "at", "refresh_token": "rt", "expires_in": 21600,
        }, method="device")
        with mock.patch(
            "platforms.grok.token_refresh.refresh_via_device_flow", return_value=fake
        ) as flow:
            result = self.platform.execute_action("refresh_oauth", account, {})
        self.assertTrue(result["ok"], "旧动作应转发到 refresh_token 并成功")
        flow.assert_called_once()


if __name__ == "__main__":
    unittest.main()
