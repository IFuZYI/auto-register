"""任务日志表（`task_logs`）仓储：读写的**唯一入口**，按后端分派。

与 `TaskRepository` 的分派方式相同：`core` **不许** import `services`
（架构约束，见 `tests/test_dependency_direction.py`），所以这一层只做
JSON 分派，SQLite 的落库由调用方（`api/tasks.py`）走原生会话。

## 对外形状统一成 dict

SQLite 版返回 `TaskLog` 模型、JSON 版返回 dict —— 调用方要的字段是
`id / platform / email / status / error / created_at`，这里统一成 dict，
上层不必关心后端。
"""

from __future__ import annotations

from typing import Any, Optional

from .models_infra import TaskLog

#: 该表在 JSON 后端下的表名（也是文件名前缀）
TABLE = "task_logs"

#: 对外暴露的字段（SQLite 版的 `detail_json` 在 JSON 里就叫 `detail`）
_FIELDS = ("id", "platform", "email", "status", "error", "detail", "created_at")


def _from_sqlite(row: TaskLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "platform": str(row.platform or ""),
        "email": str(row.email or ""),
        "status": str(row.status or ""),
        "error": str(row.error or ""),
        "detail_json": str(getattr(row, "detail_json", "") or ""),
        "created_at": getattr(row, "created_at", None),
    }


def _from_json(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in _FIELDS}


class TaskLogRepository:
    """任务日志读写。"""

    @staticmethod
    def _store():
        from core.storage.json_table import store_for

        return store_for(TABLE)

    # ---------------------------------------------------------------- 写

    @staticmethod
    def add_json(record: dict[str, Any]) -> Optional[dict[str, Any]]:
        """JSON 后端下写一行；不是 JSON 后端时返回 None（交给调用方）。"""
        store = TaskLogRepository._store()
        if store is None:
            return None
        # **丢掉值为 None 的键**，让 `insert` 的默认值生效。
        # 传 `{"created_at": None}` 的话 `setdefault` 不会替换已存在的键 ——
        # `created_at` 就永远是 None（实测：保留期清理因此一条都删不掉）。
        payload = {k: v for k, v in ((k, record.get(k)) for k in _FIELDS) if v is not None}
        return _from_json(store.insert(payload))

    # ---------------------------------------------------------------- 读

    @staticmethod
    def list_json(page: int = 1, page_size: int = 50, platform: str = "") -> Optional[dict]:
        """JSON 后端下列一页；不是 JSON 后端时返回 None（交给调用方）。

        排序与 SQLite 版一致：**id 倒序**（新的在前）。
        """
        store = TaskLogRepository._store()
        if store is None:
            return None

        rows = store.all()
        wanted = str(platform or "").strip()
        if wanted:
            rows = [r for r in rows if str(r.get("platform")) == wanted]
        # id 可能是字符串（JSON 自增是数字，但别假设）
        rows.sort(key=lambda r: _sort_key(r.get("id")), reverse=True)

        total = len(rows)
        start = max(0, (int(page) - 1) * int(page_size))
        items = [_from_json(r) for r in rows[start : start + int(page_size)]]
        return {"total": total, "items": items}

    # ---------------------------------------------------------------- 删

    @staticmethod
    def delete_many_json(ids: list[Any]) -> Optional[dict]:
        """JSON 后端下批量删；不是 JSON 后端时返回 None（交给调用方）。"""
        store = TaskLogRepository._store()
        if store is None:
            return None

        unique = list(dict.fromkeys(ids))
        existing = { _sort_key(r.get("id")) for r in store.all() }
        found = [i for i in unique if _sort_key(i) in existing]
        store.delete_many(found)
        return {
            "deleted": len(found),
            "not_found": [i for i in unique if _sort_key(i) not in existing],
            "total_requested": len(unique),
        }

    @staticmethod
    def cleanup_json(before) -> Optional[int]:
        """JSON 后端下按时间清理；不是 JSON 后端时返回 None（交给调用方）。"""
        store = TaskLogRepository._store()
        if store is None:
            return None

        doomed = []
        for row in store.all():
            created = row.get("created_at")
            if created is None:
                continue
            try:
                if created < before:
                    doomed.append(row.get("id"))
            except TypeError:
                # 一侧 naive 一侧 aware：跳过而不是炸整个清理
                continue
        return store.delete_many(doomed)


def _sort_key(value: Any) -> tuple[int, Any]:
    """`id` 排序键：数字按数值、字符串按字典序 —— 混合也不会炸。"""
    raw = str(value if value is not None else "")
    return (0, int(raw)) if raw.isdigit() else (1, raw)


task_log_repository = TaskLogRepository()

__all__ = ["TaskLogRepository", "task_log_repository", "TABLE"]
