"""注册产物 → 账号 `extra` 的规范化。

把 `AwsBuilderIdClient.register()` 的返回字典收成**落库用的字段集**。集中在一处
的理由：注册链路、导入链路、刷新链路都要产出同一组键，散着写必然漂移（本仓
此前在 grok 的 SSO 上踩过一次：导出漏了字段，往返即丢）。

字段口径（与 `kiro.rs` 的 credentials.json、面板导出的 `BuilderId` 一致）：

    refresh_token  长期凭据（刷新 AT 用）—— token 列的镜像
    access_token   短期凭据（1 小时）
    client_id      OIDC 客户端（刷新时必须与 client_secret 一起给）
    client_secret  OIDC 客户端密钥（**机密**，落盘加密）
    sso            access portal 的会话令牌（换 Kiro 凭据要用）
    region         固定 us-east-1
    provider       BuilderId
    expires_in     AT 剩余秒数（注册时刻的值）
"""
from __future__ import annotations

from typing import Any, Mapping

from .constants import PROVIDER, REGION

#: 会被写进 `extra` 的键（顺序即展示顺序）。
CREDENTIAL_KEYS: tuple[str, ...] = (
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
    "sso",
    "region",
    "provider",
    "expires_in",
    "user_code",
    "device_code",
)


def build_extra(result: Mapping[str, Any]) -> dict[str, Any]:
    """把注册结果收成 `extra`（只收非空值；`expires_in` 保持整数）。"""
    extra: dict[str, Any] = {}
    for key in CREDENTIAL_KEYS:
        value = result.get(key)
        if key == "expires_in":
            try:
                number = int(value or 0)
            except (TypeError, ValueError):
                number = 0
            if number:
                extra[key] = number
            continue
        text = str(value or "").strip()
        if text:
            extra[key] = text
    extra.setdefault("region", REGION)
    extra.setdefault("provider", PROVIDER)
    return extra


__all__ = ["CREDENTIAL_KEYS", "build_extra"]