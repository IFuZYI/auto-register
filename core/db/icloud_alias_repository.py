"""iCloud 别名表（`icloud_aliases`）仓储：读写的**唯一入口**，按后端分派。

与 `AccountRepository` / `ProxyRepository` / `TaskRepository` 同型：
**读写都收进来**（只接写会「写 JSON、读 SQLite → 看不到刚写的行」）。

## 过滤走查询层

取号的筛选含 `OR` 状态条件与 `lower()` 归一 —— 那些条件用
`core/storage/query.py` 的中立形式写**一份**，SQLite 与 JSON 各自执行
（`to_sql` / `matches`），不重写规则。

## 分派位置

`core` 不许 import `services`（架构约束），所以这一层自带 SQLite 实现
（别名表的查询不复杂），不像任务表那样要交给上层分派。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .models_platform import ICloudAliasModel

#: 该表在 JSON 后端下的表名（也是文件名前缀）
TABLE = "icloud_aliases"

#: 对外暴露的字段（两种后端形状统一成 dict）
_FIELDS = (
    "id", "account_id", "address", "label", "note", "status",
    "pool_status", "source", "used_platforms", "provider_id",
    "share_token", "created_at", "updated_at",
)


def _from_model(row: ICloudAliasModel) -> dict[str, Any]:
    return {key: getattr(row, key, None) for key in _FIELDS}


class ICloudAliasRepository:
    """别名表读写。"""

    @staticmethod
    def _store():
        """JSON 后端下的表存储；SQLite 后端返回 None。"""
        from core.storage.json_table import store_for

        return store_for(TABLE)

    # ---------------------------------------------------------------- 读

    @staticmethod
    def list_aliases(account_id: Optional[int] = None) -> list[dict[str, Any]]:
        store = ICloudAliasRepository._store()
        if store is not None:
            rows = store.all()
            if account_id is not None:
                rows = [r for r in rows if int(r.get("account_id") or 0) == int(account_id)]
            return [{k: r.get(k) for k in _FIELDS} for r in reversed(rows)]

        # **自带实现，不 import `services`**：架构约束连函数内导入也扫
        # （实测让 `test_dependency_direction.py` 变红）。别名表的 SQLite
        # 操作不复杂，直接写在这里。
        from sqlmodel import select

        from .registry import platform_database_registry

        with platform_database_registry.session_for("icloud") as s:
            stmt = select(ICloudAliasModel)
            if account_id is not None:
                stmt = stmt.where(ICloudAliasModel.account_id == int(account_id))
            # 与 JSON 分支一致：**id 倒序**（新的在前）
            rows = list(s.exec(stmt.order_by(ICloudAliasModel.id.desc())).all())
            return [_from_model(r) for r in rows]

    @staticmethod
    def get(alias_id: Any) -> Optional[dict[str, Any]]:
        store = ICloudAliasRepository._store()
        if store is not None:
            record = store.get(alias_id)
            return {k: record.get(k) for k in _FIELDS} if record else None

        # **必须走 icloud 平台库**：`icloud_aliases` 是分库表，用默认库的
        # `current_engine()` 会撞 `no such table: icloud_aliases`（实测）。
        from .registry import platform_database_registry

        with platform_database_registry.session_for("icloud") as s:
            row = s.get(ICloudAliasModel, int(alias_id))
            return _from_model(row) if row is not None else None

    @staticmethod
    def find_available(*, account_id: int) -> list[dict[str, Any]]:
        """取号候选：本主号下 `pool_status` 为可用的别名。

        过滤条件用**查询层**写一份 —— 两种后端各自执行，规则不重写。
        """
        from core.storage.query import any_of, eq, to_sql

        clause = any_of(
            eq("pool_status", "available"),
            eq("pool_status", None),
            eq("pool_status", ""),
        )

        store = ICloudAliasRepository._store()
        if store is not None:
            rows = [
                r for r in store.all()
                if int(r.get("account_id") or 0) == int(account_id) and clause.matches(r)
            ]
            return [{k: r.get(k) for k in _FIELDS} for r in rows]

        from sqlmodel import select

        from .registry import platform_database_registry

        with platform_database_registry.session_for("icloud") as s:
            rows = s.exec(
                select(ICloudAliasModel)
                .where(ICloudAliasModel.account_id == int(account_id))
                .where(to_sql(ICloudAliasModel, clause))
            ).all()
            return [_from_model(r) for r in rows]

    # ---------------------------------------------------------------- 写

    @staticmethod
    def upsert(
        *,
        account_id: int,
        address: str,
        label: str = "",
        note: str = "",
        **extra: Any,
    ) -> dict[str, Any]:
        """按 `address` upsert（地址是唯一业务键）。"""
        store = ICloudAliasRepository._store()
        if store is not None:
            existing = store.first(address=str(address))
            payload = {
                "account_id": int(account_id),
                "address": str(address),
                "label": str(label or ""),
                "note": str(note or ""),
                **extra,
            }
            if existing:
                payload.pop("account_id", None)  # 地址已绑定主号，不因 upsert 改绑
                payload.pop("address", None)
                updated = store.update(existing["id"], payload)
                return {k: (updated or {}).get(k) for k in _FIELDS}
            created = store.insert(payload)
            return {k: created.get(k) for k in _FIELDS}

        # **自带实现，不 import `services`** —— 架构约束连函数内导入也扫
        # （`test_dependency_direction.py` 实测抓到过）。别名表的 SQLite 操作
        # 不复杂，写在这里比跨层调用更清楚。
        from sqlmodel import select

        from .registry import platform_database_registry

        wanted = str(address or "").strip().lower()
        with platform_database_registry.session_for("icloud") as s:
            row = s.exec(
                select(ICloudAliasModel).where(ICloudAliasModel.address == wanted)
            ).first()
            if row is None:
                row = ICloudAliasModel(account_id=int(account_id), address=wanted)
            row.label = str(label or "")
            row.note = str(note or "")
            for key, value in extra.items():
                if hasattr(row, key):
                    setattr(row, key, value)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _from_model(row)


    @staticmethod
    def update_where(
        *, match: dict[str, Any], values: dict[str, Any]
    ) -> int:
        """**条件 UPDATE**：只改满足 `match` 的行，返回命中数。

        为什么必须支持它：`renew_alias_claim` / `release_stale_claims` 靠
        「读-改-写」会有并发窗口 —— 池页面手动改过状态后，整行写回会把对方
        的结果静默覆盖（包括把刚标好的 `used` 打回 `available`）。条件更新
        让数据库层挡掉这类竞争。

        JSON 后端下 `JsonStore.update` 已在锁内完成「读-判-写」，语义等价。
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        payload = {k: v for k, v in values.items() if k in _FIELDS}
        payload.setdefault("updated_at", now)

        store = ICloudAliasRepository._store()
        if store is not None:
            hit = 0
            for row in store.all():
                if all(str(row.get(k)) == str(v) for k, v in match.items()):
                    store.update(row.get("id"), payload)
                    hit += 1
            return hit

        from sqlalchemy import update as _update

        from .registry import platform_database_registry

        with platform_database_registry.session_for("icloud") as s:
            stmt = _update(ICloudAliasModel)
            for key, value in match.items():
                stmt = stmt.where(getattr(ICloudAliasModel, key) == value)
            result = s.exec(stmt.values(**payload))
            hit = int(getattr(result, "rowcount", 0) or 0)
            if hit:
                s.commit()
            return hit

    @staticmethod
    def release_stale(*, older_than_minutes: int = 120) -> int:
        """把「领了但一直没落定」的 `in_use` 别名放回 `available`。

        阈值与 `renew_alias_claim` 的续约配合：活着的长任务会续约，
        所以这里只回收真正崩掉的那些。
        """
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(
            minutes=max(int(older_than_minutes), 1)
        )

        store = ICloudAliasRepository._store()
        if store is not None:
            hit = 0
            for row in store.all():
                if str(row.get("pool_status")) != "in_use":
                    continue
                stamp = row.get("updated_at")
                if not isinstance(stamp, datetime):
                    continue
                # 一侧 naive 一侧 aware 时跳过（不炸整个回收）
                try:
                    stale = stamp < cutoff
                except TypeError:
                    continue
                if stale:
                    store.update(
                        row.get("id"),
                        {"pool_status": "available",
                         "updated_at": datetime.now(timezone.utc)},
                    )
                    hit += 1
            return hit

        from sqlalchemy import update as _update

        from .registry import platform_database_registry

        with platform_database_registry.session_for("icloud") as s:
            result = s.exec(
                _update(ICloudAliasModel)
                .where(ICloudAliasModel.pool_status == "in_use")
                .where(ICloudAliasModel.updated_at < cutoff)
                .values(pool_status="available",
                        updated_at=datetime.now(timezone.utc))
            )
            hit = int(getattr(result, "rowcount", 0) or 0)
            if hit:
                s.commit()
            return hit

    @staticmethod
    def set_fields(alias_id: Any, **fields: Any) -> Optional[dict[str, Any]]:
        """改若干字段（含 `updated_at` 自动刷新）。

        比 `set_pool_status` 通用：`mark_alias_used` 要同时改
        `pool_status` 与 `used_platforms`。
        """
        from datetime import datetime, timezone

        payload = {k: v for k, v in fields.items() if k in _FIELDS}
        payload["updated_at"] = datetime.now(timezone.utc)

        store = ICloudAliasRepository._store()
        if store is not None:
            updated = store.update(alias_id, payload)
            return {k: (updated or {}).get(k) for k in _FIELDS} if updated else None

        from .registry import platform_database_registry

        with platform_database_registry.session_for("icloud") as s:
            row = s.get(ICloudAliasModel, int(alias_id))
            if row is None:
                return None
            for key, value in payload.items():
                setattr(row, key, value)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _from_model(row)

    @staticmethod
    def set_pool_status(alias_id: Any, pool_status: str) -> Optional[dict[str, Any]]:
        store = ICloudAliasRepository._store()
        if store is not None:
            updated = store.update(alias_id, {"pool_status": str(pool_status)})
            return {k: (updated or {}).get(k) for k in _FIELDS} if updated else None

        from .registry import platform_database_registry

        with platform_database_registry.session_for("icloud") as s:
            row = s.get(ICloudAliasModel, int(alias_id))
            if row is None:
                return None
            row.pool_status = str(pool_status)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _from_model(row)

    @staticmethod
    def delete(alias_id: Any) -> bool:
        store = ICloudAliasRepository._store()
        if store is not None:
            return store.delete(alias_id)

        from .registry import platform_database_registry

        with platform_database_registry.session_for("icloud") as s:
            row = s.get(ICloudAliasModel, int(alias_id))
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True


icloud_alias_repository = ICloudAliasRepository()

__all__ = ["ICloudAliasRepository", "icloud_alias_repository", "TABLE"]
