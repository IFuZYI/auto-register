"""任务快照的持久化 I/O（原 api/tasks.py 的 126–347 行）。

拆出来的理由：这一整段是「内存快照 ↔ task_runs 表」的纯搬运，与注册业务无关。
`api/tasks.py` 通过 `from services.task_store_io import …` 继续暴露这些名字 ——
测试对 `_persist_task_snapshot` 等做 patch，模块级名字必须还在。

`_finalize_orphan_tasks` 需要 api.tasks 的 `_task_store` 单例，用函数内延迟 import
打破循环（本模块 ← api.tasks 模块级导入）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import or_
from sqlmodel import Session, select

from core.db import TaskRunModel, current_engine



def _store():
    """延迟取 api.tasks 的 `_task_store` 单例（打破 api.tasks ↔ 本模块的循环）。"""
    from api import tasks as _api

    return _api._task_store


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _json_dumps(value, fallback):
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return json.dumps(fallback, ensure_ascii=False)


def _json_loads(raw: str, fallback):
    try:
        return json.loads(raw or "")
    except Exception:
        return fallback


def _to_epoch_seconds(value) -> float:
    if isinstance(value, datetime):
        return value.timestamp()
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def _to_datetime(value) -> datetime:
    try:
        ts = float(value or 0)
        if ts > 1_000_000_000_000:
            ts /= 1000
        if ts <= 0:
            return _utcnow()
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except Exception:
        return _utcnow()


def _normalize_snapshot(snapshot: dict) -> dict:
    return {
        "id": str(snapshot.get("id") or ""),
        "status": str(snapshot.get("status") or "pending"),
        "platform": str(snapshot.get("platform") or ""),
        "source": str(snapshot.get("source") or "manual"),
        "meta": snapshot.get("meta") if isinstance(snapshot.get("meta"), dict) else {},
        "total": int(snapshot.get("total") or 0),
        "progress": str(snapshot.get("progress") or "0/0"),
        "logs": snapshot.get("logs") if isinstance(snapshot.get("logs"), list) else [],
        "success": int(snapshot.get("success") or 0),
        "registered": int(snapshot.get("registered") or 0),
        "skipped": int(snapshot.get("skipped") or 0),
        "errors": snapshot.get("errors") if isinstance(snapshot.get("errors"), list) else [],
        "control": snapshot.get("control") if isinstance(snapshot.get("control"), dict) else {},
        "cashier_urls": snapshot.get("cashier_urls") if isinstance(snapshot.get("cashier_urls"), list) else [],
        "error": str(snapshot.get("error") or ""),
        "created_at": _to_epoch_seconds(snapshot.get("created_at")),
        "updated_at": _to_epoch_seconds(snapshot.get("updated_at")),
    }


def _task_run_to_snapshot(row: TaskRunModel) -> dict:
    return _normalize_snapshot(
        {
            "id": row.id,
            "status": row.status,
            "platform": row.platform,
            "source": row.source,
            "meta": _json_loads(row.meta_json, {}),
            "total": row.total,
            "progress": row.progress,
            "logs": _json_loads(row.logs_json, []),
            "success": row.success,
            "registered": row.registered,
            "skipped": row.skipped,
            "errors": _json_loads(row.errors_json, []),
            "control": _json_loads(row.control_json, {}),
            "cashier_urls": _json_loads(row.cashier_urls_json, []),
            "error": row.error,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }
    )


def _upsert_task_run(snapshot: dict) -> None:
    normalized = _normalize_snapshot(snapshot)
    if not normalized["id"]:
        return
    with Session(current_engine()) as s:
        row = s.get(TaskRunModel, normalized["id"])
        if row is None:
            row = TaskRunModel(
                id=normalized["id"],
                platform=normalized["platform"],
                source=normalized["source"],
                status=normalized["status"],
                total=normalized["total"],
                progress=normalized["progress"],
                success=normalized["success"],
                registered=normalized["registered"],
                skipped=normalized["skipped"],
                error=normalized["error"],
                meta_json=_json_dumps(normalized["meta"], {}),
                logs_json=_json_dumps(normalized["logs"], []),
                errors_json=_json_dumps(normalized["errors"], []),
                cashier_urls_json=_json_dumps(normalized["cashier_urls"], []),
                control_json=_json_dumps(normalized["control"], {}),
                created_at=_to_datetime(normalized["created_at"]),
                updated_at=_to_datetime(normalized["updated_at"]),
            )
            s.add(row)
        else:
            row.platform = normalized["platform"]
            row.source = normalized["source"]
            row.status = normalized["status"]
            row.total = normalized["total"]
            row.progress = normalized["progress"]
            row.success = normalized["success"]
            row.registered = normalized["registered"]
            row.skipped = normalized["skipped"]
            row.error = normalized["error"]
            row.meta_json = _json_dumps(normalized["meta"], {})
            row.logs_json = _json_dumps(normalized["logs"], [])
            row.errors_json = _json_dumps(normalized["errors"], [])
            row.cashier_urls_json = _json_dumps(normalized["cashier_urls"], [])
            row.control_json = _json_dumps(normalized["control"], {})
            if row.created_at is None:
                row.created_at = _to_datetime(normalized["created_at"])
            row.updated_at = _to_datetime(normalized["updated_at"])
            s.add(row)
        s.commit()


def _persist_task_snapshot(task_id: str) -> None:
    if not _store().exists(task_id):
        return
    try:
        snapshot = _store().snapshot(task_id)
    except Exception:
        return
    _upsert_task_run(snapshot)


def _get_persisted_task(task_id: str) -> Optional[dict]:
    with Session(current_engine()) as s:
        row = s.get(TaskRunModel, task_id)
        if row is None:
            return None
        return _task_run_to_snapshot(row)


#: 列表查询要取的列（**不含** `logs_json` / `errors_json`）。
#:
#: 与 `_task_run_to_snapshot` 的字段一一对应，只是把两个大字段换成空数组。
_LIST_COLUMNS = (
    "id", "status", "platform", "source", "meta_json", "total", "progress",
    "success", "registered", "skipped", "control_json", "cashier_urls_json",
    "error", "created_at", "updated_at",
)

#: 这些状态的任务「还在跑」，它们的日志要随列表下发（实时日志面板依赖）。
_LIVE_STATUSES = ("pending", "running")


def _finished_status_filter():
    """「已结束任务」的 SQL 过滤条件。

    **必须显式带上 `IS NULL`**：SQL 里 `NULL NOT IN (...)` 求值为 NULL 而不是
    TRUE，只写 `notin_` 会让 NULL 行在「运行中」与「非运行中」两条查询里都不
    命中，从列表里静默消失。

    抽成函数是为了让测试能检查**生产代码真正用的**那个条件 —— 早先的测试在
    测试里重写了一遍同样的 WHERE，结果变异掉生产代码它照样绿（实测）。
    """
    return or_(
        TaskRunModel.status.is_(None),
        TaskRunModel.status.notin_(_LIVE_STATUSES),
    )


def _list_persisted_tasks(*, include_logs: bool = False) -> list[dict]:
    """列出所有任务快照。

    `include_logs=False`（默认）时，**已结束**任务的 `logs` 与 `errors`
    都不下发。实测两轮：

    - 第一轮：253 个任务、415 KB，其中 52% 是历史日志；
    - 第二轮（日志裁掉后）：仍有 5.5–6.1 MB，其中 **`errors` 占 4.49 MB
      （96%）**，最大单条 37 KB，而 715 个任务里**运行中 0 个**。

    列表页对 `errors` 只用 `.length` 显示「✗ 失败 N」，明细在单条详情里看。

    两个字段共用一个开关是刻意的：拆成 `include_logs` + `include_errors`
    会得到四种组合，而没有任何调用方需要「只要其中一个」。

    仍在跑/等待的任务**保留**两者：那是实时面板要用的，裁掉就没得看了。
    """
    # **SQL 层就不取大字段**：`logs_json` / `errors_json` 在 763 行上合计
    # 十几 MB，`SELECT *` 会先全读进内存再在 Python 里清空 —— 实测查询本身
    # 86–518ms、序列化再 200–320ms。只取轻量列后降到 42–82ms（快 6–10 倍）。
    #
    # 要全量时（`include_logs=True`）才走 `SELECT *`：那条路是「点开看明细」，
    # 用户主动触发的，慢一点可以接受。
    with Session(current_engine()) as s:
        if include_logs:
            rows = s.exec(select(TaskRunModel)).all()
            snapshots = [_task_run_to_snapshot(row) for row in rows]
        else:
            # **只对已结束的任务省列**：运行中/等待中的任务必须带 logs 与
            # errors（实时面板要显示失败原因）。用两条查询各取所需 ——
            # 运行中的通常只有个位数，读全量也不心疼。
            live_rows = s.exec(
                select(TaskRunModel).where(TaskRunModel.status.in_(_LIVE_STATUSES))
            ).all()
            cols = [getattr(TaskRunModel, name) for name in _LIST_COLUMNS]
            light_rows = s.exec(
                select(*cols).where(_finished_status_filter())
            ).all()
            snapshots = [_task_run_to_snapshot(row) for row in live_rows]
            snapshots += [
                _normalize_snapshot({
                    "id": row.id,
                    "status": row.status,
                    "platform": row.platform,
                    "source": row.source,
                    "meta": _json_loads(row.meta_json, {}),
                    "total": row.total,
                    "progress": row.progress,
                    "success": row.success,
                    "registered": row.registered,
                    "skipped": row.skipped,
                    "control": _json_loads(row.control_json, {}),
                    "cashier_urls": _json_loads(row.cashier_urls_json, []),
                    "error": row.error,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                    # 列表不下发这两个：省掉它们才是瘦身的意义所在
                    "logs": [],
                    "errors": [],
                })
                for row in light_rows
            ]
    # 已结束任务的 logs/errors 在 SQL 层就没取（见上面的 `_LIST_COLUMNS`），
    # 无需再清；运行中的任务走 `_task_run_to_snapshot`，明细原样保留。
    snapshots.sort(
        key=lambda item: (
            {"running": 0, "pending": 1, "done": 2, "failed": 3, "stopped": 4}.get(
                str(item.get("status") or ""),
                9,
            ),
            -_to_epoch_seconds(item.get("created_at")),
        )
    )
    return snapshots


def _finalize_orphan_tasks() -> None:
    with Session(current_engine()) as s:
        rows = s.exec(
            select(TaskRunModel).where(TaskRunModel.status.in_(["pending", "running"]))
        ).all()
        if not rows:
            return
        changed = False
        for row in rows:
            if _store().exists(row.id):
                continue
            row.status = "stopped"
            row.error = row.error or "任务因服务重启中断"
            logs = _json_loads(row.logs_json, [])
            tip = "[SYSTEM] 任务因服务重启中断，已自动标记为已停止"
            if tip not in logs:
                ts = datetime.now().strftime("%H:%M:%S")
                logs.append(f"[{ts}] {tip}")
            row.logs_json = _json_dumps(logs, [])
            row.updated_at = _utcnow()
            s.add(row)
            changed = True
        if changed:
            s.commit()


def _ensure_task_exists(task_id: str) -> None:
    if _store().exists(task_id):
        return
    if _get_persisted_task(task_id) is None:
        raise HTTPException(404, "任务不存在")


def _ensure_task_mutable(task_id: str) -> None:
    _ensure_task_exists(task_id)
    if _store().exists(task_id):
        snapshot = _store().snapshot(task_id)
    else:
        snapshot = _get_persisted_task(task_id) or {}
    if snapshot.get("status") in {"done", "failed", "stopped"}:
        raise HTTPException(409, "任务已结束，无法再执行控制操作")


def _get_task_snapshot(task_id: str) -> dict:
    _ensure_task_exists(task_id)
    if _store().exists(task_id):
        _persist_task_snapshot(task_id)
    snapshot = _get_persisted_task(task_id)
    if snapshot is None and _store().exists(task_id):
        snapshot = _normalize_snapshot(_store().snapshot(task_id))
    if snapshot is None:
        raise HTTPException(404, "任务不存在")
    return snapshot

