"""账号凭据的字段级加密（T1.4）。

## 加密什么

`extra_json` 里这几把是**可直接冒用身份的 bearer 凭证**，泄露一把等于泄露账号：

    access_token / refresh_token / id_token / session_token / sso / totp_secret / cookies

其余键（`client_id`、同步状态、面板登记…）保持明文 —— 它们要么不是
秘密，要么需要被 SQL 查询/排序（加密后 `WHERE extra_json LIKE '%x%'` 就失效）。

## 为什么在 `get_extra` / `set_extra` 上做

`AccountModel.get_extra()` / `set_extra()` 是**所有读写 extra 的必经之路**
（仓储、面板同步、注册链路、维护任务都走它）。在这两个方法上挂加解密，
调用方一行都不用改：读到的永远是明文，落盘的永远是密文。

绕过它们直接 `json.loads(row.extra_json)` 的地方只有两处，都已单独处理
（`repository.merge_account_fields` 与 `migrations` 的 grok token 修补）。

## 兼容：老数据是明文

`decrypt_text()` 对**不以 `v1:` 开头**的值原样返回 —— 迁移前落库的明文照常能读，
不需要一次性刷库。`encrypt_text()` 对已经是 `v1:` 的值不重复加密（幂等），
所以「读出来再写回去」不会套娃。

## 开关

`CREDENTIAL_ENCRYPTION=0` 关掉（默认开）。关掉时两个函数都退化成恒等映射，
用于排障：「解不开」时先确认是密钥文件丢了还是数据真坏了。
"""

from __future__ import annotations

import os
from typing import Any

from core.secret_box import CredentialKeyError, secret_box

#: 需要加密的 extra 键。加新键要同步 `docs/refactor/METRICS.md` 的清单。
SENSITIVE_EXTRA_KEYS: frozenset[str] = frozenset(
    {
        "access_token",
        "refresh_token",
        "id_token",
        "session_token",
        "sso",
        "totp_secret",
        # `cookies` 是**会话凭据**：带着它就能免密续上已登录会话，与 AT 同级。
        # 原来是明文（真库 607 行），理由是「需要被 SQL 查询」—— 实际全仓没有
        # 任何 `WHERE extra_json LIKE '%cookies%'`，这条理由不成立
        # （data-layer 审计 D-18）。
        "cookies",
    }
)

#: 密文信封前缀（与 `SecretBox` 一致）。
ENVELOPE_PREFIX = "v1:"

_DISABLED_VALUES = {"0", "false", "off", "no", ""}


def enabled() -> bool:
    """是否启用字段级加密。默认**开**；显式写 0/false/off 才关。"""
    raw = os.getenv("CREDENTIAL_ENCRYPTION")
    if raw is None:
        return True
    return str(raw).strip().lower() not in _DISABLED_VALUES


def is_ciphertext(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(ENVELOPE_PREFIX)


def encrypt_text(value: Any) -> Any:
    """加密一个字符串值。空值 / 非字符串 / 已是密文 → 原样返回。"""
    if not enabled():
        return value
    if not isinstance(value, str) or not value:
        return value
    if is_ciphertext(value):
        return value
    return secret_box.encrypt(value.encode("utf-8"))


def decrypt_text(value: Any) -> Any:
    """解密一个字符串值。非密文（老明文）→ 原样返回。"""
    if not isinstance(value, str) or not value:
        return value
    if not is_ciphertext(value):
        return value
    try:
        return secret_box.decrypt(value).decode("utf-8")
    except CredentialKeyError:
        raise
    except Exception as exc:  # noqa: BLE001 - 解密失败要带键名，不能只抛底层错误
        raise CredentialKeyError(f"凭据解密失败（密钥文件是否被替换过？）: {exc}") from exc


def encrypt_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    """返回**副本**：敏感键加密，其余原样。"""
    payload = dict(extra or {})
    if not enabled():
        return payload
    for key in SENSITIVE_EXTRA_KEYS & payload.keys():
        payload[key] = encrypt_text(payload[key])
    return payload


def decrypt_extra(extra: dict[str, Any] | None) -> dict[str, Any]:
    """返回**副本**：敏感键解密，其余原样。老明文原样通过。"""
    payload = dict(extra or {})
    for key in SENSITIVE_EXTRA_KEYS & payload.keys():
        payload[key] = decrypt_text(payload[key])
    return payload


def encrypted_key_count(extra: dict[str, Any] | None) -> int:
    """这份 extra 里已加密的敏感键数量（排障/测试用）。"""
    payload = extra or {}
    return sum(1 for key in SENSITIVE_EXTRA_KEYS & payload.keys() if is_ciphertext(payload[key]))


__all__ = [
    "ENVELOPE_PREFIX",
    "SENSITIVE_EXTRA_KEYS",
    "decrypt_extra",
    "decrypt_text",
    "enabled",
    "encrypt_extra",
    "encrypt_text",
    "encrypted_key_count",
    "is_ciphertext",
]
