"""邮箱渠道注册表：新增渠道 = 新文件 + 一行注册。

旧版工厂是一条 150 行的 if-elif 链，末尾用 `else: # laoudo` 兜底。后果：
把 provider 名字拼错（或传了空串）不会报错，而是**静默返回 LaoudoMailbox**——
取码方式完全不同，故障现场表现为「一直收不到验证码」，极难排查。

这里改成显式注册表：查不到就直接抛 `ValueError` 并列出可用渠道。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .base import BaseMailbox

Builder = Callable[..., BaseMailbox]

# 可选渠道来源。core 只认识 core/mailboxes/channels/ 下的内置渠道；需要
# services/、platforms/ 的渠道（如 icloud_local 要读 iCloud 主号凭据）留在
# modules/mail/ 里，import 时自注册进来。
#
# 这是 core 唯一一处反向 import modules 的地方，且只在「注册表里查不到」时
# 延迟触发，属于加载器例外——与 core/registry.py 扫描 platforms/ 同一性质。
_OPTIONAL_PROVIDER_MODULES = ("modules.mail",)


@dataclass(frozen=True)
class _Entry:
    name: str
    display_name: str
    builder: Builder


# 注册顺序即展示顺序（前端下拉、探测脚本都按这个顺序走）
_registry: dict[str, _Entry] = {}
_order: list[str] = []
_optional_loaded = False


def register_mailbox_provider(
    *names: str, display_name: str = "", replace: bool = False
) -> Callable[[Builder], Builder]:
    """把 builder 注册为一个或多个 provider 名。

    builder 签名固定为 `(*, extra: dict, proxy: str | None) -> BaseMailbox`。

    同一个 builder 挂多个名字用于别名（如 `outlook` / `microsoft`）；
    重名注册默认报错，避免两个渠道悄悄抢同一个名字。
    """

    def decorator(builder: Builder) -> Builder:
        for raw in names:
            name = str(raw or "").strip().lower()
            if not name:
                continue
            if name in _registry and not replace:
                raise ValueError(f"邮箱渠道重复注册: {name}")
            _registry[name] = _Entry(
                name=name,
                display_name=display_name or name,
                builder=builder,
            )
            if name not in _order:
                _order.append(name)
        return builder

    return decorator


def _load_optional_providers() -> None:
    """首次查不到渠道时，尝试加载 modules 里的可选渠道（只尝试一次）。

    标记先置位再 import：若 import 过程里又触发一次查询，直接返回，
    不会递归。代价是导入失败后本进程内不再重试——所以失败必须留下痕迹，
    否则表现成「渠道莫名查不到」，与「模块不存在」无法区分。
    """
    global _optional_loaded
    if _optional_loaded:
        return
    _optional_loaded = True
    for module in _OPTIONAL_PROVIDER_MODULES:
        try:
            __import__(module)
        except ModuleNotFoundError:
            # 纯 core 环境（比如只跑 core 的单测）没有 modules 也应当能跑
            continue
        except ImportError as e:
            # modules.mail 存在但自身 import 炸了（拼写错误、缺可选依赖）：
            # 与「模块不存在」是两回事，必须出声，否则查不到渠道时无从排查
            print(f"[MailboxRegistry] 加载可选渠道模块 {module} 失败: {e!r}")
            continue


def _lookup(provider: str) -> _Entry | None:
    return _registry.get(str(provider or "").strip().lower())


def build_mailbox(
    provider: str, extra: dict | None = None, proxy: str | None = None
) -> BaseMailbox:
    """按 provider 名构建邮箱渠道实例。未知名字抛 ValueError。"""
    entry = _lookup(provider)
    if entry is None:
        _load_optional_providers()
        entry = _lookup(provider)
    if entry is None:
        available = ", ".join(available_providers()) or "(空)"
        raise ValueError(f"未知邮箱提供商: {provider!r}；可用: {available}")
    return entry.builder(extra=dict(extra or {}), proxy=proxy)


def available_providers() -> list[str]:
    """已注册的 provider 名（注册顺序）。"""
    _load_optional_providers()
    return list(_order)


def provider_display_name(provider: str) -> str:
    """渠道展示名；未注册时返回原样名字。

    先尝试懒加载可选渠道：否则在全新进程里首次调用会拿到原始名字
    （`icloud_local`），调用过一次 `build_mailbox`/`available_providers`
    之后才变正常——同一个函数两种答案，纯看调用顺序，属陷阱。
    """
    entry = _lookup(provider)
    if entry is None:
        _load_optional_providers()
        entry = _lookup(provider)
    return entry.display_name if entry else str(provider or "")


def is_registered(provider: str) -> bool:
    """该渠道是否已注册（含 modules 里的可选渠道）。

    同 `provider_display_name`：先懒加载，保证答案不随调用顺序变化。
    """
    if _lookup(provider) is not None:
        return True
    _load_optional_providers()
    return _lookup(provider) is not None


__all__ = [
    "available_providers",
    "build_mailbox",
    "is_registered",
    "provider_display_name",
    "register_mailbox_provider",
]
