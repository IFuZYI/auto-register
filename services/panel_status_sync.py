"""各面板「同步远端状态」：读远端状态写回本地。

用户要求：「每个平台都最好都有 同步远端状态、更新远端凭证、更新本地凭证。」

CPA 的版本在 `services/cliproxyapi_sync.py` —— 它的列表接口不返回状态，
必须用账号 token 探活（`/v0/management/api-call` 打上游额度端点）。
其余三个面板的列表接口本身就带权威状态，读回来即可：

- grok2api：`authStatus` + `enabled`（fetcher 已归一进 `extra.disabled`）
- sub2api：`status`（active / inactive / error）
- chatgpt2api：`status_label` / `status` + `disabled`，有则读
  `credential_availability`

**不做探活**：这三个面板自己跑 token 刷新（grok2api 每 10 分钟、
chatgpt2api 跑 `refresh_account_interval_minute`、Sub2API 跑
`token_refresh_service`），再用本地 token 打一遍会与面板抢 RT ——
x.ai 的 RT 每次刷新轮换，互相作废（`services/panel_sync.py` 记录了
本地 22 个账号全部 `invalid_grant` 的事故）。

状态词表（与前端展示对齐）：

- `active`：远端在用（正常）
- `disabled`：远端明确禁用/停用（要人工处理）
- `invalid`：凭证失效（要重新登录 / 刷新）
- `error`：远端标了错误态
- `not_found`：远端没有这个账号（未上传）
- `unknown`：没见过的状态值（不误判）
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from services.panel_comparison import RemoteAccount

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lower(value: Any) -> str:
    return str(value or "").strip().lower()


#: 状态归一表：面板 → {原始值小写: 统一词}。
#:
#: 只列**实测见过**的值（fetcher 的注释记录了来源）。没见过的值落 `unknown`
#: —— 比猜一个错的状态安全（猜错会把正常账号标成失效）。
_STATUS_MAP: dict[str, dict[str, str]] = {
    "grok2api": {
        "active": "active",
        "enabled": "active",
        "normal": "active",
        "ok": "active",
        "disabled": "disabled",
        "inactive": "disabled",
        "banned": "disabled",
        "expired": "invalid",
        "invalid": "invalid",
        "unauthorized": "invalid",
        "error": "error",
        "failed": "error",
    },
    "sub2api": {
        "active": "active",
        "normal": "active",
        "inactive": "disabled",
        "disabled": "disabled",
        "error": "error",
        "invalid": "invalid",
        "expired": "invalid",
    },
    "chatgpt2api": {
        "active": "active",
        "normal": "active",
        "正常": "active",
        "可用": "active",
        # 参考实现（yukkcat 变体）的完整分类：正常/限流/异常/禁用。
        # 「限流」是图片额度耗尽（账号仍可用）；「异常」是远程确认登录态
        # 已失效 —— 映射到 invalid 让界面提示去刷新，而不是猜成 unknown。
        "限流": "limited",
        "limited": "limited",
        "异常": "invalid",
        "abnormal": "invalid",
        "disabled": "disabled",
        "已禁用": "disabled",
        "inactive": "disabled",
        "error": "error",
        "错误": "error",
        "invalid": "invalid",
        "失效": "invalid",
        "expired": "invalid",
    },
}


def classify_remote_status(panel_key: str, remote: Optional[RemoteAccount]) -> str:
    """远端记录 → 统一状态词（纯函数）。

    优先级：`not_found`（没有记录）→ `disabled` 标志（extra）→ 状态字段映射。
    `disabled` 标志先于状态字段：面板的 `enabled=False` 比状态文案更直接
    （grok2api 的 `authStatus` 在禁用后可能仍是 `active`）。
    """
    if remote is None:
        return "not_found"

    extra = remote.extra if isinstance(remote.extra, dict) else {}
    if extra.get("disabled") is True:
        return "disabled"

    # chatgpt2api 的凭据可用性标签（比 status_label 更权威）
    availability = _lower(extra.get("credential_availability"))
    if availability == "unavailable":
        return "invalid"
    if availability == "usable":
        return "active"

    mapped = _STATUS_MAP.get(str(panel_key or "").strip().lower(), {})
    raw = _lower(remote.status)
    if not raw:
        return "unknown"
    return mapped.get(raw, "unknown")


#: 状态 → 界面文案（与 `build_status_update` 的 message 一起用）。
_STATE_LABELS: dict[str, str] = {
    "active": "正常",
    "limited": "限流",
    "disabled": "已禁用",
    "invalid": "凭证失效",
    "error": "错误",
    "not_found": "未找到",
    "unknown": "未知状态",
}


def build_status_update(panel_key: str, account: Any, remote: Optional[RemoteAccount]) -> dict[str, Any]:
    """一个账号的「同步远端状态」结果（写回 `sync_statuses.<panel>` 的补丁）。

    返回 `{id, email, ok, remote_state, message, patch}`。`ok` 只在
    `active` 时为真 —— 其余状态（禁用/失效/错误/未找到）都要在界面显示成
    待处理，否则用户以为同步成功、什么都没发生。
    """
    email = str(getattr(account, "email", "") or "")
    account_id = getattr(account, "id", None)
    state = classify_remote_status(panel_key, remote)
    label = _STATE_LABELS.get(state, state)

    remote_status = str(getattr(remote, "status", "") or "").strip() if remote else ""
    if state == "not_found":
        message = f"远端未找到该账号（{panel_key}）"
    elif state == "active":
        message = f"远端状态正常（{remote_status or 'active'}）"
    else:
        message = f"远端状态：{label}" + (f"（{remote_status}）" if remote_status else "")

    patch = {
        "sync_statuses": {
            panel_key: {
                "remote_state": state,
                "last_synced_at": _utcnow_iso(),
                "status": remote_status,
                "message": message,
            }
        }
    }
    return {
        "id": account_id,
        "email": email,
        "ok": state == "active",
        "remote_state": state,
        "message": message,
        "patch": patch,
    }


def sync_panel_status_batch(panel_key: str, accounts: list[Any]) -> dict[int, dict[str, Any]]:
    """批量：拉一次远端列表，把状态写回每个账号（返回 `{account_id: update}`）。

    `accounts` 需带 `id` / `email`。拉远端走 `fetch_panel_raw` 的同一条
    路径（`panel_comparison_cache`）—— 各写一份拉取逻辑的话，状态同步看到
    的远端与对比页显示的可能来自不同快照。
    """
    from services.panel_comparison import _match_key
    from services.panel_comparison_cache import fetch_panel_raw

    _local_rows, remote_accounts, remote_error = fetch_panel_raw(panel_key)
    results: dict[int, dict[str, Any]] = {}

    if remote_error:
        for account in accounts:
            account_id = getattr(account, "id", None)
            if account_id is None:
                continue
            results[int(account_id)] = {
                "id": account_id,
                "email": str(getattr(account, "email", "") or ""),
                "ok": False,
                "remote_state": "unreachable",
                "message": remote_error,
                "patch": {},
            }
        return results

    remote_by_key = {
        _match_key(getattr(r, "platform", ""), r.email): r for r in remote_accounts
    }
    for account in accounts:
        account_id = getattr(account, "id", None)
        if account_id is None:
            continue
        platform = str(getattr(account, "platform", "") or "")
        email = str(getattr(account, "email", "") or "")
        remote = remote_by_key.get(_match_key(platform, email))
        results[int(account_id)] = build_status_update(panel_key, account, remote)
    return results


__all__ = [
    "build_status_update",
    "classify_remote_status",
    "sync_panel_status_batch",
]
