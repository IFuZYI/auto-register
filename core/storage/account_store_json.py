"""账号的 JSON 存储。

对应 `core.db.repository.AccountRepository` 的 SQLite 实现 —— 接口按
**业务语义**设计（不是 SQL 操作），两种后端可互换。

## 与 SQLite 机制的对应

| SQLite | 这里 |
| --- | --- |
| `AUTOINCREMENT` 主键 | `uuid4()` 字符串（与 SQLite 侧同口径，见 ADR-0008） |
| `(platform, email)` 唯一 | 先查后决定插入或更新 |
| `WHERE ... ORDER BY id` | Python 过滤 + 按 `(created_at, id)` 排序 |
| `LIMIT / OFFSET` | 排序之后切片 |

## id 形态

主键是 UUID4 字符串，与 SQLite 侧完全一致 —— 两种后端可互换的前提是同一份
数据搬过去 id 不变。

`_cursor()` / `_next_id()` / `_max_existing_id()` 是 UUID 之前的自增游标，
**只为读老文件保留**：老 JSON 里还是整数 id，读出来照常能用，写回去不再产生
新的整数 id。

## 序列化

`AccountModel` 的字段里 `created_at` / `updated_at` 是 datetime，JSON 里存
ISO 字符串，读回时还原成 datetime —— 否则调用方 `row.created_at.year` 会炸。
"""

from __future__ import annotations

import uuid

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from core.storage.json_backend import JsonStore, JsonStoreError

#: JSON 里的表名
TABLE = "accounts"
#: 自增游标所在的位置（与表并列，是元数据不是记录）
CURSOR_TABLE = "_meta"

#: 需要按 datetime 还原的字段
_DATETIME_FIELDS = ("created_at", "updated_at")

#: 可参与过滤的字段（其余字段忽略，避免调用方传错键时误过滤）
_FILTER_FIELDS = ("platform", "status")


def _to_storage(record: dict[str, Any]) -> dict[str, Any]:
    """datetime → ISO 字符串（JSON 存不了 datetime）。"""
    out = dict(record)
    for name in _DATETIME_FIELDS:
        value = out.get(name)
        if isinstance(value, datetime):
            out[name] = value.isoformat()
    return out


def _from_storage(record: dict[str, Any]) -> dict[str, Any]:
    """ISO 字符串 → datetime。解析失败时保留原值（不丢数据）。"""
    out = dict(record)
    for name in _DATETIME_FIELDS:
        value = out.get(name)
        if isinstance(value, str) and value:
            try:
                out[name] = datetime.fromisoformat(value)
            except ValueError:
                pass
    # 老文件里 id 可能是字符串形式的数字：还原成 int（保持老行为）。
    # UUID 不能进 int()，否则会被静默吞成 0 —— 那会让所有账号指向同一行。
    raw_id = out.get("id")
    if isinstance(raw_id, str) and raw_id.isdigit():
        out["id"] = int(raw_id)
    return out


class AccountStoreJson:
    """账号的 JSON 实现。"""

    def __init__(self, path: Path | str):
        self._store = JsonStore(path)

    # ---------------------------------------------------------------- 游标

    def _cursor(self) -> int:
        meta = self._store.get(CURSOR_TABLE, "account_id") or {}
        try:
            return int(meta.get("value") or 0)
        except (TypeError, ValueError):
            return 0

    def _next_id(self) -> int:
        """取下一个 id 并**立刻持久化游标**（只增不减，见模块 docstring）。"""
        current = max(self._cursor(), self._max_existing_id())
        nxt = current + 1
        self._store.put(CURSOR_TABLE, "account_id", {"value": nxt})
        return nxt

    def _max_existing_id(self) -> int:
        best = 0
        for row in self._store.all(TABLE):
            try:
                best = max(best, int(row.get("id") or 0))
            except (TypeError, ValueError):
                continue
        return best

    # ---------------------------------------------------------------- 基础

    def insert(self, record: dict[str, Any]) -> dict[str, Any]:
        row = _to_storage(record)
        # 调用方给了主键就用它（UUID 由模型构造时生成，两条后端必须落到同一个
        # id 上，否则同一账号在 SQLite 与 JSON 里是两个人）。
        row["id"] = str(row.get("id") or uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        row.setdefault("created_at", now)
        row.setdefault("updated_at", now)
        self._store.put(TABLE, row["id"], row)
        return _from_storage(row)

    def get(self, account_id: Any) -> Optional[dict[str, Any]]:
        record = self._store.get(TABLE, account_id)
        return _from_storage(record) if isinstance(record, dict) else None

    def update(self, account_id: Any, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
        """局部更新（不传的字段保持原值）。"""
        existing = self._store.get(TABLE, account_id)
        if not isinstance(existing, dict):
            return None
        merged = {**existing, **_to_storage(patch)}
        merged["id"] = existing.get("id", account_id)
        merged["updated_at"] = datetime.now(timezone.utc).isoformat()
        self._store.put(TABLE, account_id, merged)
        return _from_storage(merged)

    def delete(self, account_id: Any) -> bool:
        return self._store.delete(TABLE, account_id)

    def delete_many(self, ids: Iterable[Any]) -> int:
        removed = 0
        for account_id in ids:
            if self._store.delete(TABLE, account_id):
                removed += 1
        return removed

    # ---------------------------------------------------------------- 查询

    def find_by_email(self, platform: str, email: str) -> Optional[dict[str, Any]]:
        wanted_platform = str(platform or "").strip().lower()
        wanted_email = str(email or "").strip().lower()
        for row in self.list_all(platform=wanted_platform):
            if str(row.get("email") or "").strip().lower() == wanted_email:
                return row
        return None

    def list_all(
        self,
        *,
        platform: str = "",
        status: str = "",
        email: str = "",
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """按条件列出（过滤 → 排序 → 切片，顺序与 SQL 的 WHERE/ORDER/LIMIT 一致）。"""
        rows = [_from_storage(r) for r in self._store.all(TABLE)]

        wanted_platform = str(platform or "").strip().lower()
        if wanted_platform:
            rows = [
                r for r in rows
                if str(r.get("platform") or "").strip().lower() == wanted_platform
            ]

        wanted_status = str(status or "").strip().lower()
        if wanted_status:
            rows = [
                r for r in rows
                if str(r.get("status") or "").strip().lower() == wanted_status
            ]

        needle = str(email or "").strip().lower()
        if needle:
            rows = [r for r in rows if needle in str(r.get("email") or "").lower()]

        # UUID 没有大小语义，`int(id)` 直接炸。用创建时间还原「先建的在前」，
        # id 只做同一时刻的稳定 tiebreaker。
        rows.sort(key=lambda r: (str(r.get("created_at") or ""), str(r.get("id") or "")))

        if offset:
            rows = rows[offset:]
        if limit is not None:
            rows = rows[:limit]
        return rows

    def count(self, *, platform: str = "", status: str = "", email: str = "") -> int:
        return len(self.list_all(platform=platform, status=status, email=email))

    def platforms_by_email(self, email: str) -> set[str]:
        wanted = str(email or "").strip().lower()
        return {
            str(r.get("platform") or "").strip().lower()
            for r in self.list_all()
            if str(r.get("email") or "").strip().lower() == wanted
        }


__all__ = ["AccountStoreJson", "TABLE"]
