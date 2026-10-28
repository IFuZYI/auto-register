"""刷新失败必须**分类**，不能只报一个错误字符串。

## 为什么这是顺序/语义问题（2026-10-28 实测）

对 5 个真实账号跑刷新，结果：

    #1053 ok=False  OAuth token 刷新失败: HTTP 400
    #1052 ok=False  OAuth token 刷新失败: HTTP 400
    #1051 ok=True verified=False strat=session 新RT=0
    #1050 ok=True verified=True  strat=session 新RT=0
    #1049 ok=True verified=False strat=session 新RT=0

服务端原话是 `invalid_grant The refresh token has been invalidated.
A fresh sign-in is required.` —— **这是终局结论**：这个 RT 已经废了，
再刷一万次也刷不回来，只能重新登录。

但本仓的 `TokenRefreshResult` 只带 `error_message` 字符串，调用方**没法
区分**：

- `invalid_grant`（永久失效）→ 应当落「失效」，并引导重新登录；
- `invalid_client`（client 配错）→ 是我们的问题，不是账号问题；
- 网络超时（瞬时）→ 应当重试，不动状态。

结果：失效的号被反复重试（每次白打一轮上游），状态也不更新 —— 界面一直
显示正常，用户以为号还能用。

## 对照

Grok 侧**已经有**这个分类（`services/grok_account_state.py` 的
`_refresh_reason` / `refresh_kind`: permanent / configuration /
retryable / unknown）。ChatGPT 侧没有 —— 这条测试要求补齐。
"""

from __future__ import annotations

import unittest


class RefreshResultCarriesAKindTests(unittest.TestCase):
    def test_result_has_a_kind_field(self):
        from platforms.chatgpt.token_refresh import TokenRefreshResult

        r = TokenRefreshResult(success=False)
        self.assertTrue(
            hasattr(r, "kind"),
            "TokenRefreshResult 没有 kind 字段 —— 调用方分不清永久失效与瞬时故障",
        )

    def test_default_kind_is_empty(self):
        from platforms.chatgpt.token_refresh import TokenRefreshResult

        self.assertEqual(TokenRefreshResult(success=False).kind, "")


class ClassificationCoversTheRealServerErrorsTests(unittest.TestCase):
    """分类要覆盖实测见过的服务端错误。"""

    def _classify(self, text: str, status: int = 400) -> str:
        from platforms.chatgpt.token_refresh import classify_refresh_error

        return classify_refresh_error(status, text)

    def test_invalid_grant_is_permanent(self):
        self.assertEqual(
            self._classify(
                'invalid_grant The refresh token has been invalidated. '
                'A fresh sign-in is required.'
            ),
            "permanent",
            "invalid_grant 是终局结论，必须归 permanent",
        )

    def test_bare_invalid_grant_is_permanent(self):
        self.assertEqual(self._classify("invalid_grant"), "permanent")

    def test_invalid_client_is_configuration(self):
        """`invalid_client` 是**我们的** client 配错，不是账号坏了。"""
        self.assertEqual(
            self._classify("Invalid client specified.", 401), "configuration",
            "invalid_client 归 configuration —— 归 permanent 会误杀一批好号",
        )

    def test_empty_string_client_id_is_configuration(self):
        self.assertEqual(
            self._classify(
                "Invalid 'client_id': empty string. Expected a string with "
                "minimum length 1, but got an empty string instead.", 400
            ),
            "configuration",
        )

    def test_server_error_is_retryable(self):
        self.assertEqual(self._classify("internal error", 500), "retryable")

    def test_rate_limit_is_retryable(self):
        self.assertEqual(self._classify("rate limit exceeded", 429), "retryable")

    def test_unknown_is_not_classified(self):
        self.assertEqual(self._classify("something else entirely", 400), "")


class TheRefreshPathSetsTheKindTests(unittest.TestCase):
    def test_oauth_failure_sets_kind(self):
        """OAuth 刷新失败时要落 kind（不然调用方还是分不清）。"""
        from unittest import mock

        from platforms.chatgpt.token_refresh import TokenRefreshManager

        manager = TokenRefreshManager()

        class _Resp:
            status_code = 400
            text = '{"error":"invalid_grant"}'

            def json(self):
                return {}

        class _Session:
            def post(self, url, **kwargs):
                return _Resp()

            def close(self):
                pass

        with mock.patch.object(manager, "_create_session", lambda: _Session()):
            r = manager.refresh_by_oauth_token(
                refresh_token="rt-x", client_id="app_X"
            )
        self.assertFalse(r.success)
        self.assertEqual(
            r.kind, "permanent",
            f"invalid_grant 没被分类（kind={r.kind!r}）—— 失效的号会被反复重试",
        )


if __name__ == "__main__":
    unittest.main()


class StatusPolicyConsumesTheRefreshKindTests(unittest.TestCase):
    """刷新分类要能驱动账号状态 —— 否则分类白做。

    `permanent` = RT 已废只能重登 → 落「失效」（`invalid`），
    与其他「需要重新登录」的信号同侧。

    但 **`configuration` 绝不许落状态**：那是我们的 client 配错，账号本身
    可能是好的（实测：正确 client 下同一批号 4/12 刷新成功）。
    """

    def _account(self, status: str = "registered"):
        from types import SimpleNamespace

        return SimpleNamespace(status=status, extra_json="{}", token="")

    def test_permanent_sets_invalid(self):
        from services.chatgpt_account_state import apply_chatgpt_status_policy

        acc = self._account()
        reason = apply_chatgpt_status_policy(acc, refresh_kind="permanent")
        self.assertEqual(acc.status, "invalid", "permanent 没落「失效」")
        self.assertEqual(reason, "refresh_permanent", f"理由不对: {reason!r}")

    def test_configuration_does_not_touch_status(self):
        from services.chatgpt_account_state import apply_chatgpt_status_policy

        acc = self._account()
        reason = apply_chatgpt_status_policy(acc, refresh_kind="configuration")
        self.assertEqual(
            acc.status, "registered",
            "configuration 落了状态 —— 会把一批好号误标（那是我们的配错）",
        )
        self.assertEqual(reason, "", f"configuration 不该有判定理由: {reason!r}")

    def test_retryable_does_not_touch_status(self):
        from services.chatgpt_account_state import apply_chatgpt_status_policy

        acc = self._account()
        apply_chatgpt_status_policy(acc, refresh_kind="retryable")
        self.assertEqual(acc.status, "registered", "retryable 落了状态（不该动）")

    def test_banned_still_wins_over_permanent(self):
        """封禁是更强判断：与 permanent 同时出现时按封禁落。"""
        from services.chatgpt_account_state import apply_chatgpt_status_policy

        acc = self._account()
        apply_chatgpt_status_policy(acc, refresh_kind="permanent", banned=True)
        self.assertEqual(acc.status, "banned")
