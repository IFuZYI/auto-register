"""平台操作 API - 通用接口，各平台通过 get_platform_actions/execute_action 实现"""
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from pydantic import BaseModel
import json
from typing import Any, Optional
from core.db import account_repository, AccountModel, platform_session_from_path
from core.registry import get
from core.base_platform import RegisterConfig
from core.config_store import config_store
from core.time_utils import UtcDatetime
from services.chatgpt_account_selection import normalize_account_ids
from services.chatgpt_account_state import (
    apply_chatgpt_status_policy,
    filter_accounts_by_plus_status,
)
from services.chatgpt_sync import update_account_model_cliproxy_sync

router = APIRouter(prefix="/actions", tags=["actions"])


class ActionRequest(BaseModel):
    params: dict = {}


class BatchActionRequest(BaseModel):
    account_ids: list[str | int] = []
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    # 日期筛选与列表接口同口径（「处理当前筛选的 N 个账号」的 N 包含它们）。
    created_at_start: Optional[UtcDatetime] = None
    created_at_end: Optional[UtcDatetime] = None
    params: dict = {}


def _get_platform_cls_or_404(platform: str):
    try:
        return get(platform)
    except KeyError as exc:
        detail = exc.args[0] if exc.args else str(exc)
        raise HTTPException(404, detail) from exc


# 动作执行的核心逻辑住在服务层（`services/action_execution.py`）。
# 这里保留 `_` 前缀别名：历史调用点（services 与 tests）都按这个名字引用。
from services.action_execution import (
    UPLOAD_ACTIONS as _UPLOAD_ACTIONS,
    apply_action_result as _apply_action_result,
    is_banned_status as _is_banned_status,
    merge_extra_patch as _merge_extra_patch,
    result_message as _result_message,
    to_platform_account as _to_platform_account,
)



#: 上传动作 → 该动作的同步状态写入函数（import 路径延迟到调用时）。
#:
#: 表驱动而不是逐动作 if 分支：新增面板上传动作时**只加一行**，
#: 漏加的症状（界面永远不显示上传状态）曾经在 sub2api / chatgpt2api
#: 上各踩过一次。grok 的 upload_sub2api 也收进来 —— 它此前没有落库，
#: 面板上 Grok 的 Sub2API 上传状态一直是空的（同一 bug 类的兄弟路径）。

#: 面板凭据上传类动作 —— 禁用（banned）账号一律跳过（用户要求 2026-10-07
#: 「禁用的不参与同步」「已封禁的没必要在面板同步凭据」）。
#:
#: 对比页的两个方向（`panel_push.plan_push` / `panel_sync.plan_sync`）已排除
#: banned；动作端点（批量/单账号）是另一条路径，不排除的话直调仍会把死凭证
#: 推给远端（远端拿死凭证刷 token、污染号池）。集合覆盖 ChatGPT 与 Grok 的
#: 全部上传动作 —— 新增上传动作时这里要同步加。










def _resolve_action_proxy(acc_model: AccountModel) -> str:
    """给「复用账号」的动作挑代理：优先它注册时那个，不可用才换。

    返回空串表示不用代理。**无绑定或绑定不可用时都会写回** `register_proxy`：
    用户要求「不再是仅有注册才绑定，若无绑定、绑定代理不可用，后续也能更新」。

    这里和批量任务（补 RT / 绑 2FA）走**同一套**决策（`proxy_pool.
    resolve_for_account`），否则同一个账号在「测活」按钮和「补 RT」任务里
    会走不同出口，行为对不上。
    """
    from core.proxy_pool import proxy_pool

    extra = acc_model.get_extra()
    saved = str(extra.get("register_proxy") or "")
    chosen, should_bind = proxy_pool.resolve_for_account(
        saved, fallback_provider=proxy_pool.get_next
    )
    if should_bind and chosen:
        # 就地更新一个键，不碰其它字段
        extra["register_proxy"] = chosen
        acc_model.set_extra(extra)
    return chosen or ""


def _execute_platform_action(
    instance: Any,
    platform: str,
    acc_model: AccountModel,
    action_id: str,
    params: dict,
    session: Session,
) -> dict[str, Any]:
    # 用**账号自己的**代理（注册时那个）：同一账号反复换出口 IP 容易被上游
    # 当成异常登录。此前这里 `config.proxy` 恒为 None（`RegisterConfig` 构造时
    # 没传），所有动作都是直连出网 —— 与注册路径的出口不一致。
    action_proxy = _resolve_action_proxy(acc_model)
    if action_proxy:
        instance.config.proxy = action_proxy
    account = _to_platform_account(acc_model)
    result = instance.execute_action(action_id, account, params)
    _apply_action_result(platform, action_id, acc_model, result, session)
    return result


def _resolve_batch_accounts(platform: str, body: BatchActionRequest, session: Session) -> tuple[list[AccountModel], list[str]]:
    if body.account_ids:
        # 与批量任务共用同一套归一（`services.chatgpt_account_selection`）：
        # 此前这里是一份内联副本，只认正整数，主键换成 UUID 后直接把 422/500
        # 甩给前端。同一语义只留一处。
        account_ids = normalize_account_ids(body.account_ids)

        if not account_ids:
            raise HTTPException(400, "账号 ID 列表不能为空")
        if len(account_ids) > 1000:
            raise HTTPException(400, "单次最多处理 1000 个账号")

        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == platform)
            .where(AccountModel.id.in_(account_ids))
        ).all()
        row_map = {row.id: row for row in rows}
        ordered_rows = [row_map[account_id] for account_id in account_ids if account_id in row_map]
        missing_ids = [account_id for account_id in account_ids if account_id not in row_map]
        return ordered_rows, missing_ids

    if not body.all_filtered:
        raise HTTPException(400, "请提供 account_ids，或指定 all_filtered=true")

    query = select(AccountModel).where(AccountModel.platform == platform)
    if body.status:
        query = query.where(AccountModel.status == body.status)
    if body.email:
        query = query.where(AccountModel.email.contains(body.email))
    if body.created_at_start is not None:
        query = query.where(AccountModel.created_at >= body.created_at_start)
    if body.created_at_end is not None:
        query = query.where(AccountModel.created_at <= body.created_at_end)

    rows = session.exec(query).all()
    if body.plus_status:
        rows = filter_accounts_by_plus_status(rows, body.plus_status)
    if len(rows) > 1000:
        raise HTTPException(400, "单次最多处理 1000 个账号")
    return rows, []


#: 各面板「同步远端状态」的批量执行表（面板 key → 动作 id）。
#:
#: 这三个面板的列表接口自带权威状态，读回来即可（不做探活 —— 它们自己跑
#: token 刷新，再用本地 token 打一遍会与面板抢 RT，见 `services/panel_status_sync.py`）。
_PANEL_STATUS_ACTIONS: dict[str, str] = {
    "sync_sub2api_status": "sub2api",
    "sync_chatgpt2api_status": "chatgpt2api",
    "sync_grok2api_status": "grok2api",
}


def _execute_batch_panel_status(
    action_id: str,
    accounts: list[AccountModel],
    session: Session,
) -> dict[str, Any]:
    """批量「同步远端状态」：一次拉远端列表，把状态写回每个账号。

    逐账号拉会 N 次打远端（列表接口没有按账号过滤的变体）—— 拉一次，
    在内存里按 (平台, 邮箱) 匹配。结果写进 `sync_statuses.<面板>`。
    """
    from services.panel_status_sync import sync_panel_status_batch

    panel_key = _PANEL_STATUS_ACTIONS[action_id]
    updates = sync_panel_status_batch(panel_key, accounts)

    items = []
    success_count = 0
    failed_count = 0
    for acc_model in accounts:
        update = updates.get(str(acc_model.id), {})
        ok = bool(update.get("ok"))
        if ok:
            success_count += 1
        else:
            failed_count += 1
        patch = update.get("patch") if isinstance(update.get("patch"), dict) else {}
        if patch:
            extra = acc_model.get_extra()
            _merge_extra_patch(extra, patch)
            acc_model.set_extra(extra)
            from datetime import datetime, timezone

            acc_model.updated_at = datetime.now(timezone.utc)
            account_repository.save(acc_model, session=session)
        items.append(
            {
                "id": acc_model.id,
                "email": acc_model.email,
                "ok": ok,
                "message": str(update.get("message") or "同步完成"),
                "status": acc_model.status,
            }
        )
    return {
        "total": len(items),
        "success": success_count,
        "failed": failed_count,
        "items": items,
    }




def _execute_batch_cliproxy_sync(accounts: list[AccountModel], session: Session) -> dict[str, Any]:
    from services.cliproxyapi_sync import is_sync_ok, sync_chatgpt_cliproxyapi_status_batch

    class SyncAccount:
        def __init__(self, model: AccountModel):
            from core.credential_fields import get_credential, token_column_credential

            extra = model.get_extra()
            self.id = model.id
            self.email = model.email
            self.user_id = model.user_id
            self.token = model.token
            self.extra = extra
            self.access_token = get_credential(extra, "access_token") or token_column_credential(
                model, "chatgpt", "access_token"
            )
            self.refresh_token = get_credential(extra, "refresh_token")
            self.id_token = get_credential(extra, "id_token")
            self.session_token = get_credential(extra, "session_token")
            self.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
            self.cookies = extra.get("cookies", "")

    sync_accounts = [SyncAccount(model) for model in accounts]
    sync_results = sync_chatgpt_cliproxyapi_status_batch(sync_accounts)

    items = []
    success_count = 0
    failed_count = 0
    for acc_model in accounts:
        sync_result = sync_results.get(int(acc_model.id or 0), {})
        update_account_model_cliproxy_sync(acc_model, sync_result, session=session, commit=False)
        ok = is_sync_ok(sync_result)
        if ok:
            success_count += 1
        else:
            failed_count += 1
        summary = (
            f"远端状态={sync_result.get('status') or 'not_found'}, "
            f"探测={sync_result.get('remote_state') or 'not_checked'}"
        )
        items.append(
            {
                "id": acc_model.id,
                "email": acc_model.email,
                "ok": ok,
                "message": f"CLIProxyAPI 状态同步完成：{summary}",
                "status": acc_model.status,
            }
        )
    return {
        "total": len(items),
        "success": success_count,
        "failed": failed_count,
        "items": items,
    }


@router.get("/{platform}")
def list_actions(platform: str):
    """获取平台支持的操作列表。

    每条动作附带 `capability`（见 `core/capabilities.py`）：前端按能力分组，
    不用再硬编码 17 个动作 id 到界面里。未登记的动作给空串 ——
    `tests/test_capability_registry.py` 会挡住「声明了没登记」。
    """
    from core.capabilities import capability_of

    PlatformCls = _get_platform_cls_or_404(platform)
    instance = PlatformCls(config=RegisterConfig(extra=config_store.get_all()))
    actions = []
    for action in instance.get_platform_actions():
        enriched = dict(action)
        capability = capability_of(str(action.get("id") or ""))
        enriched["capability"] = capability.value if capability else ""
        actions.append(enriched)
    return {"actions": actions}


@router.post("/{platform}/{action_id}/batch")
def execute_batch_action(
    platform: str,
    action_id: str,
    body: BatchActionRequest,
    session: Session = Depends(platform_session_from_path),
):
    PlatformCls = _get_platform_cls_or_404(platform)
    instance = PlatformCls(config=RegisterConfig(extra=config_store.get_all()))
    accounts, missing_ids = _resolve_batch_accounts(platform, body, session)

    if not accounts and not missing_ids:
        return {"total": 0, "success": 0, "failed": 0, "items": []}

    # 禁用账号不参与面板凭据上传（用户要求 2026-10-07）：先摘出来，结果里
    # 逐条给出原因（与对比页 push/sync 的 reason=banned 同口径）。
    banned_items: list[dict[str, Any]] = []
    if action_id in _UPLOAD_ACTIONS:
        kept = []
        for acc_model in accounts:
            if _is_banned_status(acc_model.status):
                banned_items.append(
                    {
                        "id": acc_model.id,
                        "email": acc_model.email,
                        "ok": False,
                        "message": "账号已禁用，不参与面板凭据上传",
                        "status": acc_model.status,
                    }
                )
            else:
                kept.append(acc_model)
        accounts = kept

    if platform == "chatgpt" and action_id == "sync_cliproxyapi_status":
        batch_result = _execute_batch_cliproxy_sync(accounts, session)
        if missing_ids:
            for missing_id in missing_ids:
                batch_result["failed"] += 1
                batch_result["total"] += 1
                batch_result["items"].append(
                    {
                        "id": missing_id,
                        "email": "",
                        "ok": False,
                        "message": "账号不存在",
                        "status": "",
                    }
                )
        session.commit()
        return batch_result

    if action_id in _PANEL_STATUS_ACTIONS:
        batch_result = _execute_batch_panel_status(action_id, accounts, session)
        if missing_ids:
            for missing_id in missing_ids:
                batch_result["failed"] += 1
                batch_result["total"] += 1
                batch_result["items"].append(
                    {
                        "id": missing_id,
                        "email": "",
                        "ok": False,
                        "message": "账号不存在",
                        "status": "",
                    }
                )
        session.commit()
        return batch_result

    items = list(banned_items)
    success_count = 0
    failed_count = len(banned_items)

    for missing_id in missing_ids:
        failed_count += 1
        items.append(
            {
                "id": missing_id,
                "email": "",
                "ok": False,
                "message": "账号不存在",
                "status": "",
            }
        )

    for acc_model in accounts:
        try:
            result = _execute_platform_action(instance, platform, acc_model, action_id, body.params, session)
            ok = bool(result.get("ok"))
            if ok:
                success_count += 1
            else:
                failed_count += 1
            items.append(
                {
                    "id": acc_model.id,
                    "email": acc_model.email,
                    "ok": ok,
                    "message": _result_message(result),
                    "status": acc_model.status,
                }
            )
        except Exception as exc:
            failed_count += 1
            items.append(
                {
                    "id": acc_model.id,
                    "email": acc_model.email,
                    "ok": False,
                    "message": str(exc),
                    "status": acc_model.status,
                }
            )

    session.commit()
    return {
        "total": len(items),
        "success": success_count,
        "failed": failed_count,
        "items": items,
    }


@router.post("/{platform}/{account_id}/{action_id}")
def execute_action(
    platform: str,
    account_id: str,
    action_id: str,
    body: ActionRequest,
    session: Session = Depends(platform_session_from_path),
):
    """执行平台特定操作"""
    acc_model = session.get(AccountModel, account_id)
    if not acc_model or acc_model.platform != platform:
        raise HTTPException(404, "账号不存在")

    # 禁用账号不参与面板凭据上传（用户要求 2026-10-07）——与批量端点、
    # 对比页 push/sync 同口径。拒绝而不是静默跳过：单账号动作的调用方
    # 需要知道这次没执行。
    if action_id in _UPLOAD_ACTIONS and _is_banned_status(acc_model.status):
        return {"ok": False, "error": "账号已禁用，不参与面板凭据上传"}

    PlatformCls = _get_platform_cls_or_404(platform)
    instance = PlatformCls(config=RegisterConfig(extra=config_store.get_all()))

    try:
        result = _execute_platform_action(instance, platform, acc_model, action_id, body.params, session)
        session.commit()
        return result
    except NotImplementedError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        return {"ok": False, "error": str(e)}
