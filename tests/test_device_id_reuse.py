"""设备标识（oai-did）复用：同一账号再次登录沿用注册时的设备 ID。

用户问题（2026-10-07）：「chatgpt 是会保留指纹的吧，这个指纹能复用吗，
能不能减少后续登录封号的风险。」

实测（2026-10-07，真实网络 + 真实代理）：
- 服务端**保留**客户端预置的 oai-did（预置 A → GET chatgpt.com 返回还是 A，
  不覆盖）；
- 预置 + 种 cookie 后，check_proxy → warmup → get_auth_url → auth_oauth_init
  整条授权链保持同一个设备标识。

而登录链此前**没有**复用：`rt_backfill` 协议重登 / `login_refresh` 登录兜底 /
`bind_totp_via_login` 慢路径都新起 flow 且不预置 device_id → `get_auth_url`
生成新 UUID —— 同一个账号每次登录都换一台「新设备」，正是要避免的风控特征。

设计（为什么预置值不在 seed 时就种进 cookie）：
warmup 的判据是「cookie 到底种上没有」（CF 403 只给 __cf_bm）。若 seed 提前
把 oai-did 种进 jar，判据会永远为真 —— CF 403 被误报成功、重试轮换浏览器
家族的逻辑失效。所以 seed 只记住值（result.device_id 供 ext-oai-did 参数用），
**warmup 成功后**才把 cookie 改写回预置值。

本文件钉住：
1. `seed_device_id`：设 result.device_id、**不**种 cookie；
2. warmup 成功 → cookie 改写回预置值；失败 → 不被预置值骗成成功；
3. 三条登录链把库里的 device_id 预置进 flow（login_refresh / rt_backfill
   协议重登 / bind_totp_via_login 慢路径）。
"""

from __future__ import annotations

import unittest
from unittest import mock

D = "dddddddd-1111-2222-3333-444444444444"


class _FakeCookies:
    def __init__(self, jar=None):
        self._jar = dict(jar or {})

    def set(self, name, value, domain=None, **kwargs):
        self._jar[name] = value

    def get_dict(self):
        return dict(self._jar)

    def get(self, name, default=None, **kwargs):
        return self._jar.get(name, default)


class _FakeSession:
    """warmup/check_proxy 需要的最小会话形状。"""

    def __init__(self, jar=None, status=200, text="ip=1.2.3.4\nloc=US\n"):
        self.cookies = _FakeCookies(jar)
        self._status = status
        self._text = text

    def get(self, url, headers=None, timeout=None, **kwargs):
        return mock.Mock(status_code=self._status, text=self._text)


class SeedDeviceIdTests(unittest.TestCase):
    """AuthFlow.seed_device_id：预置 result.device_id；cookie 推迟到 warmup 后。"""

    def _flow(self):
        from platforms.chatgpt.protocol import AuthFlow, Config

        return AuthFlow(Config(proxy=None))

    def test_seed_sets_result_device_id(self):
        flow = self._flow()
        flow.seed_device_id(D)
        self.assertEqual(flow.result.device_id, D)

    def test_seed_does_not_plant_cookie(self):
        """seed 不得提前种 cookie —— 那会让 warmup 的判据失去意义。"""
        flow = self._flow()
        flow.seed_device_id(D)
        self.assertNotIn(
            "oai-did",
            flow.session.cookies.get_dict(),
            "seed 提前种了 cookie：warmup 的「是否种上」判据会永远为真，"
            "CF 403 会被误报成功",
        )

    def test_seed_empty_keeps_anonymous(self):
        """没有 device_id（注册链）→ 不生成随机值、不种 cookie。"""
        flow = self._flow()
        flow.seed_device_id("")
        self.assertEqual(flow.result.device_id, "")
        self.assertNotIn("oai-did", flow.session.cookies.get_dict())

    def test_seed_whitespace_is_ignored(self):
        flow = self._flow()
        flow.seed_device_id("   ")
        self.assertEqual(flow.result.device_id, "")


class WarmupReseedTests(unittest.TestCase):
    """warmup：成功时改写回预置值；失败判据不被预置值污染。"""

    def _flow_with_session(self, session):
        from platforms.chatgpt.protocol import AuthFlow, Config

        flow = AuthFlow(Config(proxy=None))
        flow.seed_device_id(D)
        flow.session = session
        return flow

    def test_warmup_success_rewrites_server_value_back_to_seeded(self):
        """服务端种了自己的值 → 成功时改写回预置的 D。

        实测服务端保留客户端预置的 oai-did；改写保证后续 auth_oauth_init
        读到的 cookie 与 ext-oai-did 参数是同一个值（不会中途换成别的设备）。
        """
        from platforms.chatgpt.protocol import auth_flow as auth_flow_module

        flow = self._flow_with_session(
            _FakeSession({"__cf_bm": "x", "oai-did": "server-assigned-value"})
        )

        with mock.patch.object(auth_flow_module.time, "sleep", lambda *_: None):
            ok = flow.warmup()

        self.assertTrue(ok)
        self.assertEqual(
            flow.session.cookies.get_dict().get("oai-did"),
            D,
            "warmup 成功后设备标识没有回到预置值",
        )
        self.assertEqual(flow.result.device_id, D)

    def test_warmup_success_without_seed_keeps_server_value(self):
        """没有预置（注册链）→ 服务端的值原样保留，行为不变。"""
        from platforms.chatgpt.protocol import auth_flow as auth_flow_module

        flow = self._flow_with_session(
            _FakeSession({"__cf_bm": "x", "oai-did": "server-assigned-value"})
        )
        flow._device_id_seed = ""

        with mock.patch.object(auth_flow_module.time, "sleep", lambda *_: None):
            ok = flow.warmup()

        self.assertTrue(ok)
        self.assertEqual(
            flow.session.cookies.get_dict().get("oai-did"), "server-assigned-value"
        )

    def test_warmup_failure_is_not_faked_by_seeded_id(self):
        """CF 403 一直拦 → warmup 必须如实失败。

        回归风险：若 seed 或重试重建时把预置值种进 jar，判据
        `"oai-did" in cookies` 会立即满足 —— 明明一次都没种上却被报成功，
        重试轮换浏览器家族的逻辑失效，后续 409 invalid_state 的排查方向
        也被带偏。
        """
        from platforms.chatgpt.protocol import auth_flow as auth_flow_module

        flow = self._flow_with_session(_FakeSession({"__cf_bm": "x"}, status=403))
        rebuilt = _FakeSession({"__cf_bm": "x"}, status=403)

        with mock.patch.object(
            auth_flow_module, "create_http_session", return_value=rebuilt
        ), mock.patch.object(auth_flow_module.time, "sleep", lambda *_: None):
            ok = flow.warmup()

        self.assertFalse(ok, "warmup 被预置的设备标识骗成了「成功」")


class CheckProxyRebuildTests(unittest.TestCase):
    """check_proxy 重建会话（换出口 IP）后：种子保留、cookie 不预种。"""

    def test_check_proxy_rebuild_keeps_seed_without_planting_cookie(self):
        from platforms.chatgpt.protocol import auth_flow as auth_flow_module
        from platforms.chatgpt.protocol import AuthFlow, Config

        flow = AuthFlow(Config(proxy=None))
        flow.seed_device_id(D)
        rebuilt = _FakeSession()

        with mock.patch.object(
            auth_flow_module, "create_http_session", return_value=rebuilt
        ):
            ok = flow.check_proxy()

        self.assertTrue(ok)
        self.assertIs(flow.session, rebuilt, "网络探测应按国家码重建过会话")
        self.assertEqual(flow.result.device_id, D, "重建后种子不该丢")
        self.assertNotIn(
            "oai-did",
            rebuilt.cookies.get_dict(),
            "check_proxy 重建后不应预种设备 cookie —— warmup 判据要能看到真相",
        )


class LoginRefreshSeedsDeviceIdTests(unittest.TestCase):
    """login_refresh（刷新 Token 的登录兜底）复用库里的 device_id。"""

    def test_build_flow_seeds_stored_device_id(self):
        from platforms.chatgpt.login_refresh import LoginAccessTokenRefresher

        refresher = LoginAccessTokenRefresher(
            email="a@b.c", password="pw", device_id=D
        )
        flow = refresher._build_flow()

        self.assertEqual(flow.result.device_id, D)

    def test_build_flow_without_device_id_is_anonymous(self):
        from platforms.chatgpt.login_refresh import LoginAccessTokenRefresher

        refresher = LoginAccessTokenRefresher(email="a@b.c", password="pw")
        flow = refresher._build_flow()

        self.assertEqual(flow.result.device_id, "")


class RtBackfillSeedsDeviceIdTests(unittest.TestCase):
    """补 RT 的协议重登同样复用 device_id（会话复用那条路已复用）。"""

    def test_try_login_seeds_device_id_before_relogin(self):
        from platforms.chatgpt.rt_backfill import RefreshTokenBackfiller

        backfiller = RefreshTokenBackfiller(
            email="a@b.c", password="pw", device_id=D, extra_config={}
        )

        with mock.patch("platforms.chatgpt.rt_backfill.AuthFlow") as flow_cls:
            backfiller._try_login()

        flow_cls.return_value.seed_device_id.assert_called_once_with(D)
        flow_cls.return_value.run_protocol_login.assert_called_once()


class BindTotpSlowPathSeedsDeviceIdTests(unittest.TestCase):
    """绑 2FA 慢路径（重登）同样复用 device_id。"""

    def test_slow_path_seeds_device_id(self):
        from platforms.chatgpt.protocol import two_factor

        with mock.patch.object(two_factor, "AuthFlow") as flow_cls, mock.patch.object(
            two_factor, "_login_for_access_token", return_value=("", "boom")
        ):
            two_factor.bind_totp_via_login(
                mock.Mock(), "a@b.c", "pw", device_id=D
            )

        flow_cls.return_value.seed_device_id.assert_called_once_with(D)


class ConvergenceTests(unittest.TestCase):
    """没有存量 device_id 的账号：首次登录收敛（服务端值落库），后续复用。

    存量账号里有一批（注册早于 device_id 落库、或 cookies 里有但字段没写）
    没有 `extra.device_id`。这些账号登录链会拿到服务端分配的 oai-did ——
    必须把它带回结果并落库，否则每次登录都重新拿一个服务端值，永远收敛
    不到一台「稳定设备」。
    """

    def test_resolve_device_id_prefers_the_field(self):
        from platforms.chatgpt.device_id import resolve_device_id

        extra = {
            "device_id": "field-value",
            "cookies": "a=1; oai-did=cookie-value; b=2",
        }
        self.assertEqual(resolve_device_id(extra), "field-value")

    def test_resolve_device_id_falls_back_to_cookie(self):
        """存量账号：字段没写但 cookies 里有 —— 用 cookie 里的原始设备值。"""
        from platforms.chatgpt.device_id import resolve_device_id

        extra = {"cookies": "a=1; oai-did=cookie-value; b=2"}
        self.assertEqual(resolve_device_id(extra), "cookie-value")

    def test_resolve_device_id_empty_when_absent(self):
        from platforms.chatgpt.device_id import resolve_device_id

        self.assertEqual(resolve_device_id({}), "")
        self.assertEqual(resolve_device_id({"cookies": "a=1; b=2"}), "")
        self.assertEqual(resolve_device_id({"device_id": "  "}), "")

    def test_plugin_uses_cookie_fallback_for_login_refresh(self):
        """刷新 Token 的登录兜底：字段空、cookie 有 → 用 cookie 值预置。"""
        from platforms.chatgpt.login_refresh import LoginRefreshResult
        from platforms.chatgpt.token_refresh import TokenRefreshResult
        from unittest.mock import patch

        result = TokenRefreshResult(
            success=True, verified=True, refreshed=False, access_token="same-at",
            strategy="session",
        )
        login_result = LoginRefreshResult(
            success=True, access_token="fresh-at", strategy="password_2fa",
        )
        with patch(
            "services.chatgpt_otp_mailbox.resolve_otp_mail_provider",
            return_value=(None, "不在号池里"),
        ), patch(
            "platforms.chatgpt.login_refresh.LoginAccessTokenRefresher"
        ) as refresher_cls, patch(
            "platforms.chatgpt.token_refresh.TokenRefreshManager"
        ) as verifier_cls:
            refresher_cls.return_value.run.return_value = login_result
            verifier_cls.return_value.verify_access_token.return_value = (True, "")

            from core.base_platform import RegisterConfig
            from platforms.chatgpt.plugin import ChatGPTPlatform

            platform = ChatGPTPlatform(config=RegisterConfig())

            class _Account:
                email = "user@example.com"
                password = "pw"
                extra = {"cookies": "x=1; oai-did=from-cookie; y=2"}

            platform._refresh_via_login(_Account(), result)

        refresher_cls.assert_called_once()
        self.assertEqual(
            refresher_cls.call_args.kwargs.get("device_id"),
            "from-cookie",
            "字段为空时应从 cookies 里回退提取 oai-did",
        )

    def test_login_refresh_result_absorbs_flow_device_id(self):
        from platforms.chatgpt.login_refresh import (
            LoginAccessTokenRefresher,
            LoginRefreshResult,
        )

        refresher = LoginAccessTokenRefresher(email="a@b.c", password="pw")
        flow = refresher._build_flow()
        refresher._active_flow = flow
        flow.result.device_id = "server-assigned-1"

        result = LoginRefreshResult()
        refresher._absorb(result)

        self.assertEqual(result.device_id, "server-assigned-1")

    def test_rt_backfill_result_absorbs_flow_device_id(self):
        from platforms.chatgpt.rt_backfill import BackfillResult, RefreshTokenBackfiller

        backfiller = RefreshTokenBackfiller(email="a@b.c", password="pw", extra_config={})
        flow = mock.Mock()
        flow.result = mock.Mock(
            device_id="server-assigned-2", refresh_token="", access_token="",
            session_token="", id_token="", cookie_header="",
        )
        result = BackfillResult(success=False, email="a@b.c")
        backfiller._absorb(result, flow)

        self.assertEqual(result.device_id, "server-assigned-2")

    def test_backfill_patch_includes_device_id_when_present(self):
        from platforms.chatgpt.rt_backfill import BackfillResult
        from services.chatgpt_rt_backfill import build_extra_patch

        result = BackfillResult(
            success=True, email="a@b.c", refresh_token="rt", device_id="did-3"
        )
        patch = build_extra_patch(result)

        self.assertEqual(patch.get("device_id"), "did-3")

    def test_backfill_patch_omits_empty_device_id(self):
        from platforms.chatgpt.rt_backfill import BackfillResult
        from services.chatgpt_rt_backfill import build_extra_patch

        result = BackfillResult(success=True, email="a@b.c", refresh_token="rt")
        patch = build_extra_patch(result)

        self.assertNotIn("device_id", patch)

    def test_two_factor_patch_includes_device_id_when_present(self):
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult
        from services.chatgpt_two_factor import build_extra_patch

        result = TwoFactorBindResult(ok=True, secret="SECRET", device_id="did-4")
        patch = build_extra_patch(result)

        self.assertEqual(patch.get("device_id"), "did-4")

    def test_two_factor_slow_path_carries_flow_device_id(self):
        """慢路径重登后把 flow 的设备标识带进结果（无论成败）。"""
        from platforms.chatgpt.protocol import two_factor

        with mock.patch.object(two_factor, "AuthFlow") as flow_cls, mock.patch.object(
            two_factor, "_login_for_access_token", return_value=("", "boom")
        ):
            flow_cls.return_value.result.device_id = "server-assigned-5"
            result = two_factor.bind_totp_via_login(mock.Mock(), "a@b.c", "pw")

        self.assertEqual(result.device_id, "server-assigned-5")

    def test_refresh_via_login_merges_device_id_into_result(self):
        """插件层：登录链带回的 device_id 并进 TokenRefreshResult。"""
        from platforms.chatgpt.login_refresh import LoginRefreshResult
        from platforms.chatgpt.token_refresh import TokenRefreshResult

        result = TokenRefreshResult(
            success=True, verified=True, refreshed=False, access_token="same-at",
            strategy="session",
        )
        login_result = LoginRefreshResult(
            success=True, access_token="fresh-at", strategy="password_2fa",
            device_id="did-6",
        )
        from unittest.mock import patch

        with patch(
            "services.chatgpt_otp_mailbox.resolve_otp_mail_provider",
            return_value=(None, "不在号池里"),
        ), patch(
            "platforms.chatgpt.login_refresh.LoginAccessTokenRefresher"
        ) as refresher_cls, patch(
            "platforms.chatgpt.token_refresh.TokenRefreshManager"
        ) as verifier_cls:
            refresher_cls.return_value.run.return_value = login_result
            verifier_cls.return_value.verify_access_token.return_value = (True, "")

            from core.base_platform import RegisterConfig
            from platforms.chatgpt.plugin import ChatGPTPlatform

            platform = ChatGPTPlatform(config=RegisterConfig())

            class _Account:
                email = "user@example.com"
                password = "pw"
                extra: dict = {}

            out = platform._refresh_via_login(_Account(), result)

        self.assertEqual(out.device_id, "did-6")

    def test_refresh_via_login_failure_still_carries_device_id(self):
        """失败路径也要带回 device_id：收敛发生在 warmup/登录中段，
        登录在末段失败时服务端值已在 flow 上 —— 丢掉它下次登录又是「新设备」。"""
        from platforms.chatgpt.login_refresh import LoginRefreshResult
        from platforms.chatgpt.token_refresh import TokenRefreshResult

        result = TokenRefreshResult(
            success=False, verified=False, refreshed=False, access_token="",
            strategy="session", error_message="刷新失败",
        )
        login_result = LoginRefreshResult(
            success=False, error_message="登录流程失败", strategy="password_2fa",
            device_id="did-7",
        )
        from unittest.mock import patch

        with patch(
            "services.chatgpt_otp_mailbox.resolve_otp_mail_provider",
            return_value=(None, "不在号池里"),
        ), patch(
            "platforms.chatgpt.login_refresh.LoginAccessTokenRefresher"
        ) as refresher_cls:
            refresher_cls.return_value.run.return_value = login_result

            from core.base_platform import RegisterConfig
            from platforms.chatgpt.plugin import ChatGPTPlatform

            platform = ChatGPTPlatform(config=RegisterConfig())

            class _Account:
                email = "user@example.com"
                password = "pw"
                extra: dict = {}

            out = platform._refresh_via_login(_Account(), result)

        self.assertFalse(out.success)
        self.assertEqual(out.device_id, "did-7")


class StatusProbeDeviceIdTests(unittest.TestCase):
    """探测链的设备标识读取（复审发现：重构丢过 `.cookies` 属性兜底）。

    重构前 `_extract_oai_device_id` 支持 duck-typed 对象把 cookies 挂在
    对象属性上（`getattr(account, "cookies", "")`）；切到 `resolve_device_id`
    时这层被丢掉 —— 这类对象会退到「按邮箱派生 uuid5」，探测设备与登录
    设备不再一致（docstring 的承诺失守）。
    """

    def test_cookies_attribute_is_honoured(self):
        from platforms.chatgpt.status_probe import _extract_oai_device_id

        class _Duck:
            email = "user@example.com"
            extra: dict = {}
            cookies = "a=1; oai-did=duck-device; b=2"

        self.assertEqual(_extract_oai_device_id(_Duck()), "duck-device")

    def test_field_still_wins_over_attribute(self):
        from platforms.chatgpt.status_probe import _extract_oai_device_id

        class _Duck:
            email = "user@example.com"
            extra = {"device_id": "field-device"}
            cookies = "a=1; oai-did=duck-device"

        self.assertEqual(_extract_oai_device_id(_Duck()), "field-device")


if __name__ == "__main__":
    unittest.main()
