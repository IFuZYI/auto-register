"""执行器工厂：按 executor_type 选择协议执行器或浏览器执行器。

放在 core 而不是 modules 的原因：`core/base_platform.py` 的 `_make_executor()`
需要它，而 core 不能反向 import modules。`modules/execution/browser.py` 从这里
转出，保持 `from modules.execution import BrowserExecutorFactory` 继续可用。
"""

from __future__ import annotations

from typing import Any


class BrowserExecutorFactory:
    """Create transport executors without coupling platform plugins to imports."""

    def __init__(self, protocol_cls: type | None = None, playwright_cls: type | None = None):
        self._protocol_cls = protocol_cls
        self._playwright_cls = playwright_cls

    def create(self, executor_type: str = "protocol", proxy: str | None = None) -> Any:
        normalized = str(executor_type or "protocol").strip().lower()
        if normalized == "protocol":
            return self._protocol()(proxy=proxy)
        if normalized == "headless":
            return self._playwright()(proxy=proxy, headless=True)
        if normalized == "headed":
            return self._playwright()(proxy=proxy, headless=False)
        raise ValueError(f"未知执行器类型: {executor_type}")

    def _protocol(self) -> type:
        if self._protocol_cls is None:
            from .protocol import ProtocolExecutor

            self._protocol_cls = ProtocolExecutor
        return self._protocol_cls

    def _playwright(self) -> type:
        if self._playwright_cls is None:
            from .playwright import PlaywrightExecutor

            self._playwright_cls = PlaywrightExecutor
        return self._playwright_cls


__all__ = ["BrowserExecutorFactory"]
