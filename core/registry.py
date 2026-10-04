"""平台插件注册表 - 自动扫描 platforms/ 目录加载插件"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Type

from .base_platform import BasePlatform

# 项目当前维护 ChatGPT 与 Grok 两类注册。
#
# iCloud 曾在这里：那个平台插件做的是「让远程 icloud-hme 服务开一个隐私邮箱」，
# 随远程链路整体删除。**注意**：iCloud 作为「本地隐私邮箱来源」仍然存在 ——
# 它是 `mail_provider=icloud_local` 这个邮箱渠道（modules/mail/icloud_local.py），
# 管理界面在「邮箱服务 > iCloud 隐私邮箱（本地）」，不在平台列表里。
SUPPORTED_PLATFORMS = ("chatgpt", "grok")

_registry: dict[str, Type[BasePlatform]] = {}


def normalize_platform(name: str) -> str:
    return str(name or "").strip().lower()


def is_platform_enabled(name: str) -> bool:
    return normalize_platform(name) in SUPPORTED_PLATFORMS


def register(cls: Type[BasePlatform]) -> Type[BasePlatform]:
    """装饰器：注册平台插件"""
    if is_platform_enabled(cls.name):
        _registry[cls.name] = cls
    return cls


def load_all() -> None:
    """自动扫描并加载 platforms/ 下所有受支持的插件"""
    import platforms

    for _, module_name, _ in pkgutil.iter_modules(platforms.__path__, platforms.__name__ + "."):
        if not is_platform_enabled(module_name.rsplit(".", 1)[-1]):
            continue
        try:
            importlib.import_module(f"{module_name}.plugin")
        except ModuleNotFoundError:
            pass


def get(name: str) -> Type[BasePlatform]:
    normalized = normalize_platform(name)
    if not is_platform_enabled(normalized):
        raise KeyError(f"平台 '{name}' 已下线")
    if normalized not in _registry:
        raise KeyError(f"平台 '{name}' 未注册，已注册: {list(_registry)}")
    return _registry[normalized]


def list_platforms() -> list[dict[str, Any]]:
    return [
        {
            "name": cls.name,
            "display_name": cls.display_name,
            "version": cls.version,
            # 前端据此决定「注册」表单要不要显示邮箱池选项（iCloud 自带隐私邮箱）
            "uses_mailbox": bool(getattr(cls, "uses_mailbox", True)),
            # 「默认注册方式」下面要显示每个平台支持哪些执行器 —— 界面不能自己
            # 硬编码这张表：它必须与 `BasePlatform.supported_executors` 一致，
            # 否则插件声明了 platform 不支持的执行器时，界面照旧显示「支持」，
            # 而运行时会静默降级（见 BasePlatform.__init__ 的自动切换日志）。
            "supported_executors": list(getattr(cls, "supported_executors", []) or []),
            # 执行器的显示名（平台专属执行器需要它，如 Grok 的 browser）。
            # 界面优先用它，缺失时回落到通用标签表。
            "executor_labels": dict(getattr(cls, "executor_labels", {}) or {}),
            # 该平台可选的注册方式（每个平台一套，含配置键与选项）。
            # 同上：由插件声明，界面不硬编码 —— 见 BasePlatform.registration_modes。
            "registration_modes": [
                dict(mode) for mode in (getattr(cls, "registration_modes", []) or [])
            ],
        }
        for cls in _registry.values()
    ]
