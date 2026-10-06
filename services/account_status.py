"""跨平台账号状态判定共享件。

「过期」= AT 已过期（JWT 的 exp 已过）—— 所有平台同一口径（用户要求
2026-10-06：过期即 AT 已过期，与「失效」（需要重新登录）分开）。

只放与平台无关的纯函数；各平台的状态策略在 `chatgpt_account_state.py` /
`grok_account_state.py`。
"""

from __future__ import annotations

import time as _time
from typing import Any, Optional


def _read_extra(account: Any) -> dict:
    """读账号的自定义字段 —— 兼容 `Account`（.extra）与 `AccountModel`（get_extra()）。"""
    getter = getattr(account, "get_extra", None)
    if callable(getter):
        try:
            data = getter()
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001 - 坏数据不该打断状态判定
            pass
    extra = getattr(account, "extra", None)
    return extra if isinstance(extra, dict) else {}


def access_token_expired(account: Any, *, platform: str, now_seconds: Optional[int] = None) -> bool:
    """账号的 AT 是否已过期（JWT 的 exp 已过）。

    读 AT 走凭证注册表（extra 的规范名/别名 + `token` 列兜底）：chatgpt 的
    列镜像 AT，可兜底；grok 的列镜像 SSO（`token_column_credential` 对
    access_token 不认），不会把 SSO 当 AT 解。exp 解不出（非 JWT / 无 exp）
    返回 False —— 不误判成过期，那类情况由探测结论决定。
    """
    from core.credential_fields import get_credential, token_column_credential

    extra = _read_extra(account)
    token = get_credential(extra, "access_token") or token_column_credential(
        account, platform, "access_token"
    )
    if not token:
        return False
    try:
        from services.chatgpt_token_lifecycle import decode_jwt_claims

        claims = decode_jwt_claims(token)
        exp = int(claims.get("exp") or 0)
    except Exception:  # noqa: BLE001 - 解不出不算过期
        return False
    if exp <= 0:
        return False
    now = int(_time.time()) if now_seconds is None else int(now_seconds)
    return exp <= now


__all__ = ["access_token_expired"]
