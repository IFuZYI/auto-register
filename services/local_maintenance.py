"""本地自动维护的定时任务：自动检测 / 自动刷新 Token / 日志清理。

与「面板同步」是**两件事**，不要混：

- 面板同步：**持续收敛**。每 10 分钟对账一次，问「面板和本地谁的新」。
- 本地维护：**每天对账一次**。到点跑一轮明确的动作（抽检 / 刷失效号）。

所以这里用 `core.scheduler` 的 `daily_at`（每天 HH:MM）而不是 `interval_seconds`
—— 用户要的是「凌晨三点跑」，而 interval 的语义是「距上次跑满 24 小时」，
每次进程重启都会把那个时刻往后推一点，慢慢就漂到下午去了。

跑法上刻意**复用手动动作那条链**（`api.actions` 里的
`_execute_platform_action` + `_apply_action_result`），不另写一套：
手动点「刷新 Token」和定时自动刷新必须是同一个东西，否则两边结果对不上、
凭据写回规则也会分叉。这里只负责**选谁**（范围 / 抽检策略 / 随机数）。

本模块的纯函数（`select_probe_targets` / `select_refresh_targets` /
`parse_hhmm`）不碰网络与数据库，测试直接喂数据即可。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

import logging

logger = logging.getLogger(__name__)

#: 各平台的「检测」与「刷新」动作 id。定时任务只认这两个动作，
#: 不开放任意动作 —— 一个能被配置成任意动作的定时器是个安全事故。
PLATFORM_ACTIONS: dict[str, dict[str, str]] = {
    "chatgpt": {"probe": "probe_local_status", "refresh": "refresh_token"},
    "grok": {"probe": "probe_refresh", "refresh": "refresh_token"},
}

#: 平台 → 从面板**拉回**状态的动作 id（刷新策略 `remote_first` 用）。
PULL_ACTION_BY_PANEL: dict[str, str] = {
    "chatgpt2api": "sync_chatgpt2api_status",
    "sub2api": "sync_sub2api_status",
    "cpa": "sync_cliproxyapi_status",
    "grok2api": "sync_grok2api_status",
}

#: 面板 → 它服务哪个平台。拉取时只拉该平台自己那几张面板。
PANEL_PLATFORM: dict[str, str] = {
    "chatgpt2api": "chatgpt",
    "sub2api": "chatgpt",
    "cpa": "chatgpt",
    "grok2api": "grok",
}

#: 配置开关 → 面板 key（判断「这张面板开没开自动同步」）。
PANEL_SWITCH: dict[str, str] = {
    "chatgpt2api": "chatgpt2api_auto_sync_enabled",
    "sub2api": "sub2api_auto_sync_enabled",
    "cpa": "cpa_auto_sync_enabled",
    "grok2api": "grok2api_auto_sync_enabled",
}

SYNC_STRATEGIES = ("remote_first", "local_then_push", "local_only")
PROBE_MODES = ("sample", "all")
PROBE_STRATEGIES = ("oldest", "random")
REFRESH_SCOPES = ("failed", "all", "random")

#: 刷新只针对「还能救」的状态。`banned`（上游明确停用）不在内 ——
#: 拿封号去刷只是白费出口 IP，还会把真正的封禁信号淹在噪声里。
REFRESHABLE_STATUSES = frozenset({"expired", "invalid", "limited"})
NON_REFRESHABLE_STATUSES = frozenset({"banned"})

DEFAULT_PROBE_PERCENT = 20
DEFAULT_REFRESH_RANDOM_COUNT = 50
DEFAULT_LOG_KEEP_DAYS = 7
DEFAULT_PROBE_TIME = "03:00"
DEFAULT_REFRESH_TIME = "04:00"
DEFAULT_CLEANUP_TIME = "05:00"


# ── 时间 ──────────────────────────────────────────────────────────────


def parse_hhmm(value: Any) -> str:
    """把配置里的时间规整成 `"HH:MM"`；非法值返回空串（= 这个任务不跑）。

    兼容用户手打的 `9:5` / `09：05`（全角冒号）/ 前后空格 —— 这些都不该让
    定时任务静默失效。非法值返回空串而不是抛异常：一个写错的配置不该让
    scheduler 的整个唤醒循环出问题。
    """
    text = str(value or "").strip().replace("：", ":")
    if not text or ":" not in text:
        return ""
    hh, _, mm = text.partition(":")
    try:
        hour, minute = int(hh), int(mm)
    except (TypeError, ValueError):
        return ""
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return ""
    return f"{hour:02d}:{minute:02d}"


def clamp_percent(value: Any, default: int = DEFAULT_PROBE_PERCENT) -> int:
    """抽检比例，1~100 之间；非法值回默认。"""
    try:
        num = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    if num <= 0:
        return default
    return min(100, num)


def clamp_count(value: Any, default: int = DEFAULT_REFRESH_RANDOM_COUNT) -> int:
    """随机刷新条数，1~1000 之间；非法值回默认。"""
    try:
        num = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    if num <= 0:
        return default
    return min(1000, num)


def clamp_days(value: Any, default: int = DEFAULT_LOG_KEEP_DAYS) -> int:
    """日志保留天数。下限 1 —— 0 会在下一次清理时把全部历史删掉。"""
    try:
        num = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default
    return max(1, min(3650, num))


# ── 选择逻辑（纯函数） ────────────────────────────────────────────────


@dataclass(frozen=True)
class Pick:
    """一个待处理账号。只带选择逻辑需要的字段，不背整个模型。"""

    id: Any
    email: str
    status: str
    last_probe_at: float


def last_probe_at(extra: dict[str, Any]) -> float:
    """最近一次探测时刻（epoch 秒）；从没探测过返回 0。

    「从没探测过」必须是 0 而不是 `now` —— 抽检策略「优先更久未检测的」
    里，新号正是最该被看一眼的（它连一次都没验证过）。用 now 会把新号
    排到最后，越新的号越不被检查。
    """
    candidates = (
        (extra.get("chatgpt_local") or {}).get("checked_at"),
        (extra.get("probe") or {}).get("checked_at"),
    )
    for raw in candidates:
        parsed = _parse_time(raw)
        if parsed:
            return parsed
    return 0.0


def _parse_time(value: Any) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        normalized = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def select_probe_targets(
    accounts: Iterable[Pick],
    *,
    mode: str,
    percent: int = DEFAULT_PROBE_PERCENT,
    strategy: str = "oldest",
    rng: Optional[random.Random] = None,
) -> list[Pick]:
    """挑这一轮要检测的账号。

    - `mode="all"`：全部（不动顺序 —— 「全部检测」的语义是「都过一遍」，
      排序只会让人以为有优先级）。
    - `mode="sample"`：按 `percent` 取，`strategy` 决定取哪些：
      - `oldest`：**最久没检测的优先**，从没检测过（`last_probe_at == 0`）
        排在最前。这是默认策略 —— 抽检的意义是「用有限的配额覆盖最可能
        已经坏掉的号」，而「最久没看过的」就是最可能坏的那批。
      - `random`：完全随机。用于「我怀疑某批号一起坏了，但不知道是哪批」。
    """
    rows = list(accounts)
    if mode == "all":
        return rows
    if mode not in PROBE_MODES:
        return []
    total = len(rows)
    if total == 0:
        return []
    want = max(1, round(total * clamp_percent(percent) / 100))
    want = min(want, total)
    if strategy == "random":
        return (rng or random).sample(rows, want)
    # oldest：没探测过的（0）排最前，其余按时间升序
    ordered = sorted(rows, key=lambda a: (a.last_probe_at, str(a.id)))
    return ordered[:want]


def select_refresh_targets(
    accounts: Iterable[Pick],
    *,
    scope: str,
    count: int = DEFAULT_REFRESH_RANDOM_COUNT,
    rng: Optional[random.Random] = None,
) -> list[Pick]:
    """挑这一轮要刷新的账号。

    - `failed`（默认）：只刷「还能救」的状态（过期 / 失效 / 限流）。
      **正在正常工作的号不刷** —— 刷新对上游就是一次登录动作，对好号做这个
      纯属增加被风控的面，而且没有任何收益。
    - `all`：所有非封禁账号。
    - `random`：随机取 `count` 个。存在的理由是**风控**：一次性刷几百个号，
      出口 IP 的表现就像攻击；随机摊开、每天一小批才不会触发限流。
    """
    rows = list(accounts)
    refreshable = [a for a in rows if a.status not in NON_REFRESHABLE_STATUSES]
    if scope == "all":
        return refreshable
    if scope == "random":
        want = min(clamp_count(count), len(refreshable))
        return (rng or random).sample(refreshable, want) if want else []
    if scope != "failed":
        return []
    return [a for a in refreshable if a.status in REFRESHABLE_STATUSES]


# ── 配置读取 ──────────────────────────────────────────────────────────


def _config_text(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip()
    except Exception as exc:  # noqa: BLE001 - 配置读不到时按「关闭」处理
        logger.debug("读取配置 %s 失败，按空值处理: %s", key, exc)
        return ""


#: 默认**开**的布尔键。升级时不能改掉既有部署的行为：
#: 「检测出封禁就标注为禁用」在加这个开关之前是**无条件**做的，
#: 默认关等于静默取消了一个一直在生效的动作。
BOOLEAN_DEFAULTS_TRUE: frozenset[str] = frozenset(
    f"{p}_refresh_disable_banned" for p in ("chatgpt", "grok")
)


def _config_bool(key: str, default: bool = False) -> bool:
    raw = _config_text(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def _config_int(key: str, default: int) -> int:
    try:
        return int(float(_config_text(key)))
    except (TypeError, ValueError):
        return default


def _sync_strategy(platform: str) -> str:
    value = _config_text(f"{platform}_sync_strategy").lower()
    return value if value in SYNC_STRATEGIES else "local_only"


def get_log_cleanup_daily_at() -> str:
    if not _config_bool("log_cleanup_enabled"):
        return ""
    return parse_hhmm(_config_text("log_cleanup_time")) or DEFAULT_CLEANUP_TIME


def get_auto_probe_daily_at(platform: str) -> str:
    key = f"{platform}_auto_probe_enabled"
    if not _config_bool(key):
        return ""
    mode = _config_text(f"{platform}_probe_mode").lower()
    if mode not in PROBE_MODES:
        return ""
    return parse_hhmm(_config_text(f"{platform}_probe_time")) or DEFAULT_PROBE_TIME


def get_auto_refresh_daily_at(platform: str) -> str:
    if not _config_bool(f"{platform}_auto_refresh_enabled"):
        return ""
    scope = _config_text(f"{platform}_refresh_scope").lower()
    if scope not in REFRESH_SCOPES:
        return ""
    return parse_hhmm(_config_text(f"{platform}_refresh_time")) or DEFAULT_REFRESH_TIME


#: 每处理一个账号之间的间隔。定时任务在无人值守时跑，没有理由贴着上游打 ——
#: 200 个号连打就是一次小型爬取，出口 IP 立刻进风控名单。
PER_ACCOUNT_DELAY_SECONDS = 3.0


def _iter_picks(platform: str) -> list[Pick]:
    """读该平台全部账号，转成只带选择逻辑需要字段的 `Pick`。"""
    from core.db import account_repository

    picks: list[Pick] = []
    for row in account_repository.list_accounts(platform):
        if isinstance(row, dict):
            extra = row.get("extra") or {}
            if not isinstance(extra, dict):
                extra = {}
            picks.append(
                Pick(
                    id=row.get("id"),
                    email=str(row.get("email") or ""),
                    status=str(row.get("status") or ""),
                    last_probe_at=last_probe_at(extra),
                )
            )
        else:  # 模型实例
            picks.append(
                Pick(
                    id=row.id,
                    email=str(row.email or ""),
                    status=str(row.status or ""),
                    last_probe_at=last_probe_at(row.get_extra() or {}),
                )
            )
    return picks


def _run_actions(
    platform: str,
    action_id: str,
    picks: list[Pick],
    *,
    source: str,
    label: str,
    revert_banned_mark: bool = False,
) -> dict[str, Any]:
    """把 `picks` 逐个跑一遍 `action_id`，边走边落库。

    刻意**复用手动动作那条链**：`execute_action` → `apply_action_result`。
    自己再写一份「刷新后怎么改状态/凭据」的话，手动一次和定时一次的结果
    迟早会分叉，而分叉点恰恰是凭据 —— 最难发现、代价最大。
    """
    from core.base_platform import RegisterConfig
    from core.config_store import config_store
    from core.db import account_repository, platform_session
    from core.registry import get
    from services.action_execution import (
        apply_action_result,
        resolve_action_proxy,
        result_message,
        to_platform_account,
    )

    summary = {"total": len(picks), "ok": 0, "failed": 0, "skipped": 0, "errors": []}
    if not picks:
        return summary

    task_id, control = _open_task(
        source=source,
        platform=platform,
        total=len(picks),
        meta={"kind": "maintenance", "action_id": action_id, "label": label},
    )
    log = _make_logger(task_id, None, f"[本地维护 {platform}]")
    instance = get(platform)(config=RegisterConfig(extra=config_store.get_all()))

    stopped = False
    for index, pick in enumerate(picks):
        if control is not None:
            try:
                control.checkpoint()
            except Exception as exc:  # noqa: BLE001 - 用户点了停止（checkpoint 就是这么通知的）
                logger.debug("任务被停止: %s", exc)
                stopped = True
                log(f"已停止（处理 {summary['ok'] + summary['failed']}/{len(picks)}）")
                break
        if index:
            time.sleep(PER_ACCOUNT_DELAY_SECONDS)
        try:
            with platform_session(platform) as session:
                acc = account_repository.get(pick.id, platform=platform, session=session)
                if acc is None:
                    summary["skipped"] += 1
                    continue
                proxy = resolve_action_proxy(acc)
                if proxy:
                    instance.config.proxy = proxy
                status_before = str(acc.status or "")
                result = instance.execute_action(action_id, to_platform_account(acc), {})
                apply_action_result(platform, action_id, acc, result, session)
                # 「封禁账号标记为禁用」开关关掉时，把这一轮造成的禁用标记撤回。
                # 不撤的话这个开关就是个摆设 —— 动作链自己会标，用户关不掉。
                if revert_banned_mark and str(acc.status or "") == "banned" and status_before != "banned":
                    acc.status = status_before
                    log(f"{pick.email}: 已检测到封禁，但「标记为禁用」关着 —— 保留原状态 {status_before}")
                session.commit()
            if result.get("ok"):
                summary["ok"] += 1
                log(f"{pick.email}: {label}成功")
            else:
                summary["failed"] += 1
                reason = result_message(result) or "失败"
                summary["errors"].append(f"{pick.email}: {reason}")
                log(f"{pick.email}: {label}失败 —— {reason}")
        except Exception as exc:  # noqa: BLE001 - 单个号失败不毁整轮
            summary["failed"] += 1
            summary["errors"].append(f"{pick.email}: {type(exc).__name__}: {exc}")
            log(f"{pick.email}: {label}异常 —— {type(exc).__name__}: {exc}")

    _close_auto_task(
        task_id,
        status="stopped" if stopped else "done",
        success=summary["ok"],
        registered=summary["ok"] + summary["failed"],
        skipped=summary["skipped"],
        errors=summary["errors"],
    )
    log(f"本轮结束：成功 {summary['ok']} / 失败 {summary['failed']} / 跳过 {summary['skipped']}")
    return summary


def run_auto_probe(platform: str, *, rng: Optional[random.Random] = None) -> dict[str, Any]:
    """跑一轮自动检测。"""
    actions = PLATFORM_ACTIONS.get(platform)
    if not actions:
        return {"total": 0, "ok": 0, "failed": 0, "skipped": 0, "errors": [], "reason": "unsupported_platform"}
    picks = _iter_picks(platform)
    targets = select_probe_targets(
        picks,
        mode=_config_text(f"{platform}_probe_mode").lower() or "sample",
        percent=_config_int(f"{platform}_probe_sample_percent", DEFAULT_PROBE_PERCENT),
        strategy=_config_text(f"{platform}_probe_strategy").lower() or "oldest",
        rng=rng,
    )
    return _run_actions(platform, actions["probe"], targets, source=f"auto_probe_{platform}", label="检测")


def run_auto_refresh(platform: str, *, rng: Optional[random.Random] = None) -> dict[str, Any]:
    """跑一轮自动刷新 Token。

    刷新策略决定要不要和面板对账（用户给的三个档）：
    - `remote_first`：刷新前先从面板拉回较新的凭证，刷新后再推回去。
    - `local_then_push`：只推（刷新后同步到远程）。
    - `local_only`：不与面板交互（默认，= 加这个开关之前的行为）。

    「先拉回来」不是可有可无的一步：面板那侧可能已经用别的路径换过 RT，
    本地拿着旧的去刷等于白刷一次（上游会把旧 RT 判失效）。
    """
    actions = PLATFORM_ACTIONS.get(platform)
    if not actions:
        return {"total": 0, "ok": 0, "failed": 0, "skipped": 0, "errors": [], "reason": "unsupported_platform"}

    strategy = _sync_strategy(platform)
    panels = _enabled_panels(platform)

    if strategy == "remote_first" and panels:
        _pull_from_panels(platform, panels)

    picks = _iter_picks(platform)
    targets = select_refresh_targets(
        picks,
        scope=_config_text(f"{platform}_refresh_scope").lower() or "failed",
        count=_config_int(f"{platform}_refresh_random_count", DEFAULT_REFRESH_RANDOM_COUNT),
        rng=rng,
    )
    summary = _run_actions(
        platform,
        actions["refresh"],
        targets,
        source=f"auto_refresh_{platform}",
        label="刷新 Token",
        revert_banned_mark=not _config_bool(f"{platform}_refresh_disable_banned", True),
    )

    if strategy in {"remote_first", "local_then_push"} and panels:
        summary["pushed"] = _push_to_panels(platform, panels)
    return summary


def _enabled_panels(platform: str) -> list[str]:
    """该平台下「开了自动同步」的面板 key。"""
    return [
        key
        for key, owner in PANEL_PLATFORM.items()
        if owner == platform and _config_bool(PANEL_SWITCH.get(key, ""))
    ]


def _pull_from_panels(platform: str, panels: list[str]) -> None:
    """从面板拉回状态（复用「同步面板状态」动作）。失败只记日志。"""
    actions = PLATFORM_ACTIONS.get(platform)
    if not actions:
        return
    picks = _iter_picks(platform)
    for panel in panels:
        pull_action = PULL_ACTION_BY_PANEL.get(panel)
        if not pull_action:
            continue
        try:
            _run_actions(platform, pull_action, picks, source=f"auto_pull_{panel}", label=f"同步 {panel} 状态")
        except Exception as exc:  # noqa: BLE001 - 拉取失败不该挡住刷新
            logger.warning("自动维护：从 %s 拉取状态失败（继续刷新）: %s", panel, exc)


def _push_to_panels(platform: str, panels: list[str]) -> list[str]:
    """刷新后把结果推回面板。"""
    from services.chatgpt_maintenance import run_panel_auto_sync

    pushed: list[str] = []
    for panel in panels:
        try:
            run_panel_auto_sync(panel)
            pushed.append(panel)
        except Exception as exc:  # noqa: BLE001 - 推送失败不该毁掉刷新结果
            logger.warning("自动维护：推送到 %s 失败: %s", panel, exc)
    return pushed


# ── 任务记录（复用 chatgpt_maintenance 里那套，不另写一份） ──────────────
#
# 「任务运行」页要能看到定时任务跑过什么，所以每次执行都要建一条记录。
# 创建/收尾/日志出口这三件事在 `chatgpt_maintenance` 里已经写过一遍，
# 直接复用 —— 复制一份的下场是两边字段慢慢长歪，而页面只认其中一种。


def _open_task(*, source: str, platform: str, total: int, meta: Optional[dict[str, Any]] = None):
    from services.chatgpt_maintenance import _open_auto_task

    return _open_auto_task(source=source, platform=platform, total=total, meta=meta)


def _close_auto_task(task_id: Optional[str], **kwargs: Any) -> None:
    from services.chatgpt_maintenance import _close_auto_task as _impl

    return _impl(task_id, **kwargs)


def _make_logger(task_id: Optional[str], sink: Optional[Callable[[str], None]], prefix: str):
    from services.chatgpt_maintenance import _make_logger as _impl

    return _impl(task_id, sink, prefix)


def run_log_cleanup(*, now: Optional[datetime] = None) -> dict[str, Any]:
    """删掉超过保留天数的任务历史。

    只按**保留天数**删，没有「全清」档位：一个每天定点执行的动作不该带
    「抹掉全部历史」这种选项 —— 误配一次就再也查不到「上周那个号为什么掉」。

    两个存储后端各走各的：JSON 用仓储自带的 `cleanup_json`（它已经处理了
    naive/aware 混用的脏数据），SQLite 走一条 `DELETE WHERE created_at < ?`。
    """
    keep_days = clamp_days(_config_int("log_cleanup_keep_days", DEFAULT_LOG_KEEP_DAYS))
    cutoff_dt = (now or datetime.now(timezone.utc)) - timedelta(days=keep_days)
    summary: dict[str, Any] = {"keep_days": keep_days, "deleted": 0, "backend": "sqlite"}

    from core.db.task_log_repository import task_log_repository

    deleted = task_log_repository.cleanup_json(cutoff_dt)
    if deleted is not None:
        summary["backend"] = "json"
        summary["deleted"] = int(deleted)
        logger.info("自动清理任务历史：%s 条（保留 %s 天）", summary["deleted"], keep_days)
        return summary

    from core.db import get_session
    from core.db.models_infra import TaskLog
    from sqlmodel import delete as sqlmodel_delete

    # SQLite 存的是 naive UTC 字符串比较；用不带时区的时间戳去比，
    # 免得 `2026-10-31 12:00:00` 和 `2026-10-31T12:00:00+00:00` 直接比字符串。
    naive_cutoff = cutoff_dt.replace(tzinfo=None)
    with get_session() as session:
        result = session.exec(sqlmodel_delete(TaskLog).where(TaskLog.created_at < naive_cutoff))
        session.commit()
        summary["deleted"] = int(getattr(result, "rowcount", 0) or 0)
    logger.info("自动清理任务历史：%s 条（保留 %s 天）", summary["deleted"], keep_days)
    return summary


# ── 周期任务自注册 ────────────────────────────────────────────────────
#
# 用 `daily_at`（每天 HH:MM）而不是 `interval_seconds`：这些任务要的是
# 「凌晨三点跑」。读配置失败 / 开关关着 / 时间非法 → 返回空串 = 本次不跑，
# scheduler 会跳过（不需要注销）。

from core.scheduler import register_job as _register_job  # noqa: E402

_register_job(
    "log_auto_cleanup",
    daily_at=get_log_cleanup_daily_at,
    runner=run_log_cleanup,
)


def _bind_probe(_platform: str):
    def _runner() -> dict[str, Any]:
        return run_auto_probe(_platform)

    return _runner


def _bind_refresh(_platform: str):
    def _runner() -> dict[str, Any]:
        return run_auto_refresh(_platform)

    return _runner


for _p in ("chatgpt", "grok"):
    _register_job(
        f"{_p}_auto_probe",
        daily_at=(lambda _k=_p: get_auto_probe_daily_at(_k)),
        runner=_bind_probe(_p),
    )
    _register_job(
        f"{_p}_auto_refresh_daily",
        daily_at=(lambda _k=_p: get_auto_refresh_daily_at(_k)),
        runner=_bind_refresh(_p),
    )


__all__ = [
    "PLATFORM_ACTIONS",
    "PROBE_MODES",
    "PROBE_STRATEGIES",
    "REFRESH_SCOPES",
    "SYNC_STRATEGIES",
    "Pick",
    "clamp_count",
    "clamp_days",
    "clamp_percent",
    "get_auto_probe_daily_at",
    "get_auto_refresh_daily_at",
    "get_log_cleanup_daily_at",
    "last_probe_at",
    "parse_hhmm",
    "run_auto_probe",
    "run_auto_refresh",
    "run_log_cleanup",
    "select_probe_targets",
    "select_refresh_targets",
]
