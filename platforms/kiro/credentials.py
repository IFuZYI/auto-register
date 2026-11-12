"""Kiro 凭据格式：账号 `extra` ↔ `kiro.json`（账号管理器导出格式，v2）。

## 为什么要有这个模块

用户给的 `kiro.json` 是 Kiro 账号管理器的导出文件，形状是：

```json
{
  "accounts": [{
    "id": "kiro-account-<sha256>",
    "name": "a@b.com (GitHub)",
    "pluginIdentity": "kiro:a@b.com:github",
    "pluginPublic": { "hasAccessToken": true, "tokenTail": "15Bg", "expiresAtMs": … },
    "pluginSecret": {
      "access": "…", "refresh": "…", "clientId": "", "clientSecret": "",
      "expires": 1794478346773, "profileArn": "arn:aws:codewhisperer:…",
      "region": "us-east-1", "method": "social", "loginProvider": "github",
      "plan": "Kiro FREE", "accountId": "a@b.com", "uid": "github"
    },
    "provider": "kiro", "source": "plugin", "uid": "github"
  }],
  "customProviders": [],
  "exportedAt": 1794474836094,
  "version": 2
}
```

**落库口径**（`extra`，蛇形命名，与 `core.credential_fields` 的规范名一致）：

    access_token / refresh_token     AT / RT（加密落盘）
    client_id / client_secret        Kiro IDE 的 OIDC 客户端（secret 加密落盘）
    profile_arn / region             调用 Kiro 后端要用
    auth_method                      idc（Builder ID）| social（GitHub/Google）
    login_provider                   BuilderId | github | google
    plan                             套餐名
    account_id / uid                 账号标识
    source                           来源平台（本项目注册的一律 aws）
    source_email                     来源 AWS 账号的邮箱
    expires_at                       AT 到期时刻（毫秒 epoch，与 kiro.json 同单位）

两个方向都必须**不丢字段**：`test_round_trip_keeps_every_credential` 钉住这一点。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Iterable, Mapping

from .constants import (
    AUTH_METHOD_IDC,
    EXPORT_SOURCE,
    AUTH_METHOD_SOCIAL,
    DEFAULT_PLAN,
    DEFAULT_PROFILE_ARN,
    REGION,
    SOURCE_AWS,
)

logger = logging.getLogger(__name__)

#: 写进 `extra` 的规范键（顺序即展示顺序）。
CREDENTIAL_KEYS: tuple[str, ...] = (
    "access_token",
    "refresh_token",
    "client_id",
    "client_secret",
    "profile_arn",
    "region",
    "auth_method",
    "login_provider",
    "plan",
    "account_id",
    "uid",
    "expires_at",
    "source",
    "source_email",
    "name",
)

#: 导出 JSON 的版本号（与用户给的样例一致）。
EXPORT_VERSION = 2

#: 导出时的默认优先级（kiro.json 里的 `priority`）。
DEFAULT_PRIORITY = 100

_PROVIDER_LABELS = {
    "github": "GitHub",
    "google": "Google",
    "builderid": "BuilderId",
}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clean(value: Any) -> str:
    return str(value or "").strip()


def build_extra(
    *,
    access_token: str = "",
    refresh_token: str = "",
    client_id: str = "",
    client_secret: str = "",
    expires_in: int = 0,
    expires_at: int = 0,
    profile_arn: str = "",
    region: str = REGION,
    auth_method: str = AUTH_METHOD_IDC,
    login_provider: str = "BuilderId",
    plan: str = DEFAULT_PLAN,
    account_id: str = "",
    uid: str = "",
    email: str = "",
    name: str = "",
    source: str = SOURCE_AWS,
    source_email: str = "",
) -> dict[str, Any]:
    """把一次授权的结果收成落库用的 `extra`。

    `expires_at` 优先用显式值（毫秒）；只给了 `expires_in` 就按当前时间换算 ——
    单位与 kiro.json 的 `pluginSecret.expires` 一致（**毫秒**）。
    """
    resolved_expires = int(expires_at or 0)
    if not resolved_expires and expires_in:
        resolved_expires = _now_ms() + int(expires_in) * 1000
    extra: dict[str, Any] = {
        "access_token": _clean(access_token),
        "refresh_token": _clean(refresh_token),
        "client_id": _clean(client_id),
        "client_secret": _clean(client_secret),
        "profile_arn": _clean(profile_arn) or DEFAULT_PROFILE_ARN,
        "region": _clean(region) or REGION,
        "auth_method": _clean(auth_method) or AUTH_METHOD_IDC,
        "login_provider": _clean(login_provider) or "BuilderId",
        "plan": _clean(plan) or DEFAULT_PLAN,
        "account_id": _clean(account_id) or _clean(email),
        "uid": _clean(uid) or ("github" if _clean(auth_method) == AUTH_METHOD_SOCIAL else "builderid"),
        "expires_at": resolved_expires,
        "source": _clean(source) or SOURCE_AWS,
        "source_email": _clean(source_email) or _clean(email),
        "name": _clean(name),
    }
    if not extra["name"]:
        provider = _PROVIDER_LABELS.get(extra["login_provider"].lower(), extra["login_provider"])
        extra["name"] = f"{extra['account_id']} ({provider})" if provider else extra["account_id"]
    return {key: value for key, value in extra.items() if value not in ("", 0, None)}


def _extra_of(account: Any) -> dict[str, Any]:
    extra = getattr(account, "extra", None)
    if isinstance(extra, dict):
        return extra
    getter = getattr(account, "get_extra", None)
    if callable(getter):
        try:
            value = getter()
        except Exception as exc:  # noqa: BLE001 - 读不出 extra 时按空处理
            logger.debug("读取 extra 失败（按空处理）: %s", exc)
            return {}
        return value if isinstance(value, dict) else {}
    return {}


def _attr(account: Any, name: str) -> str:
    return _clean(getattr(account, name, ""))


def _ms_of(value: Any) -> int:
    """把 `expires_at` 归一成毫秒（兼容秒级旧值/ISO 串）。"""
    if isinstance(value, (int, float)) and value:
        number = int(value)
        return number if number > 10**12 else number * 1000
    text = _clean(value)
    if not text:
        return 0
    if text.isdigit():
        return _ms_of(int(text))
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0


def to_account_entry(account: Any) -> dict[str, Any]:
    """账号 → kiro.json 的 `accounts[]` 条目（字段名与样例逐一对齐）。"""
    extra = _extra_of(account)
    email = _attr(account, "email") or _clean(extra.get("account_id"))
    access = _clean(extra.get("access_token")) or _attr(account, "access_token")
    refresh = _clean(extra.get("refresh_token")) or _attr(account, "refresh_token") or _attr(account, "token")
    auth_method = _clean(extra.get("auth_method")) or AUTH_METHOD_IDC
    login_provider = _clean(extra.get("login_provider")) or "BuilderId"
    plan = _clean(extra.get("plan")) or DEFAULT_PLAN
    region = _clean(extra.get("region")) or REGION
    profile_arn = _clean(extra.get("profile_arn")) or DEFAULT_PROFILE_ARN
    account_id = _clean(extra.get("account_id")) or email
    uid = _clean(extra.get("uid")) or ("github" if auth_method == AUTH_METHOD_SOCIAL else "builderid")
    expires_ms = _ms_of(extra.get("expires_at"))
    added_ms = _ms_of(getattr(account, "created_at", 0)) or _now_ms()
    updated_ms = _ms_of(getattr(account, "updated_at", 0)) or _now_ms()

    provider_label = _PROVIDER_LABELS.get(login_provider.lower(), login_provider)
    name = _clean(extra.get("name")) or (f"{account_id} ({provider_label})" if provider_label else account_id)
    identity_hash = hashlib.sha256(f"{email}:{uid}".encode("utf-8")).hexdigest()

    return {
        "accessToken": "",
        "addedAt": added_ms,
        "desktop": False,
        "id": f"kiro-account-{identity_hash}",
        "name": name,
        "pluginIdentity": f"kiro:{email}:{uid}",
        "pluginPublic": {
            "accessTokenExpiresAt": expires_ms,
            "accountId": account_id,
            "canRefresh": bool(refresh),
            "expiresAt": expires_ms,
            "expiresAtMs": expires_ms,
            "hasAccessToken": bool(access),
            "hasCredentials": bool(refresh),
            "hasRefreshToken": bool(refresh),
            "name": name,
            "plan": plan,
            "tokenExpireAt": expires_ms,
            "tokenExpiresAt": expires_ms,
            "tokenTail": refresh[-4:] if refresh else "",
            "uid": uid,
        },
        "pluginSecret": {
            "access": access,
            "accountId": account_id,
            "clientId": _clean(extra.get("client_id")),
            "clientSecret": _clean(extra.get("client_secret")),
            "expires": expires_ms,
            "loginProvider": login_provider,
            "method": auth_method,
            "name": name,
            "plan": plan,
            "profileArn": profile_arn,
            "proxy": None,
            "refresh": refresh,
            "region": region,
            "uid": uid,
        },
        "priority": DEFAULT_PRIORITY,
        "provider": "kiro",
        "proxy": {"source": "direct"},
        "refreshToken": "",
        # 导出写 `plugin`（与账号管理器样例一致），不是我们的 `source=aws`
        "source": EXPORT_SOURCE,
        "uid": uid,
        "updatedAt": updated_ms,
    }


def build_document(
    entries: Iterable[Mapping[str, Any]], *, exported_at: int | None = None
) -> dict[str, Any]:
    """拼出完整的 kiro.json（`version: 2`）。"""
    return {
        "accounts": [dict(item) for item in entries],
        "customProviders": [],
        "exportedAt": int(exported_at or _now_ms()),
        "version": EXPORT_VERSION,
    }


def parse_entry(entry: Mapping[str, Any]) -> dict[str, Any]:
    """单条 kiro.json 账号 → 规范 `extra` 字典。"""
    secret = entry.get("pluginSecret") if isinstance(entry.get("pluginSecret"), dict) else {}
    public = entry.get("pluginPublic") if isinstance(entry.get("pluginPublic"), dict) else {}
    auth_method = _clean(secret.get("method")).lower() or (
        AUTH_METHOD_SOCIAL if _clean(secret.get("loginProvider")).lower() in {"github", "google"} else AUTH_METHOD_IDC
    )
    login_provider = _clean(secret.get("loginProvider")) or "BuilderId"
    account_id = _clean(secret.get("accountId")) or _clean(public.get("accountId")) or _clean(entry.get("name"))
    email = account_id if "@" in account_id else _clean(public.get("accountId"))
    raw_source = _clean(entry.get("source"))
    # 管理器导出的 `plugin` 对 idc（Builder ID）账号等价于我们的 `source=aws`；
    # social 账号保留原值（它们的来源是 GitHub/Google，不是 AWS）。
    # idc（Builder ID）默认/plugin → aws；social 账号没有 source 时默认 plugin，
    # 不能把 GitHub/Google 号标成 AWS 来源。
    if auth_method == AUTH_METHOD_IDC:
        source = SOURCE_AWS if raw_source in ("", EXPORT_SOURCE) else raw_source
    else:
        source = raw_source or EXPORT_SOURCE
    record = build_extra(
        access_token=_clean(secret.get("access")) or _clean(entry.get("accessToken")),
        refresh_token=_clean(secret.get("refresh")) or _clean(entry.get("refreshToken")),
        client_id=_clean(secret.get("clientId")),
        client_secret=_clean(secret.get("clientSecret")),
        expires_at=_ms_of(secret.get("expires")) or _ms_of(public.get("expiresAtMs")),
        profile_arn=_clean(secret.get("profileArn")),
        region=_clean(secret.get("region")) or REGION,
        auth_method=auth_method,
        login_provider=login_provider,
        plan=_clean(secret.get("plan")) or _clean(public.get("plan")) or DEFAULT_PLAN,
        account_id=account_id,
        uid=_clean(secret.get("uid")) or _clean(entry.get("uid")),
        email=email or account_id,
        name=_clean(entry.get("name")) or _clean(secret.get("name")),
        source=source,
        source_email=_clean(public.get("accountId")) or email or account_id,
    )
    record["email"] = email or account_id
    return record


def parse_document(payload: Any) -> list[dict[str, Any]]:
    """kiro.json（对象或裸数组）→ 规范 `extra` 列表。"""
    if isinstance(payload, dict):
        entries = payload.get("accounts")
        if not isinstance(entries, list):
            entries = []
    elif isinstance(payload, list):
        entries = payload
    else:
        entries = []
    records: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, dict):
            records.append(parse_entry(entry))
    return records


def records_from_entries(entries: Iterable[Mapping[str, Any]]):
    """规范 `extra` 列表 → `AccountRecord` 列表（供导入落库）。"""
    from core.models.account import AccountRecord

    records = []
    for entry in entries:
        extra = dict(entry)
        email = _clean(extra.get("source_email")) or _clean(extra.get("account_id"))
        if not email:
            continue
        records.append(
            AccountRecord(
                platform="kiro",
                email=email,
                password="",
                region=_clean(extra.get("region")),
                token=_clean(extra.get("refresh_token")),
                extra=extra,
            )
        )
    return records


def export_document(accounts: Iterable[Any], *, exported_at: int | None = None) -> dict[str, Any]:
    """账号序列 → kiro.json 文档（只保留 kiro 平台的行）。"""
    entries = [
        to_account_entry(account)
        for account in accounts
        if _clean(getattr(account, "platform", "")) == "kiro"
    ]
    return build_document(entries, exported_at=exported_at)


def dumps(document: Mapping[str, Any]) -> str:
    """序列化成可直接下载的 JSON 文本。"""
    return json.dumps(document, ensure_ascii=False, indent=2)


__all__ = [
    "CREDENTIAL_KEYS",
    "EXPORT_VERSION",
    "build_document",
    "build_extra",
    "dumps",
    "export_document",
    "parse_document",
    "parse_entry",
    "records_from_entries",
    "to_account_entry",
]