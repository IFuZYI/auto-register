"""日志清理：按保留期删除历史记录。

用户要求（2026-10-09）：「增加日志清理功能，比如清理1天/周/月前的日志。」

两条数据面共用同一套保留期口径：

- **任务历史**（`task_logs`）：注册任务逐账号的结果记录，「任务历史」页在列。
- **任务运行记录**（`task_runs`）：任务快照，「任务运行」页在列。

保留期一律按 `created_at < 现在 - N 天` 判定。cutoff 必须是 **aware** UTC
datetime：库里的时间列是 aware，naive 值做比较会让 SQLAlchemy 直接抛
`StatementError`（实测），而 UI 传入的是天数、不是时间戳，所以归一放在这里
做一次，调用方不必各自记得。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlmodel import Session, delete

from core.db import TaskLog, TaskRunModel, current_engine

__all__ = ["retention_cutoff", "cleanup_task_logs", "cleanup_task_runs"]

#: 保留期上限（天）。10 年 —— 上界存在的意义是拦住误填的巨数，
#: 不是限制业务：`older_than_days=10**9` 会算出 overflow 的 cutoff。
MAX_RETENTION_DAYS = 3650

#: 「已结束」的任务状态 —— 只有这些行允许被保留期清理。
#: 与 `services/task_store_io._list_persisted_tasks` 的排序档位同源。
FINISHED_TASK_STATUSES = ("done", "failed", "stopped")


def retention_cutoff(older_than_days: int) -> datetime:
    """「保留 N 天」对应的删除界线（aware UTC）。"""
    return datetime.now(timezone.utc) - timedelta(days=int(older_than_days))


def cleanup_task_logs(older_than_days: int) -> int:
    """删除超过保留期的任务历史，返回删除条数。"""
    cutoff = retention_cutoff(older_than_days)
    with Session(current_engine()) as session:
        result = session.exec(delete(TaskLog).where(TaskLog.created_at < cutoff))
        session.commit()
        return int(result.rowcount or 0)


def cleanup_task_runs(older_than_days: int) -> int:
    """删除超过保留期的**已结束**任务运行记录，返回删除条数。

    只清 `done / failed / stopped`：`running / pending` 的行可能正被任务线程
    持有，删掉它下一次 `_persist_task_snapshot` 又会把同一行写回来 ——
    看着「清了」，实际删了个寂寞，还多一次无谓的写入竞争。

    孤儿（库里 `running`、内存里没有）由调用方先走 `_finalize_orphan_tasks()`
    收尾成 `stopped`，否则它们永远不是终态、会被这条清理永久略过 ——
    保留期形同虚设。
    """
    cutoff = retention_cutoff(older_than_days)
    with Session(current_engine()) as session:
        result = session.exec(
            delete(TaskRunModel).where(
                TaskRunModel.created_at < cutoff,
                TaskRunModel.status.in_(FINISHED_TASK_STATUSES),
            )
        )
        session.commit()
        return int(result.rowcount or 0)
