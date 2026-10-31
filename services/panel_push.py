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
from core.panel_tls import verify_for

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlmodel import select

from core.db import AccountModel, account_repository, platform_database_registry
from services.panel_comparison import FETCHERS
from core.credential_fields import (
    first_present as _first_present,
    sync_aliases,
)
from services.panel_comparison import (
    RemoteAccount,
    compare_credential_time,
    compare_credentials,
)

logger = logging.getLogger(__name__)

#: 本地可推的凭证字段 —— 注册表一处定义（`core/credential_fields.py`），
#: 与 `panel_sync._PULLABLE_FIELDS` 同一套字段、相反方向。
_PUSHABLE_FIELDS: tuple[tuple[str, ...], ...] = sync_aliases()


def _is_banned_status(status: Any) -> bool:
    """本地行是否处于「禁用」（banned）状态（大小写/空白容忍）。

    用户要求（2026-10-07）：「禁用的不参与同步」。判据是账号行的 `status`
    列（与 `AccountStatus.BANNED` 同值）—— 库里的历史值可能有空白/大写，
    归一后再比。
    """
    return str(status or "").strip().lower() == "banned"


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
    local_status: str = "",
) -> PushOutcome:
    """决定这个账号该不该把本地凭证推到远端（纯函数，不落库）。

    规则见模块 docstring。方向判定优先按**凭证签发时间**（JWT `iat`）——
    记录时间会被「同步远端状态」等操作 touch 成噪声（实测把本地旧凭证的行
    顶成「本地较新」，会把死凭证推上去覆盖远端新的）。凭证解不出 iat 时
    回落记录时间，比较前先归一（远端时区与本地不同）。

    `email` / `platform` 是本地行的标识：远端没有记录时（`remote is None`）
    结果里也要带上它，否则界面上的条目没有邮箱、无从定位是哪个账号。

    `local_status`（用户要求 2026-10-07「禁用的不参与同步」）：`banned`
    的账号凭证已死，推上去只会污染远端面板（远端会拿死凭证去刷 token）——
    直接跳过，不进方向判定。
    """
    resolved_email = str((remote.email if remote else "") or email or "")
    resolved_platform = str((remote.platform if remote else "") or platform or "")
    outcome = PushOutcome(email=resolved_email, platform=resolved_platform)

    if _is_banned_status(local_status):
        outcome.reason = "banned"
        return outcome

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

    # 凭证不同：按时间决定方向。每侧独立「iat 优先、无则回落该侧记录时间」
    # —— 与对比页两列的显示口径一致。整体回落记录时间会被状态回写的噪声
    # 带反（用户实测报过的 bug）。
    relation = compare_credential_time(
        local_extra or {}, remote_creds,
        local_record=local_updated, remote_record=remote.updated_at,
    )
    if not relation:
        # 至少一侧连记录时间都没有 → 无法判定方向，保守不动
        outcome.reason = "unknown_time"
        return outcome
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
            # 禁用的不参与同步（用户要求 2026-10-07）：在 plan 层跳过 ——
            # 这里传行上的 status，避免 upload 被调、也避免进「失败」计数。
            local_status=str(row.get("status") or ""),
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
            verify=verify_for("panel-push"),
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
            verify=verify_for("panel-push"),
            timeout=30,
            impersonate="chrome110",
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"删除异常: {type(exc).__name__}: {str(exc)[:160]}"

    if int(getattr(resp, "status_code", 0) or 0) >= 400:
        return False, f"删除失败: HTTP {resp.status_code} {str(resp.text or '')[:200]}"
    return True, "旧记录已删除"




# ============================================================================
# 推送编排（原 `api/integrations.py` 的 132–408 行）
#
# 为什么搬下来：`services/chatgpt_maintenance.py` 的自动维护要调
# `push_panel_endpoint` 把凭证推到 chatgpt2api，于是服务层只能
# `from api.integrations import push_panel_endpoint` —— 为了 63 行编排逻辑
# 反向依赖 HTTP 层，还得连带认识 `HTTPException`。
#
# 搬下来之后：未知面板抛 `ValueError`，由 API 端点翻译成 404。服务层不再
# 需要知道 HTTP 状态码这回事。
# ============================================================================

_PANEL_PUSH_KIND: dict[str, str] = {
    "cpa": "overwrite",
    "grok2api": "overwrite",
    "sub2api": "append",
    "chatgpt2api": "append",
}


def _push_account_uploader(panel_key: str, platform: str, row: dict):
    """把一行本地账号推给对应面板，返回 `(ok, message)`。

    `row` 是 `fetch_panel_raw` 的本地行形状：`{id, email, platform,
    updated_at, extra}`。上传器按面板 + 账号所属平台分发 —— CPA 同时托管
    ChatGPT 与 Grok，用错平台的上传器会传错凭据类型。
    """
    email = str(row.get("email") or "")
    raw_extra = row.get("extra")
    extra: dict = raw_extra if isinstance(raw_extra, dict) else {}
    plat = str(platform or row.get("platform") or "").strip().lower()

    if panel_key == "grok2api":
        from platforms.grok.grok2api import Grok2ApiClient

        sso = str(extra.get("sso") or "").strip()
        if not sso:
            return False, "账号没有 SSO（grok2api 的 Web 导入需要 SSO）"
        client = Grok2ApiClient.from_config()
        if not client.configured:
            return False, "grok2api 未配置（「全局配置 → 面板配置 → grok2api」）"
        from services.chatgpt_sync import upload_proxy_for

        ok, msg = client.ingest_sso(
            sso, email, proxy=upload_proxy_for("grok2api", extra)
        )
        if ok:
            from services.chatgpt_sync import record_grok2api_sync_result

            record_grok2api_sync_result(extra, ok, msg)
        return ok, msg

    if panel_key == "cpa":
        from core.base_platform import Account, AccountStatus
        from services.chatgpt_sync import upload_proxy_for

        if plat == "grok":
            # **Grok 不再支持 CPA**（2026-10-27，用户要求只保留 SSO + grok2api）。
            # 明说原因而不是静默跳过：批量推送时用户需要知道「为什么这个号没传」。
            return False, (
                "Grok 已不再支持 CPA 上传（只支持 grok2api）"
                "—— 请在面板管理里用 grok2api"
            )

        # ChatGPT：走 generate_token_json（与手动动作 / 自动上传同一条路径）
        from core.credential_fields import get_credential
        from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

        account = Account(
            platform=plat or "chatgpt", email=email, password="",
            token=get_credential(extra, "access_token"),
            status=AccountStatus.REGISTERED, extra=extra,
        )
        return upload_to_cpa(
            generate_token_json(account), proxy=upload_proxy_for("cpa", extra)
        )

    if panel_key == "sub2api":
        from core.base_platform import Account, AccountStatus
        from core.credential_fields import get_credential
        from platforms.chatgpt.sub2api_upload import upload_to_sub2api

        account = Account(
            platform=plat or "chatgpt", email=email, password="",
            token=get_credential(extra, "access_token"),
            status=AccountStatus.REGISTERED, extra=extra,
        )
        from services.chatgpt_sync import upload_proxy_for

        return upload_to_sub2api(
            account, proxy=upload_proxy_for("sub2api", extra)
        )

    if panel_key == "chatgpt2api":
        from core.base_platform import Account, AccountStatus
        from core.credential_fields import get_credential
        from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api
        from services.chatgpt_sync import upload_proxy_for

        account = Account(
            platform=plat or "chatgpt", email=email,
            # `password` 在**独立列**上、不在 extra 里 —— `row` 只带了 extra，
            # 所以按 id 回查一次。对端 chatgpt2api 2.2.0 起靠 password +
            # totp_secret 做「RT 失效后自动密码重登」，不给就只能人工救号。
            password=_password_for_row(row),
            token=get_credential(extra, "access_token"),
            status=AccountStatus.REGISTERED, extra=extra,
        )
        return upload_to_chatgpt2api(
            account, proxy=upload_proxy_for("chatgpt2api", extra)
        )

    return False, f"面板 {panel_key} 没有推送器"


def _panel_remote_deleter(panel_key: str):
    """新建式面板的旧记录删除器；覆盖式面板返回 None（没有可删的重复）。"""
    if panel_key == "chatgpt2api":
        from services.panel_push import delete_chatgpt2api_account

        return lambda _platform, remote_id: delete_chatgpt2api_account(remote_id)
    if panel_key == "sub2api":
        from services.panel_push import delete_sub2api_account

        return lambda _platform, remote_id: delete_sub2api_account(remote_id)
    return None


def _persist_push_results(panel_key: str, items: list[dict]) -> None:
    """把 push 结果写回账号行（`sync_statuses.<面板>`）。

    与手动动作（`api/actions.py` 的 `_UPLOAD_SYNC_WRITERS`）和自动上传
    （`services/external_sync.py`）保持同一语义：推了（成功或失败）都留痕；
    跳过（无需更新）的动都没动，不写。

    `items` 是 `push_local_to_remote` 的产出 —— 只有 `push=True` 的条目
    代表真正发起过上传。写回按 `email` 定位账号（跨平台库：grok2api 的行
    在 grok 库，CPA 的行可能是 chatgpt 或 grok）。
    """
    from services.chatgpt_sync import (
        update_account_model_chatgpt2api_sync,
        update_account_model_cpa_sync,
        update_account_model_sub2api_sync,
    )

    writers = {
        "cpa": update_account_model_cpa_sync,
        "sub2api": update_account_model_sub2api_sync,
        "chatgpt2api": update_account_model_chatgpt2api_sync,
    }

    for item in items:
        if not item.get("push"):
            continue  # 跳过的没发生任何事，不留痕
        # 归一邮箱：远端返回的大小写可能与本地行不同（本地行经仓储统一为
        # 小写），不归一会精确匹配失败、静默不落库（实测 'User@X.com' 查
        # 'user@x.com' 落空）。
        from core.db import normalize_email

        email = normalize_email(str(item.get("email") or ""))
        platform = str(item.get("platform") or "").strip().lower()
        if not email or not platform:
            continue
        ok = bool(item.get("pushed"))
        message = str(item.get("message") or "")

        try:
            with platform_database_registry.session_for(platform) as session:
                row = session.exec(
                    select(AccountModel)
                    .where(AccountModel.platform == platform)
                    .where(AccountModel.email == email)
                ).first()
                if row is None:
                    continue
                # 远端删号 → 自动撤销登记（ADR-0002）。这里掌握的是「推送成功」
                # 的账号，所以 present=True 恒成立 —— 真正的撤销判定在拉回路径
                # （那里能确定远端有没有这个账号，见下面的 `_revoke_missing`）。
                from services.panel_enrollment import enroll

                extra = row.get_extra()
                enroll(
                    extra, panel_key, ok=ok, message=message,
                    remote_id=str(item.get("remote_id") or ""),
                )
                row.set_extra(extra)

                if panel_key == "grok2api":
                    # grok2api 的落库没有 update_account_model_* 版本
                    # （它走 record_grok2api_sync_result 的 extra 形状）——
                    # 就地写 extra 并提交。
                    from services.chatgpt_sync import record_grok2api_sync_result

                    extra = row.get_extra()
                    record_grok2api_sync_result(extra, ok, message)
                    row.set_extra(extra)
                else:
                    writer = writers.get(panel_key)
                    if writer is None:
                        continue
                    writer(row, ok, message, session=session, commit=False)
                account_repository.save(row, session=session)
                session.commit()
        except Exception as exc:  # noqa: BLE001 - 落库失败不该毁整批结果
            logger.warning("push 结果落库失败 %s/%s: %s", panel_key, email, exc)


def push_local_rows(
    panel_key: str,
    platform: str = "",
    delete_old: bool = False,
):
    """把「本地较新 / 未上传」的凭证推到远端（「更新远程凭证」的动作面）。

    与 `/sync`（更新本地，只拉 remote_newer）是一对**方向互斥**的动作：
    这里只推 `local_newer` 与 `local_only` 的行，`remote_newer` 的行不碰
    （推上去会用本地旧凭证覆盖远端新的）。

    `delete_old=true`：对**新建式面板**（sub2api / chatgpt2api）推成功后删除
    旧远端记录 —— 它们的上传每次新增一条，不删会留重复。覆盖式面板
    （CPA / grok2api）传了也无效（没有可删的重复记录）。

    `platform`（`chatgpt` / `grok`）给多平台面板用（CPA）：界面筛了 Grok
    再点更新时只推 Grok 的行。空串 = 全部（老行为）。
    """
    from services.panel_comparison_cache import fetch_panel_raw
    from services.panel_push import push_local_to_remote
    from services.panel_registry import resolve_panel_key

    key = resolve_panel_key(panel_key)
    if key not in FETCHERS:
        raise ValueError(f"未知面板: {panel_key}")

    local_rows, remote_accounts, remote_error = fetch_panel_raw(key)
    if remote_error:
        return {
            "panel": key,
            "total": 0,
            "pushed": 0,
            "deleted": 0,
            "failed": 0,
            "skipped": 0,
            "items": [],
            "remote_error": remote_error,
        }

    wanted = str(platform or "").strip().lower()
    if wanted:
        local_rows = [
            row for row in local_rows
            if str(row.get("platform") or "").strip().lower() == wanted
        ]

    kind = _PANEL_PUSH_KIND.get(key, "overwrite")
    deleter = _panel_remote_deleter(key) if (delete_old and kind == "append") else None

    summary = push_local_to_remote(
        local_rows,
        remote_accounts,
        upload=lambda row: _push_account_uploader(key, row.get("platform") or "", row),
        delete_remote=deleter,
    )
    summary["panel"] = key
    summary["remote_error"] = ""
    # 推送结果落库（与手动动作/自动上传同语义）——此前只写内存副本，
    # 经 push 路径上传的账号 sync_statuses 永远空白。
    _persist_push_results(key, summary.get("items") or [])
    return summary




def _lookup_account_row(account_id):
    """按 id 取账号仓储行（拿独立列上的字段，如 password）。

    对比行（`fetch_panel_raw` 的产物）只带 `extra`，而 password 存在独立列上。
    """
    from core.db import account_repository

    try:
        return account_repository.get(account_id)
    except Exception:
        return None


def _password_for_row(row) -> str:
    """取该行的 password（独立列）。

    取不到就返回空串 —— 对端是「非空才写入」，空串等于不下发该字段，
    不会抹掉远端已有的密码。
    """
    account_id = row.get("id") if isinstance(row, dict) else None
    if account_id is None:
        return ""
    found = _lookup_account_row(account_id)
    return str(getattr(found, "password", "") or "").strip() if found else ""


__all__ = [
    "push_local_rows",
    "PushOutcome",
    "plan_push",
    "push_local_to_remote",
    "delete_chatgpt2api_account",
    "delete_sub2api_account",
]
