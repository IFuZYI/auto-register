"""动作执行的核心逻辑（从 `api/actions.py` 搬下来的业务层）。

为什么搬下来：`services/task_runners.py`（批量任务）与
`services/chatgpt_maintenance.py`（自动维护）都要「执行一个动作并把结果落库」，
而这段逻辑原来住在 API 层 —— 服务层只能反向 `from api.actions import ...`，
依赖方向反了（M1 指标里 5 处真违规）。

搬下来之后方向恢复成 api → services，服务层不再认识 HTTP 层。

`api/actions.py` 保留 `_` 前缀的同名别名（`_apply_action_result` 等），
历史调用点与测试继续按老名字引用，行为一字未改。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session

from core.base_platform import AccountStatus
from core.capabilities import Capability, capability_of
from core.db import AccountModel, account_repository
from services.chatgpt_account_state import apply_chatgpt_status_policy
from services.chatgpt_sync import update_account_model_cliproxy_sync

__all__ = [
    "UPLOAD_SYNC_WRITERS",
    "UPLOAD_ACTIONS",
    "apply_action_result",
    "is_banned_status",
    "merge_extra_patch",
    "record_upload_sync",
    "result_message",
    "to_platform_account",
]


def merge_extra_patch(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            merge_extra_patch(base[key], value)
        else:
            base[key] = value
    return base


UPLOAD_ACTIONS: frozenset[str] = frozenset({
    "upload_cpa",
    "upload_sub2api",
    "upload_chatgpt2api",
    "upload_grok2api",
})


def record_upload_sync(
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
    target = UPLOAD_SYNC_WRITERS.get(action_id)
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


def is_banned_status(status: Any) -> bool:
    """账号是否处于「禁用」状态（大小写/空白容忍）。"""
    return str(getattr(status, "value", status) or "").strip().lower() == "banned"


def to_platform_account(acc_model: AccountModel):
    """ORM 行 → 领域对象。转换只有 `AccountModel.to_record()` 一处实现。"""
    from core.base_platform import AccountStatus

    record = acc_model.to_record()
    # 有些动作要按 id 记账（如 Grok 的 CPA 状态同步拿它当结果字典的键）。
    # 放进 extra 传给平台侧，平台代码不必知道仓储的存在。
    if record.id:
        record.extra.setdefault("account_id", record.id)
    # 用 coerce 而非 AccountStatus(...)：库里可能存在枚举外的历史状态值
    # （如 chatgpt 侧的 active/banned），直接构造会抛 ValueError 打断整个操作
    record.status = AccountStatus.coerce(record.status)
    return record


def apply_action_result(
    platform: str,
    action_id: str,
    acc_model: AccountModel,
    result: dict[str, Any],
    session: Session,
) -> None:
    """把动作结果落库：状态 → extra 补丁 → 上传账 → 凭证字段。

    四步各一个函数，平台差异只体现在 `_STATUS_APPLIERS` 这张表上。
    以前这里是 120 行的单体：两个平台的状态策略、extra 补丁、上传记账、
    凭证写入全糊在一起，加一个平台就要再嵌一层 `elif`。
    """
    applier = _STATUS_APPLIERS.get(str(platform or "").strip().lower())
    if applier is not None:
        applier(acc_model, action_id, result, session)
    if isinstance(result.get("account_extra_patch"), dict):
        _apply_extra_patch(acc_model, platform, result, session)
    record_upload_sync(platform, action_id, acc_model, result, session)
    _apply_credential_writes(acc_model, platform, result, session)


def _apply_chatgpt_status(acc_model, action_id, result, session) -> None:
    raw_data = result.get("data")
    data: dict = raw_data if isinstance(raw_data, dict) else {}
    status_reason = ""
    # 按**能力**分派，不再逐个比较动作 id：`check_plus_trial` 与
    # `probe_local_status` 同属 PROBE，`sync_sub2api_status` /
    # `sync_chatgpt2api_status` 与 `sync_cliproxyapi_status` 同属 SYNC_STATUS。
    # 它们的结果里没有 `probe` / `sync` 键 → 策略函数收到 None → 返回空串
    # → 不落库，与改造前逐 id 分派完全一致（`tests/test_capability_dispatch.py`
    # 钉住了这一点，包括「禁用不被无信号的同步洗掉」）。
    capability = capability_of(action_id)
    if capability is Capability.PROBE:
        status_reason = apply_chatgpt_status_policy(acc_model, local_probe=data.get("probe"))
    elif capability is Capability.SYNC_STATUS:
        status_reason = apply_chatgpt_status_policy(acc_model, remote_sync=data.get("sync"))
    elif capability is Capability.REFRESH:
        # 刷新链自己认过封禁措辞，这里只把结论交给共享策略落状态。
        # 刷新成功（ok）是正向信号 → usable=True 让过期/失效恢复「正常」。
        status_reason = apply_chatgpt_status_policy(
            acc_model,
            banned=bool(data.get("banned")),
            usable=bool(result.get("ok")),
            # 失败分类：`permanent`（invalid_grant 等）落「失效」并停止重试；
            # `configuration`（invalid_client）**不动状态** —— 那是我们的
            # 配错，账号可能是好的。
            refresh_kind=str(data.get("refresh_kind") or ""),
        )
    elif capability_of(action_id) in (Capability.BACKFILL, Capability.BIND_2FA):
        # 补 RT / 绑 2FA 的登录链同样会发掘「号没了」（用户要求：禁用
        # 靠登录流程发掘）—— 结论经 data.banned 传上来，这里落状态。
        status_reason = apply_chatgpt_status_policy(
            acc_model,
            banned=bool(data.get("banned")),
        )
    if status_reason:
        from datetime import datetime, timezone

        acc_model.updated_at = datetime.now(timezone.utc)
        account_repository.save(acc_model, session=session)


def _apply_grok_status(acc_model, action_id, result, session) -> None:
    # grok 的探测/刷新动作同样要落状态（对齐 grok2api 的失效判定）——
    # 此前只有 chatgpt 有状态策略，grok 测活失败不会改状态。
    from services.grok_account_state import apply_grok_status_policy

    raw_data = result.get("data")
    data: dict = raw_data if isinstance(raw_data, dict) else {}
    status_reason = ""
    if action_id == "probe":
        code = data.get("status")
        # 只认真实的探测码。**不**读 `sso_status` 当失效判据：
        # `_sso_status` 那套判定已知不可靠（响应没有 url、i18n 文案表里带
        # sign-in 字样、全新会话会把有效 SSO 判成未登录），用它落「失效」
        # 会误杀好号（评审实测）。SSO 是否还有效，用「刷新 Token（登录协议）」
        # 的真实结果判 —— 那条走 `probe_refresh` / `refresh_token` 分支。
        status_reason = apply_grok_status_policy(
            acc_model,
            probe_code=int(code) if code is not None else None,
            probe_summary=str(data.get("summary") or ""),
        )
    elif action_id == "probe_refresh":
        status_reason = apply_grok_status_policy(
            acc_model,
            refresh_kind=str(data.get("kind") or ""),
            refresh_error=str(data.get("error_code") or result.get("error") or ""),
            usable=bool(result.get("ok")),
        )
    elif action_id == "sync_cliproxyapi_status":
        sync_raw = data.get("sync")
        sync = sync_raw if isinstance(sync_raw, dict) else {}
        status_reason = apply_grok_status_policy(
            acc_model,
            remote_state=str(sync.get("remote_state") or ""),
        )
    elif action_id == "refresh_token":
        status_reason = apply_grok_status_policy(
            acc_model,
            refresh_kind=str(data.get("kind") or ""),
            refresh_error=str(result.get("error") or ""),
            usable=bool(result.get("ok")),
        )
    if status_reason:
        from datetime import datetime, timezone

        acc_model.updated_at = datetime.now(timezone.utc)
        account_repository.save(acc_model, session=session)


def _apply_extra_patch(acc_model, platform, result, session) -> None:
    patch = result["account_extra_patch"]
    extra = acc_model.get_extra()
    merge_extra_patch(extra, patch)
    acc_model.set_extra(extra)
    # token 列 = 平台主凭证的镜像（chatgpt → AT，grok → SSO）。
    # 整理前这里无条件写 access_token —— grok 的刷新补丁里带 AT，把 SSO
    # 镜像盖成了 AT（线上 32 行里 12 行如此，读侧 `or account.token` 的
    # 兜底会拿错值）。改为按平台取镜像字段；没有对应值时不碰列。
    from core.credential_fields import sync_token_column

    sync_token_column(acc_model, platform, extra)
    from datetime import datetime, timezone
    acc_model.updated_at = datetime.now(timezone.utc)
    account_repository.save(acc_model, session=session)


def _apply_credential_writes(acc_model, platform, result, session) -> None:
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
            # **换了 AT 就把「AT 什么时候签发的」记准**（用户问题 2026-10-30）。
            #
            # 这条路径（`_apply_credential_writes`）是所有「动作写回凭证」的
            # 必经之处：刷新、补 RT、面板同步拉回远端 AT 都从这里过。
            # 原来 `chatgpt_token_refresh.at` 只在**刷新动作**里写 —— 别的路径
            # 更新了 AT 却不更新记录，于是记录说 10-24、AT 其实是 10-29 签的，
            # 差 4~5 天（实测 45 个账号），看起来像「AT 没换」。
            #
            # 修法：只要写进来的 AT 与库里那个**不同**，就把 `at_iat` / `at_exp`
            # 与 `at` 一起对齐到 AT 自身 —— 记录与凭证同源，不再两套时间。
            if platform == "chatgpt":
                _restamp_at_provenance(extra, writes)
            acc_model.set_extra(extra)
            # token 列 = 平台主凭证的镜像（chatgpt → AT，grok → SSO）
            sync_token_column(acc_model, platform, writes)
            from datetime import datetime, timezone

            acc_model.updated_at = datetime.now(timezone.utc)
            account_repository.save(acc_model, session=session)


def _restamp_at_provenance(extra: dict, writes: dict) -> None:
    """AT 换了 → 把 `chatgpt_token_refresh` 里的时间对齐到 AT 自身的 iat。

    只动「时间/来源」这几个字段，**不改** `ok` / `verified` / `refreshed`
    / `message` —— 那些是「上一次刷新动作」的结论，别的路径（面板同步）
    不该冒充它。这里只回答一个问题：**这个 AT 是什么时候签发的**。

    写入形态：
        at          = AT 的签发时刻（ISO）；解不出 iat 才退回当前时刻
        at_iat      = AT 的 iat（epoch 秒）
        at_exp      = AT 的 exp（epoch 秒）
        issued_at   = 同 at（历史字段，与列表接口 `_at_claims_for_list` 一致）
        at_source   = 这条记录是「由凭证写入对齐」的，不是刷新动作写的
    """
    new_at = str(writes.get("access_token") or "").strip()
    if not new_at:
        return
    from datetime import datetime, timezone

    from services.chatgpt_token_lifecycle import decode_jwt_claims

    claims = decode_jwt_claims(new_at) or {}
    iat = int(claims.get("iat") or 0)
    exp = int(claims.get("exp") or 0)
    if iat <= 0:
        # 解不出 iat（非 JWT 或被改写过的值）：只记「刚写入」，不冒充签发时间
        return

    stamp = extra.get("chatgpt_token_refresh")
    if not isinstance(stamp, dict):
        stamp = {}
    else:
        stamp = dict(stamp)
    stamp["at"] = datetime.fromtimestamp(iat, timezone.utc).isoformat()
    stamp["at_iat"] = iat
    stamp["at_exp"] = exp
    stamp["issued_at"] = stamp["at"]
    stamp["at_source"] = "credential_write"
    extra["chatgpt_token_refresh"] = stamp


#: 平台 → 状态策略。加一个平台只加一行，不再往 `apply_action_result` 里嵌 `elif`。
_STATUS_APPLIERS = {
    "chatgpt": _apply_chatgpt_status,
    "grok": _apply_grok_status,
}



def result_message(result: dict[str, Any]) -> str:
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


UPLOAD_SYNC_WRITERS: dict[str, tuple[str, str]] = {
    # action_id: (模块, 函数名)
    "upload_cpa": ("services.chatgpt_sync", "update_account_model_cpa_sync"),
    "upload_sub2api": ("services.chatgpt_sync", "update_account_model_sub2api_sync"),
    "upload_chatgpt2api": ("services.chatgpt_sync", "update_account_model_chatgpt2api_sync"),
}
