"""本地自动维护的定时任务：自动检测 / 自动刷新 Token / 日志清理。

与「面板同步」是**两件事**，不要混：

- 面板同步：**持续收敛**。每 10 分钟对账一次，问「面板和本地谁的新」。
- 本地维护：**每天对账一次**。到点跑一轮明确的动作（抽检 / 刷失效号）。

所以这里用 `interval_seconds`（每 N 秒一轮），间隔由界面配置。
（2026-10-31 一度做成「每天 HH:MM」，同日改回间隔 —— 用户要的是「执行间隔」：
「每天三点」只是间隔 24 小时的特例，而「每 6 小时」「每 30 分钟」用固定时刻
根本表达不了。）

跑法上刻意**复用手动动作那条链**（`api.actions` 里的
`_execute_platform_action` + `_apply_action_result`），不另写一套：
手动点「刷新 Token」和定时自动刷新必须是同一个东西，否则两边结果对不上、
凭据写回规则也会分叉。这里只负责**选谁**（范围 / 抽检策略 / 随机数）。

本模块的纯函数（`select_probe_targets` / `select_refresh_targets` /
`parse_interval`）不碰网络与数据库，测试直接喂数据即可。
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


# ── 间隔 ──────────────────────────────────────────────────────────────

#: 单位 → 秒。用户 2026-10-31 要的是「执行间隔」而不是「执行时间」：
#: 「每天凌晨三点」只是**间隔 24 小时**的一个特例，而「每 6 小时」「每 30 分钟」
#: 这种粒度用固定时刻根本表达不了。所以配置形态从 `HH:MM` 换成 `<数字><单位>`。
INTERVAL_UNITS: dict[str, int] = {"m": 60, "h": 3600, "d": 86400}

#: 最短 5 分钟。再短就不是「定时维护」而是贴着上游打 —— 自动刷新本身就是
#: 一次登录动作，几分钟一轮足以让出口 IP 进风控名单。
MIN_INTERVAL_SECONDS = 5 * 60
#: 最长 30 天。再长等于关掉，不如把开关关掉（意图更清楚）。
MAX_INTERVAL_SECONDS = 30 * 86400

DEFAULT_PROBE_INTERVAL = "6h"
DEFAULT_REFRESH_INTERVAL = "6h"
DEFAULT_CLEANUP_INTERVAL = "24h"


def parse_interval(value: Any) -> int:
    """把配置里的间隔解析成秒；非法值返回 0（= 这个任务不跑）。

    接受三种写法：
    - `30m` / `6h` / `1d` —— 界面写出来的就是这个
    - 裸数字 `90` —— 按**分钟**算（用户手打时最自然的单位）
    - 全角/空格混杂（` 6h `）—— 顺手 trim

    返回 0 而不是抛异常：一个写错的配置不该让调度循环出问题。超范围则**钳制**
    而不是判非法 —— 用户填 `1m` 的本意是「尽量勤」，给他 5 分钟比让他什么都不跑
    更接近意图，而且界面上那个值会立刻显示成钳制后的结果。
    """
    text = str(value or "").strip().lower()
    if not text:
        return 0
    unit = INTERVAL_UNITS.get(text[-1])
    if unit is None:
        number, unit = text, 60
    else:
        number = text[:-1].strip()
    try:
        amount = float(number)
    except (TypeError, ValueError):
        return 0
    if amount <= 0:
        return 0
    seconds = int(amount * unit)
    return max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, seconds))


def format_interval(seconds: int) -> str:
    """秒 → `<数字><单位>`（界面回显）。取**能整除的最大单位**。"""
    value = int(seconds or 0)
    if value <= 0:
        return ""
    for suffix, unit in (("d", 86400), ("h", 3600), ("m", 60)):
        if value % unit == 0:
            return f"{value // unit}{suffix}"
    return f"{value // 60}m"


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


def get_log_cleanup_interval_seconds() -> int:
    if not _config_bool("log_cleanup_enabled"):
        return 0
    return parse_interval(_config_text("log_cleanup_interval") or DEFAULT_CLEANUP_INTERVAL)


def get_auto_probe_interval_seconds(platform: str) -> int:
    if not _config_bool(f"{platform}_auto_probe_enabled"):
        return 0
    # 范围不在白名单时整条规则不跑，**不**退化成「全部」：静默放大执行量最危险。
    if _config_text(f"{platform}_probe_mode").lower() not in PROBE_MODES:
        return 0
    return parse_interval(_config_text(f"{platform}_probe_interval") or DEFAULT_PROBE_INTERVAL)


def get_auto_refresh_interval_seconds(platform: str) -> int:
    if not _config_bool(f"{platform}_auto_refresh_enabled"):
        return 0
    if _config_text(f"{platform}_refresh_scope").lower() not in REFRESH_SCOPES:
        return 0
    return parse_interval(_config_text(f"{platform}_refresh_interval") or DEFAULT_REFRESH_INTERVAL)


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
        # 拉取失败**不能**拖住刷新：面板连不上是面板的事，本地该刷的号还是
        # 该刷。`_pull_from_panels` 内部已按面板逐个兜底，这里再兜一层是因为
        # 它自己也可能在进入循环前就抛（平台不支持 / 读账号列表失败）。
        try:
            _pull_from_panels(platform, panels)
        except Exception as exc:  # noqa: BLE001 - 拉取是优化，不是前提
            logger.warning("自动维护：同步面板状态失败（继续刷新）: %s", exc)

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
        try:
            summary["pushed"] = _push_to_panels(platform, panels)
        except Exception as exc:  # noqa: BLE001 - 推送失败不改刷新结果
            logger.warning("自动维护：推送刷新结果到面板失败: %s", exc)
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

    只按**保留天数**删，没有「全清」档位：一个定时执行的动作不该带
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
# 用 `interval_seconds`：用户要的是「执行间隔」。开关关着 / 间隔非法 → 返回 0
# = 本次不跑，scheduler 会跳过（不需要注销）。每 N 秒是**距上次跑满 N 秒**，
# 重启时 `start()` 会把 last_run_at 顶到当前时间，不会一启动就瞬间触发。

from core.scheduler import register_job as _register_job  # noqa: E402

_register_job(
    "log_auto_cleanup",
    interval_seconds=get_log_cleanup_interval_seconds,
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
        interval_seconds=(lambda _k=_p: get_auto_probe_interval_seconds(_k)),
        runner=_bind_probe(_p),
    )
    _register_job(
        f"{_p}_auto_refresh",
        interval_seconds=(lambda _k=_p: get_auto_refresh_interval_seconds(_k)),
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
    "get_auto_probe_interval_seconds",
    "get_auto_refresh_interval_seconds",
    "get_log_cleanup_interval_seconds",
    "last_probe_at",
    "format_interval",
    "parse_interval",
    "run_auto_probe",
    "run_auto_refresh",
    "run_log_cleanup",
    "select_probe_targets",
    "select_refresh_targets",
]
