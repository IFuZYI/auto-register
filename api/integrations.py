"""面板管理接口。

面板**全部是远程服务**：本应用只存它们的地址、提供跳转入口，以及把账号推过去。
历史上这里还有一套「本地插件」接口（安装 / 启动 / 停止 / 卸载 CLIProxyAPI 源码），
已按用户要求整块删除 —— 面板管理页不再管本机进程。

保留的是 `/backfill`：把已注册的账号补传到远端面板。它是纯 HTTP 调用，
与本地插件无关。
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.config_store import config_store
from core.db import account_repository, platform_database_registry
from services.chatgpt_account_state import filter_accounts_by_plus_status
from services.chatgpt_sync import backfill_chatgpt_account_to_cpa, get_cliproxy_sync_state
from services.panel_comparison import FETCHERS
from services.panel_comparison_cache import get_panel_comparison
from services.panel_registry import list_panels

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
