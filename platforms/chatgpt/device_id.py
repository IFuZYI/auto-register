"""设备标识（oai-did）的读取助手 —— 一处定义，三条登录链共用。

背景（用户问题 2026-10-07）：「chatgpt 是会保留指纹的吧，这个指纹能复用吗，
能不能减少后续登录封号的风险。」设备标识 `oai-did` 在注册时经
`AuthFlow.auth_oauth_init` 拿到并落库；后续登录沿用同一个值（服务端实测
保留客户端预置的 oai-did）。

读取优先级：
1. `extra.device_id` —— 正规字段（注册链/收敛链写入）；
2. `extra.cookies` 里的 `oai-did=` —— 存量账号（注册早于 device_id 字段
   落库，或早期版本只存了 cookie 串）的设备标识就在 cookie 里，那是它们
   真正的「原始设备」；
3. 都没有 → 空串（登录链拿服务端分配值并在首次登录后收敛落库）。

为什么单独成模块：`plugin._refresh_via_login` / `services.chatgpt_rt_backfill` /
`services.chatgpt_two_factor` 三处都要用同一套回退逻辑，散着写会漂移
（status_probe 里已有一份只读 cookie 的私有版本，口径不同）。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = ["resolve_device_id"]


def resolve_device_id(extra: Optional[Mapping[str, Any]]) -> str:
    """从账号 extra 里解析要复用的设备标识（oai-did）。"""
    data = dict(extra or {})
    field = str(data.get("device_id") or "").strip()
    if field:
        return field
    return _from_cookies(str(data.get("cookies") or ""))


def _from_cookies(cookies: str) -> str:
    for part in cookies.split(";"):
        part = part.strip()
        if part.startswith("oai-did="):
            value = part[len("oai-did=") :].strip()
            if value:
                return value
    return ""
