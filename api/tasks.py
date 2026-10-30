from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from typing import Callable, Optional
from copy import deepcopy
from core.db import TaskLog, TaskRunModel, current_engine
from core.time_utils import UtcDatetime
from services.log_cleanup import MAX_RETENTION_DAYS
from core.task_runtime import (
    AttemptOutcome,
    AttemptResult,
    NonRetryableRegisterError,
    RegisterTaskStore,
    SkipCurrentAttemptRequested,
    StopTaskRequested,
)
import time, json, asyncio, threading, logging

# 任务运行时的共享状态与写原语住在服务层（`services/task_service.py`）。
# 这里保留 `_` 前缀别名：路由与测试都按老名字引用，行为一字未改。
from services.task_service import (  # noqa: E402
    CLEANUP_THRESHOLD,
    DEFAULT_REGISTER_RETRY_TIMES,
    MAX_DEAD_END_ROUNDS,
    MAX_FINISHED_TASKS,
    MAX_REGISTER_RETRY_TIMES,
    account_already_registered as _account_already_registered,
    auto_upload_integrations as _auto_upload_integrations,
    log as _log,
    has_active_register_task,
    normalize_register_retry_times,
    save_task_log as _save_task_log,
    save_task_log_sync as _save_task_log_sync,
    task_store as _task_store,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])
logger = logging.getLogger(__name__)



# 「重开也是同样结局」的失败最多连着出现几轮就收手。
#
# 手机注册里这类失败（账号已建好、接码平台一条短信都没收到）以前是一票否决：
# 第一轮撞上就把用户填的重试轮数整个作废，看上去就是「我写了没反应」。可一轮
# 只用了一个号，凭它断定整个号源都被静默拦码证据太薄 —— 再开一轮确认一下，
# 连着两轮同样结局才是真的号源问题，那时候继续只会多几个孤号。




#: 单次注册任务的账号数上限。
#:
#: 没有上界时 `count=1000000` 会建出一个**停不掉**的任务：线程池按 count 提交
#: 一百万个 future，`stop` 请求只置标志位，而已提交的任务还在排队执行；实测
#: 内存涨到 863MB、CPU 37.6%，`DELETE` 因「运行中」被 409 拒绝 —— 用户没有任何
#: UI 手段清掉它。上限的意义不是限制业务，而是保证误填一个大数不会产生
#: 无法回收的任务。
MAX_REGISTER_COUNT = 10000
#: 并发上限。线程池再大也只是排队打上游风控，且每个线程都持有浏览器/会话资源。
MAX_REGISTER_CONCURRENCY = 200


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    # count 必须 >= 1：0 会让 `ThreadPoolExecutor(max_workers=min(concurrency, count))`
    # 抛 `ValueError: max_workers must be greater than 0` —— 而任务记录**已经建好**
    # 了，用户看到的是一个立刻「致命错误」的空任务。负数同理。
    # 上界见 `MAX_REGISTER_COUNT` 的说明：超界给 422 比建出一个失控任务好。
    count: int = Field(default=1, ge=1, le=MAX_REGISTER_COUNT)
    concurrency: int = Field(default=1, ge=1, le=MAX_REGISTER_CONCURRENCY)
    # 整条注册流程失败后再开几轮（每轮都是全新的邮箱/号码/会话）。
    # 0 = 不重试；一次网络抖动、一个二手号就判 FAIL 太浪费。
    register_retry_times: int = DEFAULT_REGISTER_RETRY_TIMES
    register_delay_seconds: float = 0
    proxy: Optional[str] = None
    #: 空串 = 用平台声明的默认执行器（见 `BasePlatform.supported_executors`）。
    #: 写死某个值会替所有平台预选 —— 各平台支持集合不同。
    executor_type: str = ""
    captcha_solver: str = "yescaptcha"
    extra: dict = Field(default_factory=dict)


class TaskLogBatchDeleteRequest(BaseModel):
    ids: list[int]


class LogCleanupRequest(BaseModel):
    """按保留期清理：保留最近 N 天，更早的记录删除。

    上界与清理实现同源（`services/log_cleanup.MAX_RETENTION_DAYS`），
    两份定义会分叉。
    """

    older_than_days: int = Field(ge=1, le=MAX_RETENTION_DAYS)


class BackfillRtTaskRequest(BaseModel):
    """批量补 RT 的任务参数。

    默认串行 + 每个号之间隔几秒：补 RT 会对同一批号连续打 OpenAI 的授权链，
    并发拉满等于主动送风控素材，宁可慢点。
    """

    account_ids: list[str | int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    # 日期筛选与列表接口同口径（「处理当前筛选的 N 个账号」的 N 包含它们）。
    created_at_start: Optional[UtcDatetime] = None
    created_at_end: Optional[UtcDatetime] = None
    only_missing_rt: bool = True
    allow_login: bool = True
    concurrency: int = 1
    delay_seconds: float = 5
    proxy: Optional[str] = None


class Bind2faTaskRequest(BaseModel):
    """批量绑 2FA 的任务参数。

    和补 RT 一样默认串行 + 间隔几秒：绑定链要连着打 OpenAI 的登录/enroll 接口，
    并发拉满只会更快撞上风控。
    """

    account_ids: list[str | int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    # 日期筛选与列表接口同口径（「处理当前筛选的 N 个账号」的 N 包含它们）。
    created_at_start: Optional[UtcDatetime] = None
    created_at_end: Optional[UtcDatetime] = None
    only_missing_2fa: bool = True
    allow_login: bool = True
    concurrency: int = 1
    delay_seconds: float = 5
    proxy: Optional[str] = None


class RefreshTokenTaskRequest(BaseModel):
    """批量刷新 Token 的任务参数（用户要求「选中多个批量刷新token」）。

    刷新走 `refresh_token` 动作：session token 优先，OAuth RT 兜底，最后
    登录链。默认串行 + 间隔几秒 —— 连着打 OpenAI 的会话/授权端点容易触发风控。
    """

    account_ids: list[str | int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    # 日期筛选与列表接口同口径（「处理当前筛选的 N 个账号」的 N 包含它们）。
    created_at_start: Optional[UtcDatetime] = None
    created_at_end: Optional[UtcDatetime] = None
    concurrency: int = 1
    delay_seconds: float = 5
    proxy: Optional[str] = None


#: 哪些状态算「可恢复」：凭证失效（重登可能救回）/ 过期 / 限流（额度会重置）。
#: `banned` **不在里面** —— 那是终局结论，恢复链只会被同样拒绝，白打上游。
RECOVERABLE_STATUSES = frozenset({"invalid", "expired", "limited"})


class RecoverAccountsTaskRequest(BaseModel):
    """**异常账号尝试恢复**的任务参数（用户要求 2026-10-30）。

    与「刷新 Token」的区别：那个是用户点一次就跑一次；这个是**恢复语义** ——
    专门挑 `invalid` / `expired` / `limited` 这些「凭证或额度出问题、但理论上
    还有救」的账号，逐个试一次，**失败的按退避计数、达到上限就落禁用**
    （用户原话：「若不成功则禁用」）。

    `account_ids` 为空时按 `statuses` 自动挑（默认三种可恢复状态）。
    """

    account_ids: list[str | int] = Field(default_factory=list)
    #: 自动挑选的状态集合；`account_ids` 给了就忽略它。
    statuses: list[str] = Field(
        default_factory=lambda: sorted(RECOVERABLE_STATUSES)
    )
    #: 只挑「还没被禁用重试」的（`chatgpt_auto_refresh.disabled=True` 的跳过）。
    skip_exhausted: bool = True
    #: 单次最多恢复多少个（防止误点把 400 个号全打一遍上游）。
    limit: int = Field(default=50, ge=1, le=500)
    concurrency: int = Field(default=1, ge=1, le=8)
    delay_seconds: float = Field(default=5, ge=0)
    proxy: Optional[str] = None


class BatchActionTaskRequest(BaseModel):
    """**通用**批量动作任务参数。

    一个入口覆盖所有「批量点按钮」的场景：上传到面板、同步面板状态、
    Plus 试用检测……不必为每个动作再写一个任务端点。

    与 `_run_refresh_token` 那类专用任务的区别：这里不内置任何动作知识，
    动作 id 与参数都由调用方给，执行走 `execute_action`（与单账号按钮
    同一条实现）。
    """

    platform: str
    action_id: str
    #: 动作参数（面板地址 / 口令等；留空时动作自己回落全局配置）
    params: dict = Field(default_factory=dict)
    #: 任务页显示的标签（缺省时用动作 id）
    action_label: str = ""

    account_ids: list[str | int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    created_at_start: Optional[UtcDatetime] = None
    created_at_end: Optional[UtcDatetime] = None
    concurrency: int = 1
    delay_seconds: float = 1
    proxy: Optional[str] = None


class ProbeStatusTaskRequest(BaseModel):
    """批量检测本地状态的任务参数。

    与「补 RT」「刷新 Token」同款：勾选优先，否则按列表筛选范围。
    `probe_local_status` 要真打一次上游请求（每个号几秒），几百个号同步跑
    会把请求挂住几分钟且界面无反馈 —— 所以改成任务。
    """

    account_ids: list[str | int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    created_at_start: Optional[UtcDatetime] = None
    created_at_end: Optional[UtcDatetime] = None
    concurrency: int = 1
    delay_seconds: float = 1
    proxy: Optional[str] = None


# ── 拆包后的重导出：测试按名字 patch 这些目标，必须仍能从 api.tasks 取到 ──
from services.task_store_io import (  # noqa: E402,F401
    _ensure_task_exists,
    _ensure_task_mutable,
    _finalize_orphan_tasks,
    _get_persisted_task,
    _get_task_snapshot,
    _json_dumps,
    _json_loads,
    _list_persisted_tasks,
    _normalize_snapshot,
    _persist_task_snapshot,
    _task_run_to_snapshot,
    _to_datetime,
    _to_epoch_seconds,
    _upsert_task_run,
    _utcnow,
)
from services.task_runners import (  # noqa: E402,F401
    _load_account_fields,
    _run_account_batch_task,
    _run_backfill_rt,
    _run_batch_action,
    _run_bind_2fa,
    _run_probe_status,
    _run_recover_accounts,
    _run_refresh_token,
    _run_register,
)


def _prepare_register_request(req: RegisterTaskRequest) -> RegisterTaskRequest:
    from core.config_store import config_store
    from core.registry import is_platform_enabled

    req_data = req.model_dump()
    req_data["extra"] = deepcopy(req_data.get("extra") or {})
    prepared = RegisterTaskRequest(**req_data)
    prepared.platform = str(prepared.platform or "").strip().lower()

    if not is_platform_enabled(prepared.platform):
        raise HTTPException(400, f"{prepared.platform} 平台已下线，不再支持注册")

    return prepared


def _create_task_record(
    task_id: str, req: RegisterTaskRequest, source: str, meta: dict | None = None
):
    _task_store.create(
        task_id,
        platform=req.platform,
        total=req.count,
        source=source,
        meta=meta,
    )
    _persist_task_snapshot(task_id)


def enqueue_register_task(
    req: RegisterTaskRequest,
    *,
    background_tasks: BackgroundTasks | None = None,
    source: str = "manual",
    meta: dict | None = None,
) -> str:
    prepared = _prepare_register_request(req)
    task_id = f"task_{int(time.time() * 1000)}"
    _create_task_record(task_id, prepared, source, meta)
    if background_tasks is None:
        thread = threading.Thread(
            target=_run_register, args=(task_id, prepared), daemon=True
        )
        thread.start()
    else:
        background_tasks.add_task(_run_register, task_id, prepared)
    return task_id












@router.post("/backfill-rt")
def create_backfill_rt_task(req: BackfillRtTaskRequest, background_tasks: BackgroundTasks):
    """批量给缺 refresh_token 的 ChatGPT 账号补 RT。"""
    from core.db import platform_session
    from services.chatgpt_rt_backfill import select_backfill_targets

    with platform_session("chatgpt") as s:
        try:
            accounts, missing_ids = select_backfill_targets(
                s,
                account_ids=req.account_ids,
                all_filtered=req.all_filtered,
                email=req.email,
                status=req.status,
                plus_status=req.plus_status,
                created_at_start=req.created_at_start,
                created_at_end=req.created_at_end,
                only_missing_rt=req.only_missing_rt,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        account_ids = [str(row.id) for row in accounts if row.id]

    if not account_ids:
        if missing_ids:
            detail = "所选账号不存在"
        elif req.only_missing_rt:
            detail = "所选账号都已经有 RT 了"
        else:
            detail = "没有匹配的账号"
        raise HTTPException(400, detail)

    task_id = f"backfill_rt_{int(time.time() * 1000)}"
    _task_store.create(
        task_id,
        platform="chatgpt",
        total=len(account_ids),
        source="backfill_rt",
        meta={
            "kind": "backfill_rt",
            "only_missing_rt": req.only_missing_rt,
            "allow_login": req.allow_login,
            "concurrency": req.concurrency,
            "delay_seconds": req.delay_seconds,
            "missing_ids": missing_ids,
        },
    )
    _persist_task_snapshot(task_id)
    _log(task_id, f"待补 RT 账号 {len(account_ids)} 个")
    if missing_ids:
        _log(task_id, f"忽略不存在的账号: {missing_ids}")
    background_tasks.add_task(_run_backfill_rt, task_id, account_ids, req)
    return {"task_id": task_id, "total": len(account_ids), "missing_ids": missing_ids}


@router.post("/batch-action")
def create_batch_action_task(
    req: BatchActionTaskRequest, background_tasks: BackgroundTasks
):
    """**通用**批量动作任务：把任意「批量点按钮」的动作跑成任务。

    覆盖上传到面板、同步面板状态、Plus 试用检测等 —— 新增动作不必再写任务
    端点。执行走 `execute_action`（与单账号按钮同一条实现），结果经
    `_apply_action_result` 落库，状态策略只有一份。

    校验：平台必须已注册、动作必须在该平台上存在（否则明确 400/404，
    而不是建一个跑起来只会全部失败的任务）。
    """
    from core.base_platform import RegisterConfig
    from core.config_store import config_store as _cfg
    from core.db import platform_session
    from core.registry import get, list_platforms
    from services.chatgpt_rt_backfill import select_backfill_targets

    platform = str(req.platform or "").strip().lower()
    action_id = str(req.action_id or "").strip()
    if not platform or not action_id:
        raise HTTPException(400, "缺少 platform 或 action_id")

    if not any(str(p.get("name") or "").strip().lower() == platform for p in list_platforms()):
        raise HTTPException(404, f"平台 {platform} 未注册")

    try:
        PlatformCls = get(platform)
    except Exception as exc:  # noqa: BLE001 - 平台加载失败按未知处理
        raise HTTPException(404, f"平台 {platform} 不可用: {exc}") from exc

    instance = PlatformCls(config=RegisterConfig(extra=_cfg.get_all()))
    known = {str(a.get("id") or "") for a in instance.get_platform_actions()}
    if action_id not in known:
        raise HTTPException(
            400,
            f"平台 {platform} 不支持操作 {action_id}（可用: {sorted(known)}）",
        )

    with platform_session(platform) as s:
        try:
            accounts, missing_ids = select_backfill_targets(
                s,
                # **必须传 platform**：选择函数原来硬编码 chatgpt，grok 的
                # 批量动作永远「所选账号不存在」（实测 2026-10-28）。
                platform=platform,
                account_ids=req.account_ids,
                all_filtered=req.all_filtered,
                email=req.email,
                status=req.status,
                plus_status=req.plus_status,
                created_at_start=req.created_at_start,
                created_at_end=req.created_at_end,
                # 批量动作与「有没有 RT」无关 —— 全量都要处理
                only_missing_rt=False,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    account_ids = [str(row.id) for row in accounts if row.id]
    if not account_ids:
        raise HTTPException(400, "所选账号不存在" if missing_ids else "没有匹配的账号")

    label = str(req.action_label or action_id)
    task_id = f"action_{int(time.time() * 1000)}"
    _task_store.create(
        task_id,
        platform=platform,
        total=len(account_ids),
        source=f"action:{action_id}",
        meta={
            "kind": "batch_action",
            "action_id": action_id,
            "action_label": label,
            "concurrency": req.concurrency,
            "delay_seconds": req.delay_seconds,
            "missing_ids": missing_ids,
        },
    )
    _persist_task_snapshot(task_id)
    _log(task_id, f"{label}：待处理账号 {len(account_ids)} 个")
    if missing_ids:
        _log(task_id, f"忽略不存在的账号: {missing_ids}")
    background_tasks.add_task(_run_batch_action, task_id, account_ids, req)
    return {"task_id": task_id, "total": len(account_ids), "missing_ids": missing_ids}


@router.post("/probe-status")
def create_probe_status_task(
    req: ProbeStatusTaskRequest, background_tasks: BackgroundTasks
):
    """批量检测本地状态（跑成任务，可停可看日志）。

    与「补 RT」「刷新 Token」同一套骨架：解析目标 → 建任务 → 后台跑。
    单账号按钮仍走 `/actions/{platform}/probe_local_status`（即时返回），
    两条路径共用 `_apply_action_result` 落状态 —— 判定口径不会漂移。
    """
    from core.db import platform_session
    from services.chatgpt_rt_backfill import select_backfill_targets

    with platform_session("chatgpt") as s:
        try:
            accounts, missing_ids = select_backfill_targets(
                s,
                account_ids=req.account_ids,
                all_filtered=req.all_filtered,
                email=req.email,
                status=req.status,
                plus_status=req.plus_status,
                created_at_start=req.created_at_start,
                created_at_end=req.created_at_end,
                # 检测状态与「有没有 RT」无关 —— 全量都要检测
                only_missing_rt=False,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        account_ids = [str(row.id) for row in accounts if row.id]

    if not account_ids:
        raise HTTPException(400, "所选账号不存在" if missing_ids else "没有匹配的账号")

    task_id = f"probe_status_{int(time.time() * 1000)}"
    _task_store.create(
        task_id,
        platform="chatgpt",
        total=len(account_ids),
        source="probe_status",
        meta={
            "kind": "probe_status",
            "concurrency": req.concurrency,
            "delay_seconds": req.delay_seconds,
            "missing_ids": missing_ids,
        },
    )
    _persist_task_snapshot(task_id)
    _log(task_id, f"待检测状态账号 {len(account_ids)} 个")
    if missing_ids:
        _log(task_id, f"忽略不存在的账号: {missing_ids}")
    background_tasks.add_task(_run_probe_status, task_id, account_ids, req)
    return {"task_id": task_id, "total": len(account_ids), "missing_ids": missing_ids}


@router.post("/bind-2fa")
def create_bind_2fa_task(req: Bind2faTaskRequest, background_tasks: BackgroundTasks):
    """给库里已有的 ChatGPT 账号补绑 TOTP 2FA。"""
    from core.db import platform_session
    from services.chatgpt_two_factor import select_two_factor_targets

    with platform_session("chatgpt") as s:
        try:
            accounts, missing_ids = select_two_factor_targets(
                s,
                account_ids=req.account_ids,
                all_filtered=req.all_filtered,
                email=req.email,
                status=req.status,
                plus_status=req.plus_status,
                created_at_start=req.created_at_start,
                created_at_end=req.created_at_end,
                only_missing_2fa=req.only_missing_2fa,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        account_ids = [str(row.id) for row in accounts if row.id]

    if not account_ids:
        if missing_ids:
            detail = "所选账号不存在"
        elif req.only_missing_2fa:
            detail = "所选账号都已经有 2FA 密钥了"
        else:
            detail = "没有匹配的账号"
        raise HTTPException(400, detail)

    task_id = f"bind_2fa_{int(time.time() * 1000)}"
    _task_store.create(
        task_id,
        platform="chatgpt",
        total=len(account_ids),
        source="bind_2fa",
        meta={
            "kind": "bind_2fa",
            "only_missing_2fa": req.only_missing_2fa,
            "allow_login": req.allow_login,
            "concurrency": req.concurrency,
            "delay_seconds": req.delay_seconds,
            "missing_ids": missing_ids,
        },
    )
    _persist_task_snapshot(task_id)
    _log(task_id, f"待绑 2FA 账号 {len(account_ids)} 个")
    if missing_ids:
        _log(task_id, f"忽略不存在的账号: {missing_ids}")
    background_tasks.add_task(_run_bind_2fa, task_id, account_ids, req)
    return {"task_id": task_id, "total": len(account_ids), "missing_ids": missing_ids}


@router.post("/refresh-token")
def create_refresh_token_task(req: RefreshTokenTaskRequest, background_tasks: BackgroundTasks):
    """批量刷新 ChatGPT 账号的 Token（session → OAuth → 登录链）。

    用户要求：「ChatGPT应该可以选中多个批量刷新token」。
    """
    from core.db import platform_session
    from services.chatgpt_account_selection import select_chatgpt_accounts

    with platform_session("chatgpt") as s:
        try:
            accounts, missing_ids = select_chatgpt_accounts(
                s,
                account_ids=req.account_ids,
                all_filtered=req.all_filtered,
                email=req.email,
                status=req.status,
                plus_status=req.plus_status,
                created_at_start=req.created_at_start,
                created_at_end=req.created_at_end,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        account_ids = [str(row.id) for row in accounts if row.id]

    if not account_ids:
        detail = "所选账号不存在" if missing_ids else "没有匹配的账号"
        raise HTTPException(400, detail)

    task_id = f"refresh_token_{int(time.time() * 1000)}"
    _task_store.create(
        task_id,
        platform="chatgpt",
        total=len(account_ids),
        source="refresh_token",
        meta={
            "kind": "refresh_token",
            "concurrency": req.concurrency,
            "delay_seconds": req.delay_seconds,
            "missing_ids": missing_ids,
        },
    )
    _persist_task_snapshot(task_id)
    _log(task_id, f"待刷新 Token 账号 {len(account_ids)} 个")
    if missing_ids:
        _log(task_id, f"忽略不存在的账号: {missing_ids}")
    background_tasks.add_task(_run_refresh_token, task_id, account_ids, req)
    return {"task_id": task_id, "total": len(account_ids), "missing_ids": missing_ids}


@router.post("/recover")
def create_recover_accounts_task(
    req: RecoverAccountsTaskRequest, background_tasks: BackgroundTasks
):
    """**异常账号尝试恢复**（用户要求 2026-10-30）。

    挑出 `invalid` / `expired` / `limited` 的账号，逐个跑一次恢复链
    （refresh_token 的完整路径：session → OAuth → 登录兜底）；**救不回来的
    按退避计数，达到上限就落 `banned`** —— 这就是用户要的「若不成功则禁用」。

    与 `/refresh-token` 的差别：

    - 选号是**按状态自动挑**（不用先筛列表再勾选）；
    - 结果的语义是「恢复」，失败要累计到退避计数器上（`record_auto_refresh_result`），
      所以同一个号连续三次恢复失败会自动禁用，不会无限重试。

    `dry_run` 由 `?preview=1` 走单独的分支（见 `GET /recover/candidates`）。
    """
    from core.db import AccountModel, platform_session
    from services.chatgpt_account_selection import normalize_account_ids

    wanted_ids = [str(i) for i in normalize_account_ids(req.account_ids)] if req.account_ids else []
    statuses = {str(s or "").strip().lower() for s in (req.statuses or []) if str(s or "").strip()}
    # **显式传了 statuses 就以它为准**（哪怕交集为空 → 没有可恢复的号 → 400）。
    # 之前写成 `statuses & RECOVERABLE or 默认三种`：传 `statuses=["banned"]`
    # 时交集为空 → 回退到默认三种 → 反而把 invalid 号挑出来跑了。显式指定
    # 却被静默换成默认值，是最难查的那类「参数不起作用」。
    if statuses:
        allowed = statuses & RECOVERABLE_STATUSES
    else:
        allowed = set(RECOVERABLE_STATUSES)

    with platform_session("chatgpt") as s:
        rows = s.exec(
            select(AccountModel).where(AccountModel.platform == "chatgpt")
        ).all()

    candidates: list[str] = []
    skipped_exhausted = 0
    for row in rows:
        if wanted_ids and str(row.id) not in wanted_ids:
            continue
        if not wanted_ids and str(row.status or "").strip().lower() not in allowed:
            continue
        if req.skip_exhausted:
            state = (row.get_extra() or {}).get("chatgpt_auto_refresh")
            if isinstance(state, dict) and state.get("disabled"):
                skipped_exhausted += 1
                continue
        candidates.append(str(row.id))

    if not candidates:
        raise HTTPException(
            400,
            "没有需要恢复的账号"
            + (f"（{skipped_exhausted} 个已达重试上限，已跳过）" if skipped_exhausted else ""),
        )

    candidates = candidates[: int(req.limit)]
    task_id = f"recover_{int(time.time() * 1000)}"
    _task_store.create(
        task_id,
        platform="chatgpt",
        total=len(candidates),
        source="recover",
        meta={
            "kind": "recover",
            "statuses": sorted(allowed),
            "skipped_exhausted": skipped_exhausted,
        },
    )
    _persist_task_snapshot(task_id)
    _log(task_id, f"待恢复账号 {len(candidates)} 个（状态: {', '.join(sorted(allowed))}）")
    if skipped_exhausted:
        _log(task_id, f"跳过 {skipped_exhausted} 个已达重试上限的账号")
    background_tasks.add_task(
        _run_recover_accounts,
        task_id,
        candidates,
        req,
    )
    return {
        "task_id": task_id,
        "total": len(candidates),
        "skipped_exhausted": skipped_exhausted,
    }


@router.get("/recover/candidates")
def preview_recover_candidates():
    """预览「异常账号尝试恢复」会挑中哪些账号（不改任何状态）。"""
    from core.db import AccountModel, platform_session

    allowed = {"invalid", "expired", "limited"}
    with platform_session("chatgpt") as s:
        rows = s.exec(
            select(AccountModel).where(AccountModel.platform == "chatgpt")
        ).all()

    by_status: dict[str, int] = {}
    exhausted = 0
    items: list[dict] = []
    for row in rows:
        status = str(row.status or "").strip().lower()
        state = (row.get_extra() or {}).get("chatgpt_auto_refresh")
        is_exhausted = isinstance(state, dict) and bool(state.get("disabled"))
        # 「用尽」要**独立于状态**统计：老数据里失败的号还是 invalid
        # （旧实现只置 disabled、不改状态），新数据里已经落 banned ——
        # 只看可恢复状态的话，后者永远数不到，界面上会显示「0 个已放弃」。
        if is_exhausted:
            exhausted += 1
            if status not in allowed:
                continue
        if status not in allowed:
            continue
        by_status[status] = by_status.get(status, 0) + 1
        if is_exhausted:
            continue
        items.append(
            {
                "id": str(row.id),
                "email": row.email,
                "status": status,
                "attempts": int((state or {}).get("attempts") or 0)
                if isinstance(state, dict)
                else 0,
            }
        )
    return {
        "recoverable": len(items),
        "exhausted": exhausted,
        "by_status": by_status,
        "items": items[:100],
    }


@router.post("/register")
def create_register_task(
    req: RegisterTaskRequest,
    background_tasks: BackgroundTasks,
):
    task_id = enqueue_register_task(req, background_tasks=background_tasks)
    return {"task_id": task_id}


@router.post("/{task_id}/skip-current")
def skip_current_account(task_id: str):
    _finalize_orphan_tasks()
    _ensure_task_mutable(task_id)
    if not _task_store.exists(task_id):
        raise HTTPException(409, "任务已结束或服务已重启，无法跳过当前账号")
    control = _task_store.request_skip_current(task_id)
    _log(task_id, "收到手动跳过当前账号请求")
    return {"ok": True, "task_id": task_id, "control": control}


@router.post("/{task_id}/stop")
def stop_task(task_id: str):
    _finalize_orphan_tasks()
    _ensure_task_mutable(task_id)
    if not _task_store.exists(task_id):
        raise HTTPException(409, "任务已结束或服务已重启，无法停止")
    control = _task_store.request_stop(task_id)
    _log(task_id, "收到手动停止任务请求")
    return {"ok": True, "task_id": task_id, "control": control}


@router.get("/logs")
def get_logs(platform: str = None, page: int = 1, page_size: int = 50):
    # 与账号列表同样的上界：日志行比账号行更重（detail_json 里带完整步骤）
    page_size = min(max(1, int(page_size or 50)), 500)
    # **两种后端返回同一形状**（dict 列表）。SQLite 版原来返回 `TaskLog`
    # 模型、JSON 版返回 dict —— 上层要按后端写两套取值逻辑，很容易漏。
    from core.db.task_log_repository import task_log_repository

    from_json = task_log_repository.list_json(
        page=page, page_size=page_size, platform=platform or ""
    )
    if from_json is not None:
        return from_json

    from core.db.task_log_repository import _from_sqlite

    with Session(current_engine()) as s:
        q = select(TaskLog)
        if platform:
            q = q.where(TaskLog.platform == platform)
        q = q.order_by(TaskLog.id.desc())
        total = len(s.exec(q).all())
        items = s.exec(q.offset((page - 1) * page_size).limit(page_size)).all()
    return {"total": total, "items": [_from_sqlite(row) for row in items]}


@router.post("/logs/batch-delete")
def batch_delete_logs(body: TaskLogBatchDeleteRequest):
    if not body.ids:
        raise HTTPException(400, "任务历史 ID 列表不能为空")

    unique_ids = list(dict.fromkeys(body.ids))
    if len(unique_ids) > 1000:
        raise HTTPException(400, "单次最多删除 1000 条任务历史")

    from core.db.task_log_repository import task_log_repository

    from_json = task_log_repository.delete_many_json(unique_ids)
    if from_json is not None:
        logger.info("批量删除任务历史成功: %s 条", from_json["deleted"])
        return from_json

    with Session(current_engine()) as s:
        try:
            logs = s.exec(select(TaskLog).where(TaskLog.id.in_(unique_ids))).all()
            found_ids = {log.id for log in logs if log.id is not None}

            for log in logs:
                s.delete(log)

            s.commit()
            deleted_count = len(found_ids)
            not_found_ids = [log_id for log_id in unique_ids if log_id not in found_ids]
            logger.info("批量删除任务历史成功: %s 条", deleted_count)

            return {
                "deleted": deleted_count,
                "not_found": not_found_ids,
                "total_requested": len(unique_ids),
            }
        except Exception as e:
            s.rollback()
            logger.exception("批量删除任务历史失败")
            raise HTTPException(500, f"批量删除任务历史失败: {str(e)}")


@router.post("/logs/cleanup")
def cleanup_logs(body: LogCleanupRequest):
    """按保留期清理任务历史（`task_logs`）。

    用户要求：「增加日志清理功能，比如清理1天/周/月前的日志。」
    保留期由前端给天数（1 / 7 / 30），删除界线在服务端归一为 aware UTC ——
    UI 发的是天数，不该要求调用方自己算时间戳。
    """
    from services.log_cleanup import cleanup_task_logs, retention_cutoff

    deleted = cleanup_task_logs(body.older_than_days)
    logger.info("清理任务历史成功: %s 条（保留 %s 天）", deleted, body.older_than_days)
    return {
        "deleted": deleted,
        "older_than_days": body.older_than_days,
        "cutoff": retention_cutoff(body.older_than_days).isoformat(),
    }


@router.post("/runs/cleanup")
def cleanup_runs(body: LogCleanupRequest):
    """按保留期清理任务运行记录（`task_runs`）。

    先收尾孤儿再清理：崩溃留下的 `running` 行在内存里没有对应记录，
    不收尾就永远不是终态、会被「只清已结束」的规则永久略过。
    """
    from services.log_cleanup import cleanup_task_runs, retention_cutoff

    _finalize_orphan_tasks()
    deleted = cleanup_task_runs(body.older_than_days)
    logger.info("清理任务运行记录成功: %s 条（保留 %s 天）", deleted, body.older_than_days)
    return {
        "deleted": deleted,
        "older_than_days": body.older_than_days,
        "cutoff": retention_cutoff(body.older_than_days).isoformat(),
    }


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    """SSE 实时日志流"""
    _finalize_orphan_tasks()
    _ensure_task_exists(task_id)

    async def event_generator():
        sent = since
        use_memory = _task_store.exists(task_id)
        while True:
            if use_memory:
                logs, status = _task_store.log_state(task_id)
                snapshot = _task_store.snapshot(task_id)
                _persist_task_snapshot(task_id)
            else:
                snapshot = _get_persisted_task(task_id) or {}
                logs = snapshot.get("logs") or []
                status = snapshot.get("status") or "failed"
            counters = {
                "success": int(snapshot.get("success") or 0),
                "registered": int(snapshot.get("registered") or 0),
                "total": int(snapshot.get("total") or 0),
            }
            while sent < len(logs):
                yield f"data: {json.dumps({'line': logs[sent], **counters})}\n\n"
                sent += 1
            if status in ("done", "failed", "stopped"):
                yield f"data: {json.dumps({'done': True, 'status': status, **counters})}\n\n"
                break
            if not use_memory:
                # 非内存任务仅提供持久化快照，不进入无限轮询
                yield f"data: {json.dumps({'done': True, 'status': 'stopped', **counters})}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}")
def get_task(task_id: str):
    _finalize_orphan_tasks()
    return _get_task_snapshot(task_id)


@router.get("")
def list_tasks(include_logs: bool = False, request: Request = None):
    """任务列表。

    **默认不返回已结束任务的日志**（`?include_logs=1` 可要全量）：列表被前端
    每 2.5 秒轮询一次，历史日志占了一半以上负载，且会让页面卡顿。历史任务的
    日志走 `GET /tasks/{id}`。

    响应带 `ETag`：内容没变时浏览器带回 `If-None-Match`，这里直接 304，
    **不传响应体**。这个端点一次要序列化 800+ 个任务（实测 373 KB），走
    SSH 隧道 / 公网时那 373 KB 就是「切个页面卡一下」的来源 —— 轮询里
    99% 的时候内容没变，没必要重传。

    不做 TTL 缓存：任务状态随时会变，缓存一秒都可能让刚结束的任务还显示
    「运行中」（`tests/test_tasks_list_perf.py` 就是这么挂的）。ETag 不改变
    语义 —— 内容一变，ETag 就变。
    """
    import hashlib
    import json as _json

    _finalize_orphan_tasks()
    # 以 DB 为主返回，避免进程重启导致列表丢失
    body = _list_persisted_tasks(include_logs=include_logs)
    etag = '"' + hashlib.md5(
        _json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest() + '"'

    if request is not None and request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    return JSONResponse(content=body, headers={"ETag": etag, "Cache-Control": "no-cache"})


@router.delete("/{task_id}")
def delete_task(task_id: str):
    _finalize_orphan_tasks()
    snapshot = _get_task_snapshot(task_id)
    status = str(snapshot.get("status") or "")
    if status in {"pending", "running"}:
        raise HTTPException(409, "运行中的任务不允许删除，请先停止任务")
    with Session(current_engine()) as s:
        row = s.get(TaskRunModel, task_id)
        if row is not None:
            s.delete(row)
            s.commit()
    return {"ok": True, "task_id": task_id}
