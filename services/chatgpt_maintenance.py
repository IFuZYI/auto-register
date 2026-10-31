"""维护任务的**共享底座**：面板自动同步 + 刷新退避记账。

## 这个模块现在管什么

1. **面板与本地同步**（`chatgpt2api_auto_sync_enabled` /
   `sub2api_auto_sync_enabled` / `cpa_auto_sync_enabled` /
   `grok2api_auto_sync_enabled`）：定期比对本地与面板的凭证，本地较新就推过去。
   `PANEL_AUTO_SYNC` 是这张登记表，四个面板各注册一个 interval 任务。
   这类任务要的是**持续收敛**（每 10 分钟对账一次），所以用
   `interval_seconds`。

2. **刷新退避记账**（`record_auto_refresh_result` / `AUTO_REFRESH_STATE_KEY`）：
   状态存 extra 的 `chatgpt_auto_refresh`
   （`{at, for_exp, attempts, disabled}`），进程重启不丢；失败按 1h → 6h
   退避，连续 `MAX_ATTEMPTS` 次置 `disabled`。AT 换发后（exp 变了）旧计划
   自动作废重排。

   消费方是**手动恢复链**（`/api/tasks/recover` → `services/task_runners.py`）：
   它按这套计数决定「还能不能再试」以及「用尽后落禁用」。

3. **任务可见性**（`_open_auto_task` / `_close_auto_task` / `_make_logger`）：
   维护任务要在「任务运行」页看得到、日志可回看、支持停止。定时任务与
   本地维护（`services/local_maintenance.py`）共用这三个helper。

## 已经搬走的东西（别在这里找）

- **自动刷新 Token**：2026-10-31 搬到 `services/local_maintenance.py`，
  改成**按间隔**触发（用户要的是「设置执行间隔 + 范围：只刷失效 / 全部 /
  随机 N 个」）。原来那套 interval 版（`run_auto_refresh_pass`：按 AT 临期
  窗口随机挑时刻、每轮限量扫描）已删除 —— 两个入口同时存在会让同一个账号
  每天被刷两遍，而其中一条链在界面上看不见。
  它依赖的 `plan_auto_refresh` / `compute_next_attempt_at` / `_default_execute`
  一并删除。
- **自动检测**：同样住在 `services/local_maintenance.py`。

设计约束：

- **避免高并发**：逐个串行执行、之间留间隔，不用并发池 —— 这些任务打的是
  上游真实接口，贴着打就是一次小型爬取。
- **可见性**：有实际动作时才建任务记录，空轮不建（每 10 分钟一条空任务会把
  「任务运行」列表淹掉）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

from services.task_store_io import persist_task_snapshot

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional


#: 已过期 / 剩余不足 1 小时时的紧急抖动上限（秒）：尽快刷，但错开同秒并发。
URGENT_JITTER_SECONDS = 5 * 60

#: 失败退避表（秒）：第 1 次失败退 1h，第 2 次退 6h；第 3 次直接禁用。
_RETRY_BACKOFF_SECONDS = (60 * 60, 6 * 60 * 60)

#: 连续失败上限 —— 达到后同一 AT 周期内不再自动尝试。
MAX_ATTEMPTS = 3

#: 「失效/过期/限流」状态：不管 AT 是否临期都进重试队列（用户要求 2026-10-10
#: 「定期尝试自动刷新 gpt 失效的账号」）。这类账号的 AT 要么被吊销、要么
#: 解不出 exp —— 按临期窗口判定会把它们永远挡在队列外，而刷新链（session →
#: OAuth → 登录兜底）正是可能把它们救回来的路。
#:
#: `limited`（限流）也在里面：额度会重置，刷新/重登拿不到额度但**能确认
#: 额度是否回来了** —— 不进队列的话，限流的号会永远停在 limited 没人再看。
RETRY_STATUSES = frozenset({"expired", "invalid", "limited"})

#: extra 里的状态键。
AUTO_REFRESH_STATE_KEY = "chatgpt_auto_refresh"


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
    # 与计划侧（`project_access_token_lifecycle` 的 `_positive_int`）同口径：
    # 非正/缺失 exp 归一为 0。写回负值会让 `same_cycle` 永远为假 —— 每轮
    # 扫描都当「AT 已换发」重排、attempts 清零，退避与上限全部失效。
    raw_exp = _safe_int(claims.get("exp"))
    for_exp = raw_exp if raw_exp > 0 else 0

    state = _state_from_extra(extra)
    attempts = _safe_int(state.get("attempts")) + 1
    backoff = _RETRY_BACKOFF_SECONDS[min(attempts, len(_RETRY_BACKOFF_SECONDS)) - 1]
    exhausted = attempts >= MAX_ATTEMPTS
    new_state = {
        "at": int(now) + backoff,
        "for_exp": for_exp,
        "attempts": attempts,
        "disabled": exhausted,
    }
    extra[AUTO_REFRESH_STATE_KEY] = new_state
    # **「不成功则禁用」**（用户要求 2026-10-30）：连续 MAX_ATTEMPTS 次都救不
    # 回来，就落 `banned` —— 否则界面上永远显示「失效」，用户分不清
    # 「还能救」和「已经放弃了」，而队列还会在下一个 AT 周期重新排一次。
    #
    # 为什么落 banned 而不是新增「已放弃」状态：banned 的既有语义就是
    # 「怎么登都登不回来、移出所有重试队列」，正是这里要表达的；新增状态
    # 会让判重/面板同步/恢复队列三处都要跟着改。
    #
    # 只在**确实是凭证问题**时落：`configuration`（我们 client 配错）不算
    # 账号的错，落禁用会误杀一批好号（见 `classify_refresh_error` 的说明）。
    if exhausted and row is not None and _refresh_kind_of(extra) != "configuration":
        current = str(getattr(row, "status", "") or "").strip().lower()
        if current != "banned":
            setattr(row, "status", "banned")
            new_state["disabled_reason"] = "recovery_exhausted"
    return new_state


def _refresh_kind_of(extra: dict[str, Any]) -> str:
    """从 stamp 里读最后一次刷新的失败分类（permanent / configuration / retryable）。"""
    stamp = (extra or {}).get("chatgpt_token_refresh")
    if not isinstance(stamp, dict):
        return ""
    return str(stamp.get("refresh_kind") or "").strip().lower()


#: 自动维护任务记录的 source 值（任务运行页据此显示彩色标签）。
AUTO_REFRESH_SOURCE = "auto_refresh"
CHATGPT2API_SYNC_SOURCE = "chatgpt2api_sync"


#: chatgpt2api 自动同步的间隔（秒）。10 分钟：远端凭证变更不频繁，
#: 且推送有外网成本。
CHATGPT2API_AUTO_SYNC_INTERVAL_SECONDS = 10 * 60

#: 面板「与本地同步」的自动维护注册表：panel_key → 参数。
#:
#: 字段：`(开关配置键, 推哪个平台的行, 是否删远端旧记录, 任务记录 source)`。
#:
#: `delete_old` 只对**新建式**面板为真（sub2api / chatgpt2api 每次 upload
#: 新增一条记录，不删会留重复）；覆盖式（cpa / grok2api）按邮箱原地更新，
#: 没有可删的重复记录，开它只会多一轮无意义的删除请求。
#:
#: 为什么这张表在维护模块而不是注册表里：`services/panel_registry.py` 是
#: 「面板是什么」的静态描述（前端也读它），而「多久跑一次、跑的时候删不删旧
#: 记录」是本模块的调度决定。混进注册表会让 API 响应里出现前端用不上的字段。
PANEL_AUTO_SYNC: dict[str, tuple[str, str, bool, str]] = {
    "chatgpt2api": (
        "chatgpt2api_auto_sync_enabled",
        "chatgpt",
        True,
        CHATGPT2API_SYNC_SOURCE,
    ),
    "sub2api": ("sub2api_auto_sync_enabled", "chatgpt", True, "sub2api_sync"),
    "cpa": ("cpa_auto_sync_enabled", "chatgpt", False, "cpa_sync"),
    "grok2api": ("grok2api_auto_sync_enabled", "grok", False, "grok2api_sync"),
}


def get_panel_auto_sync_interval_seconds(panel_key: str) -> int:
    """某个面板的自动同步间隔；关闭 / 未配置 / 未知面板都返回 0（不调度）。

    「未配置」也要返回 0：面板地址或密钥空着时每 10 分钟跑一次只会每轮在
    日志里写一条「未配置」，把任务历史淹掉。
    """
    from services.panel_registry import PANELS_BY_KEY

    meta = PANEL_AUTO_SYNC.get(str(panel_key or "").strip().lower())
    if not meta:
        return 0
    switch_key, _platform, _delete_old, _source = meta
    if not _config_bool(switch_key):
        return 0
    panel = PANELS_BY_KEY.get(panel_key) or {}
    url_key = str(panel.get("url_key") or "").strip()
    if url_key and not _config_text(url_key):
        return 0
    return CHATGPT2API_AUTO_SYNC_INTERVAL_SECONDS


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


def get_chatgpt2api_auto_sync_interval_seconds() -> int:
    """chatgpt2api 自动同步的调度间隔；关闭或未配置时返回 0。"""
    if not _config_bool("chatgpt2api_auto_sync_enabled"):
        return 0
    if not _config_text("chatgpt2api_api_url") or not _config_text("chatgpt2api_api_key"):
        return 0
    return CHATGPT2API_AUTO_SYNC_INTERVAL_SECONDS


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

        from services import task_service as _api

        task_id = f"{source}_{int(_time.time() * 1000)}"
        _api.task_store.create(
            task_id,
            platform=platform,
            total=max(0, int(total)),
            source=source,
            meta=dict(meta or {}),
        )
        _api.task_store.mark_running(task_id)
        persist_task_snapshot(task_id)
        return task_id, _api.task_store.control_for(task_id)
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
        from services import task_service as _api

        _api.task_store.finish(
            task_id,
            status=status,
            success=max(0, int(success)),
            registered=max(0, int(registered)),
            skipped=max(0, int(skipped)),
            errors=[str(e) for e in errors],
        )
        persist_task_snapshot(task_id)
        _api.task_store.cleanup()
    except Exception:  # noqa: BLE001 - 收尾失败只影响可见性
        logger.debug("忽略异常（保留原行为）", exc_info=True)


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
            from services import task_service as _api

            _api.log(task_id, msg)
        elif sink is None:
            print(f"{prefix} {msg}")

    return _emit


def run_panel_auto_sync(
    panel_key: str,
    *,
    push: Optional[Callable[[], dict[str, Any]]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """跑一轮「面板 ↔ 本地」凭证自动同步（本地较新 → 推送远端）。

    复用 `push_local_rows` 的完整管线（方向判定 + 推送 + 旧记录清理 + 落库），
    `delete_old` 由 `PANEL_AUTO_SYNC` 按面板决定 —— 新建式面板必须删旧记录
    （每次 upload 新增一条，不删会留重复），覆盖式面板删了也没意义。

    **有实际推送动作**时创建任务记录（`source` 取自注册表）——
    「任务运行」页可见、日志可回看（用户要求 2026-10-07）；纯空转 /
    远端读取失败（下一轮会重试）不建记录。

    `panel_key` 未知时返回空结果而不是抛异常：调度器每 60 秒扫一遍所有
    注册的任务，一条配置错误不该把整个调度线程带崩。
    """
    key = str(panel_key or "").strip().lower()
    meta = PANEL_AUTO_SYNC.get(key)
    if not meta:
        return {
            "panel": key,
            "total": 0,
            "pushed": 0,
            "failed": 0,
            "skipped": 0,
            "items": [],
            "remote_error": f"未登记自动同步的面板: {key}",
        }
    _switch_key, platform, delete_old, source = meta

    push_fn = push or (lambda: _default_panel_push(key, platform, delete_old))
    base_log = log or (lambda msg: print(f"[{key} 维护] {msg}"))

    # push 本身抛异常（外网抖动、认证失败路径意外抛出）不得把调度线程带崩
    # —— 记日志返回空结果，下一周期自然重试（复审建议 S5）。
    try:
        summary = push_fn() or {}
    except Exception as exc:  # noqa: BLE001
        base_log(f"同步异常（本轮跳过）: {type(exc).__name__}: {exc}")
        return {
            "panel": key,
            "total": 0,
            "pushed": 0,
            "failed": 0,
            "skipped": 0,
            "items": [],
            "remote_error": f"{type(exc).__name__}: {exc}",
        }

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
            source=source,
            platform=platform,
            total=pushed + failed,
            meta={"pushed": pushed, "failed": failed, "deleted": deleted, "skipped": skipped},
        )
    log_fn = _make_logger(task_id, log, f"[{key} 维护]")

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


def run_chatgpt2api_auto_sync(
    *,
    push: Optional[Callable[[], dict[str, Any]]] = None,
    log: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """chatgpt2api 自动同步（`run_panel_auto_sync` 的具名入口）。

    保留这个名字是因为它被调度注册与测试直接引用；实现已按面板泛化，
    别的面板走 `run_panel_auto_sync(<key>)`。
    """
    return run_panel_auto_sync("chatgpt2api", push=push, log=log)


def _default_panel_push(panel_key: str, platform: str, delete_old: bool) -> dict[str, Any]:
    """生产推送：直接调用接口层的 push 端点函数（同一份管线）。"""
    from services.panel_push import push_local_rows

    return push_local_rows(panel_key, platform=platform, delete_old=delete_old)


# ── 周期任务自注册（core.scheduler 不认识具体业务，见其模块 docstring）──

from core.scheduler import register_job as _register_job  # noqa: E402

# ⚠️ 这里**曾经**注册过一个 interval 版的自动刷新（`chatgpt_auto_refresh`）。
# 2026-10-31 撤掉了：自动刷新现在由 `services/local_maintenance.py` 的
# `chatgpt_auto_refresh_daily` 承担 —— 用户明确要「设置刷新时间 + 范围
# （只刷失效 / 全部 / 随机 N 个）」，那是**每天定点一次**的语义，而
# interval 是「距上次跑满 24 小时」，每次重启都会把时刻往后推。
# 两个都注册 = 同一个账号被两条链各刷一遍，且其中一条在界面上看不见。
#
# `run_auto_refresh_pass` / `plan_auto_refresh` / `compute_next_attempt_at` /
# `_default_execute` / `get_auto_refresh_interval_seconds` 于同日**删除** ——
# 留着会被下一个人当成活的。
_register_job(
    "chatgpt2api_auto_sync",
    interval_seconds=get_chatgpt2api_auto_sync_interval_seconds,
    runner=run_chatgpt2api_auto_sync,
)

# 其余面板的自动同步各注册一个任务（用户的「不同平台都要一个卡片」——
# 每个卡片背后都得有一个真的跑起来的任务，否则开关只是个摆设）。
# 用默认参数绑定 panel_key，避免闭包捕获循环变量。
def _bind_panel_sync(_panel_key: str):
    def _runner() -> dict[str, Any]:
        return run_panel_auto_sync(_panel_key)

    return _runner


for _panel_key in ("sub2api", "cpa", "grok2api"):
    _register_job(
        f"{_panel_key}_auto_sync",
        interval_seconds=(lambda _k=_panel_key: get_panel_auto_sync_interval_seconds(_k)),
        runner=_bind_panel_sync(_panel_key),
    )


__all__ = [
    "AUTO_REFRESH_STATE_KEY",
    "CHATGPT2API_AUTO_SYNC_INTERVAL_SECONDS",
    "PANEL_AUTO_SYNC",
    "MAX_ATTEMPTS",
    "RETRY_STATUSES",
    "AutoRefreshPlan",
    "get_chatgpt2api_auto_sync_interval_seconds",
    "get_panel_auto_sync_interval_seconds",
    "run_panel_auto_sync",
    "record_auto_refresh_result",
    "run_chatgpt2api_auto_sync",
]
