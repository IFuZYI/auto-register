"""Grok 注册路径唯一性 + 执行器声明的回归。

背景（实测问题）：执行器此前是个摆设 —— 任务页能选，但插件不读它，
注册路径由别的开关决定（Grok 是 `grok_register_mode`）。用户选了「浏览器」
却跑了协议，界面上没有任何提示。

后来协议路径被整体删除（2026-10-01，见 `platforms/grok/plugin.py` 的
`register()` 注释）：x.ai 对协议层发码「假接受」（gRPC grpc-status:0 但零投递）、
协议验码在无 CF 通行证的客户端里恒为 grpc=3，参考项目 grok-hub-clean 也放弃了
这条路。**现在 Grok 只有浏览器一条路**，执行器声明收敛为单个元素。

这组测试钉住两件事：
1. 无论老配置怎么写，`register()` 都走浏览器路径（没有第二条路可走）。
2. 声明的执行器与实际实现一致（`supported_executors` / `executor_labels`）。
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

import platforms.grok.plugin as plugin_mod
from core.base_platform import RegisterConfig
from core.registry import get, load_all


def _platform(executor: str, extra: dict | None = None):
    """构造一个 Grok 实例，执行器显式给定。"""
    load_all()
    cls = get("grok")
    return cls(
        RegisterConfig(
            executor_type=executor,
            proxy="",
            extra=dict(extra or {}),
        )
    )


_QUIET = {
    "grok_oauth_exchange": "0",
    "grok_probe_after_register": "0",
    "grok_grok2api_ingest": "0",
}


class SinglePathTests(unittest.TestCase):
    """协议路径已删除：任何输入都走浏览器路径。"""

    def _took_browser_path(self, executor: str, extra: dict) -> bool:
        plat = _platform(executor, extra)
        with patch.object(
            plugin_mod.GrokPlatform, "_register_via_browser"
        ) as fake_browser:
            fake_browser.return_value = object()
            try:
                plat.register()
            except Exception:
                pass
            return fake_browser.called

    def test_browser_executor_takes_browser_path(self):
        self.assertTrue(
            self._took_browser_path("browser", dict(_QUIET)),
            "executor=browser 必须走浏览器路径",
        )

    def test_stale_protocol_executor_still_takes_browser_path(self):
        """老配置里存着 `protocol` 也要走浏览器路径 —— 没有协议路径可走了。

        这是**迁移安全**的关键：用户库里可能还留着 `grok_executor=protocol`
        （或老任务带着它）。若不落到浏览器路径，那些调用会直接失败。
        """
        self.assertTrue(
            self._took_browser_path("protocol", dict(_QUIET)),
            "protocol 已不存在；它必须被落到浏览器路径，而不是报错或静默跳过",
        )

    def test_legacy_register_mode_is_ignored_not_obeyed(self):
        """遗留键 `grok_register_mode=protocol` 不再改变路径（只记一行日志）。"""
        logs: list = []
        plat = _platform("", {**_QUIET, "grok_register_mode": "protocol"})
        plat._log_fn = logs.append
        with patch.object(
            plugin_mod.GrokPlatform, "_register_via_browser"
        ) as fake_browser:
            fake_browser.return_value = object()
            try:
                plat.register()
            except Exception:
                pass
            self.assertTrue(
                fake_browser.called,
                "遗留键说 protocol，但那条路没了 —— 必须落到浏览器路径",
            )
        self.assertTrue(
            any("grok_register_mode" in str(x) for x in logs),
            f"忽略遗留键要留痕（否则用户以为它还在生效）；日志: {logs}",
        )

    def test_empty_executor_takes_browser_path(self):
        self.assertTrue(
            self._took_browser_path("", dict(_QUIET)),
            "执行器为空时回落平台默认（browser）",
        )


class DeclaredExecutorsMatchImplementationTests(unittest.TestCase):
    """声明的执行器必须与实际实现一致（这是本机制的契约）。"""

    def test_grok_only_declares_browser(self):
        load_all()
        cls = get("grok")
        self.assertEqual(
            cls.supported_executors,
            ["browser"],
            "协议路径已删除，Grok 只剩浏览器一条路；"
            "多声明一个选不动的选项 = 给用户一条假路",
        )

    def test_no_declared_executor_lacks_an_implementation(self):
        """每个声明的执行器都必须真的有代码路径 —— 不能是装饰。

        Grok 的实现是 `_register_via_browser`；ChatGPT 的协议实现走
        `curl_cffi`。这条测试防止「声明了但没实现」重新出现。
        """
        load_all()
        grok = get("grok")
        self.assertEqual(grok.supported_executors, ["browser"])
        self.assertTrue(
            hasattr(grok, "_register_via_browser"),
            "声明的 browser 执行器必须有对应实现",
        )

    def test_every_declared_executor_has_a_label(self):
        """每个声明的执行器都要有显示名 —— 否则界面显示原始值（如 'browser'）。"""
        load_all()
        for name in ("grok", "chatgpt"):
            cls = get(name)
            labels = getattr(cls, "executor_labels", {}) or {}
            for executor in cls.supported_executors:
                self.assertTrue(
                    labels.get(executor),
                    f"{name} 的执行器 {executor!r} 没有显示名（executor_labels）",
                )

    def test_platforms_endpoint_ships_labels(self):
        """`/api/platforms` 要下发标签 —— 界面靠它渲染。"""
        from core.registry import list_platforms

        by_name = {p["name"]: p for p in list_platforms()}
        grok = by_name["grok"]
        self.assertEqual(grok["supported_executors"], ["browser"])
        self.assertTrue(grok["executor_labels"].get("browser"))
        chatgpt = by_name["chatgpt"]
        self.assertEqual(chatgpt["supported_executors"], ["protocol"])

    def test_default_executor_is_first_declared(self):
        """列表第一个 = 平台默认（界面与后端都这么解释）。"""
        load_all()
        cls = get("grok")
        instance = cls(RegisterConfig(executor_type=""))
        self.assertEqual(instance.config.executor_type, cls.supported_executors[0])


if __name__ == "__main__":
    unittest.main()
