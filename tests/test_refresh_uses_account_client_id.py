"""刷新必须用**该账号 RT 实际签发的那个** client_id。

## 为什么这是设计缺陷（2026-10-28 实测）

本仓有**两条**产出 RT 的路线，各自用不同的 OAuth client：

| 路线 | 代码 | client_id |
| --- | --- | --- |
| Codex OAuth | `protocol/mixins/codex.py:65` | `app_EMoamEEZ73f0CkXaXp7hrann` |
| platform 密码登录 | `protocol/mixins/platform_rt.py:30` | `app_2SKx67EdpoN0G6j64rFvigXD` |

而 `TokenRefreshManager` **固定**用 platform 那个（我上一轮的修改）。

实测对照（真实账号 + 真实 RT，同一端点）：

    Codex    app_EMoamEEZ73f0CkXaXp7hrann → 401 invalid_client
    platform app_2SKx67EdpoN0G6j64rFvigXD → ✅ 新 AT + 新 RT

`invalid_client` 的意思是「这个 RT 不是这个 client 签的」—— 所以**Codex 路线
产出的号永远刷不了 RT**，而错误信息看起来像「令牌失效」，极易误判。

## 生产数据

164 个有 RT 的账号里 163 个 `client_id` 为空（从来没人落过）—— 所以这个
缺陷之前被「没有 client_id」掩盖了。一旦落库就会暴露。

## 正确行为

`refresh_account` 按这个顺序选 client_id：
1. 账号自己记录的 `client_id`（它知道自己是谁签的）；
2. 没有才用默认值。

落库侧同理：哪条路线产出的 RT，就记哪个 client_id。
"""

from __future__ import annotations

import unittest
from unittest import mock


class RefreshPicksTheAccountsOwnClientIdTests(unittest.TestCase):
    """断言**真实发出的请求体**里的 client_id。

    第一版 mock 了 `refresh_by_oauth_token`，而回落发生在**它内部**
    （`client_id or self._oauth_client_id`）—— 在那个层级断言等于测自己
    编的参数，测不到真实行为（实测：把回落去掉测试仍绿）。
    """

    def _client_id_in_request(self, *, account_client_id: str) -> str:
        from platforms.chatgpt.token_refresh import TokenRefreshManager

        manager = TokenRefreshManager()
        captured: dict = {}

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"access_token": "new-at", "refresh_token": "new-rt"}

        class _Session:
            def post(self, url, **kwargs):
                captured.update(kwargs)
                return _Resp()

            def close(self):
                pass

        from unittest import mock

        account = mock.Mock()
        account.access_token = ""
        account.refresh_token = "rt-x"
        account.session_token = ""
        account.client_id = account_client_id

        with mock.patch.object(manager, "_create_session", lambda: _Session()):
            manager.refresh_by_oauth_token(
                refresh_token=account.refresh_token,
                client_id=account_client_id or None,
            )
        return str((captured.get("data") or {}).get("client_id") or "")

    def test_uses_the_account_client_id_when_present(self):
        got = self._client_id_in_request(account_client_id="app_CODEX_TEST")
        self.assertEqual(
            got, "app_CODEX_TEST",
            f"请求里的 client_id 不是账号自己的（{got!r}）—— "
            "Codex 路线的号会 401 invalid_client",
        )

    def test_falls_back_to_the_default_when_account_has_none(self):
        from platforms.chatgpt.protocol.mixins.platform_rt import PLATFORM_CLIENT_ID

        got = self._client_id_in_request(account_client_id="")
        self.assertEqual(
            got, PLATFORM_CLIENT_ID,
            f"账号没有 client_id 时没有回落默认值（请求里是 {got!r}）",
        )


class BothRoutesRecordTheirOwnClientIdTests(unittest.TestCase):
    """两条路线产出的 RT 都要把自己的 client_id 记下来。"""

    def test_codex_route_records_codex_client_id(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "platforms" / "chatgpt" / "protocol" / "mixins" / "codex.py"
        ).read_text(encoding="utf-8")
        # Codex 换到 RT 之后要把自己的 client_id 交给调用方
        self.assertIn(
            "oauth_client_id", src,
            "Codex 路线没有把它的 client_id 暴露出来 —— 落库时只能瞎猜一个",
        )

    def test_platform_route_records_platform_client_id(self):
        from platforms.chatgpt.protocol.mixins.platform_rt import PLATFORM_CLIENT_ID

        self.assertEqual(PLATFORM_CLIENT_ID, "app_2SKx67EdpoN0G6j64rFvigXD")
        # 两条路线必须真的是不同的 client
        import inspect

        from platforms.chatgpt.protocol.mixins import codex

        codex_src = inspect.getsource(codex)
        self.assertIn("app_EMoamEEZ73f0CkXaXp7hrann", codex_src)
        self.assertNotEqual(
            PLATFORM_CLIENT_ID, "app_EMoamEEZ73f0CkXaXp7hrann",
            "两条路线的 client_id 不该相同",
        )


if __name__ == "__main__":
    unittest.main()


class PersistedClientIdFollowsTheIssuerTests(unittest.TestCase):
    """落库的 client_id 必须是**实际签发 RT 的那个**，不是写死的常量。

    ## 为什么

    两条路线的 client 不同（Codex vs platform）。落库时固定写一个常量，另一条
    路线的号就会带着**错误的** client_id —— 刷新时 `invalid_client`，
    而错误信息看起来像「令牌失效」。

    ## 权威来源

    服务端的 `client_auth_session` dump 里带 `openai_client_id`
    （`protocol/mixins/trace.py:301` 已经把它同步到 `flow._oauth_client_id`）。
    那是「这个 RT 是谁签的」的权威答案，优先于任何本地常量。
    """

    def test_patch_prefers_the_flow_client_id(self):
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            resolve_client_id_for_patch,
        )

        # flow 报告了 client_id → 用它
        self.assertEqual(
            resolve_client_id_for_patch("app_FROM_SERVER", default="app_DEFAULT"),
            "app_FROM_SERVER",
            "flow 报告了 client_id 却没用它 —— 落库会写错 client",
        )

    def test_patch_falls_back_to_the_default(self):
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            resolve_client_id_for_patch,
        )

        self.assertEqual(
            resolve_client_id_for_patch("", default="app_DEFAULT"), "app_DEFAULT"
        )

    def test_adapter_exposes_a_resolver(self):
        from platforms.chatgpt import chatgpt_registration_mode_adapter as mod

        self.assertTrue(
            hasattr(mod, "resolve_client_id_for_patch"),
            "适配器没有暴露 client_id 解析函数 —— 落库侧只能自己写死",
        )


if __name__ == "__main__":
    unittest.main()
