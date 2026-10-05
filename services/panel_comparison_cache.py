"""面板对比的缓存 + 接口编排。

远端接口是外网调用（CPA 还要逐账号探活），面板管理页每次渲染都打一遍既慢又
容易被限流，所以结果带缓存。缓存放内存 —— 这是**页面级**缓存，进程重启后
重拉一次是可接受的代价；落库反而要处理"库里存的是不是过期数据"。

用户要求"有同步到最新功能"：`refresh=True` 绕过缓存重拉。
"可以加个同步时间显示"：响应里带 `fetched_at`（这次数据是何时拉的）。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from services.panel_comparison import (
    FETCHERS,
    STATE_LABELS,
    build_comparison,
    summarize,
)
from services.panel_registry import resolve_panel_key

logger = logging.getLogger(__name__)

#: 缓存有效期（秒）。60 秒是「连点两次刷新不会重复打远端、但也不会看到过期
#: 太久的数据」的折中；用户主动点"同步到最新"会绕过它。
CACHE_TTL_SECONDS = 60
#: 远端拉取失败时的缓存有效期（秒）。失败**不能**按 60 秒缓存 —— 一次代理抖动
#: 或面板重启会把「远端读取失败」钉在界面上整整一分钟，而「刷新」按钮走的是
#: 缓存路径（清不掉），用户只能点更重的「同步到最新」。短 TTL 让下一次刷新
#: 自动重试，又不至于把失败路径变成对远端的连续打击。
FAILURE_CACHE_TTL_SECONDS = 5

_lock = threading.Lock()
#: panel_key → {"payload": {...}, "monotonic": float}
_cache: dict[str, dict[str, Any]] = {}
#: 每个面板一把「正在重建」的锁：缓存未命中时只有一个请求真正去拉远端，
#: 其余请求等它拉完直接吃结果。
#:
#: 不这么做的话（原来的写法）：锁在重建前就放掉了，N 个并发请求会各跑一遍
#: 本地扫描 + 远端抓取（grok2api 实测 300–550ms），最后互相覆盖缓存 ——
#: 面板管理页开两个标签、或连点两次「刷新」就能触发。每个请求还会占住一个
#: FastAPI 线程池线程整整一个远端往返的时间。
_rebuild_locks: dict[str, threading.Lock] = {}
_rebuild_locks_guard = threading.Lock()


def _rebuild_lock(panel_key: str) -> threading.Lock:
    with _rebuild_locks_guard:
        lock = _rebuild_locks.get(panel_key)
        if lock is None:
            lock = threading.Lock()
            _rebuild_locks[panel_key] = lock
        return lock


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clear_cache(panel_key: str = "") -> None:
    """清缓存（测试与手动刷新用）。传空串清全部。"""
    with _lock:
        if panel_key:
            _cache.pop(str(panel_key).strip().lower(), None)
        else:
            _cache.clear()


def _cache_age(entry: dict[str, Any]) -> float:
    return time.monotonic() - float(entry.get("monotonic") or 0.0)


def _entry_is_fresh(entry: dict[str, Any]) -> bool:
    """缓存条目还算不算新鲜。

    失败的条目按更短的 TTL 判（见 `FAILURE_CACHE_TTL_SECONDS`）——
    命中失败缓存时不重试远端，等于把一次抖动放大成整整一分钟的"读取失败"。
    """
    ttl = FAILURE_CACHE_TTL_SECONDS if entry.get("failed") else CACHE_TTL_SECONDS
    return _cache_age(entry) < ttl


def _local_accounts_for_panel(panel_key: str) -> list[dict[str, Any]]:
    """这个面板对应的本地账号。

    - cpa / sub2api：ChatGPT 账号（它们都是 ChatGPT 账号的推送目标）
    - grok2api：Grok 账号
    面板与平台的对应关系写在 `_PANEL_PLATFORMS` 里 —— 加新面板时只改那一处。

    **`token` 列要并进 extra 再交出去**：账号表的 `token` 列是历史遗留的凭证位
    （前端编辑弹窗的「Token / Access Token」字段、`POST /api/accounts` 都写它），
    而较新的落库路径把主凭证写进 `extra`。对比只看 extra 的话，凭证在列上的
    账号会被当成「本地没有」—— 与远端一比对得出「两边都没有」→
    `unknown_credential`，或者更糟：本地有 RT 而远端 AT 不同时判成 `synced`
    （实测复现）。这里是两边凭证汇合的唯一入口，统一在这里补齐。

    镜像规则见注册表（`core/credential_fields.py`）：token 列 = 平台主凭证
    （chatgpt → AT，grok → SSO）；grok 列上是 OAuth 形态的 JWT（被 AT 盖过的
    脏值）时不认 —— 那是启动迁移要修的行，读侧兜底也要挡住。
    """
    from core.db import account_repository
    from core.credential_fields import token_column_credential, token_column_field

    platforms = _PANEL_PLATFORMS.get(panel_key, ())
    rows: list[dict[str, Any]] = []
    for platform in platforms:
        for row in account_repository.list_accounts(platform):
            extra = row.get_extra()
            # 列上的值只在 extra 里没有对应键时补 —— extra 是更新的来源，
            # 两处都有值时以 extra 为准（避免列上的旧值盖掉刷新后的新值）。
            # 镜像规则见注册表：token 列 = 平台主凭证（chatgpt → AT，
            # grok → SSO）；grok 列上是 OAuth 形态的 JWT（被 AT 盖过的脏值）
            # 时不认 —— 那正是迁移要修的行，读侧兜底也要挡住。
            mirror = token_column_field(platform)
            if mirror:
                legacy = token_column_credential(row, platform, mirror)
                if legacy:
                    extra.setdefault(mirror, legacy)
            rows.append(
                {
                    "id": row.id,
                    "email": row.email,
                    # 平台要带上：CPA 同时托管 ChatGPT 与 Grok，同一个邮箱
                    # 可能两边都有账号，匹配键是 (平台, 邮箱)。
                    "platform": platform,
                    "status": row.status,
                    "updated_at": row.updated_at,
                    "extra": extra,
                }
            )
    return rows


#: 面板 key → 本地对应平台
_PANEL_PLATFORMS: dict[str, tuple[str, ...]] = {
    # CPA（CLIProxyAPI）同时托管 ChatGPT（codex）与 Grok（xai）两类凭据 ——
    # 两个平台的账号都在这个面板的对比里出现，行上带 `platform` 区分。
    "cpa": ("chatgpt", "grok"),
    "sub2api": ("chatgpt",),
    "grok2api": ("grok",),
    "chatgpt2api": ("chatgpt",),
}


def _panel_credentials(panel_key: str) -> dict[str, str]:
    """从配置里取这个面板的连接信息（与面板配置页写的是同一份键）。

    只读规范键：`config_store.get_all()` 会把重复服务的别名（`cliproxyapi_*`）
    折叠进规范键（`cpa_*`，见 `_collapse_duplicate_service_keys`），所以这里
    再写一遍 `canonical or alias` 是多余的 —— 而且会在折叠规则变化时静默走偏。
    """
    from core.config_store import config_store

    all_cfg = config_store.get_all()

    def _get(key: str) -> str:
        return str(all_cfg.get(key, "") or "").strip()

    if panel_key == "cpa":
        return {"api_url": _get("cpa_api_url"), "api_key": _get("cpa_api_key")}
    if panel_key == "sub2api":
        return {"api_url": _get("sub2api_api_url"), "api_key": _get("sub2api_api_key")}
    if panel_key == "grok2api":
        # grok2api 用账号密码登录，凭据从它的两个配置键来
        return {"api_url": _get("grok2api_base_url"), "api_key": _get("grok2api_password")}
    if panel_key == "chatgpt2api":
        return {
            "api_url": _get("chatgpt2api_api_url"),
            "api_key": _get("chatgpt2api_api_key"),
        }
    return {}


def _call_fetcher(key: str, credentials: dict[str, str], local_emails: set[str]) -> list:
    """调 fetcher，按签名决定要不要传 `emails`。

    先看签名而不是 `try/except TypeError` —— 后者会把 fetcher **内部**抛出的
    TypeError 一起吞掉（真 bug 会被误当成"这个 fetcher 不收 emails"）。
    """
    import inspect

    fetcher = FETCHERS[key]
    try:
        accepts = "emails" in inspect.signature(fetcher).parameters
    except (TypeError, ValueError):
        accepts = False
    if accepts:
        return fetcher(**credentials, emails=local_emails)
    return fetcher(**credentials)


def get_panel_comparison(
    panel_key: str,
    *,
    refresh: bool = False,
) -> dict[str, Any]:
    """拉一次面板对比（默认走缓存）。

    返回形状：
        {
          "panel": "cpa",
          "fetched_at": "2026-03-31T12:00:00+00:00",   # 这次数据的拉取时间
          "cached": true,                               # 是否命中缓存
          "summary": {...},                             # 各状态计数
          "rows": [...],                                # 逐邮箱对比
          "remote_error": "",                           # 远端拉不到时的原因
          "local_count": 12,
          "remote_count": 10,
        }

    远端拉不到**不是异常**：本地账号照常返回，`remote_error` 说明原因，
    界面上还能看到"本地有哪些号"。这比整页报错有用得多 —— 面板没配或没起
    的时候，用户仍然想看到本地清单。
    """
    # 旧 key 归一（`cliproxyapi` → `cpa`）。放在这里而不是只放 API 层：
    # 直接调这个函数的调用方（脚本、测试、以后的批处理）也走同一条规则，
    # 与 `panel_registry` 文档承诺的「老 key 不 404」一致。
    key = resolve_panel_key(panel_key)
    if key not in FETCHERS:
        raise ValueError(f"未知面板: {panel_key}")

    if not refresh:
        with _lock:
            entry = _cache.get(key)
            if entry and _entry_is_fresh(entry):
                payload = dict(entry["payload"])
                payload["cached"] = True
                return payload

    # 缓存未命中：同一个面板只让一个请求去重建，其余等它。
    # 重建期间再进来的请求会拿到刚写好的新鲜结果（下面的 `cached=True` 分支
    # 覆盖不到 —— 它们是等锁，不是命中旧缓存）。
    with _rebuild_lock(key):
        # 等锁期间可能已经有别的请求重建完了，先看一眼再干活
        if not refresh:
            with _lock:
                entry = _cache.get(key)
                if entry and _entry_is_fresh(entry):
                    payload = dict(entry["payload"])
                    payload["cached"] = True
                    return payload

        return _rebuild_panel_comparison(key)


def fetch_panel_raw(panel_key: str) -> tuple[list[dict[str, Any]], list, str]:
    """拉一次面板的**原始数据**：`(local_rows, remote_accounts, remote_error)`。

    对比（`build_comparison`）与同步（`panel_sync.sync_local_from_remote`）共用
    这条拉取路径 —— 各写一份的话，两边看到的「远端较新」可能来自不同的快照，
    同步动作会把对比页没显示的东西改掉。
    """
    local_rows = _local_accounts_for_panel(panel_key)
    credentials = _panel_credentials(panel_key)
    remote_accounts: list = []
    remote_error = ""

    if not credentials.get("api_url"):
        remote_error = "面板地址未配置，无法读取远端账号"
        return local_rows, remote_accounts, remote_error

    # 本地邮箱集合传给 fetcher：只有"两边都有"的账号才需要抓完整凭证
    # （CPA 逐个 download 有成本；只为不参与比对的行付费没意义）。
    local_emails = {
        str(row.get("email") or "").strip().lower()
        for row in local_rows
        if str(row.get("email") or "").strip()
    }
    try:
        remote_accounts = _call_fetcher(panel_key, credentials, local_emails)
    except Exception as exc:  # noqa: BLE001 - 远端故障要变成可展示的原因
        remote_error = str(exc) or exc.__class__.__name__
        logger.warning("面板 %s 远端拉取失败: %s", panel_key, remote_error)
    return local_rows, remote_accounts, remote_error


def _rebuild_panel_comparison(key: str) -> dict[str, Any]:
    """真正去拉一次数据并写缓存（调用方需已持有该面板的重建锁）。"""
    local_rows, remote_accounts, remote_error = fetch_panel_raw(key)

    rows = build_comparison(local_rows, remote_accounts)
    payload = {
        "panel": key,
        "fetched_at": _utcnow_iso(),
        "cached": False,
        "summary": summarize(rows),
        # 状态名 → 中文标签。前端据此渲染筛选条 —— 这套映射只在后端定义一份，
        # 前端再抄一份的话加一个状态要改两个地方，还容易漏（UI 显示旧文案）。
        "labels": dict(STATE_LABELS),
        "rows": [row.to_dict() for row in rows],
        "remote_error": remote_error,
        "local_count": len(local_rows),
        "remote_count": len(remote_accounts),
    }

    with _lock:
        _cache[key] = {
            "payload": payload,
            "monotonic": time.monotonic(),
            # 远端没读到时标失败：这类条目按更短的 TTL 过期，好让下一次刷新重试
            "failed": bool(remote_error),
        }
    return payload


__all__ = [
    "CACHE_TTL_SECONDS",
    "clear_cache",
    "fetch_panel_raw",
    "get_panel_comparison",
]
