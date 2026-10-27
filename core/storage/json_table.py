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


# --------------------------------------------------------------------- 分派
#
# 一张表一张表地写 `save()` 的话，5 张表就是 5 份形状相同的代码。这里按
# **表名**自动路由 —— 加一张表只需要往下面那个集合里加名字。

#: 哪些表**已完整**接了 JSON 后端（读与写都走 store）。
#:
#: **现在是空的** —— 这不是遗漏，是刻意的。
#:
#: 判据是「读与写都接线」：只接写不接读会得到
#:
#:     写：save_model → JSON
#:     读：session.exec(select(Model)) → SQLite → **看不到刚写的行**
#:
#: 与账号那轮踩的坑同型（读走 JSON、写走 SQLite），只是方向反过来。
#: 账号表不在这个集合里，因为它的读写走的是 `AccountRepository` 自己的
#: 分派（`_json_store`），不经过这里。
#:
#: 接线一张表的完整步骤（代理 / 任务 / iCloud / 邮箱池都还没做）：
#:   1. 给该表加一个持有 `JsonTableStore` 的仓储，读也走它；
#:   2. 把散落的 `session.exec(select(Model)...)` 改成仓储调用；
#:   3. 把 `session.add(...)` 改成这里的 `save_model(...)`；
#:   4. 最后把表名加进下面这个集合。
JSON_TABLES: frozenset[str] = frozenset()


def _data_dir() -> Path:
    from core.paths import DATA_DIR

    return DATA_DIR


def table_path(table: str) -> Optional[Path]:
    """该表在 JSON 后端下的文件路径；不在白名单时返回 None。"""
    if table not in JSON_TABLES:
        return None
    return _data_dir() / f"{table}.json"


def table_wants_json(table: str) -> bool:
    """该表当前是否走 JSON 后端。"""
    if table not in JSON_TABLES:
        return False
    from core.paths import DATA_DIR
    from core.storage import BACKEND_JSON, resolve_backend_name

    db_exists = (DATA_DIR / "account_manager.db").exists()
    return resolve_backend_name(db_exists=db_exists) == BACKEND_JSON


def store_for(table: str) -> Optional[JsonTableStore]:
    path = table_path(table)
    return JsonTableStore(path, table=table) if path is not None and table_wants_json(table) else None


def model_to_record(model: Any) -> dict[str, Any]:
    """把 SQLModel 实例转成可存的 dict（只取模型声明的字段）。

    排掉 `__tablename__` 之类的类属性；`model_fields` 拿不到时退回
    `__dict__`（鸭子类型的小对象）。
    """
    fields = getattr(type(model), "model_fields", None)
    names = list(fields.keys()) if isinstance(fields, dict) else [
        k for k in vars(model) if not k.startswith("_")
    ]
    return {name: getattr(model, name, None) for name in names}


def save_model(model: Any, *, session: Any = None, commit: bool = True) -> Any:
    """**通用**写回：按模型的表名选后端。

    SQLite 后端下与原来的 `session.add(model)` 等价（调用方负责 commit，
    除非 `commit=True` 且 session 是自己开的）。
    """
    if model is None:
        return None
    table = str(getattr(type(model), "__tablename__", "") or "")
    store = store_for(table) if table else None

    if store is None:
        if session is not None:
            session.add(model)
            if commit:
                session.commit()
        return model

    record = model_to_record(model)
    row_id = record.get("id")
    if row_id:
        updated = store.update(row_id, record)
        return updated if updated is not None else record
    return store.insert(record)


__all__ = [
    "JSON_TABLES",
    "JsonTableStore",
    "META_TABLE",
    "model_to_record",
    "save_model",
    "store_for",
    "table_path",
    "table_wants_json",
]
