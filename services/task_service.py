"""任务运行时的共享状态与写入原语。

## 为什么单独一个模块

这些名字原来住在 `api/tasks.py`（902 行的路由模块）里，而 `services/task_runners.py`
与 `services/chatgpt_maintenance.py` 需要它们 —— 服务层只能反向
反向从 api 层取，每次调用都延迟 import 一次（共 114 处）。
依赖方向是反的：服务层认识 HTTP 层，只为了拿一个内存字典和三个写函数。

搬到这里之后方向恢复成 api → services。

## 里面有什么

* `task_store` —— 内存任务表单例（`RegisterTaskStore`）。**只能有一份**：
  api 的查询端点和 runner 的写入必须看到同一个对象，否则「任务列表空、日志有」。
* `log` / `save_task_log` / `save_task_log_sync` —— 三个写原语。
* `account_already_registered` / `auto_upload_integrations` —— 注册收尾用的判重与自动上传。
* 注册重试次数的归一与上限常量。

`persist_task_snapshot` 不在这里：它属于「内存快照 ↔ task_runs 表」的搬运，
住在 `services/task_store_io.py`。本模块调用它时**延迟 import**，
否则会形成 `task_service → task_store_io → task_service` 的模块级循环
（后者要拿 `task_store` 单例）。
"""

from __future__ import annotations

import json
import logging
import threading
import time

from sqlmodel import Session

from core.db import TaskLog, current_engine
from core.task_runtime import RegisterTaskStore

logger = logging.getLogger(__name__)

MAX_FINISHED_TASKS = 200
CLEANUP_THRESHOLD = 250

#: 内存任务表。**全进程唯一**：api 的列表/详情端点与 runner 的写入共用它。
task_store = RegisterTaskStore(
    max_finished_tasks=MAX_FINISHED_TASKS,
    cleanup_threshold=CLEANUP_THRESHOLD,
)

MAX_REGISTER_RETRY_TIMES = 10
DEFAULT_REGISTER_RETRY_TIMES = 1

#: 「重开也是同样结局」的失败最多连着出现几轮就收手。
#:
#: 这类失败（账号已建好、接码平台一条短信都没收到）以前是一票否决：第一轮撞上
#: 就把用户填的重试轮数整个作废，看上去就是「我写了没反应」。可一轮只用了一个号，
#: 凭它断定整个号源都被静默拦码证据太薄 —— 再开一轮确认一下，连着两轮同样结局
#: 才是真的号源问题，那时候继续只会多几个孤号。
MAX_DEAD_END_ROUNDS = 2


def has_active_register_task(
    *, platform: str | None = None, source: str | None = None
) -> bool:
    """当前是否有未结束的注册任务（同平台/同来源可选过滤）。

    注册入口用它做互斥：同一来源已经在跑时不再开第二个，否则两个任务抢同一批
    邮箱与代理。`data_bundle` 的导出也用它判断「能不能安全打包」。
    """
    return task_store.has_active(platform=platform, source=source)


def normalize_register_retry_times(value) -> int:
    """空值按默认算，越界夹回去 —— 这个数字来自表单和配置项，什么都可能填。"""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_REGISTER_RETRY_TIMES
    return max(0, min(parsed, MAX_REGISTER_RETRY_TIMES))


def log(task_id: str, msg: str) -> None:
    """向任务追加一条日志。

    stdout 用 `safe_print`：服务被 `... | tee` 或编辑器终端拉起时，那个进程
    一走 stdout 读端就消失，裸 print 会抛 BrokenPipeError —— 任务线程里会把它
    当成注册失败，收尾路径再抛一次还会跳过 finish() 让任务卡在 running
    （详见 core/console.py）。
    """
    from core.console import safe_print
    from services.task_store_io import persist_task_snapshot

    ts = time.strftime("%H:%M:%S")
    entry = f"[{ts}] {msg}"
    task_store.append_log(task_id, entry)
    persist_task_snapshot(task_id)
    safe_print(entry)


def save_task_log(
    platform: str, email: str, status: str, error: str = "", detail: dict | None = None
) -> None:
    """写一条任务历史（fire-and-forget，不阻塞调用方）。"""

    def _write() -> None:
        save_task_log_sync(platform, email, status, error=error, detail=detail)

    threading.Thread(target=_write, daemon=True).start()


def save_task_log_sync(
    platform: str, email: str, status: str, error: str = "", detail: dict | None = None
) -> dict:
    """同步写一条任务历史（测试与 JSON 后端用；生产走线程版）。

    JSON 后端下写路径要能**同步**返回刚写的行（调用方拿 id），而
    `save_task_log` 是 fire-and-forget（起线程、不返回）。
    """
    payload = {
        "platform": platform,
        "email": email,
        "status": status,
        "error": error,
        "detail": detail or {},
    }
    from core.db.task_log_repository import task_log_repository

    written = task_log_repository.add_json(payload)
    if written is not None:
        return written

    with Session(current_engine()) as s:
        entry = TaskLog(
            platform=platform,
            email=email,
            status=status,
            error=error,
            detail_json=json.dumps(detail or {}, ensure_ascii=False),
        )
        s.add(entry)
        s.commit()
        s.refresh(entry)
        return {
            "id": entry.id,
            "platform": entry.platform,
            "email": entry.email,
            "status": entry.status,
            "error": entry.error,
            "detail_json": entry.detail_json,
            "created_at": entry.created_at,
        }


def account_already_registered(platform: str, email: str) -> bool:
    """该 (平台, 邮箱) 是否已注册过。

    邮箱是账号唯一业务键；命中即跳过，避免重复注册同一个邮箱。
    查不到或库不可用时返回 False —— 宁可多注册一次，也不要因为判重故障
    把正常任务全卡死。
    """
    try:
        from core.db import account_repository

        return account_repository.is_registered(platform, email)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning("判重失败（按未注册处理）: %s", exc)
        return False


def auto_upload_integrations(task_id: str, account) -> None:
    """注册成功后自动导入外部系统（后台线程，不阻塞注册流程）。"""

    def _run() -> None:
        try:
            from services.external_sync import sync_account

            for result in sync_account(account):
                name = result.get("name", "Auto Upload")
                ok = bool(result.get("ok"))
                msg = result.get("msg", "")
                log(task_id, f"  [{name}] {'[OK] ' + msg if ok else '[FAIL] ' + msg}")
        except Exception as exc:
            log(task_id, f"  [Auto Upload] 自动导入异常: {exc}")

    threading.Thread(target=_run, daemon=True).start()


__all__ = [
    "DEFAULT_REGISTER_RETRY_TIMES",
    "MAX_DEAD_END_ROUNDS",
    "MAX_FINISHED_TASKS",
    "MAX_REGISTER_RETRY_TIMES",
    "CLEANUP_THRESHOLD",
    "account_already_registered",
    "auto_upload_integrations",
    "has_active_register_task",
    "log",
    "normalize_register_retry_times",
    "save_task_log",
    "save_task_log_sync",
    "task_store",
]
