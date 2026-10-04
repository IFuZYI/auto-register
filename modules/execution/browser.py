"""Executor selection for protocol and Playwright registration flows.

实现在 `core/executors/factory.py`；这里转出以保持
`from modules.execution import BrowserExecutorFactory` 与
`patch("modules.execution.BrowserExecutorFactory")` 继续可用。
"""

from __future__ import annotations

from core.executors.factory import BrowserExecutorFactory

__all__ = ["BrowserExecutorFactory"]
