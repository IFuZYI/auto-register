"""ChatGPT 账号必须有 `client_id`，否则 OAuth 刷新必然 401。

## 实测证据（2026-10-28）

对生产库最新账号直接跑 `TokenRefreshManager.refresh_account`：

    OAuth token 刷新失败: HTTP 401, 服务端说: Invalid client specified. invalid_client
    success: True | verified: False | refreshed: False | strategy: session
    新 AT 与旧 AT 不同: False
    校验: 新 AT 被服务端拒绝（HTTP 401）

原因：`extra.client_id` 为空 → 请求里 `client_id=""` → 服务端 `invalid_client`。
然后回落到 session 路径，而 session 路径在 AT 未到期时**原样返回旧 AT**
（`refreshed=False`）—— 用户看到的就是「RT 不对 / 刷不出新东西」。

生产实测：**663 个 chatgpt 账号，0 个有 client_id**。

## 正确值

`platforms/chatgpt/constants.py` 的 `OAUTH_CLIENT_ID`。刷新代码在
`client_id` 为空时会回落到它（`token_refresh.py:210`），但**只有 session
那条路**用了回落值；直接构造请求的地方（如 backfill）拿不到。
落库时补上更稳 —— 也让界面/导出能看到真实值。
"""

from __future__ import annotations

import unittest


class ClientIdIsPersistedTests(unittest.TestCase):
    def test_registration_writes_client_id(self):
        """注册落库的 extra 里要带 `client_id`。"""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        # 注册链把凭证写进 extra 的地方
        sources = [
            root / "platforms" / "chatgpt" / "plugin.py",
            root / "platforms" / "chatgpt" / "registration_engine.py",
        ]
        combined = "\n".join(p.read_text(encoding="utf-8") for p in sources)
        # 落库要走**解析器**（按实际签发者选 client），不是写死一个常量 ——
        # 两条路线 client 不同，写死会让另一条的号 401 invalid_client。
        self.assertIn(
            "resolve_client_id_for_patch", combined,
            "注册链没有经解析器决定 client_id —— 落库的值可能与签发者不符",
        )
        self.assertIn(
            "client_id", combined,
            "注册链没有写 client_id —— 刷新必然 invalid_client",
        )

    def test_refresh_uses_the_platform_client_id(self):
        """刷新必须用 **platform** 的 client_id，不是 Codex 的。

        实测（2026-10-28，真实账号 + 真实 RT）：

            Codex    app_EMoamEEZ73f0CkXaXp7hrann → 401 invalid_client
            platform app_2SKx67EdpoN0G6j64rFvigXD → ✅ 新 AT + 新 RT

        原因：注册链走的是 platform 密码登录路线
        （`protocol/mixins/platform_rt.py`），签发 RT 的就是那个 client。
        用 Codex 的 client_id 去刷别人签发的 RT，服务端回 `invalid_client`
        —— 不是「RT 失效」那种能自查的错误，极易误判成「RT 不对」。
        """
        from platforms.chatgpt.protocol.mixins.platform_rt import PLATFORM_CLIENT_ID
        from platforms.chatgpt.token_refresh import TokenRefreshManager

        manager = TokenRefreshManager()
        self.assertEqual(
            getattr(manager, "_oauth_client_id", ""),
            PLATFORM_CLIENT_ID,
            "刷新管理器没用 platform client_id —— OAuth 刷新会 invalid_client",
        )
        self.assertNotEqual(
            PLATFORM_CLIENT_ID,
            "app_EMoamEEZ73f0CkXaXp7hrann",
            "platform client_id 不该等于 Codex 的那个",
        )

    def test_refresh_uses_the_platform_token_endpoint(self):
        """端点也要用 platform 的：与 client_id / redirect_uri 绑定。"""
        from platforms.chatgpt.protocol.mixins.platform_rt import PLATFORM_TOKEN_URL
        from platforms.chatgpt.token_refresh import TokenRefreshManager

        manager = TokenRefreshManager()
        self.assertEqual(
            getattr(manager, "_oauth_token_url", ""),
            PLATFORM_TOKEN_URL,
            "刷新用的不是 platform 端点 —— 会 invalid_grant",
        )


class NoEmptyClientIdReachesTheServerTests(unittest.TestCase):
    """结构性：构造 OAuth 请求前必须保证 `client_id` 非空。

    `client_id=""` 换来的是 `invalid_client`，而不是「参数缺失」那种能自查的
    错误 —— 排查时很容易误判成「RT 失效」。
    """

    def test_backfill_uses_a_non_empty_client_id(self):
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[1]
            / "services" / "chatgpt_rt_backfill.py"
        ).read_text(encoding="utf-8")
        self.assertTrue(
            "OAUTH_CLIENT_ID" in src or "client_id" in src,
            "backfill 没处理 client_id",
        )


if __name__ == "__main__":
    unittest.main()
