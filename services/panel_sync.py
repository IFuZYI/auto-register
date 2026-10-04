"""本地 ↔ 远程面板凭证同步（把「对比」变成「动作」）。

背景（用户的三个诉求与实测约束）：

1. **「增加本地远程同步功能，同步到最新」** —— 现有的上传方向已经有了
   （本地 → 远端，见 `services/external_sync.py` 与各平台的 `upload_*`），
   缺的是**反方向**：远端可能比本地新（远端面板自己刷新过 token、轮换了
   RT），本地需要拉回来。x.ai 的 RT 每次刷新都会轮换，谁最后刷新谁持有
   有效 RT —— 不拉回来，本地存的就是失效 RT（实测：本地 22 个账号的 RT
   全部 `invalid_grant`，而远端可用）。
2. **判定谁更新**：凭证比对（`panel_comparison.compare_credentials`）已经
   给出 `synced` / `credential_diff`；时间（`compare_by_hour`）给出
   `local_newer` / `remote_newer`。本模块按「**远端较新 → 拉回本地**」
   执行。
3. **时区**：远端面板服务器时区可能与本地不同（grok2api 实测
   `+08:00`，CPA 也 `+08:00`，本地 UTC）—— 时间比较必须**先归一成
   epoch**（`parse_timestamp` 已做），展示时标明时区。

方向规则（保守原则）：
- 远端较新且远端有凭证 → 拉回本地（覆盖 AT/RT/id_token，保留本地其它字段）；
- 本地较新 → 不动（等下一次上传把本地推上去）；
- 同小时/无法判定 → 不动（不拿不确定的数据覆盖本地）；
- 远端没有凭证 → 不动（没有可拉的东西）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from services.panel_comparison import (
    CREDENTIAL_FIELDS,
    RemoteAccount,
    compare_credentials,
    parse_timestamp,
)

logger = logging.getLogger(__name__)

#: 可从远端拉回的凭证字段（规范名 → 本地 extra 的写入键）。
#:
#: 只拉凭证类字段：本地还存着注册上下文（register_proxy、mail_provider、
#: cpa_record 的派生字段等），远端没有这些，整包覆盖会把它们抹掉。
_PULLABLE_FIELDS: tuple[tuple[str, ...], ...] = (
    ("access_token", "accessToken"),
    ("refresh_token", "refreshToken"),
    ("id_token", "idToken"),
    ("sso", "sso_token"),
)


@dataclass
class SyncOutcome:
    """单个账号的同步结果。"""

    email: str
    platform: str
    #: 是否拉取了远端凭证（True = 本地被更新）
    pulled: bool = False
    #: 不拉时的原因（synced / local_newer / remote_missing_credential / ...）
    reason: str = ""
    #: 实际更新的字段名
    fields: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.fields is None:
            self.fields = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "platform": self.platform,
            "pulled": self.pulled,
            "reason": self.reason,
            "fields": list(self.fields),
        }


def _first_present(extra: dict[str, Any], aliases: tuple[str, ...]) -> str:
    for name in aliases:
        value = str(extra.get(name) or "").strip()
        if value:
            return value
    return ""


def plan_sync(
    local_extra: dict[str, Any],
    remote: RemoteAccount,
    *,
    local_updated: Optional[str] = None,
) -> SyncOutcome:
    """决定这个账号该不该从远端拉凭证（纯函数，不落库）。

    规则见模块 docstring。时间用 `parse_timestamp` 归一后比 epoch ——
    远端时区与本地不同（`+08:00` vs UTC），比字符串会得出错误结论。
    """
    email = str(remote.email or "")
    platform = str(remote.platform or "")
    outcome = SyncOutcome(email=email, platform=platform)

    remote_creds = remote.credentials if isinstance(remote.credentials, dict) else {}
    if not remote_creds:
        outcome.reason = "remote_missing_credential"
        return outcome

    state, _diff = compare_credentials(local_extra, remote_creds)
    if state == "synced":
        outcome.reason = "synced"
        return outcome

    # 凭证不同：按时间决定方向。远端时间缺失 → 不动（保守）。
    local_time = parse_timestamp(local_updated) if local_updated else None
    remote_time = remote.updated_at
    if remote_time is None:
        outcome.reason = "unknown_time"
        return outcome
    if local_time is not None and local_time >= remote_time:
        outcome.reason = "local_newer"
        return outcome

    # 远端较新：检查至少有一个可拉字段有值
    pullable = {
        aliases[0]: _first_present(remote_creds, aliases)
        for aliases in _PULLABLE_FIELDS
        if _first_present(remote_creds, aliases)
    }
    if not pullable:
        outcome.reason = "no_pullable_field"
        return outcome

    outcome.pulled = True
    outcome.reason = "remote_newer"
    outcome.fields = sorted(pullable.keys())
    return outcome


def apply_pull(local_extra: dict[str, Any], remote: RemoteAccount) -> dict[str, Any]:
    """把远端凭证写进本地 extra（返回新 dict，不改原对象）。

    只覆盖凭证字段；本地其它键（注册上下文、CPA 记录等）保留。
    键名统一写蛇形（`access_token`）—— 项目里较新的落库路径都读它。
    """
    merged = dict(local_extra or {})
    remote_creds = remote.credentials if isinstance(remote.credentials, dict) else {}
    for aliases in _PULLABLE_FIELDS:
        value = _first_present(remote_creds, aliases)
        if value:
            merged[aliases[0]] = value
    return merged


def sync_local_from_remote(
    local_rows: list[dict[str, Any]],
    remote_accounts: list[RemoteAccount],
    *,
    commit: bool = True,
) -> dict[str, Any]:
    """按对比结果把「远端较新」的凭证拉回本地（批量）。

    `local_rows` 每项：`{id, email, platform, updated_at, extra}`（与
    `build_comparison` 的入参同形）。只有同时满足「两边都有该账号」且
    「远端较新」的行会被写。

    返回 `{total, pulled, skipped, items: [SyncOutcome.to_dict()]}`。
    """
    from services.panel_comparison import _match_key

    remote_by_key = {
        _match_key(getattr(r, "platform", ""), r.email): r for r in remote_accounts
    }
    summary = {"total": 0, "pulled": 0, "skipped": 0, "items": []}

    for row in local_rows:
        platform = str(row.get("platform") or "")
        email = str(row.get("email") or "")
        key = _match_key(platform, email)
        remote = remote_by_key.get(key)
        if remote is None:
            continue  # 远端没有这个账号 → 不在「拉回」的职责范围（那是上传）
        summary["total"] += 1
        extra = row.get("extra") if isinstance(row.get("extra"), dict) else {}
        outcome = plan_sync(
            extra, remote, local_updated=row.get("updated_at")
        )
        if not outcome.pulled:
            summary["skipped"] += 1
            summary["items"].append(outcome.to_dict())
            continue

        merged = apply_pull(extra, remote)
        try:
            _persist_local(row, merged, platform, commit=commit)
            summary["pulled"] += 1
        except Exception as exc:  # noqa: BLE001 - 单账号落库失败不该毁整批
            logger.warning("拉回远端凭证失败 %s: %s", email, exc)
            outcome.pulled = False
            outcome.reason = f"persist_failed: {type(exc).__name__}"
            summary["skipped"] += 1
        summary["items"].append(outcome.to_dict())

    return summary


def _persist_local(row: dict[str, Any], merged_extra: dict[str, Any], platform: str, *, commit: bool) -> None:
    """把合并后的 extra 写回账号行（按平台选库）。"""
    from core.db import platform_database_registry
    from core.db.models_account import AccountModel

    account_id = row.get("id")
    if account_id is None:
        raise ValueError("账号行没有 id")

    session = platform_database_registry.session_for(platform)
    try:
        model = session.get(AccountModel, int(account_id))
        if model is None:
            raise ValueError(f"账号 {account_id} 不在 {platform} 库里")
        model.set_extra(merged_extra)
        # access_token 也同步到 token 列（历史消费方读列）
        at = str(merged_extra.get("access_token") or "").strip()
        if at:
            model.token = at
        from datetime import datetime, timezone

        model.updated_at = datetime.now(timezone.utc)
        session.add(model)
        if commit:
            session.commit()
    finally:
        session.close()


__all__ = [
    "SyncOutcome",
    "apply_pull",
    "plan_sync",
    "sync_local_from_remote",
]
