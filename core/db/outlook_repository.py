"""邮箱池（`outlook_accounts`）仓储：读写的**唯一入口**，按后端分派。

最后一张接 JSON 的表。它的筛选最复杂 —— 全部用查询层
（`core/storage/query.py`）的中立 clause 表达，两种后端各执行一份定义：

| 条件 | clause |
| --- | --- |
| `enabled == True` | `eq("enabled", True)` |
| 状态 ∈ {available, used, NULL, ""} | `any_of(...)` |
| `lower(email) NOT IN (...)` | `not_in_lower("email", ...)` |
| 账号类型匹配（NULL/空串算 OAuth） | `any_of(eq(..), eq(..))` |

## 分库

`outlook_accounts` 在 **outlook 平台库**，不在默认库。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from .models_platform import OutlookAccountModel

#: 该表在 JSON 后端下的表名（也是文件名前缀）
TABLE = "outlook_accounts"

#: 对外字段（两种后端统一成 dict）。**含密码与 refresh_token** ——
#: 仓储只搬运，不脱敏（脱敏在 API 层）。
_FIELDS = (
    "id", "email", "password", "client_id", "refresh_token", "account_type",
    "mailapi_url", "enabled", "status", "used_platforms", "created_at",
    "updated_at", "last_used",
)


def _from_model(row: OutlookAccountModel) -> dict[str, Any]:
    return {key: getattr(row, key, None) for key in _FIELDS}


def _account_type_clause(wanted: str):
    """账号类型匹配：**NULL 与空串都算 `microsoft_oauth`**（老库形态）。"""
    from core.storage.query import any_of, eq

    if wanted == "microsoft_oauth":
        return any_of(eq("account_type", "microsoft_oauth"), eq("account_type", ""))
    return eq("account_type", wanted)


class OutlookRepository:
    """邮箱池读写。"""

    @staticmethod
    def _store():
        from core.storage.json_table import store_for

        return store_for(TABLE)

    # ---------------------------------------------------------------- 读

    @staticmethod
    def list_all(session: Any = None) -> list[dict[str, Any]]:
        store = OutlookRepository._store()
        if store is not None:
            rows = sorted(store.all(), key=lambda r: int(r.get("id") or 0))
            return [{k: r.get(k) for k in _FIELDS} for r in rows]

        from sqlmodel import select

        from . import mailbox_pool_session

        def _run(s) -> list[dict[str, Any]]:
            return [
                _from_model(r)
                for r in s.exec(
                    select(OutlookAccountModel).order_by(OutlookAccountModel.id)
                ).all()
            ]

        if session is not None:
            return _run(session)

        with mailbox_pool_session("outlook") as s:
            return _run(s)

    @staticmethod
    def get(row_id: Any) -> Optional[dict[str, Any]]:
        store = OutlookRepository._store()
        if store is not None:
            record = store.get(row_id)
            return {k: record.get(k) for k in _FIELDS} if record else None

        from . import mailbox_pool_session

        with mailbox_pool_session("outlook") as s:
            row = s.get(OutlookAccountModel, int(row_id))
            return _from_model(row) if row is not None else None

    @staticmethod
    def find_by_email(email: str) -> Optional[dict[str, Any]]:
        wanted = str(email or "").strip().lower()
        if not wanted:
            return None
        store = OutlookRepository._store()
        if store is not None:
            for row in store.all():
                if str(row.get("email") or "").strip().lower() == wanted:
                    return {k: row.get(k) for k in _FIELDS}
            return None

        from sqlmodel import select

        from . import mailbox_pool_session

        with mailbox_pool_session("outlook") as s:
            row = s.exec(
                select(OutlookAccountModel).where(OutlookAccountModel.email == wanted)
            ).first()
            return _from_model(row) if row is not None else None

    @staticmethod
    def find_claimable(
        *,
        registered_emails: Optional[Iterable[str]] = None,
        wanted_type: str = "",
        session: Any = None,
    ) -> list[dict[str, Any]]:
        """取号候选：启用 + 状态可用（含 NULL/空串）+ 排除已注册 + 类型匹配。

        按 id 升序。**`unpooled` 不在候选里** —— 用户要求「选择导入才能导入
        邮箱池，不是全部导入」。
        """
        from core.storage.query import any_of, eq, matches_all, not_in_lower, to_sql

        clauses = [
            eq("enabled", True),
            any_of(
                eq("status", "available"),
                eq("status", "used"),
                eq("status", None),
                eq("status", ""),
            ),
        ]
        registered = {str(e).strip().lower() for e in (registered_emails or ()) if str(e).strip()}
        if registered:
            clauses.append(not_in_lower("email", registered))
        if wanted_type:
            clauses.append(_account_type_clause(wanted_type))

        store = OutlookRepository._store()
        if store is not None:
            rows = [r for r in store.all() if matches_all(clauses, r)]
            rows.sort(key=lambda r: int(r.get("id") or 0))
            return [{k: r.get(k) for k in _FIELDS} for r in rows]

        from sqlmodel import select

        from . import mailbox_pool_session

        def _run(s) -> list[dict[str, Any]]:
            stmt = select(OutlookAccountModel)
            for clause in clauses:
                stmt = stmt.where(to_sql(OutlookAccountModel, clause))
            stmt = stmt.order_by(OutlookAccountModel.id)
            return [_from_model(r) for r in s.exec(stmt).all()]

        if session is not None:
            return _run(session)

        with mailbox_pool_session("outlook") as s:
            return _run(s)

    @staticmethod
    def count_by_status(statuses: Iterable[str], *, enabled_only: bool = True) -> int:
        from core.storage.query import any_of, eq, in_, matches_all

        wanted = [str(s) for s in statuses]
        clauses = [in_("status", wanted)] if wanted else []
        if enabled_only:
            clauses.insert(0, eq("enabled", True))

        store = OutlookRepository._store()
        if store is not None:
            return sum(1 for r in store.all() if matches_all(clauses, r))

        from sqlmodel import func, select

        from . import mailbox_pool_session

        with mailbox_pool_session("outlook") as s:
            stmt = select(func.count(OutlookAccountModel.id))
            for clause in clauses:
                from core.storage.query import to_sql

                stmt = stmt.where(to_sql(OutlookAccountModel, clause))
            return int(s.exec(stmt).one() or 0)

    # ---------------------------------------------------------------- 写

    @staticmethod
    def add(**fields: Any) -> dict[str, Any]:
        payload = {k: v for k, v in fields.items() if k in _FIELDS}
        if "email" in payload:
            payload["email"] = str(payload["email"] or "").strip().lower()

        store = OutlookRepository._store()
        if store is not None:
            created = store.insert(payload)
            return {k: created.get(k) for k in _FIELDS}

        from . import mailbox_pool_session

        with mailbox_pool_session("outlook") as s:
            row = OutlookAccountModel(**payload)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _from_model(row)

    @staticmethod
    def set_fields(row_id: Any, **fields: Any) -> Optional[dict[str, Any]]:
        from datetime import datetime, timezone

        payload = {k: v for k, v in fields.items() if k in _FIELDS}
        payload["updated_at"] = datetime.now(timezone.utc)

        store = OutlookRepository._store()
        if store is not None:
            updated = store.update(row_id, payload)
            return {k: (updated or {}).get(k) for k in _FIELDS} if updated else None

        from . import mailbox_pool_session

        with mailbox_pool_session("outlook") as s:
            row = s.get(OutlookAccountModel, int(row_id))
            if row is None:
                return None
            for key, value in payload.items():
                setattr(row, key, value)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _from_model(row)

    @staticmethod
    def delete(row_id: Any) -> bool:
        store = OutlookRepository._store()
        if store is not None:
            return store.delete(row_id)

        from . import mailbox_pool_session

        with mailbox_pool_session("outlook") as s:
            row = s.get(OutlookAccountModel, int(row_id))
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True


outlook_repository = OutlookRepository()

__all__ = ["OutlookRepository", "outlook_repository", "TABLE"]
