"""把「本地较新」的凭证推到远端（「更新远程凭证」的动作面）。

用户要求：「同步到最新要变成两个 —— 一个是更新本地、一个是更新远程。
更新本地是将远程新的数据同步到本地，更新远程是将本地新的推送到远程
（如可行删除远端旧的）。」

方向规则（与「更新本地」相反、互斥）：

- 远端没有（未上传）→ 推送；
- 凭证不同且**本地较新** → 推送（推送成功后可选删除旧远端记录）；
- 远端较新 → 不推（那是「更新本地」的职责 —— 推上去会用本地旧凭证
  覆盖远端新凭证，x.ai 的 RT 轮换场景实测会把远端打成死值）；
- 同小时 / 无法判定时间 / 凭证相同 / 无法比对 → 不动（保守原则）。

「删除远端旧的」只在**新建式面板**上需要：CPA / grok2api 的上传是
覆盖式（同名/同邮箱记录原地更新），重推不会留重复；sub2api /
chatgpt2api 的上传是新建式（每次 add 一条新记录），所以本地较新的行
推上去之后要把旧记录删掉 —— 删除目标 id 由 `plan_push` 的
`remote_id` 带出，`allow_delete` 为真时才真正执行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from services.panel_comparison import (
    RemoteAccount,
    compare_by_hour,
    compare_credential_time,
    compare_credentials,
    parse_timestamp,
)

logger = logging.getLogger(__name__)

#: 本地可推的凭证字段（规范名 → 别名）。与 `panel_sync._PULLABLE_FIELDS`
#: 同一套字段、相反方向 —— 两边增删要同步。
_PUSHABLE_FIELDS: tuple[tuple[str, ...], ...] = (
    ("access_token", "accessToken"),
    ("refresh_token", "refreshToken"),
    ("id_token", "idToken"),
    ("sso", "sso_token"),
)


def _first_present(extra: dict[str, Any], aliases: tuple[str, ...]) -> str:
    for name in aliases:
        value = str(extra.get(name) or "").strip()
        if value:
            return value
    return ""


@dataclass
class PushOutcome:
    """单个账号的推送决策与结果。"""

    email: str
    platform: str
    #: 是否需要推送（True = 本地较新或未上传）
    push: bool = False
    #: 不推/推送的原因（not_uploaded / local_newer / remote_newer / synced / ...）
    reason: str = ""
    #: 远端已有记录的 id（新建式面板推成功后删除它；覆盖式面板忽略）
    remote_id: str = ""
    #: 推送结果
    pushed: bool = False
    deleted: bool = False
    message: str = ""
    fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "platform": self.platform,
            "push": self.push,
            "reason": self.reason,
            "remote_id": self.remote_id,
            "pushed": self.pushed,
            "deleted": self.deleted,
            "message": self.message,
            "fields": list(self.fields),
        }


def plan_push(
    local_extra: dict[str, Any],
    remote: Optional[RemoteAccount],
    *,
    local_updated: Optional[str] = None,
    email: str = "",
    platform: str = "",
) -> PushOutcome:
    """决定这个账号该不该把本地凭证推到远端（纯函数，不落库）。

    规则见模块 docstring。方向判定优先按**凭证签发时间**（JWT `iat`）——
    记录时间会被「同步远端状态」等操作 touch 成噪声（实测把本地旧凭证的行
    顶成「本地较新」，会把死凭证推上去覆盖远端新的）。凭证解不出 iat 时
    回落记录时间，比较前先归一（远端时区与本地不同）。

    `email` / `platform` 是本地行的标识：远端没有记录时（`remote is None`）
    结果里也要带上它，否则界面上的条目没有邮箱、无从定位是哪个账号。
    """
    resolved_email = str((remote.email if remote else "") or email or "")
    resolved_platform = str((remote.platform if remote else "") or platform or "")
    outcome = PushOutcome(email=resolved_email, platform=resolved_platform)

    pushable = {
        aliases[0]: _first_present(local_extra or {}, aliases)
        for aliases in _PUSHABLE_FIELDS
        if _first_present(local_extra or {}, aliases)
    }
    if not pushable:
        outcome.reason = "no_pushable_field"
        return outcome

    # 远端没有 → 未上传，直接推
    if remote is None:
        outcome.push = True
        outcome.reason = "not_uploaded"
        outcome.fields = sorted(pushable.keys())
        return outcome
    remote_creds = remote.credentials if isinstance(remote.credentials, dict) else {}
    state, _diff = compare_credentials(local_extra or {}, remote_creds)
    if state == "synced":
        outcome.reason = "synced"
        return outcome
    if state == "unknown_credential":
        # 远端不返回凭证（比不了）→ 不动：可能远端其实有同样的凭证，
        # 盲推会产生重复记录（新建式面板）。
        outcome.reason = "unknown_credential"
        return outcome

    # 凭证不同：按时间决定方向。优先凭证签发时间，解不出回落记录时间。
    relation = compare_credential_time(local_extra or {}, remote_creds)
    if not relation:
        local_time = parse_timestamp(local_updated) if local_updated else None
        remote_time = remote.updated_at
        if local_time is None or remote_time is None:
            outcome.reason = "unknown_time"
            return outcome
        relation = compare_by_hour(local_time, remote_time)
    if relation == "time_synced":
        outcome.reason = "time_synced"
        return outcome
    if relation == "remote_newer":
        outcome.reason = "remote_newer"
        return outcome

    outcome.push = True
    outcome.reason = "local_newer"
    # 只有要推送时才带删除目标（远端已有记录的 id）—— 新建式面板推成功后
    # 删掉它避免重复；不推时留空（「不推就不该删」由调用方据此保证）。
    outcome.remote_id = str(remote.remote_id or "")
    outcome.fields = sorted(pushable.keys())
    return outcome


def push_local_to_remote(
    local_rows: list[dict[str, Any]],
    remote_accounts: list[RemoteAccount],
    *,
    upload: Callable[[dict[str, Any]], tuple[bool, str]],
    delete_remote: Optional[Callable[[str, str], tuple[bool, str]]] = None,
) -> dict[str, Any]:
    """按方向判定把「本地较新 / 未上传」的账号推到远端（批量）。

    `local_rows` 每项：`{id, email, platform, updated_at, extra}`（与
    `build_comparison` 的入参同形）。`upload(row)` 负责真正的推送（返回
    `(ok, message)`）；`delete_remote(platform, remote_id)` 负责删除旧远端
    记录（新建式面板才传，覆盖式面板传 None）。

    返回 `{total, pushed, deleted, skipped, items: [PushOutcome.to_dict()]}`。
    """
    from services.panel_comparison import _match_key

    remote_by_key = {
        _match_key(getattr(r, "platform", ""), r.email): r for r in remote_accounts
    }
    summary = {"total": 0, "pushed": 0, "deleted": 0, "failed": 0, "skipped": 0, "items": []}

    for row in local_rows:
        platform = str(row.get("platform") or "")
        email = str(row.get("email") or "")
        key = _match_key(platform, email)
        remote = remote_by_key.get(key)
        extra = row.get("extra")
        if not isinstance(extra, dict):
            extra = {}
        outcome = plan_push(
            extra, remote,
            local_updated=row.get("updated_at"),
            email=email, platform=platform,
        )
        summary["total"] += 1

        if not outcome.push:
            summary["skipped"] += 1
            summary["items"].append(outcome.to_dict())
            continue

        try:
            ok, msg = upload(row)
        except Exception as exc:  # noqa: BLE001 - 单账号失败不该毁整批
            logger.warning("推送远端凭证失败 %s: %s", email, exc)
            ok, msg = False, f"{type(exc).__name__}: {str(exc)[:160]}"
        outcome.pushed = bool(ok)
        outcome.message = str(msg or "")

        if ok and outcome.remote_id and delete_remote is not None:
            try:
                d_ok, d_msg = delete_remote(platform, outcome.remote_id)
            except Exception as exc:  # noqa: BLE001
                d_ok, d_msg = False, f"{type(exc).__name__}: {str(exc)[:160]}"
            outcome.deleted = bool(d_ok)
            if not d_ok and d_msg:
                outcome.message = (outcome.message + f"；旧记录删除失败: {d_msg}")[:300]

        if outcome.pushed:
            summary["pushed"] += 1
        else:
            # 推了但失败 ≠ 无需更新 —— 混进 skipped 会让界面显示
            # 「没有需要推送的账号」，把配置缺失/网络错误藏起来（实测踩过）。
            summary["failed"] += 1
        if outcome.deleted:
            summary["deleted"] += 1
        summary["items"].append(outcome.to_dict())

    return summary


def delete_chatgpt2api_account(remote_id: str, *, api_url: str = "", api_key: str = "") -> tuple[bool, str]:
    """删除 chatgpt2api 上的一条账号记录（新建式面板推完后的清理）。

    chatgpt2api 的 `DELETE /api/accounts` 按 `account_ids`（列表里的 `id`，
    即 management_id）选目标。`remote_id` 就是对比行里的那个 id。
    """
    from core.config_store import config_store

    base = str(api_url or config_store.get("chatgpt2api_api_url", "") or "").strip()
    key = str(api_key or config_store.get("chatgpt2api_api_key", "") or "").strip()
    target = str(remote_id or "").strip()
    if not base:
        return False, "chatgpt2api 地址未配置"
    if not key:
        return False, "chatgpt2api 管理密钥未配置"
    if not target:
        return False, "缺少远端记录 id"

    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.delete(
            f"{base.rstrip('/')}/api/accounts",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {key}",
            },
            json={"account_ids": [target]},
            verify=False,
            timeout=30,
            impersonate="chrome110",
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"删除异常: {type(exc).__name__}: {str(exc)[:160]}"

    if int(getattr(resp, "status_code", 0) or 0) >= 400:
        return False, f"删除失败: HTTP {resp.status_code} {str(resp.text or '')[:200]}"
    return True, "旧记录已删除"


def delete_sub2api_account(remote_id: str, *, api_url: str = "", api_key: str = "") -> tuple[bool, str]:
    """删除 Sub2API 上的一条账号记录（新建式面板推完后的清理）。

    Sub2API 的 `DELETE /api/v1/admin/accounts/:id` 按数字 id 删。
    """
    from core.config_store import config_store

    base = str(api_url or config_store.get("sub2api_api_url", "") or "").strip()
    key = str(api_key or config_store.get("sub2api_api_key", "") or "").strip()
    target = str(remote_id or "").strip()
    if not base:
        return False, "Sub2API 地址未配置"
    if not key:
        return False, "Sub2API API Key 未配置"
    if not target:
        return False, "缺少远端记录 id"

    try:
        from curl_cffi import requests as cffi_requests

        resp = cffi_requests.delete(
            f"{base.rstrip('/')}/api/v1/admin/accounts/{target}",
            headers={
                "Accept": "application/json",
                "x-api-key": key,
            },
            verify=False,
            timeout=30,
            impersonate="chrome110",
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"删除异常: {type(exc).__name__}: {str(exc)[:160]}"

    if int(getattr(resp, "status_code", 0) or 0) >= 400:
        return False, f"删除失败: HTTP {resp.status_code} {str(resp.text or '')[:200]}"
    return True, "旧记录已删除"


__all__ = [
    "PushOutcome",
    "plan_push",
    "push_local_to_remote",
    "delete_chatgpt2api_account",
    "delete_sub2api_account",
]
