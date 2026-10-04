"""ChatGPT Access Token 生命周期：生成时间 / 到期时间 / 状态。

用户要求：「Chatgpt能不能增加一个计算 AT 生成时间和到期时间的功能，就像
reference/panel/chatgpt2api 这个里面的。」

口径与参考实现（`reference/panel/chatgpt2api/services/account_credentials.py`）
一致 —— `tests/test_chatgpt_token_lifecycle.py` 直接跑参考实现对照三档判定，
避免「抄了一份但抄歪了」：

- `iat` = 签发时间（生成时间）、`exp` = 到期时间；
- 状态：`valid` / `expiring`（剩余 ≤ 24h）/ `invalid`（已过期或确认失效）/
  `unknown`（没有 exp claim —— 不是标准 JWT，不误判成失效）；
- 只解 JWT payload，**不验签**（拿到的 token 本来就是可信来源写进库的）。

只读纯函数，不落库。**生产展示路径在前端**（`frontend/src/lib/accountFormat.ts`
的 `atLifecycleMeta`，账号详情弹窗即时显示）；本模块是后端口径的基准实现 ——
`tests/test_chatgpt_token_lifecycle.py` 用它对照参考实现验证三档判定，前端
harness（`run_account_format_checks.mjs`）镜像同一组断言。后续若需要后端
计算（如上传前预检），从这里取。
"""

from __future__ import annotations

import base64
import json
import time
from typing import Any, Optional

#: 到期前多久算「即将过期」。与参考实现同一个值（24 小时）。
ACCESS_TOKEN_EXPIRING_SKEW_SECONDS = 24 * 60 * 60


def decode_jwt_claims(token: Any) -> dict[str, Any]:
    """解 JWT 的 payload 段（不验签，只读声明）。失败返回空 dict。"""
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + padding)
        claims = json.loads(raw.decode("utf-8"))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _positive_int(value: Any) -> Optional[int]:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def project_access_token_lifecycle(
    access_token: Any,
    *,
    confirmed_invalid: bool = False,
    now_seconds: Optional[int] = None,
) -> dict[str, Any]:
    """AT → `{status, issued_at, expires_at, expires_in_seconds}`。

    `issued_at` / `expires_at` 是 epoch 秒（界面自己转本地时区显示）。
    `expires_in_seconds` = 距到期的秒数（负数 = 已过期）；没有 exp claim
    时为 None。`status` 见模块 docstring。
    """
    token = str(access_token or "").strip()
    if not token:
        return {
            "status": "invalid",
            "issued_at": None,
            "expires_at": None,
            "expires_in_seconds": None,
        }

    claims = decode_jwt_claims(token)
    issued_at = _positive_int(claims.get("iat"))
    expires_at = _positive_int(claims.get("exp"))
    now = int(time.time()) if now_seconds is None else int(now_seconds)
    expires_in = expires_at - now if expires_at is not None else None

    if confirmed_invalid or (expires_in is not None and expires_in <= 0):
        status = "invalid"
    elif expires_in is not None and expires_in <= ACCESS_TOKEN_EXPIRING_SKEW_SECONDS:
        status = "expiring"
    elif expires_in is None:
        # 没有 exp：能解出 iat 但判不了到期 —— 不误判成失效。
        status = "unknown"
    else:
        status = "valid"

    return {
        "status": status,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "expires_in_seconds": expires_in,
    }


__all__ = [
    "ACCESS_TOKEN_EXPIRING_SKEW_SECONDS",
    "decode_jwt_claims",
    "project_access_token_lifecycle",
]
