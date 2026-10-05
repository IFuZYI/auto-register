"""平台操作 API - 通用接口，各平台通过 get_platform_actions/execute_action 实现"""
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from pydantic import BaseModel
import json
from typing import Any
from core.db import AccountModel, platform_session_from_path
from core.registry import get
from core.base_platform import RegisterConfig
from core.config_store import config_store
from services.chatgpt_account_state import (
    apply_chatgpt_status_policy,
    filter_accounts_by_plus_status,
)
from services.chatgpt_sync import update_account_model_cliproxy_sync

router = APIRouter(prefix="/actions", tags=["actions"])


class ActionRequest(BaseModel):
    params: dict = {}


class BatchActionRequest(BaseModel):
    account_ids: list[int] = []
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    params: dict = {}


def _get_platform_cls_or_404(platform: str):
    try:
        return get(platform)
    except KeyError as exc:
        detail = exc.args[0] if exc.args else str(exc)
        raise HTTPException(404, detail) from exc


def _merge_extra_patch(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_extra_patch(base[key], value)
        else:
            base[key] = value
    return base


#: 上传动作 → 该动作的同步状态写入函数（import 路径延迟到调用时）。
#:
#: 表驱动而不是逐动作 if 分支：新增面板上传动作时**只加一行**，
#: 漏加的症状（界面永远不显示上传状态）曾经在 sub2api / chatgpt2api
#: 上各踩过一次。grok 的 upload_sub2api 也收进来 —— 它此前没有落库，
#: 面板上 Grok 的 Sub2API 上传状态一直是空的（同一 bug 类的兄弟路径）。
_UPLOAD_SYNC_WRITERS: dict[str, tuple[str, str]] = {
    # action_id: (模块, 函数名)
    "upload_cpa": ("services.chatgpt_sync", "update_account_model_cpa_sync"),
    "upload_sub2api": ("services.chatgpt_sync", "update_account_model_sub2api_sync"),
    "upload_chatgpt2api": ("services.chatgpt_sync", "update_account_model_chatgpt2api_sync"),
}


def _record_upload_sync(
    platform: str,
    action_id: str,
    acc_model: AccountModel,
    result: dict[str, Any],
    session: Session,
) -> None:
    """把上传动作的结果写进账号的 `sync_statuses.<面板名>`。

    没写的话面板管理页「上传状态」永远不显示 —— 界面读的是
    `extra.sync_statuses[<面板名>]`，与这里写入的位置必须一致。

    `upload_cpa` 对 ChatGPT 与 Grok 都有效（同一个 CLIProxyAPI 端点）；
    sub2api / chatgpt2api 当前只在对应平台声明。查表按 action_id 即可，
    平台差异由「该平台是否声明并实现了这个动作」决定（不声明就调不到）。
    """
    target = _UPLOAD_SYNC_WRITERS.get(action_id)
    if target is None:
        return
    module_name, func_name = target
    import importlib

    writer = getattr(importlib.import_module(module_name), func_name)
    sync_msg = result.get("data") or result.get("error") or ""
    writer(
        acc_model,
        bool(result.get("ok")),
        str(sync_msg),
        session=session,
        commit=False,
    )


def _to_platform_account(acc_model: AccountModel):
    from core.base_platform import Account, AccountStatus

    # `Account` 本身没有 id 字段（那是仓储行的概念），但有些动作要按 id 记账
    # （如 Grok 的 CPA 状态同步要拿它当结果字典的键）。放进 extra 传给平台侧。
    extra = acc_model.get_extra()
    if acc_model.id is not None:
        extra.setdefault("account_id", acc_model.id)
    return Account(
        platform=acc_model.platform,
        email=acc_model.email,
        password=acc_model.password,
        user_id=acc_model.user_id,
        token=acc_model.token,
        # 用 coerce 而非 AccountStatus(...)：库里可能存在枚举外的历史状态值
        # （如 chatgpt 侧的 active/banned），直接构造会抛 ValueError 打断整个操作
        status=AccountStatus.coerce(acc_model.status),
        extra=extra,
    )


def _apply_action_result(
    platform: str,
    action_id: str,
    acc_model: AccountModel,
    result: dict[str, Any],
    session: Session,
) -> None:
    if platform == "chatgpt":
        raw_data = result.get("data")
        data: dict = raw_data if isinstance(raw_data, dict) else {}
        status_reason = ""
        if action_id == "probe_local_status":
            status_reason = apply_chatgpt_status_policy(acc_model, local_probe=data.get("probe"))
        elif action_id == "sync_cliproxyapi_status":
            status_reason = apply_chatgpt_status_policy(acc_model, remote_sync=data.get("sync"))
        elif action_id == "refresh_token":
            # 刷新链自己认过封禁措辞，这里只把结论交给共享策略落状态
            status_reason = apply_chatgpt_status_policy(acc_model, banned=bool(data.get("banned")))
        if status_reason:
            from datetime import datetime, timezone

            acc_model.updated_at = datetime.now(timezone.utc)
            session.add(acc_model)
    if isinstance(result.get("account_extra_patch"), dict):
        patch = result["account_extra_patch"]
        extra = acc_model.get_extra()
        _merge_extra_patch(extra, patch)
        acc_model.set_extra(extra)
        # token 列 = 平台主凭证的镜像（chatgpt → AT，grok → SSO）。
        # 整理前这里无条件写 access_token —— grok 的刷新补丁里带 AT，把 SSO
        # 镜像盖成了 AT（线上 32 行里 12 行如此，读侧 `or account.token` 的
        # 兜底会拿错值）。改为按平台取镜像字段；没有对应值时不碰列。
        from core.credential_fields import sync_token_column

        sync_token_column(acc_model, platform, extra)
        from datetime import datetime, timezone
        acc_model.updated_at = datetime.now(timezone.utc)
        session.add(acc_model)
    _record_upload_sync(platform, action_id, acc_model, result, session)
    if result.get("ok") and result.get("data", {}) and isinstance(result["data"], dict):
        data = result["data"]
        # 凭证字段统一走注册表（`core/credential_fields.py`）：只收凭证类字段、
        # 归一到规范名（camelCase 落库的也认），其余展示字段（message/status/
        # strategy 等）不落。整理前这里是手写集合，混着 camelCase 与
        # `clientId`/`clientSecret`/`webAccessToken` 三个零生产方的死键。
        from core.credential_fields import canonical_writes, sync_token_column

        writes = canonical_writes(data)
        if writes:
            extra = acc_model.get_extra()
            extra.update(writes)
            acc_model.set_extra(extra)
            # token 列 = 平台主凭证的镜像（chatgpt → AT，grok → SSO）
            sync_token_column(acc_model, platform, writes)
            from datetime import datetime, timezone

            acc_model.updated_at = datetime.now(timezone.utc)
            session.add(acc_model)


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


def _resolve_batch_accounts(platform: str, body: BatchActionRequest, session: Session) -> tuple[list[AccountModel], list[int]]:
    if body.account_ids:
        account_ids = []
        seen = set()
        for raw in body.account_ids:
            value = int(raw)
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            account_ids.append(value)

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
        update = updates.get(int(acc_model.id or 0), {})
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
            session.add(acc_model)
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


def _result_message(result: dict[str, Any]) -> str:
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("message", "detail", "url", "checkout_url", "cashier_url"):
            value = str(data.get(key) or "").strip()
            if value:
                return value
        # data 里没有可展示的文案时，退回 `error` 而不是把 data 序列化成 JSON。
        # 实测踩过：refresh_token 失败时 data 只有 {"banned": false}，于是批量界面
        # 上那一行显示成 `{"banned": false}` —— 而人话原因就写在 error 里。
        error = str(result.get("error") or "").strip()
        if error:
            return error
        return json.dumps(data, ensure_ascii=False)
    if str(data or "").strip():
        return str(data)
    return str(result.get("error") or "").strip()


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
    """获取平台支持的操作列表"""
    PlatformCls = _get_platform_cls_or_404(platform)
    instance = PlatformCls(config=RegisterConfig(extra=config_store.get_all()))
    return {"actions": instance.get_platform_actions()}


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

    items = []
    success_count = 0
    failed_count = 0

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
    account_id: int,
    action_id: str,
    body: ActionRequest,
    session: Session = Depends(platform_session_from_path),
):
    """执行平台特定操作"""
    acc_model = session.get(AccountModel, account_id)
    if not acc_model or acc_model.platform != platform:
        raise HTTPException(404, "账号不存在")

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
