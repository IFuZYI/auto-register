"""凭证字段注册表 —— 一处定义，处处消费。

背景（用户要求「有 sso_token AT RT 这些 请好好思考、整理数据结构」）

整理前，凭证字段表散落在六处、各自维护、已经漂移：

- `services/panel_comparison.py` 的 `CREDENTIAL_FIELDS`（5 字段，参与比对）
- `services/panel_sync.py` 的 `_PULLABLE_FIELDS`（4 字段，漏了 session_token）
- `services/panel_push.py` 的 `_PUSHABLE_FIELDS`（4 字段，同样漏）
- `services/account_export.py` 的导出字段（漏了 sso —— grok 账号 JSON
  导出→导入会丢 SSO，实测真 bug）
- `api/accounts.py` 的 `_IMPORT_EXTRA_KEYS`（漏了 sso，同上）
- `api/actions.py` 的 `tracked_keys`（手写集合，混着 camelCase 拼写与
  `clientId` / `clientSecret` / `webAccessToken` 三个**零生产方的死键**）

再加上 `token` 列语义混乱：grok 注册写 SSO，AT 刷新路径又把它盖成 AT
（线上实测 32 行里 12 行是 AT）——读侧的 `or account.token` 兜底会拿错值。

本模块把这些收敛成一处：

- `CREDENTIAL_FIELDS` —— 规范名 + 别名 + 短标签（每个字段认蛇形与 camelCase）；
- `compare_aliases()` / `sync_aliases()` —— 比对/同步用的别名表；
- `export_names()` —— 导出用的规范名清单；
- `first_present()` / `get_credential()` / `canonical_writes()` —— 读写助手；
- `token_column_field()` / `sync_token_column()` —— `token` 列的镜像规则
  （token 列 = 平台主凭证的镜像：chatgpt → AT，grok → SSO）。

**同步范围与对比范围当前一致**（`session_token` 也纳入同步）：整理前
session_token 只参与对比不参与同步，那种「能比出来却没法同步」的半截状态
说不清是故意还是漏了。统一规则：凭证字段全量参与对比与同步；面板实际消费
哪些由各上传器决定。

改动本文件 = 改全系统凭证口径。字段增删时跟着跑 `tests/test_credential_fields.py`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class CredentialField:
    """一个凭证字段：规范名（蛇形）+ 别名（含 camelCase）+ 短标签。"""

    name: str
    aliases: tuple[str, ...]
    label: str


#: 凭证字段的规范定义。顺序有含义：读取/比对按此顺序，注册表契约测试钉住它。
CREDENTIAL_FIELDS: tuple[CredentialField, ...] = (
    CredentialField("access_token", ("access_token", "accessToken"), "AT"),
    CredentialField("refresh_token", ("refresh_token", "refreshToken"), "RT"),
    CredentialField("session_token", ("session_token", "sessionToken"), "ST"),
    CredentialField("id_token", ("id_token", "idToken"), "IDT"),
    # 面板导出格式用 `sso_token` 这个拼写（grok2api 的 export 字段名）
    CredentialField("sso", ("sso", "sso_token"), "SSO"),
)

#: `token` 列（历史遗留列）在各平台的镜像字段。
#:
#: token 列 = **平台主凭证的镜像**：chatgpt 镜像 AT、grok 镜像 SSO。
#: 整理前没有这条规则，AT 刷新路径把 grok 的 token 列盖成 AT（线上 32 行里
#: 12 行如此），读侧 `or account.token` 的兜底会拿错值。写侧一律走
#: `sync_token_column` 后，列与主凭证保持一致。
_TOKEN_COLUMN_FIELDS: dict[str, str] = {
    "chatgpt": "access_token",
    "grok": "sso",
}


def _field(name: str) -> CredentialField:
    for item in CREDENTIAL_FIELDS:
        if item.name == name:
            return item
    raise KeyError(f"未注册的凭证字段: {name}")


def field_aliases(name: str) -> tuple[str, ...]:
    """字段的全部别名（规范名排第一位）。"""
    return _field(name).aliases


def compare_aliases() -> tuple[tuple[str, ...], ...]:
    """参与「是否同步」比对的全部字段别名表（消费方遍历用）。"""
    return tuple(item.aliases for item in CREDENTIAL_FIELDS)


def sync_aliases() -> tuple[tuple[str, ...], ...]:
    """参与同步（拉/推）的全部字段别名表 —— 与对比范围一致。"""
    return tuple(item.aliases for item in CREDENTIAL_FIELDS)


def export_names() -> list[str]:
    """导出/导入覆盖的规范名清单（含 sso —— 整理前缺失导致往返丢 SSO）。"""
    return [item.name for item in CREDENTIAL_FIELDS]


def first_present(extra: Mapping[str, Any], aliases: tuple[str, ...]) -> str:
    """按别名顺序取第一个非空值（规范名优先，兼容 camelCase 落库）。"""
    for name in aliases:
        value = str(extra.get(name) or "").strip()
        if value:
            return value
    return ""


def get_credential(extra: Mapping[str, Any], name: str) -> str:
    """按规范名读取凭证值（自动认 camelCase 别名）。"""
    return first_present(extra or {}, field_aliases(name))


def canonical_writes(data: Mapping[str, Any]) -> dict[str, str]:
    """把一段数据里的凭证字段归一到规范名（只收凭证字段、跳过空值）。

    用于动作结果的落库：插件返回的 `data` 可能混着 message/status 等展示
    字段与 camelCase 拼写的凭证 —— 只把凭证部分写进 extra，其余不落。
    """
    writes: dict[str, str] = {}
    for item in CREDENTIAL_FIELDS:
        value = first_present(data or {}, item.aliases)
        if value:
            writes[item.name] = value
    return writes


def token_column_field(platform: str) -> str:
    """该平台 `token` 列的镜像字段名；未登记的平台返回空串。"""
    return _TOKEN_COLUMN_FIELDS.get(str(platform or "").strip().lower(), "")


def sync_token_column(row: Any, platform: str, credentials: Mapping[str, Any]) -> str:
    """把平台主凭证的镜像写进 `token` 列，返回写入的字段名（没写返回空串）。

    规则：token 列 = 平台主凭证镜像（chatgpt → access_token，grok → sso）。
    没有对应值时不碰列 —— 沉默不等于清空。
    """
    field_name = token_column_field(platform)
    if not field_name:
        return ""
    value = get_credential(credentials or {}, field_name)
    if not value:
        return ""
    row.token = value
    return field_name


def _jwt_payload(value: Any) -> Optional[dict]:
    """解 JWT 的 payload（不验签）。解不出返回 None。"""
    import base64
    import json as _json

    parts = str(value or "").strip().split(".")
    if len(parts) != 3:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = _json.loads(base64.urlsafe_b64decode(padded))
    except Exception:  # noqa: BLE001 - 不是合法 JWT 就返回 None
        return None
    return payload if isinstance(payload, dict) else None


def is_oauth_access_token(value: Any) -> bool:
    """像不像 OAuth 的 AT/IDT —— JWT 且带 `exp` / `iat` / `iss` 生命周期 claim。

    x.ai 的 SSO 也是三段式 JWT，但 payload 只有一个 `session_id` —— 用
    「带不带 exp/iat/iss」区分 AT 与 SSO；单看段数区分不了（线上实测）。
    用于读侧兜底拦截「token 列被 AT 盖过」的脏值被当成 SSO 使用。
    """
    payload = _jwt_payload(value)
    if payload is None:
        return False
    return any(key in payload for key in ("exp", "iat", "iss"))


def token_column_credential(row: Any, platform: str, field_name: str) -> str:
    """从 `token` 列兜底读某个字段的值 —— 仅当镜像是该字段且形态合理。

    例：grok 的 `token` 列镜像 sso。读 sso 时可用它兜底，但列上如果是
    OAuth 形态的 JWT（被 AT 盖过的脏值）则不认；读 access_token 时它根本
    不是镜像（token 列不是 AT），也不认。
    """
    if token_column_field(platform) != field_name:
        return ""
    value = str(getattr(row, "token", "") or "").strip()
    if not value:
        return ""
    if field_name == "sso" and is_oauth_access_token(value):
        return ""
    return value


__all__ = [
    "CREDENTIAL_FIELDS",
    "CredentialField",
    "canonical_writes",
    "compare_aliases",
    "export_names",
    "field_aliases",
    "first_present",
    "get_credential",
    "is_oauth_access_token",
    "sync_aliases",
    "sync_token_column",
    "token_column_credential",
    "token_column_field",
]
