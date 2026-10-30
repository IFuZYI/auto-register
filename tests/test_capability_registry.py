"""能力层登记表与平台声明必须一一对应。

这条测试的价值不在「现在是对的」，而在**以后加动作时会被挡住**：
新写一个 `{"id": "xxx", ...}` 却没在 `core/capabilities.py` 登记，
这里当场红 —— 而不是等到运行时走进某个 `else` 分支才发现它没有策略。
"""

from __future__ import annotations

import unittest

from core.base_platform import RegisterConfig
from core.capabilities import (
    ACTION_CAPABILITIES,
    Capability,
    actions_with,
    capability_of,
    group_by_capability,
)
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.grok.plugin import GrokPlatform

PLATFORMS = (ChatGPTPlatform, GrokPlatform)


def _declared(cls) -> list[str]:
    return [str(action["id"]) for action in cls(config=RegisterConfig()).get_platform_actions()]


class RegistryMatchesDeclarationsTests(unittest.TestCase):
    def test_every_declared_action_is_registered(self):
        for cls in PLATFORMS:
            for action_id in _declared(cls):
                self.assertIsNotNone(
                    capability_of(action_id),
                    f"{cls.__name__} 声明了 {action_id!r} 但没在 core/capabilities.py 登记",
                )

    def test_registry_has_no_orphans(self):
        """反向：登记了但没有任何平台声明 —— 那是删动作时忘了清登记。"""
        declared = {action_id for cls in PLATFORMS for action_id in _declared(cls)}
        self.assertEqual(
            set(ACTION_CAPABILITIES), declared, "登记表与平台声明对不上"
        )

    def test_every_capability_has_at_least_one_action(self):
        for capability in Capability:
            self.assertTrue(
                actions_with(capability), f"能力 {capability.value} 没有任何动作"
            )

    def test_capability_count(self):
        self.assertEqual(len(list(Capability)), 6, "能力数变了就要同步改文档与指标")

    def test_grouping_reports_unknown_ids(self):
        grouped = group_by_capability(["refresh_token", "not_a_real_action"])
        self.assertIn("refresh_token", grouped[Capability.REFRESH])
        self.assertIn("not_a_real_action", grouped[None])


class StrategyTablesUseCapabilitiesTests(unittest.TestCase):
    """两张策略表必须只引用对应能力的动作 —— 这是「按能力分支」的前提。"""

    def test_upload_writers_are_upload_actions(self):
        from services.action_execution import UPLOAD_SYNC_WRITERS

        for action_id in UPLOAD_SYNC_WRITERS:
            self.assertIs(
                capability_of(action_id),
                Capability.UPLOAD,
                f"{action_id} 在 UPLOAD_SYNC_WRITERS 里但不是 UPLOAD 能力",
            )

    def test_panel_status_actions_are_sync_actions(self):
        from api.actions import _PANEL_STATUS_ACTIONS

        for action_id in _PANEL_STATUS_ACTIONS:
            self.assertIs(
                capability_of(action_id),
                Capability.SYNC_STATUS,
                f"{action_id} 在 _PANEL_STATUS_ACTIONS 里但不是 SYNC_STATUS 能力",
            )


if __name__ == "__main__":
    unittest.main()
