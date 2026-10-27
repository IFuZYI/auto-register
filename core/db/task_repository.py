"""任务表仓储：`task_runs` 读写的**唯一入口**，按后端分派。

与 `AccountRepository` / `ProxyRepository` 同型。**读写都收进来** —— 只接写
会「写 JSON、读 SQLite → 看不到刚写的行」（代理那轮已经踩过这个坑的形状）。

## 与另两张表的差别：主键是字符串

任务 id 形如 `task_1793000000000` / `backfill_rt_...` —— **不是自增整数**。
所以：
- 写入用 `JsonTableStore.put(id, record)`（显式主键），不是 `insert`；
- 排序与过滤都要容忍字符串主键（`JsonTableStore.all` 已处理）。

## 快照形态

对外的形状是 `_task_run_to_snapshot` 那一份 dict（`id` / `status` /
`logs` / `meta` …），JSON 后端直接存它 —— `*_json` 字符串列的拆装在底层，
上层不需要知道。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .models_infra import TaskRunModel

#: 该表在 JSON 后端下的表名（也是文件名前缀）
TABLE = "task_runs"


class TaskRepository:
    """任务行读写。带 `session` 的调用方走原生 SQLite（它有自己的事务）。"""

    # ---------------------------------------------------------------- 后端

    @staticmethod
    def _store():
        """JSON 后端下的表存储；SQLite 后端返回 None。"""
        from core.storage.json_table import store_for

        return store_for(TABLE)

    # ---------------------------------------------------------------- 读写

    #: 落库时关心的字段（表结构固定，多余的键不带）
    FIELDS: tuple[str, ...] = (
        "id", "platform", "source", "status", "total", "progress",
        "success", "registered", "skipped", "error", "meta", "logs",
        "errors", "cashier_urls", "control", "created_at", "updated_at",
    )

    @staticmethod
    def put_json(snapshot: dict[str, Any]) -> bool:
        """JSON 后端下写一行；**不是 JSON 后端时返回 False**（交给调用方）。

        为什么不让仓储自己回落 SQLite：`core` **不许** import `services`
        （架构约束，`tests/test_dependency_direction.py` 会红），而 SQLite 的
        落库实现（`_upsert_task_run`，含 `normalize` 与 `*_json` 列拆装）在
        `services/task_store_io.py` 里。所以分派放在**上层**：

            services 决定「谁来做」→ core 只负责 JSON 那一支

        这是本仓既有的方向（`core` 不依赖 `services`），第一版写反了。
        """
        store = TaskRepository._store()
        if store is None:
            return False
        task_id = str((snapshot or {}).get("id") or "")
        if not task_id:
            return True  # 没有 id 的没什么可写，当作已处理
        store.put(task_id, {key: snapshot.get(key) for key in TaskRepository.FIELDS})
        return True

    @staticmethod
    def get_json(task_id: str) -> Optional[dict[str, Any]]:
        """JSON 后端下取一行；不是 JSON 后端时返回 None（交给调用方）。"""
        store = TaskRepository._store()
        if store is None:
            return None
        return store.get(str(task_id or ""))

    @staticmethod
    def list_json_by_status(statuses: Iterable[str]) -> Optional[list[dict[str, Any]]]:
        """JSON 后端下按状态列出；不是 JSON 后端时返回 None（交给调用方）。"""
        store = TaskRepository._store()
        if store is None:
            return None
        return store.find_in("status", [str(s) for s in statuses])


task_repository = TaskRepository()

__all__ = ["TaskRepository", "task_repository", "TABLE"]
