"""路线选择必须**真的接进协议层** —— 声明了注入点却从不调用等于没做。

用户 2026-10-27 报：设置选「密码登录」，注册时仍走 Codex 交换 RT，最后
「本模式要求 refresh_token，但 Codex OAuth 交换没拿到」。

实测根因：`AuthFlow._acquire_rt` 在 `auth_flow.py` 里**只出现在赋值处**
（`self._acquire_rt = acquire_rt`），四个 `oauth_codex_rt_exchange` 调用点
（804 / 841 / 1068 / 1083 行）全都直接走 Codex。适配器
（`registration_engine._acquire_rt_adapter`）写得很完整，就是没人调它。

这个 bug 的形态值得单独防：**注入点声明了、参数传了、测试也过了，但没人调用**。
所以除了行为测试，还要一条**结构性**测试盯住真实调用点。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from platforms.chatgpt.protocol.auth_flow import AuthFlow, Config

ROOT = Path(__file__).resolve().parent.parent
AUTH_FLOW_SRC = ROOT / "platforms" / "chatgpt" / "protocol" / "auth_flow.py"


class InjectedRtRouteIsActuallyCalledTests(unittest.TestCase):
    """`_acquire_rt` 成功时，不得再走 Codex 交换。"""

    def _flow(self, acquire):
        flow = AuthFlow(Config(proxy=None))
        flow._acquire_rt = acquire
        return flow

    def test_uses_injected_route_instead_of_codex(self):
        calls: list[str] = []

        def _injected(_flow) -> bool:
            calls.append("injected")
            _flow.result.refresh_token = "rt-from-login"
            return True

        flow = self._flow(_injected)
        flow.oauth_codex_rt_exchange = lambda **_kw: calls.append("codex")

        got = flow._ensure_refresh_token()

        self.assertEqual(calls, ["injected"],
                         f"注入的路线没被调用，或调用后又走了 Codex: {calls}")
        self.assertTrue(got)
        self.assertEqual(flow.result.refresh_token, "rt-from-login")

    def test_falls_back_to_codex_when_injected_route_fails(self):
        calls: list[str] = []

        flow = self._flow(lambda _flow: (calls.append("injected"), False)[1])
        flow.oauth_codex_rt_exchange = lambda **_kw: calls.append("codex")

        flow._ensure_refresh_token()

        self.assertEqual(calls, ["injected", "codex"],
                         "注入路线失败后应回退 Codex")

    def test_skips_everything_when_rt_already_present(self):
        calls: list[str] = []
        flow = self._flow(lambda _flow: (calls.append("injected"), True)[1])
        flow.result.refresh_token = "rt-already"
        flow.oauth_codex_rt_exchange = lambda **_kw: calls.append("codex")

        self.assertTrue(flow._ensure_refresh_token())
        self.assertEqual(calls, [], "已有 RT 却还去换了一次")

    def test_injected_route_exception_does_not_break_the_chain(self):
        calls: list[str] = []

        def _boom(_flow) -> bool:
            calls.append("injected")
            raise RuntimeError("登录链炸了")

        flow = self._flow(_boom)
        flow.oauth_codex_rt_exchange = lambda **_kw: calls.append("codex")

        flow._ensure_refresh_token()
        self.assertEqual(calls, ["injected", "codex"],
                         "注入路线抛异常应被吞掉并回退，不能炸整条注册链")

    def test_without_injection_goes_to_codex(self):
        calls: list[str] = []
        flow = AuthFlow(Config(proxy=None))
        flow.oauth_codex_rt_exchange = lambda **_kw: calls.append("codex")
        flow._ensure_refresh_token()
        self.assertEqual(calls, ["codex"])


class RealCallSitesGoThroughTheRouteTests(unittest.TestCase):
    """结构性：真实的交换点必须走两个 helper。

    纯行为测试挡不住这个 bug —— 它测的是新函数自己，而问题是**旧调用点没改**。

    **必须扫整个 `platforms/chatgpt/` 目录**：第一版只扫了 `auth_flow.py`，
    于是 `phone_flow.py`（手机注册链）里两处裸调完全不在覆盖内 ——
    `chatgpt_register_flow=phone` 时设置选「密码登录」仍然全程走 Codex
    （代码审查 2026-10-27 C4 抓到）。手机注册链已删除（ADR-0007），
    但「扫整个目录」这条约束保留：新增的 mixin 同样可能在里层裸调。
    """

    def _sources(self) -> list[tuple[str, str]]:
        """所有可能定义/调用 Codex 交换的文件。"""
        base = AUTH_FLOW_SRC.parent
        return [
            (str(path.relative_to(base.parent.parent.parent)), path.read_text(encoding="utf-8"))
            for path in sorted(base.rglob("*.py"))
            if "__pycache__" not in path.parts
        ]

    def _src(self) -> str:
        return AUTH_FLOW_SRC.read_text(encoding="utf-8")

    def test_no_bare_codex_exchange_anywhere_under_platforms_chatgpt(self):
        """整个 `platforms/chatgpt/` 下不许有裸调 —— 含手机注册链。"""
        offenders: list[str] = []
        for rel, text in self._sources():
            if "oauth_codex_rt_exchange(" not in text:
                continue
            # 切掉两个 helper 的定义体（它们内部允许直接调）
            outside = text
            for name in ("_ensure_refresh_token", "_speculative_rt_attempt"):
                marker = f"def {name}("
                if marker not in outside:
                    continue
                start = outside.index(marker)
                end = outside.find("\n    def ", start + 10)
                if end == -1:
                    end = len(outside)
                outside = outside[:start] + outside[end:]
            if re.search(r"self\.oauth_codex_rt_exchange\(", outside):
                offenders.append(rel)
        self.assertEqual(
            offenders, [],
            f"这些文件仍有裸调 Codex（会绕过 chatgpt_rt_route 设置）: {offenders}",
        )

    def test_no_bare_codex_exchange_outside_the_helper(self):
        src = self._src()
        # 两个 helper 的定义体内部允许直接调 Codex：
        # - `_ensure_refresh_token`：路线判断 + 回落
        # - `_speculative_rt_attempt`：抢跑，要带 `speculative=True`
        outside = src
        for name in ("_ensure_refresh_token", "_speculative_rt_attempt"):
            start = outside.index(f"def {name}(")
            end = outside.index("\n    def ", start + 10)
            outside = outside[:start] + outside[end:]

        bare = len(re.findall(r"self\.oauth_codex_rt_exchange\(", outside))
        guarded = len(re.findall(r"self\._ensure_refresh_token\(", outside))
        spec = len(re.findall(r"self\._speculative_rt_attempt\(", outside))

        # 真正的意图是「一个裸调用都不许有」。修复前是 4 裸 / 0 走路线，
        # 这正是用户报的 bug（设置选密码登录仍走 Codex）。
        self.assertEqual(
            bare, 0,
            f"还有 {bare} 处直接调 Codex —— 那些点会绕过 `chatgpt_rt_route` 设置",
        )
        # 两个正式交换点（注册链 / 协议登录链）必须走路线判断
        self.assertGreaterEqual(guarded, 2, "正式的交换点没有走 `_ensure_refresh_token`")
        # 两个抢跑点走 `_speculative_rt_attempt`（它内部按路线决定是否抢跑）
        self.assertGreaterEqual(spec, 2, "抢跑点没有走 `_speculative_rt_attempt`")

    def test_helper_exists(self):
        self.assertIn("def _ensure_refresh_token", self._src())


if __name__ == "__main__":
    unittest.main()


class EnginePassesTheRouteIntoProtocolLayerTests(unittest.TestCase):
    """注册引擎必须把「Token 方案」带进 `env_overrides`。

    抢跑判定（`_speculative_rt_attempt`）从 `env_overrides` 读路线 ——
    引擎不带的话协议层读不到设置，只能按默认走（实测 2026-10-27：
    设置选密码登录，抢跑仍是 Codex）。
    """

    def _overrides(self, config: dict) -> dict:
        from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine

        engine = ChatGPTRegistrationEngine.__new__(ChatGPTRegistrationEngine)
        engine.extra_config = dict(config)
        engine.mode = "refresh_token"
        return engine._env_overrides()

    def test_carries_the_merged_mode(self):
        ov = self._overrides({"chatgpt_registration_mode": "refresh_token_login"})
        self.assertEqual(ov.get("chatgpt_registration_mode"), "refresh_token_login")
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            RT_ROUTE_LOGIN, resolve_chatgpt_rt_route,
        )
        self.assertEqual(resolve_chatgpt_rt_route(ov), RT_ROUTE_LOGIN,
                         "协议层读不出「密码登录」路线")

    def test_carries_the_legacy_route_key(self):
        ov = self._overrides({"chatgpt_rt_route": "codex"})
        self.assertEqual(ov.get("chatgpt_rt_route"), "codex")

    def test_absent_setting_does_not_inject_a_bogus_key(self):
        ov = self._overrides({})
        self.assertNotIn("chatgpt_registration_mode", ov)
        self.assertNotIn("chatgpt_rt_route", ov)
