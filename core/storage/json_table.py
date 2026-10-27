"""通用 JSON 表存储：一套代码覆盖所有表。

## 为什么不做「每张表一个 store」

账号那轮写了 `AccountStoreJson`。再给代理、任务、iCloud、邮箱池各写一个，
就是四份形状相同、只在字段列表上不同的代码 —— 加一张表要再抄一遍，字段
改了要改四处（本仓已因「同一规则两份定义」踩过多次）。

这里按**表名 + 自增游标**做通用 CRUD：
- 每张表一个**独立游标**（`_meta.<table>_id`），id 只增不减；
- datetime 存 ISO 字符串、读回还原（调用方读 `.year` / 做比较）；
- 排序按 id，与 SQLite 的 `ORDER BY id` 观感一致。

## 不做的事

**不校验字段**：调用方给什么字段就存什么（与 SQLite 的宽表语义不同，但
JSON 后端下「多存一个键」比「报错拒绝」更接近用户预期）。字段清单由调用
方自己保证 —— 这里不引第二份定义。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from core.storage.json_backend import JsonStore

#: 元数据表名（放自增游标）
META_TABLE = "_meta"


def _to_storage(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    for key, value in list(out.items()):
        if isinstance(value, datetime):
            out[key] = value.isoformat()
    return out


def _from_storage(record: dict[str, Any]) -> dict[str, Any]:
    out = dict(record)
    for key, value in list(out.items()):
        if isinstance(value, str) and len(value) >= 19 and value[4] == "-" and value[10] in "T ":
            try:
                out[key] = datetime.fromisoformat(value)
                continue
            except ValueError:
                pass
    if "id" in out and not isinstance(out["id"], int):
        try:
            out["id"] = int(out["id"])
        except (TypeError, ValueError):
            pass
    return out


class JsonTableStore:
    """某一张表的 JSON 存储。线程安全由底层 `JsonStore` 保证。"""

    def __init__(self, path: Path | str, *, table: str):
        self._store = JsonStore(path)
        self.table = str(table)

    # ---------------------------------------------------------------- 游标

    def _cursor_key(self) -> str:
        return f"{self.table}_id"

    def _next_id(self) -> int:
        """取下一个 id 并**立刻持久化游标**（只增不减，见模块 docstring）。"""
        current = 0
        meta = self._store.get(META_TABLE, self._cursor_key()) or {}
        try:
            current = int(meta.get("value") or 0)
        except (TypeError, ValueError):
            current = 0
        for row in self._store.all(self.table):
            try:
                current = max(current, int(row.get("id") or 0))
            except (TypeError, ValueError):
                continue
        nxt = current + 1
        self._store.put(META_TABLE, self._cursor_key(), {"value": nxt})
        return nxt

    # ---------------------------------------------------------------- 基础

    def insert(self, record: dict[str, Any]) -> dict[str, Any]:
        row = _to_storage(dict(record))
        row["id"] = self._next_id()
        now = datetime.now(timezone.utc).isoformat()
        row.setdefault("created_at", now)
        row.setdefault("updated_at", now)
        self._store.put(self.table, row["id"], row)
        return _from_storage(row)

    def get(self, row_id: Any) -> Optional[dict[str, Any]]:
        record = self._store.get(self.table, row_id)
        return _from_storage(record) if isinstance(record, dict) else None

    def update(self, row_id: Any, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
        existing = self._store.get(self.table, row_id)
        if not isinstance(existing, dict):
            return None
        merged = {**existing, **_to_storage(patch)}
        merged["id"] = existing.get("id", row_id)
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._store.put(self.table, row_id, merged)
        return _from_storage(merged)

    def delete(self, row_id: Any) -> bool:
        return self._store.delete(self.table, row_id)

    def delete_many(self, ids: Iterable[Any]) -> int:
        removed = 0
        for row_id in ids:
            if self._store.delete(self.table, row_id):
                removed += 1
        return removed

    # ---------------------------------------------------------------- 查询

    def all(self) -> list[dict[str, Any]]:
        rows = [_from_storage(r) for r in self._store.all(self.table)]
        rows.sort(key=lambda r: int(r.get("id") or 0))
        return rows

    def find(self, **equals: Any) -> list[dict[str, Any]]:
        """按字段等值过滤（`find(region="JP")`）。"""
        out = []
        for row in self.all():
            if all(str(row.get(k)) == str(v) for k, v in equals.items()):
                out.append(row)
        return out

    def first(self, **equals: Any) -> Optional[dict[str, Any]]:
        found = self.find(**equals)
        return found[0] if found else None

    def count(self) -> int:
        return self._store.count(self.table)


__all__ = ["JsonTableStore", "META_TABLE"]
