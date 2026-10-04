"""延迟解析门面模块 `services.icloud_service` 的代理。

门面 import 本包、本包又需要按名字查找门面上的对象（例如 `claim_alias` 必须
经门面调 `generate_alias`，这样测试 `monkeypatch.setattr(icloud_service,
"generate_alias", …)` 才能生效），构成循环 import。模块级 `from services import
icloud_service` 会在包 `__init__` 加载期炸掉，所以改用「运行时经 sys.modules
取门面」的代理 —— 所有业务函数都在门面加载完成后才被调用，取到的一定是同一个模块对象。
"""
from __future__ import annotations

import sys
from types import ModuleType


class FacadeProxy:
    @property
    def _module(self) -> ModuleType:
        return sys.modules["services.icloud_service"]

    def __getattr__(self, name: str):
        return getattr(self._module, name)

    def __setattr__(self, name: str, value) -> None:
        setattr(self._module, name, value)


_facade = FacadeProxy()
