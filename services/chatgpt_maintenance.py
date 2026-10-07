"""ChatGPT 账号自动维护（用户要求 2026-10-06）。

两条功能，都由「全局配置 → 面板配置」里的开关控制：

1. **自动刷新临期/过期 Token**（`chatgpt_auto_refresh_enabled`）：
   后台定期扫描 ChatGPT 账号，对 AT 临期（剩余 ≤ 24h）或已过期的账号，
   在临期窗口内**随机**一个时刻刷新 —— 不设固定更新时刻（用户要求：
   「不要设定固定值来更新，应该在临期时间段内随机一个时间更新」），
   且至少提前 1 小时（「尽量不要拖到过期，比如至少在过期前 1 小时」）。
   封禁的、连续失败达上限的不重复尝试（「封禁/无法更新 token 的不要
   重复尝试」）。刷新走 `refresh_token` 动作的完整链（session → OAuth →
   登录兜底），与手动批量刷新同一条路径。

2. **chatgpt2api 凭证自动维护**（`chatgpt2api_auto_sync_enabled`）：
   定期对比本地与 chatgpt2api 远端凭证，本地较新时自动推送更新
   （复用 `panel_push` 的方向判定），使远端始终持有最新凭证。

设计约束：

- **避免高并发**：每轮扫描限量（`_SCAN_BATCH_SIZE`），逐个串行执行，
  每个之间留间隔（`_PER_ACCOUNT_DELAY_SECONDS`）；随机时刻本身就是
  错峰机制（各账号的窗口不同、采样独立）。
- **随机时刻持久化**：计划存 extra 的 `chatgpt_auto_refresh`
  （`{at, for_exp, attempts, disabled}`），进程重启不丢；AT 换发后
  （exp 变了）旧计划自动作废重排。
- **退避**：失败后 1h → 6h 退避重试；连续 3 次失败置 `disabled`，
  同一 AT 周期内不再尝试。手动刷新成功（AT 换发）后 exp 变化会自然
  复位重排。
- **任务可见性**（用户要求 2026-10-07）：「任务运行」页要能看到这些维护
  任务、方便查看日志。两趟维护在**有实际动作**时创建任务记录
  （source = `auto_refresh` / `chatgpt2api_sync`），日志逐行进记录、
  支持停止/跳过；空轮不建记录（每 5/10 分钟一条空任务会把列表淹掉）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

#: 临期判定：剩余 ≤ 24h 视为「临期」（与 `chatgpt_token_lifecycle` 的
#: `ACCESS_TOKEN_EXPIRING_SKEW_SECONDS` 同口径）。
EXPIRING_SKEW_SECONDS = 24 * 60 * 60

#: 随机窗口的「最晚刷新时刻」= 到期前 1 小时（用户要求：至少提前 1 小时）。
MIN_LEAD_SECONDS = 60 * 60

#: 已过期 / 剩余不足 1 小时时的紧急抖动上限（秒）：尽快刷，但错开同秒并发。
URGENT_JITTER_SECONDS = 5 * 60

#: 失败退避表（秒）：第 1 次失败退 1h，第 2 次退 6h；第 3 次直接禁用。
_RETRY_BACKOFF_SECONDS = (60 * 60, 6 * 60 * 60)

#: 连续失败上限 —— 达到后同一 AT 周期内不再自动尝试。
MAX_ATTEMPTS = 3

#: extra 里的状态键。
AUTO_REFRESH_STATE_KEY = "chatgpt_auto_refresh"


def _rand_default() -> float:
    import random

    return random.random()


def compute_next_attempt_at(
    expires_at: Optional[int],
    now: int,
    *,
    rand: Callable[[], float] = _rand_default,
) -> Optional[int]:
    """算出这个账号下次自动刷新的时刻（epoch 秒）；不临期返回 None。

    窗口规则（用户要求「临期时间段内随机一个时间，至少提前 1 小时」）：

    - 剩余 > 24h：None（还不到排计划的时候）；
    - 1h < 剩余 ≤ 24h：`[now, expires_at - 1h]` 内均匀随机 —— 上限保证
      至少提前 1 小时刷新；
    - 剩余 ≤ 1h 或已过期：`[now, now + 5min]` 内小抖动，尽快刷、错峰。
    """
    if expires_at is None:
        return None
    remaining = int(expires_at) - int(now)
    if remaining > EXPIRING_SKEW_SECONDS:
        return None

    r = min(max(float(rand()), 0.0), 1.0)
    latest = int(expires_at) - MIN_LEAD_SECONDS
    if latest > now:
        span = latest - now
        return int(now + span * r)
    # 剩余不足 1 小时（或已过期）：尽快 + 小抖动
    return int(now + URGENT_JITTER_SECONDS * r)


@dataclass
class AutoRefreshPlan:
    """一个账号的自动刷新决策。"""

    action: str  # schedule / wait / due / skip
    reason: str = ""
    at: Optional[int] = None
    #: action == schedule 时要写回 extra 的新状态
    state: dict[str, Any] = field(default_factory=dict)


def _state_from_extra(extra: dict[str, Any]) -> dict[str, Any]:
    raw = (extra or {}).get(AUTO_REFRESH_STATE_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _safe_int(value: Any, default: int = 0) -> int:
    """脏数据容忍的 int 转换（仓库惯例，见 `AccountModel.get_extra`）。

    持久化的状态值可能被手改/半截写入弄坏（`at="xyz"`）；裸 `int()` 会
    ValueError 把整轮扫描带崩 —— 一条脏行不该毁掉所有账号的维护。
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def plan_auto_refresh(
    extra: dict[str, Any],
    *,
    expires_at: Optional[int],
    status: str,
    now: int,
    rand: Callable[[], float] = _rand_default,
) -> AutoRefreshPlan:
    """决定这个账号现在该不该（以及何时）自动刷新。

    规则（纯函数，不落库）：
    - `banned` / 无 exp / 未临期 → skip；
    - 有计划且 `for_exp == 当前 exp`：`disabled` → skip；
      未到点 → wait；到点 → due；
    - 无计划或 `for_exp` 变了（AT 已换发）→ 重排（清 attempts/disabled）。
    """
    if str(status or "").strip().lower() == "banned":
        return AutoRefreshPlan(action="skip", reason="banned")

    if expires_at is None:
        return AutoRefreshPlan(action="skip", reason="no_expiry")

    next_at = compute_next_attempt_at(expires_at, now, rand=rand)
    if next_at is None:
        return AutoRefreshPlan(action="skip", reason="not_expiring")

    state = _state_from_extra(extra)
    same_cycle = _safe_int(state.get("for_exp")) == _safe_int(expires_at)
    if state and same_cycle:
        if state.get("disabled"):
            return AutoRefreshPlan(action="skip", reason="disabled")
        at = _safe_int(state.get("at"))
        if at > now:
            return AutoRefreshPlan(action="wait", at=at)
        return AutoRefreshPlan(action="due", at=at)

    # 无计划，或 AT 已换发（for_exp 不再是当前 exp）→ 重排
    new_state = {
        "at": next_at,
        "for_exp": int(expires_at),
        "attempts": 0,
        "disabled": False,
    }
    return AutoRefreshPlan(action="schedule", at=next_at, state=new_state)


def _access_token_of(extra: Mapping[str, Any], row: Any = None) -> str:
    """读 AT 的统一口径：凭证注册表（规范名 + camelCase 别名）+ token 列兜底。

    与同分支的其他消费者（`account_status.access_token_expired` /
    `chatgpt_sync` / `panel_comparison_cache`）同源 —— 导入路径会把未知键
    原样收进 extra（`api/accounts.py`），历史/导入的行可能以 `accessToken`
    存 AT、或只在 `token` 列上（chatgpt 的 token 列镜像是 AT）。直接
    `extra.get("access_token")` 会把这些行静默判成「无 exp」。
    """
    from core.credential_fields import get_credential, token_column_credential

    token = get_credential(extra or {}, "access_token")
    if token:
        return token
    if row is not None:
        return token_column_credential(row, "chatgpt", "access_token")
    return ""


def record_auto_refresh_result(
    extra: dict[str, Any],
    *,
    ok: bool,
    now: int,
    row: Any = None,
) -> dict[str, Any]:
    """把一次自动刷新的结果写回 extra（原地修改），返回新状态。

    - 成功：清掉计划 —— 下一次扫描按换发后的新 exp 重排；
    - 失败：attempts+1 + 退避（1h → 6h）；达 `MAX_ATTEMPTS` 置 `disabled`
      （同一 AT 周期内不再尝试）。

    `row` 可选传入（token 列兜底读 AT 用）；不传只从 extra 读。
    """
    if ok:
        extra.pop(AUTO_REFRESH_STATE_KEY, None)
        return {}

    from services.chatgpt_token_lifecycle import decode_jwt_claims

    claims = decode_jwt_claims(_access_token_of(extra, row))
    for_exp = _safe_int(claims.get("exp"))

    state = _state_from_extra(extra)
    attempts = _safe_int(state.get("attempts")) + 1
    backoff = _RETRY_BACKOFF_SECONDS[min(attempts, len(_RETRY_BACKOFF_SECONDS)) - 1]
    new_state = {
        "at": int(now) + backoff,
        "for_exp": for_exp,
        "attempts": attempts,
        "disabled": attempts >= MAX_ATTEMPTS,
    }
    extra[AUTO_REFRESH_STATE_KEY] = new_state
    return new_state


# ── 扫描执行（集成层）─────────────────────────────────────────────

#: 每轮扫描最多执行的账号数 —— 避免高并发（用户要求）。
#: 60 秒调度 tick 下每轮最多 5 个，一分钟内的实际刷新请求不会超过 5 个。
_SCAN_BATCH_SIZE = 5

#: 相邻两个账号执行之间的间隔（秒）—— 串行 + 间隔，进一步错峰。
_PER_ACCOUNT_DELAY_SECONDS = 3.0

#: 自动维护任务记录的 source 值（任务运行页据此显示彩色标签）。
AUTO_REFRESH_SOURCE = "auto_refresh"
CHATGPT2API_SYNC_SOURCE = "chatgpt2api_sync"

#: 自动刷新的扫描间隔（秒）。5 分钟一跳：随机窗口的粒度是分钟级，
#: 更密只是空转。
AUTO_REFRESH_INTERVAL_SECONDS = 5 * 60

#: chatgpt2api 自动同步的间隔（秒）。10 分钟：远端凭证变更不频繁，
#: 且推送有外网成本。
CHATGPT2API_AUTO_SYNC_INTERVAL_SECONDS = 10 * 60


def _config_bool(key: str) -> bool:
    try:
        from core.config_store import config_store

        raw = str(config_store.get(key, "") or "").strip().lower()
    except Exception:
        return False
    return raw in {"1", "true", "yes", "on", "enabled"}


def _config_text(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip()
    except Exception:
        return ""


def get_auto_refresh_interval_seconds() -> int:
    """自动刷新任务的调度间隔；关闭时返回 0（scheduler 跳过）。"""
    if not _config_bool("chatgpt_auto_refresh_enabled"):
        return 0
    return AUTO_REFRESH_INTERVAL_SECONDS


def get_chatgpt2api_auto_sync_interval_seconds() -> int:
    """chatgpt2api 自动同步的调度间隔；关闭或未配置时返回 0。"""
    if not _config_bool("chatgpt2api_auto_sync_enabled"):
        return 0
    if not _config_text("chatgpt2api_api_url") or not _config_text("chatgpt2api_api_key"):
        return 0
    return CHATGPT2API_AUTO_SYNC_INTERVAL_SECONDS


def _default_execute(account_id: int) -> dict[str, Any]:
    """生产执行器：走 `refresh_token` 动作的完整链（与手动批量刷新同路径）。

    代理规则与批量刷新一致（`_run_account_batch_task` 的
    `_resolve_proxy_for_account`）：优先账号注册时那个；不可用/未绑定时
    从池里取备用（惰性来源 —— 避免白推轮转游标）并**写回** `register_proxy`
    （用户要求「不再是仅有注册才绑定」）。
    """
    from api.actions import _apply_action_result, _to_platform_account
    from core.base_platform import RegisterConfig
    from core.config_store import config_store
    from core.db import AccountModel, platform_session
    from core.registry import get

    with platform_session("chatgpt") as s:
        row = s.get(AccountModel, account_id)
        if row is None:
            return {"ok": False, "error": "账号不存在"}
        account = _to_platform_account(row)

    base_config = config_store.get_all() or {}
    PlatformCls = get("chatgpt")
    instance = PlatformCls(config=RegisterConfig(extra=base_config))

    from core.proxy_pool import proxy_pool

    saved = str((account.extra or {}).get("register_proxy") or "")
    chosen, should_bind = proxy_pool.resolve_for_account(
        saved, fallback_provider=lambda: proxy_pool.get_next()
    )
    if chosen:
        instance.config.proxy = chosen
    if should_bind and chosen:
        # 写回绑定（与原代理不可用/未绑定的场景对齐，见 resolve_for_account 的 docstring）。
        # 写回失败只忽略 —— 不该让整轮任务失败。
        try:
            with platform_session("chatgpt") as s:
                row = s.get(AccountModel, account_id)
                if row is not None:
                    extra = row.get_extra()
                    extra["register_proxy"] = chosen
                    row.set_extra(extra)
                    s.add(row)
                    s.commit()
        except Exception:  # noqa: BLE001
            pass

    result = instance.execute_action("refresh_token", account, {})
    with platform_session("chatgpt") as s:
        row = s.get(AccountModel, account_id)
        if row is not None:
            _apply_action_result("chatgpt", "refresh_token", row, result, s)
            s.add(row)
            s.commit()
    return result


def run_auto_refresh_pass(
    *,
    now: Optional[int] = None,
    execute: Optional[Callable[[int], dict[str, Any]]] = None,
    max_accounts: int = _SCAN_BATCH_SIZE,
    delay_seconds: float = _PER_ACCOUNT_DELAY_SECONDS,
    log: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """跑一轮扫描：排计划 / 执行到点的刷新。

    每轮最多执行 `max_accounts` 个（避免高并发），串行 + `delay_seconds`
    间隔；结果按账号落库（`record_auto_refresh_result` 的语义）。

    **有实际执行**时创建任务记录（source=`auto_refresh`）——「任务运行」页
    可见、日志可回看、支持停止/跳过（用户要求 2026-10-07）；空轮不建记录。

    返回 `{scanned, scheduled, due, executed, ok, failed, skipped}`。
    """
    import time as _time

    from core.db import AccountModel, account_repository, platform_session
    from services.chatgpt_token_lifecycle import project_access_token_lifecycle

    now_s = int(now if now is not None else _time.time())
    execute_fn = execute or _default_execute

    summary = {
        "scanned": 0,
        "scheduled": 0,
        "due": 0,
        "executed": 0,
        "ok": 0,
        "failed": 0,
        "skipped": 0,
    }

    rows = account_repository.list_accounts("chatgpt")
    due_ids: list[int] = []
    for row in rows:
        summary["scanned"] += 1
        extra = row.get_extra()

        lifecycle = project_access_token_lifecycle(_access_token_of(extra, row))
        expires_at = lifecycle.get("expires_at")

        plan = plan_auto_refresh(
            extra,
            expires_at=expires_at,
            status=str(row.status or ""),
            now=now_s,
        )
        if plan.action == "schedule":
            extra[AUTO_REFRESH_STATE_KEY] = plan.state
            _write_extra(int(row.id), extra)
            summary["scheduled"] += 1
        elif plan.action == "due":
            summary["due"] += 1
            due_ids.append(int(row.id))
        else:
            summary["skipped"] += 1
            # 清掉「针对旧 AT」的死计划：AT 被手动刷新后 exp 变了，旧计划
            # 已无意义 —— 保持「extra 里只有当前 AT 周期的计划」这一不变量。
            if plan.reason == "not_expiring":
                state = _state_from_extra(extra)
                if state and _safe_int(state.get("for_exp")) != _safe_int(expires_at):
                    extra.pop(AUTO_REFRESH_STATE_KEY, None)
                    _write_extra(int(row.id), extra)

    to_run = due_ids[: max(0, int(max_accounts))]
    # 有实际执行才建任务记录（空轮不建 —— 每 5 分钟一条空任务会淹列表）。
    task_id: Optional[str] = None
    control = None
    if to_run:
        task_id, control = _open_auto_task(
            source=AUTO_REFRESH_SOURCE,
            platform="chatgpt",
            total=len(to_run),
            meta={"due": len(due_ids), "batch": len(to_run)},
        )
    log_fn = _make_logger(task_id, log, "[ChatGPT 维护]")

    if task_id is not None:
        log_fn(
            f"本轮待刷新 {len(to_run)} 个账号（共 {len(due_ids)} 个到点，"
            "每轮限量避免高并发）"
        )

    exec_errors: list[str] = []
    exec_skipped = 0
    stopped = False
    for index, account_id in enumerate(to_run):
        if index > 0 and delay_seconds > 0:
            _time.sleep(float(delay_seconds))
        # 停止/跳过在账号间隙生效（与批量刷新任务同款协作式控制）。
        if control is not None:
            try:
                control.checkpoint()
            except Exception as exc:  # StopTaskRequested / SkipCurrentAttemptRequested
                from core.task_runtime import SkipCurrentAttemptRequested, StopTaskRequested

                if isinstance(exc, StopTaskRequested):
                    stopped = True
                    log_fn("收到停止请求，本轮剩余账号不再执行")
                    break
                if isinstance(exc, SkipCurrentAttemptRequested):
                    summary["skipped"] += 1
                    exec_skipped += 1
                    log_fn(f"跳过账号 #{account_id}（手动跳过）")
                    continue
                raise
        summary["executed"] += 1
        # 执行前复查：扫描与执行之间 AT 可能被手动刷新（exp 变了）或账号
        # 状态变了 —— 按**当下**的状态重新判定，避免对已换发的 AT 白跑一次
        # 并把一次本可避免的失败记到新周期上（复审建议 S3）。
        try:
            with platform_session("chatgpt") as s:
                fresh = s.get(AccountModel, account_id)
                if fresh is None:
                    summary["skipped"] += 1
                    exec_skipped += 1
                    summary["executed"] -= 1
                    continue
                fresh_extra = fresh.get_extra()
                fresh_status = str(fresh.status or "")

            fresh_lc = project_access_token_lifecycle(_access_token_of(fresh_extra, fresh))
            fresh_plan = plan_auto_refresh(
                fresh_extra,
                expires_at=fresh_lc.get("expires_at"),
                status=fresh_status,
                now=now_s,
            )
            if fresh_plan.action != "due":
                summary["skipped"] += 1
                exec_skipped += 1
                summary["executed"] -= 1
                continue
        except Exception as exc:  # noqa: BLE001 - 复查失败按跳过处理，不毁整轮
            log_fn(f"账号 #{account_id} 执行前复查失败（跳过）: {type(exc).__name__}: {exc}")
            summary["skipped"] += 1
            exec_skipped += 1
            summary["executed"] -= 1
            continue

        result: dict[str, Any]
        try:
            result = execute_fn(account_id) or {}
        except Exception as exc:  # noqa: BLE001 - 单账号失败不该毁整轮
            log_fn(f"账号 #{account_id} 自动刷新异常: {type(exc).__name__}: {exc}")
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        # 成功 = ok 且 AT 真换发（未换发视为「无法更新」的一次失败，
        # 与批量刷新任务的如实报告同一口径）。
        ok = bool(result.get("ok")) and bool(data.get("refreshed"))
        # `state` 先给默认值：账号可能在执行期间被删（写回落空）——
        # 失败路径引用它时不能是未绑定变量（复审 E1，已复现 UnboundLocalError）。
        state: dict[str, Any] = {}
        try:
            with platform_session("chatgpt") as s:
                row = s.get(AccountModel, account_id)
                if row is not None:
                    extra = row.get_extra()
                    state = record_auto_refresh_result(extra, ok=ok, now=now_s, row=row)
                    row.set_extra(extra)
                    s.add(row)
                    s.commit()
        except Exception as exc:  # noqa: BLE001 - 写回失败只记账，不毁整轮
            log_fn(f"账号 #{account_id} 结果写回失败（忽略）: {type(exc).__name__}: {exc}")
        if ok:
            summary["ok"] += 1
            log_fn(f"账号 #{account_id} Token 已自动刷新（AT 已换发）")
        else:
            summary["failed"] += 1
            reason = str(result.get("error") or "") or "未换发"
            suffix = ""
            if state.get("disabled"):
                suffix = "；已连续失败 3 次，本 AT 周期内不再自动尝试"
            exec_errors.append(f"账号 #{account_id}: {reason}")
            log_fn(f"账号 #{account_id} 自动刷新失败: {reason}{suffix}")

    if due_ids[max(0, int(max_accounts)):]:
        log_fn(
            f"本轮执行 {min(len(due_ids), max_accounts)}/{len(due_ids)} 个，"
            "其余下轮继续（避免高并发）"
        )
    # 收尾任务记录：停止优先；否则 done（部分失败也如实带 errors）。
    # skipped 只统计**执行阶段**的跳过（手动跳过/账号消失/计划过期）——
    # 扫描阶段的「未临期」是 170+ 量级的正常跳过，进记录会误导。
    _close_auto_task(
        task_id,
        status="stopped" if stopped else "done",
        success=summary["ok"],
        registered=summary["ok"] + summary["failed"],
        skipped=exec_skipped,
        errors=exec_errors,
    )
    return summary


def _write_extra(account_id: int, extra: dict[str, Any]) -> None:
    """把 extra 写回账号行（短会话，不占网络链的连接）。"""
    from core.db import AccountModel, platform_session

    with platform_session("chatgpt") as s:
        row = s.get(AccountModel, account_id)
        if row is not None:
            row.set_extra(extra)
            s.add(row)
            s.commit()


def _open_auto_task(
    *,
    source: str,
    platform: str,
    total: int,
    meta: Optional[dict[str, Any]] = None,
) -> tuple[Optional[str], Any]:
    """为自动维护的一轮创建任务记录（「任务运行」页可见 + 日志可回看）。

    返回 `(task_id, control)`；创建失败返回 `(None, None)` —— 可见性是
    尽力而为，记录层的异常不能让维护本身失败。
    """
    try:
        import time as _time

        from api import tasks as _api

        task_id = f"{source}_{int(_time.time() * 1000)}"
        _api._task_store.create(
            task_id,
            platform=platform,
            total=max(0, int(total)),
            source=source,
            meta=dict(meta or {}),
        )
        _api._task_store.mark_running(task_id)
        _api._persist_task_snapshot(task_id)
        return task_id, _api._task_store.control_for(task_id)
    except Exception:  # noqa: BLE001 - 记录失败不影响本轮执行
        return None, None


def _close_auto_task(
    task_id: Optional[str],
    *,
    status: str,
    success: int,
    registered: int,
    skipped: int,
    errors: list[str],
) -> None:
    """收尾自动维护的任务记录（状态 + 计数 + 落库）。"""
    if task_id is None:
        return
    try:
        from api import tasks as _api

        _api._task_store.finish(
            task_id,
            status=status,
            success=max(0, int(success)),
            registered=max(0, int(registered)),
            skipped=max(0, int(skipped)),
            errors=[str(e) for e in errors],
        )
        _api._persist_task_snapshot(task_id)
        _api._task_store.cleanup()
    except Exception:  # noqa: BLE001 - 收尾失败只影响可见性
        pass


def _make_logger(
    task_id: Optional[str],
    sink: Optional[Callable[[str], None]],
    prefix: str,
) -> Callable[[str], None]:
    """日志出口：注入的 sink 优先（测试）；有任务记录时同步进记录（UI 可查）。"""

    def _emit(msg: str) -> None:
        if sink is not None:
            sink(msg)
        if task_id is not None:
            from api import tasks as _api

            _api._log(task_id, msg)
        elif sink is None:
            print(f"{prefix} {msg}")

    return _emit


def run_chatgpt2api_auto_sync(
    *,
    push: Optional[Callable[[], dict[str, Any]]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """跑一轮 chatgpt2api 凭证自动同步（本地较新 → 推送远端）。

    复用 `POST /api/integrations/panels/chatgpt2api/push` 的完整管线
    （方向判定 + 推送 + 旧记录清理 + 落库），`delete_old=true` ——
    chatgpt2api 是新建式面板，不删旧记录会留重复。

    **有实际推送动作**时创建任务记录（source=`chatgpt2api_sync`）——
    「任务运行」页可见、日志可回看（用户要求 2026-10-07）；纯空转 /
    远端读取失败（下一轮会重试）不建记录。
    """
    push_fn = push or _default_chatgpt2api_push
    base_log = log or (lambda msg: print(f"[chatgpt2api 维护] {msg}"))

    # push 本身抛异常（外网抖动、认证失败路径意外抛出）不得把调度线程带崩
    # —— 记日志返回空结果，下一周期自然重试（复审建议 S5）。
    try:
        summary = push_fn() or {}
    except Exception as exc:  # noqa: BLE001
        base_log(f"同步异常（本轮跳过）: {type(exc).__name__}: {exc}")
        return {"panel": "chatgpt2api", "total": 0, "pushed": 0, "failed": 0, "skipped": 0, "items": [], "remote_error": f"{type(exc).__name__}: {exc}"}

    remote_error = str(summary.get("remote_error") or "")
    if remote_error:
        base_log(f"远端读取失败，本轮跳过: {remote_error}")
        return summary

    pushed = _safe_int(summary.get("pushed"))
    failed = _safe_int(summary.get("failed"))
    deleted = _safe_int(summary.get("deleted"))
    skipped = _safe_int(summary.get("skipped"))

    # 只有真正发生了动作（推成功/失败/清理）才建记录。
    has_action = bool(pushed or failed or deleted)
    task_id: Optional[str] = None
    if has_action:
        task_id, _control = _open_auto_task(
            source=CHATGPT2API_SYNC_SOURCE,
            platform="chatgpt",
            total=pushed + failed,
            meta={"pushed": pushed, "failed": failed, "deleted": deleted, "skipped": skipped},
        )
    log_fn = _make_logger(task_id, log, "[chatgpt2api 维护]")

    log_fn(
        "推送 {pushed}，跳过 {skipped}，失败 {failed}，清理旧记录 {deleted}".format(
            pushed=pushed,
            skipped=skipped,
            failed=failed,
            deleted=deleted,
        )
    )
    # 逐账号结果进日志（方便排查「哪个号没推上去」）。
    raw_items = summary.get("items")
    items: list = raw_items if isinstance(raw_items, list) else []
    for item in items:
        if not isinstance(item, dict) or not item.get("push"):
            continue
        email = str(item.get("email") or "-")
        if item.get("pushed"):
            log_fn(f"  [OK] {email} 已推送")
        else:
            log_fn(f"  [FAIL] {email}: {str(item.get('message') or '推送失败')}")

    errors: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("push") and not item.get("pushed"):
            errors.append(f"{item.get('email') or '-'}: {str(item.get('message') or '推送失败')}")
    _close_auto_task(
        task_id,
        status="done",
        success=pushed,
        registered=pushed + failed,
        skipped=skipped,
        errors=errors,
    )
    return summary


def _default_chatgpt2api_push() -> dict[str, Any]:
    """生产推送：直接调用接口层的 push 端点函数（同一份管线）。"""
    from api.integrations import push_panel_endpoint

    return push_panel_endpoint("chatgpt2api", platform="chatgpt", delete_old=True)


# ── 周期任务自注册（core.scheduler 不认识具体业务，见其模块 docstring）──

from core.scheduler import register_job as _register_job  # noqa: E402

_register_job(
    "chatgpt_auto_refresh",
    interval_seconds=get_auto_refresh_interval_seconds,
    runner=run_auto_refresh_pass,
)
_register_job(
    "chatgpt2api_auto_sync",
    interval_seconds=get_chatgpt2api_auto_sync_interval_seconds,
    runner=run_chatgpt2api_auto_sync,
)


__all__ = [
    "AUTO_REFRESH_INTERVAL_SECONDS",
    "AUTO_REFRESH_STATE_KEY",
    "CHATGPT2API_AUTO_SYNC_INTERVAL_SECONDS",
    "EXPIRING_SKEW_SECONDS",
    "MAX_ATTEMPTS",
    "MIN_LEAD_SECONDS",
    "AutoRefreshPlan",
    "compute_next_attempt_at",
    "get_auto_refresh_interval_seconds",
    "get_chatgpt2api_auto_sync_interval_seconds",
    "plan_auto_refresh",
    "record_auto_refresh_result",
    "run_auto_refresh_pass",
    "run_chatgpt2api_auto_sync",
]
