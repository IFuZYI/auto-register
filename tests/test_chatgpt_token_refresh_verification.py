"""刷新 Token 的真实验证 / 登录兜底 / 封禁识别。"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from core.base_platform import AccountStatus
from platforms.chatgpt.login_refresh import (
    LoginAccessTokenRefresher,
    LoginRefreshResult,
    looks_like_banned,
)
from platforms.chatgpt.rt_backfill import MailboxUnavailableProvider
from platforms.chatgpt.token_refresh import TokenRefreshManager, TokenRefreshResult

# 用户给的原文（OpenAI 对已停用账号的措辞）
DEACTIVATED_TEXT = (
    "You do not have an account because it has been deleted or deactivated. "
    "If you believe this was an error, please contact us through our help center at help.openai.com."
)


class FakeResponse:
    def __init__(self, status_code: int, body: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._body = body or {}
        self.text = text

    def json(self):
        return self._body


class _FakeCookies:
    """假 cookie jar：`refresh_by_session_token` 会往里 set session cookie。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    def set(self, name, value, domain=None, path=None):
        self.store[name] = value


class FakeSession:
    def __init__(self, response=None, error: Exception | None = None):
        self._response = response
        self._error = error
        self.calls: list[tuple[str, dict]] = []
        self.cookies = _FakeCookies()

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers or {}))
        if self._error:
            raise self._error
        return self._response


def _manager_with_session(session) -> TokenRefreshManager:
    manager = TokenRefreshManager()
    manager._create_session = lambda: session  # type: ignore[method-assign]
    return manager


class VerifyAccessTokenTests(unittest.TestCase):
    """刷出来的 AT 到底能不能用 —— 不能只看刷新接口的 200。"""

    def test_a_working_token_passes(self):
        manager = _manager_with_session(FakeSession(FakeResponse(200, {"email": "a@b.c"})))
        ok, reason = manager.verify_access_token("fresh-token")
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_a_rejected_token_fails_and_says_so(self):
        manager = _manager_with_session(FakeSession(FakeResponse(401)))
        ok, reason = manager.verify_access_token("dead-token")
        self.assertFalse(ok)
        self.assertIn("401", reason)

    def test_network_errors_are_not_reported_as_a_dead_token(self):
        """网络抖动不该把刚刷好的 AT 判死 —— 原因要能区分开。"""
        manager = _manager_with_session(FakeSession(error=RuntimeError("connection reset")))
        ok, reason = manager.verify_access_token("maybe-fine")
        self.assertFalse(ok)
        self.assertIn("网络", reason)
        self.assertIn("不代表 AT 无效", reason)

    def test_empty_token_fails_without_a_request(self):
        session = FakeSession(FakeResponse(200))
        manager = _manager_with_session(session)
        ok, reason = manager.verify_access_token("")
        self.assertFalse(ok)
        self.assertEqual(session.calls, [])

    def test_the_probe_uses_the_bearer_header(self):
        session = FakeSession(FakeResponse(200))
        manager = _manager_with_session(session)
        manager.verify_access_token("tok-123")
        url, headers = session.calls[0]
        self.assertEqual(url, "https://chatgpt.com/backend-api/me")
        self.assertEqual(headers["authorization"], "Bearer tok-123")


class RefreshAccountFallbackTests(unittest.TestCase):
    """刷新链路：session → oauth，每条都校验；没通过就交给登录兜底。"""

    def _account(self, **overrides):
        class _Account:
            email = "user@example.com"
            password = "pw"
            session_token = ""
            refresh_token = ""
            client_id = ""
            access_token = ""
        account = _Account()
        for key, value in overrides.items():
            setattr(account, key, value)
        return account

    def test_oauth_result_is_verified_before_being_reported(self):
        manager = _manager_with_session(FakeSession(FakeResponse(200)))
        manager.refresh_by_oauth_token = lambda **_: TokenRefreshResult(  # type: ignore[method-assign]
            success=True, access_token="fresh"
        )
        result = manager.refresh_account(self._account(refresh_token="rt"))
        self.assertTrue(result.success)
        self.assertTrue(result.verified)

    def test_a_token_that_fails_verification_is_not_reported_as_verified(self):
        manager = _manager_with_session(FakeSession(FakeResponse(401)))
        manager.refresh_by_oauth_token = lambda **_: TokenRefreshResult(  # type: ignore[method-assign]
            success=True, access_token="junk"
        )
        result = manager.refresh_account(self._account(refresh_token="rt"))
        # success 保留「刷新调用本身成功了」，但 verified=False 让调用方知道别写库
        self.assertTrue(result.success)
        self.assertFalse(result.verified)
        self.assertIn("401", result.verify_message)

    def test_session_token_is_tried_first_and_skipped_when_unverified(self):
        manager = _manager_with_session(FakeSession(FakeResponse(401)))
        manager.refresh_by_session_token = lambda *_: TokenRefreshResult(  # type: ignore[method-assign]
            success=True, access_token="session-junk"
        )
        manager.refresh_by_oauth_token = lambda **_: TokenRefreshResult(  # type: ignore[method-assign]
            success=True, access_token="oauth-good"
        )
        result = manager.refresh_account(
            self._account(session_token="st", refresh_token="rt")
        )
        self.assertEqual(result.access_token, "oauth-good")

    def test_failed_oauth_keeps_the_unverified_session_result(self):
        """OAuth 失败时不能把 session 的线索丢掉 —— 登录兜底就靠它触发。

        回归：两条路都失败时直接 return OAuth 的失败结果，`success=True +
        verified=False` 那个信号永远到不了调用方，于是「刷新失败」而不是
        「去走登录流程」，最后一条拿 AT 的路被跳过。
        """
        manager = _manager_with_session(FakeSession(FakeResponse(401)))
        manager.refresh_by_session_token = lambda *_: TokenRefreshResult(  # type: ignore[method-assign]
            success=True, access_token="session-junk"
        )
        manager.refresh_by_oauth_token = lambda **_: TokenRefreshResult(  # type: ignore[method-assign]
            success=False, error_message="invalid_grant"
        )
        result = manager.refresh_account(
            self._account(session_token="st", refresh_token="rt")
        )
        # 保留 session 的「拿到了但没用」信号，调用方据此去走登录兜底
        self.assertTrue(result.success, "OAuth 失败把 session 的结果覆盖掉了")
        self.assertFalse(result.verified)
        self.assertEqual(result.strategy, "session")

    def test_failed_oauth_without_session_still_reports_oauth_error(self):
        """没有 session 结果可回看时，照实报 OAuth 的失败原因。"""
        manager = _manager_with_session(FakeSession(FakeResponse(401)))
        manager.refresh_by_oauth_token = lambda **_: TokenRefreshResult(  # type: ignore[method-assign]
            success=False, error_message="invalid_grant"
        )
        result = manager.refresh_account(self._account(refresh_token="rt"))
        self.assertFalse(result.success)
        self.assertEqual(result.error_message, "invalid_grant")


class SessionBannedDetectionTests(unittest.TestCase):
    """session 端点对已停用账号会在响应体里写明「号没了」的措辞 —— 必须读出来。

    用户原文（2026-10-06）：You do not have an account because it has been
    deleted or deactivated. …（= 账号被停用的报错）。

    此前非 200 分支只报「HTTP 401」，响应体从不读 —— 号被停用与
    「session 过期」表现完全一样，封号被当普通失败重试。
    """

    def _account(self, **overrides):
        class _Account:
            email = "user@example.com"
            password = "pw"
            session_token = ""
            refresh_token = ""
            client_id = ""
            access_token = ""

        account = _Account()
        for key, value in overrides.items():
            setattr(account, key, value)
        return account

    def test_deactivated_body_marks_banned(self):
        session = FakeSession(
            FakeResponse(
                401,
                {"error": {"code": "account_deactivated", "message": DEACTIVATED_TEXT}},
                text='{"error":{"code":"account_deactivated","message":"' + DEACTIVATED_TEXT + '"}}',
            )
        )
        manager = _manager_with_session(session)
        result = manager.refresh_by_session_token("st-old")
        self.assertFalse(result.success)
        self.assertTrue(result.banned, "session 端点回了停用措辞，却没被认成封禁")

    def test_plain_401_is_not_banned(self):
        session = FakeSession(FakeResponse(401, {"error": "unauthorized"}, text="unauthorized"))
        manager = _manager_with_session(session)
        result = manager.refresh_by_session_token("st-old")
        self.assertFalse(result.success)
        self.assertFalse(result.banned, "普通 401 被误判成封禁")

    def test_banned_session_result_skips_oauth_and_login(self):
        """封号是终局结论：session 已明确「号没了」，不再试 OAuth。"""
        manager = _manager_with_session(
            FakeSession(FakeResponse(403, text=DEACTIVATED_TEXT))
        )
        manager.refresh_by_oauth_token = lambda **_: TokenRefreshResult(  # type: ignore[method-assign]
            success=True, access_token="should-not-be-tried"
        )
        result = manager.refresh_account(
            self._account(session_token="st", refresh_token="rt")
        )
        self.assertFalse(result.success)
        self.assertTrue(result.banned)
        self.assertNotEqual(result.access_token, "should-not-be-tried")


class BannedDetectionTests(unittest.TestCase):
    """封禁措辞识别 —— 用户原文里的那句必须能认出来。"""

    def test_the_exact_message_from_the_request_is_recognized(self):
        self.assertTrue(looks_like_banned(DEACTIVATED_TEXT))

    def test_ordinary_failures_are_not_banned(self):
        for text in ("invalid_grant", "invalid_state", "network timeout", ""):
            self.assertFalse(looks_like_banned(text), text)

    def test_banned_account_status_value_exists(self):
        self.assertEqual(AccountStatus.BANNED.value, "banned")
        # 封禁账号的邮箱仍被这个平台占用，不该被当成"没注册过"
        self.assertTrue(AccountStatus.counts_as_registered(AccountStatus.BANNED.value))
        # 但不该进测活/重试队列
        self.assertFalse(AccountStatus.is_active(AccountStatus.BANNED.value))

    def test_login_result_surfaces_the_banned_verdict(self):
        result = LoginRefreshResult(success=False, banned=True, error_message=DEACTIVATED_TEXT)
        self.assertIn("已封禁", result.summary())


class LoginRefresherTests(unittest.TestCase):
    """登录流程重取 AT 的组装与失败路径。"""

    def test_missing_password_fails_fast_with_a_clear_reason(self):
        result = LoginAccessTokenRefresher(email="a@b.c", password="").run()
        self.assertFalse(result.success)
        self.assertIn("没有密码", result.error_message)

    def test_missing_email_fails_fast(self):
        result = LoginAccessTokenRefresher(email="", password="pw").run()
        self.assertFalse(result.success)
        self.assertIn("没有邮箱", result.error_message)

    def test_login_reuses_the_protocol_chain_instead_of_hand_rolled_http(self):
        """登录链必须复用 AuthFlow —— 手搓必然 409 invalid_state。"""
        refresher = LoginAccessTokenRefresher(email="a@b.c", password="pw")
        with patch("platforms.chatgpt.login_refresh.AuthFlow") as flow_cls:
            flow_cls.return_value.result = type(
                "R", (), {"access_token": "at", "session_token": "", "refresh_token": "",
                          "id_token": "", "cookie_header": "", "totp_secret": ""}
            )()
            result = refresher.run()
        self.assertTrue(result.success)
        self.assertEqual(result.access_token, "at")
        self.assertEqual(flow_cls.return_value.run_protocol_login.call_count, 1)

    def test_a_mailbox_that_is_not_in_the_pool_fails_with_a_human_reason(self):
        """邮箱没入池：等码前就说清，而不是等满一个超时。"""
        provider = MailboxUnavailableProvider("a@b.c", "不在号池里")
        with self.assertRaises(RuntimeError) as ctx:
            provider.wait_for_otp("a@b.c", timeout=1)
        self.assertIn("不在号池里", str(ctx.exception))

    def test_banned_error_from_the_chain_marks_the_result_banned(self):
        refresher = LoginAccessTokenRefresher(email="a@b.c", password="pw")
        with patch("platforms.chatgpt.login_refresh.AuthFlow") as flow_cls:
            # result 要像真实的 AuthResult：异常发生在拿到凭证之前，字段还是空的
            flow_cls.return_value.result = type(
                "R", (), {"access_token": "", "session_token": "", "refresh_token": "",
                          "id_token": "", "cookie_header": "", "totp_secret": ""}
            )()
            flow_cls.return_value.run_protocol_login.side_effect = RuntimeError(DEACTIVATED_TEXT)
            result = refresher.run()
        self.assertFalse(result.success)
        self.assertTrue(result.banned)


class LoginChainVerificationTests(unittest.TestCase):
    """登录链拿到的 AT 也要真校验 —— 用户要求「GPT刷新token你要确认AT真的更新了」。

    回归背景：`_refresh_via_login` 此前把 `result.verified = True` 写死 ——
    登录链刚跑完就假定可用，若服务端返回一个已失效的令牌会被当成功写回库。
    """

    def _platform(self):
        from core.base_platform import RegisterConfig
        from platforms.chatgpt.plugin import ChatGPTPlatform

        return ChatGPTPlatform(config=RegisterConfig())

    def _account(self):
        class _A:
            email = "user@example.com"
            password = "pw"
            extra: dict = {}

        return _A()

    def _run(self, verify_ok: bool, verify_reason: str = ""):
        from unittest.mock import patch

        from platforms.chatgpt.login_refresh import LoginRefreshResult
        from platforms.chatgpt.token_refresh import TokenRefreshResult

        result = TokenRefreshResult(success=True, access_token="stale", verified=False)
        login_result = LoginRefreshResult(
            success=True,
            access_token="fresh-at",
            refresh_token="fresh-rt",
            session_token="fresh-st",
            strategy="password_2fa",
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
            verifier_cls.return_value.verify_access_token.return_value = (
                verify_ok,
                verify_reason,
            )
            out = self._platform()._refresh_via_login(self._account(), result)
        return out, verifier_cls

    def test_verified_login_at_is_reported_as_success(self):
        out, verifier_cls = self._run(verify_ok=True)
        self.assertTrue(out.success)
        self.assertTrue(out.verified)
        self.assertEqual(out.access_token, "fresh-at")
        # 校验必须真的发生（打了 /backend-api/me），而不是写死 True
        verifier_cls.return_value.verify_access_token.assert_called_once_with("fresh-at")

    def test_unverified_login_at_is_reported_as_failure(self):
        """登录链拿到的 AT 未通过校验 → 不写库（success=False）。"""
        out, _ = self._run(verify_ok=False, verify_reason="新 AT 被服务端拒绝（HTTP 401）")
        self.assertFalse(out.success, "未通过校验的 AT 不该被当成功")
        self.assertFalse(out.verified)
        self.assertIn("未通过校验", out.error_message)


class SessionTokenRotationTests(unittest.TestCase):
    """session 端点每次响应都会轮换 session token（实测 2026-10-06）。

    OpenAI 对 `/api/auth/session` 的每次响应都会在 JSON 里下发**新的**
    `sessionToken`（滑动窗口）。此前代码只读 `accessToken`，轮换后的新值
    从未保存 —— 长期不更新会让旧值滑出窗口后失效。必须保存回来。
    """

    def test_rotated_session_token_is_captured(self):
        session = FakeSession(
            FakeResponse(200, {"accessToken": "at-1", "sessionToken": "st-rotated"})
        )
        manager = _manager_with_session(session)
        result = manager.refresh_by_session_token("st-old")
        self.assertTrue(result.success)
        self.assertEqual(
            result.session_token, "st-rotated",
            "响应里的新 sessionToken 没有被保存 —— 轮换值丢了",
        )

    def test_missing_rotation_keeps_previous_value(self):
        """响应里没有 sessionToken 时不误写空值。"""
        session = FakeSession(FakeResponse(200, {"accessToken": "at-1"}))
        manager = _manager_with_session(session)
        result = manager.refresh_by_session_token("st-old")
        self.assertTrue(result.success)
        self.assertEqual(result.session_token, "")


class RefreshChangedFlagTests(unittest.TestCase):
    """`refreshed` 标志：AT 是否真的换发了（用户要求「确保AT真的刷新了」）。

    实测（2026-10-06）：AT 未到期时服务端返回**原值**（iat/exp 不变）——
    此时不能假装「刷新成功换新」，必须如实标记未换发。
    """

    def _account(self, **overrides):
        class _Account:
            email = "user@example.com"
            password = "pw"
            session_token = ""
            refresh_token = ""
            client_id = ""
            access_token = ""

        account = _Account()
        for key, value in overrides.items():
            setattr(account, key, value)
        return account

    def test_unchanged_at_is_not_marked_refreshed(self):
        manager = _manager_with_session(
            FakeSession(FakeResponse(200, {"accessToken": "same-at"}))
        )
        result = manager.refresh_account(
            self._account(session_token="st", access_token="same-at")
        )
        self.assertTrue(result.success)
        self.assertFalse(
            result.refreshed,
            "服务端返回的 AT 与旧值相同，refreshed 不该为 True",
        )

    def test_changed_at_is_marked_refreshed(self):
        manager = _manager_with_session(
            FakeSession(FakeResponse(200, {"accessToken": "new-at"}))
        )
        result = manager.refresh_account(
            self._account(session_token="st", access_token="old-at")
        )
        self.assertTrue(result.success)
        self.assertTrue(result.refreshed, "拿到新 AT 后 refreshed 应为 True")

    def test_oauth_always_marks_refreshed(self):
        """OAuth 路径签发的新 AT 与旧值不同 → refreshed=True。"""
        manager = _manager_with_session(FakeSession(FakeResponse(200)))
        manager.refresh_by_oauth_token = lambda **_: TokenRefreshResult(  # type: ignore[method-assign]
            success=True, access_token="oauth-new"
        )
        result = manager.refresh_account(
            self._account(refresh_token="rt", access_token="old-at")
        )
        self.assertTrue(result.refreshed)

    def test_unchanged_at_still_reports_success_when_verified(self):
        """AT 未变但验证可用 —— 刷新流程本身成功（未到期无需刷新）。"""
        manager = _manager_with_session(
            FakeSession(FakeResponse(200, {"accessToken": "same-at"}))
        )
        result = manager.refresh_account(
            self._account(session_token="st", access_token="same-at")
        )
        self.assertTrue(result.success)
        self.assertTrue(result.verified)
        self.assertFalse(result.refreshed)


class RefreshActionFallbackTests(unittest.TestCase):
    """刷新动作的最后兜底：刷新链没产出可用 AT（非封禁）→ 走登录链。

    用户要求：「失效 = 需要重新登录的，走流程登录」「禁用靠登录流程发掘」。
    实测（2026-10-06）：10 个 chatgpt 账号全是 session-only —— 会话死了之后
    刷新链自身无路可走，登录链是唯一能拿回 AT 的路；「号没了」的措辞也只
    在登录链里出现。此前只在「拿到 AT 但校验不过」时兜底，整链失败直接返回，
    死会话的号永远刷不活、封禁也发掘不到。
    """

    def _platform(self):
        from core.base_platform import RegisterConfig
        from platforms.chatgpt.plugin import ChatGPTPlatform

        return ChatGPTPlatform(config=RegisterConfig())

    def _account(self, **overrides):
        from core.base_platform import Account, AccountStatus

        account = Account(
            platform="chatgpt",
            email="user@example.com",
            password="pw",
            status=AccountStatus.REGISTERED,
            extra={"session_token": "st"},
        )
        for key, value in overrides.items():
            setattr(account, key, value)
        return account

    def test_dead_session_falls_back_to_login(self):
        """session 死了（整链失败）→ 登录链兜底把 AT 拿回来。"""
        from platforms.chatgpt.plugin import ChatGPTPlatform
        from platforms.chatgpt.token_refresh import TokenRefreshResult

        failed = TokenRefreshResult(
            success=False, error_message="Session token 刷新失败: HTTP 401"
        )
        recovered = TokenRefreshResult(
            success=True, verified=True, access_token="fresh-at", strategy="password_2fa"
        )
        with patch.object(
            TokenRefreshManager, "refresh_account", return_value=failed
        ), patch.object(
            ChatGPTPlatform, "_refresh_via_login", return_value=recovered
        ) as login:
            out = self._platform().execute_action("refresh_token", self._account(), {})

        login.assert_called_once()
        self.assertTrue(out["ok"], "登录链救回后应报成功")
        self.assertEqual(out["data"]["access_token"], "fresh-at")
        # 登录链换回来的 AT 是新签发的 —— refreshed 必须按它重算（此前刷新链
        # 失败时是 False，不重算会把「换发了」误报成「没换发」）。
        self.assertTrue(out["data"]["refreshed"], "登录链救回后 refreshed 应为 True")

    def test_verified_refresh_does_not_fall_back(self):
        """刷新链直接产出可用 AT → 不跑登录链（那是几十秒的协议重登）。"""
        from platforms.chatgpt.plugin import ChatGPTPlatform
        from platforms.chatgpt.token_refresh import TokenRefreshResult

        ok = TokenRefreshResult(success=True, verified=True, access_token="same-at")
        with patch.object(
            TokenRefreshManager, "refresh_account", return_value=ok
        ), patch.object(ChatGPTPlatform, "_refresh_via_login") as login:
            out = self._platform().execute_action("refresh_token", self._account(), {})

        login.assert_not_called()
        self.assertTrue(out["ok"])

    def test_banned_refresh_does_not_fall_back(self):
        """封禁是终局结论：不再兜底（登录链只会被同样拒绝）。"""
        from platforms.chatgpt.plugin import ChatGPTPlatform
        from platforms.chatgpt.token_refresh import TokenRefreshResult

        banned = TokenRefreshResult(
            success=False, banned=True, error_message="账号已封禁（session 端点拒绝: HTTP 403）"
        )
        with patch.object(
            TokenRefreshManager, "refresh_account", return_value=banned
        ), patch.object(ChatGPTPlatform, "_refresh_via_login") as login:
            out = self._platform().execute_action("refresh_token", self._account(), {})

        login.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertTrue(out["data"]["banned"])

    def test_no_password_keeps_both_reasons(self):
        """登录链跑不动（没密码）时，两段原因都要留下。"""
        from platforms.chatgpt.token_refresh import TokenRefreshResult

        failed = TokenRefreshResult(
            success=False, error_message="Session token 刷新失败: HTTP 401"
        )
        with patch.object(TokenRefreshManager, "refresh_account", return_value=failed):
            out = self._platform().execute_action(
                "refresh_token", self._account(password=""), {}
            )

        self.assertFalse(out["ok"])
        self.assertIn("HTTP 401", out["error"])
        self.assertIn("没有密码", out["error"])


if __name__ == "__main__":
    unittest.main()
