"""面板管理接口。

面板**全部是远程服务**：本应用只存它们的地址、提供跳转入口，以及把账号推过去。
历史上这里还有一套「本地插件」接口（安装 / 启动 / 停止 / 卸载 CLIProxyAPI 源码），
已按用户要求整块删除 —— 面板管理页不再管本机进程。

保留的是 `/backfill`：把已注册的账号补传到远端面板。它是纯 HTTP 调用，
与本地插件无关。
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from core.config_store import config_store
from core.db import AccountModel, account_repository, platform_database_registry
from services.chatgpt_account_state import filter_accounts_by_plus_status
from services.chatgpt_sync import backfill_chatgpt_account_to_cpa, get_cliproxy_sync_state
from services.panel_comparison import FETCHERS
from services.panel_comparison_cache import get_panel_comparison
from services.panel_registry import list_panels

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])


class BackfillRequest(BaseModel):
    platforms: list[str] = Field(default_factory=lambda: ["chatgpt"])
    account_ids: list[int] = Field(default_factory=list)
    pending_only: bool = False
    status: Optional[str] = None
    email: Optional[str] = None
    plus_status: Optional[str] = None


@router.get("/panels")
def get_panels():
    """面板清单 + 各自的当前地址。

    地址从 configs 表现读（面板管理页与「全局配置 → 面板配置」写的是同一份键），
    所以这里返回的是用户真实配置，而不是默认值。

    一次 `get_all()` 取全量：`config_store.get()` 每次调用都要重读并解析
    `.env`、再开一个 DB 会话，逐个面板取会把 6 个面板放大成 7 次读盘 +
    7 个会话 —— 而这些键本来就在同一张表里。
    """
    all_cfg = config_store.get_all()
    items = []
    for panel in list_panels():
        url = str(all_cfg.get(panel["url_key"], "") or "").strip()
        secret_key = panel.get("secret_key")
        items.append({
            **panel,
            "url": url,
            "configured": bool(url),
            "secret_set": bool(secret_key and str(all_cfg.get(secret_key, "") or "").strip()),
        })
    return {"items": items}


@router.get("/panels/{panel_key}/comparison")
def get_panel_comparison_endpoint(panel_key: str, refresh: bool = False):
    """本地账号 ↔ 远端面板的对比。

    `refresh=true` 绕过缓存重拉（对应界面上的「同步到最新」）；默认走缓存，
    避免每次渲染都打远端接口。响应里的 `fetched_at` 是这次数据的拉取时间。
    旧 key（`cliproxyapi`）由 `get_panel_comparison` 内部归一，不 404。
    """
    try:
        return get_panel_comparison(panel_key, refresh=refresh)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/panels/{panel_key}/sync")
def sync_panel_endpoint(panel_key: str, platform: str = ""):
    """把远端较新的凭证拉回本地（「同步到最新」的动作面）。

    与「重新拉取对比」的区别：那个只重拉**对比表**，这个会**改本地账号**
    （覆盖 AT/RT/id_token —— 远端刷新过 token 之后本地存的就是死值）。

    方向规则（见 `services/panel_sync.py`）：只有「远端较新」的账号会被拉回；
    本地较新/同小时/远端没有凭证的都不动。返回逐账号的原因，界面展示汇总。

    `platform`（`chatgpt` / `grok`）给多平台面板用（CPA）：界面上的平台筛选
    只作用在前端，用户筛了 Grok 再点同步时，这里不按平台过滤就会把 ChatGPT
    的凭证也一起拉回 ——「看到的」与「被改的」对不上。空串 = 全部（老行为）。
    """
    from services.panel_comparison_cache import fetch_panel_raw
    from services.panel_registry import resolve_panel_key
    from services.panel_sync import sync_local_from_remote

    key = resolve_panel_key(panel_key)
    if key not in FETCHERS:
        raise HTTPException(404, f"未知面板: {panel_key}")

    local_rows, remote_accounts, remote_error = fetch_panel_raw(key)
    if remote_error:
        return {
            "panel": key,
            "total": 0,
            "pulled": 0,
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
    summary = sync_local_from_remote(local_rows, remote_accounts)
    summary["panel"] = key
    summary["remote_error"] = ""
    return summary


#: 面板 key → 推送器类型。
#:
#: `overwrite`：上传即原地更新（同名/同邮箱记录被覆盖），重推不留重复 ——
#:   CPA（同名 auth 文件覆写）、grok2api（SSO 按邮箱 upsert）。
#: `append`：上传即新增一条记录，重推会产生重复 —— sub2api / chatgpt2api，
#:   推成功后用 `delete_remote` 删掉旧记录（`delete_old=true` 时）。
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
        ok, msg = client.ingest_sso(sso, email)
        if ok:
            from services.chatgpt_sync import record_grok2api_sync_result

            record_grok2api_sync_result(extra, ok, msg)
        return ok, msg

    if panel_key == "cpa":
        from core.base_platform import Account, AccountStatus
        from services.chatgpt_sync import upload_proxy_for

        if plat == "grok":
            from platforms.grok.oauth_device import token_to_cpa_record
            from platforms.grok.upload import upload_to_cpa

            record = extra.get("cpa_record")
            if not isinstance(record, dict) or not record:
                access = str(extra.get("access_token") or "").strip()
                if not access:
                    return False, "账号没有 CPA 记录（也没有 access_token 可重建）"
                record = token_to_cpa_record(
                    {"access_token": access,
                     "refresh_token": str(extra.get("refresh_token") or ""),
                     "id_token": str(extra.get("id_token") or "")},
                    email=email,
                    sso=str(extra.get("sso") or ""),
                )
            return upload_to_cpa(record, proxy=upload_proxy_for("cpa", extra))

        # ChatGPT：走 generate_token_json（与手动动作 / 自动上传同一条路径）
        from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

        account = Account(
            platform=plat or "chatgpt", email=email, password="",
            token=str(extra.get("access_token") or ""),
            status=AccountStatus.REGISTERED, extra=extra,
        )
        return upload_to_cpa(
            generate_token_json(account), proxy=upload_proxy_for("cpa", extra)
        )

    if panel_key == "sub2api":
        from core.base_platform import Account, AccountStatus
        from platforms.chatgpt.sub2api_upload import upload_to_sub2api

        account = Account(
            platform=plat or "chatgpt", email=email, password="",
            token=str(extra.get("access_token") or ""),
            status=AccountStatus.REGISTERED, extra=extra,
        )
        return upload_to_sub2api(account)

    if panel_key == "chatgpt2api":
        from core.base_platform import Account, AccountStatus
        from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api
        from services.chatgpt_sync import upload_proxy_for

        account = Account(
            platform=plat or "chatgpt", email=email, password="",
            token=str(extra.get("access_token") or ""),
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
                session.add(row)
                session.commit()
        except Exception as exc:  # noqa: BLE001 - 落库失败不该毁整批结果
            logger.warning("push 结果落库失败 %s/%s: %s", panel_key, email, exc)


@router.post("/panels/{panel_key}/push")
def push_panel_endpoint(
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
        raise HTTPException(404, f"未知面板: {panel_key}")

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


@router.post("/backfill")
def backfill_integrations(body: BackfillRequest):
    summary = {"total": 0, "success": 0, "failed": 0, "skipped": 0, "items": []}
    targets = set(body.platforms or [])

    # 分库后账号散落在各平台库：按平台分组，各用自己的库会话处理。
    # 直接用默认库会漏掉所有分库平台的账号（静默少干活）。
    if body.account_ids:
        rows = account_repository.get_many(body.account_ids)
        if targets:
            rows = [r for r in rows if r.platform in targets]
    elif targets:
        rows = []
        for platform in sorted(targets):
            rows.extend(account_repository.list_accounts(
                platform, status=body.status or "",
                email_contains=body.email or "",
            ))
    else:
        return summary

    if body.status and body.account_ids:
        rows = [r for r in rows if r.status == body.status]
    if body.email and body.account_ids:
        needle = body.email.strip().lower()
        rows = [r for r in rows if needle in (r.email or "").lower()]
    if body.plus_status:
        rows = list(filter_accounts_by_plus_status(rows, body.plus_status))
    if body.pending_only:
        rows = [
            row for row in rows
            if row.platform != "chatgpt"
            or str(get_cliproxy_sync_state(row).get("remote_state") or "").strip().lower() == "not_found"
        ]

    # 按平台分组：每组在自己的库会话里回填（写回也进正确的库）
    by_platform: dict[str, list] = {}
    for row in rows:
        by_platform.setdefault(str(row.platform or "").lower(), []).append(row)

    for platform_key, platform_rows in by_platform.items():
        with platform_database_registry.session_for(platform_key) as s:
            for row in platform_rows:
                item = {"platform": row.platform, "email": row.email, "results": []}
                try:
                    results = []
                    if row.platform == "chatgpt":
                        outcome = backfill_chatgpt_account_to_cpa(row, session=s, commit=True)
                        ok = bool(outcome.get("ok"))
                        skipped = bool(outcome.get("skipped"))
                        results.extend(outcome.get("results") or [])
                        if not results:
                            results.append({"name": "CLIProxyAPI", "ok": ok, "msg": outcome.get("message", "")})
                        if skipped:
                            summary["skipped"] += 1
                        elif ok:
                            summary["success"] += 1
                        else:
                            summary["failed"] += 1

                    if not results:
                        item["results"].append({"name": "skip", "ok": False, "msg": "未配置对应导入目标"})
                        summary["failed"] += 1
                    else:
                        item["results"] = results
                except Exception as e:
                    s.rollback()
                    item["results"].append({"name": "error", "ok": False, "msg": str(e)})
                    summary["failed"] += 1
                summary["items"].append(item)
                summary["total"] += 1

    return summary
