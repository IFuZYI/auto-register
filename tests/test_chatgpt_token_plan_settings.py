"""设置页的「Token 方案」必须是**三选一**，与任务页一致。

背景（用户 2026-10-26）：设置页此前渲染成两个独立下拉
（「Token 方案」= 有RT/无RT、「RT 获取方式」= Codex/密码登录），而任务页早已
合并成三选一（`frontend/src/lib/chatgptTokenPlan.ts`）。同一件事两处呈现不同，
用户会以为设置页少了东西 —— 而且他明确要求合并。

做法：把声明合并成**一个** `chatgpt_registration_mode`，三个取值，每个取值在
后端解析时同时决定「要不要 RT」与「怎么换 RT」。旧的 `chatgpt_rt_route` 键
保留为兼容输入（老配置里存过它），但不再单独渲染。
"""

from __future__ import annotations

import unittest

from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    RT_ROUTE_CODEX,
    RT_ROUTE_LOGIN,
    resolve_chatgpt_rt_route,
)
from platforms.chatgpt.plugin import ChatGPTPlatform


def _mode(key: str) -> dict:
    for item in ChatGPTPlatform.registration_modes:
        if item["key"] == key:
            return item
    raise AssertionError(f"registration_modes 里没有 {key}")


class SettingsDeclaresOneCombinedModeTests(unittest.TestCase):
    def test_token_plan_has_three_options(self):
        mode = _mode("chatgpt_registration_mode")
        values = [o["value"] for o in mode["options"]]
        self.assertEqual(
            values,
            ["refresh_token_login", "refresh_token_codex", "access_token_only"],
            f"Token 方案不是三选一: {values}",
        )

    def test_rt_route_is_no_longer_a_separate_field(self):
        keys = [m["key"] for m in ChatGPTPlatform.registration_modes]
        self.assertNotIn(
            "chatgpt_rt_route", keys,
            "「RT 获取方式」仍单独渲染 —— 用户要求合并进 Token 方案",
        )

    def test_each_option_explains_the_tradeoff(self):
        """三个选项的文案要说清代价（接码与否、有无 RT）。"""
        mode = _mode("chatgpt_registration_mode")
        labels = {o["value"]: o["label"] for o in mode["options"]}
        self.assertIn("不需要接码", labels["refresh_token_login"])
        self.assertIn("需要接码", labels["refresh_token_codex"])
        self.assertNotIn("RT", labels["access_token_only"].replace("无 RT", ""))

    def test_desc_mentions_the_three_way_split(self):
        mode = _mode("chatgpt_registration_mode")
        desc = mode.get("desc", "")
        self.assertIn("接码", desc, "desc 要说明接码代价")


class CombinedModeResolvesBothDimensionsTests(unittest.TestCase):
    """一个取值要同时决定「要不要 RT」与「怎么换 RT」。"""

    def test_login_plan_means_refresh_token_via_login(self):
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            resolve_chatgpt_registration_mode,
        )

        extra = {"chatgpt_registration_mode": "refresh_token_login"}
        self.assertEqual(resolve_chatgpt_registration_mode(extra), "refresh_token")
        self.assertEqual(resolve_chatgpt_rt_route(extra), RT_ROUTE_LOGIN)

    def test_codex_plan_means_refresh_token_via_codex(self):
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            resolve_chatgpt_registration_mode,
        )

        extra = {"chatgpt_registration_mode": "refresh_token_codex"}
        self.assertEqual(resolve_chatgpt_registration_mode(extra), "refresh_token")
        self.assertEqual(resolve_chatgpt_rt_route(extra), RT_ROUTE_CODEX)

    def test_access_only_plan_means_no_rt(self):
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            resolve_chatgpt_registration_mode,
        )

        extra = {"chatgpt_registration_mode": "access_token_only"}
        self.assertEqual(resolve_chatgpt_registration_mode(extra), "access_token_only")

    def test_legacy_keys_still_work(self):
        """老配置里存的是 `refresh_token` + `chatgpt_rt_route`，不能失效。"""
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            resolve_chatgpt_registration_mode,
        )

        legacy = {"chatgpt_registration_mode": "refresh_token", "chatgpt_rt_route": "login"}
        self.assertEqual(resolve_chatgpt_registration_mode(legacy), "refresh_token")
        self.assertEqual(resolve_chatgpt_rt_route(legacy), RT_ROUTE_LOGIN)

    def test_unknown_value_falls_back_safely(self):
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            resolve_chatgpt_registration_mode,
        )

        self.assertEqual(
            resolve_chatgpt_registration_mode({"chatgpt_registration_mode": "banana"}),
            "refresh_token",
        )


if __name__ == "__main__":
    unittest.main()


class LegacyValueMapsToTheRightOptionTests(unittest.TestCase):
    """已保存的旧值（`refresh_token`）必须显示成对应的新选项，而不是原始串。

    实测（2026-10-26）：设置页下拉显示 `refresh_token` —— 那是合并前的旧值，
    新选项里没有它，antd `Select` 只能把原始串显示出来。用户看到的是一个
    内部标识符，不知道当前到底选了哪个方案。
    """

    def test_declares_legacy_aliases_for_display(self):
        mode = _mode("chatgpt_registration_mode")
        aliases = mode.get("legacy_aliases") or {}
        self.assertEqual(
            aliases.get("refresh_token"), "refresh_token_login",
            "没声明旧值 → 新选项的映射，已保存的旧配置会显示成原始串",
        )

    def test_backend_normalizes_legacy_value_into_the_plan(self):
        """后端把旧值解析成合并方案（前端据此显示）。"""
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            resolve_chatgpt_token_plan,
        )

        self.assertEqual(resolve_chatgpt_token_plan({"chatgpt_registration_mode": "refresh_token"}),
                         "refresh_token_login")
        self.assertEqual(resolve_chatgpt_token_plan({"chatgpt_registration_mode": "access_token_only"}),
                         "access_token_only")
        self.assertEqual(resolve_chatgpt_token_plan({}), "refresh_token_login")
