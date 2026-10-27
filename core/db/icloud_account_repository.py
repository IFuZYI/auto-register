"""iCloud 主号表（`icloud_accounts`）仓储：读写的**唯一入口**，按后端分派。

与别名表仓储同型。**注意加密列**：`credentials_cipher` /
`remote_password_cipher` 是密文，仓储只搬运、不解读 —— 加解密留在
`services/icloud/`（那里才知道密钥从哪来）。

## 与别名表的关系

别名表已完整走仓储（`icloud_alias_repository`）。主号表接完后 iCloud 领域
才完整 —— 在那之前 `icloud_aliases` **不能**进 `JSON_TABLES`（否则别名在
JSON、主号在 SQLite，`list_aliases` 里的主号邮箱关联会读不到）。
"""

from __future__ import annotations

from typing import Any, Optional

from .models_platform import ICloudAccountModel

#: 该表在 JSON 后端下的表名（也是文件名前缀）
TABLE = "icloud_accounts"

#: 对外字段（两种后端统一成 dict）
_FIELDS = (
    "id", "email", "display_name", "region", "status", "enabled",
    "credentials_cipher", "source", "base_url", "remote_account_id",
    "remote_password_cipher", "proxy", "sync_error", "last_sync_at",
    "created_at", "updated_at",
)


def _from_model(row: ICloudAccountModel) -> dict[str, Any]:
    return {key: getattr(row, key, None) for key in _FIELDS}


class ICloudAccountRepository:
    """主号表读写。"""

    @staticmethod
    def _store():
        """JSON 后端下的表存储；SQLite 后端返回 None。"""
        from core.storage.json_table import store_for

        return store_for(TABLE)

    # ---------------------------------------------------------------- 读

    @staticmethod
    def list_all(session: Any = None) -> list[dict[str, Any]]:
        """全部主号。

        **接受调用方的 session**：测试会用内存库替换 `platform_session`
        （`tests/test_remote_account_alias_availability.py` 就是这么做的），
        复用它的会话才能让那些测试看到自己造的数据。
        """
        store = ICloudAccountRepository._store()
        if store is not None:
            rows = sorted(store.all(), key=lambda r: int(r.get("id") or 0))
            return [{k: r.get(k) for k in _FIELDS} for r in rows]

        from sqlmodel import select

        def _run(s) -> list[dict[str, Any]]:
            rows = s.exec(
                select(ICloudAccountModel).order_by(ICloudAccountModel.id)
            ).all()
            return [_from_model(r) for r in rows]

        if session is not None:
            return _run(session)

        from .registry import platform_session

        with platform_session("icloud") as s:
            return _run(s)

    @staticmethod
    def get(account_id: Any) -> Optional[dict[str, Any]]:
        store = ICloudAccountRepository._store()
        if store is not None:
            record = store.get(account_id)
            return {k: record.get(k) for k in _FIELDS} if record else None

        from .registry import platform_session

        with platform_session("icloud") as s:
            row = s.get(ICloudAccountModel, int(account_id))
            return _from_model(row) if row is not None else None

    @staticmethod
    def find_by_email(email: str) -> Optional[dict[str, Any]]:
        wanted = str(email or "").strip().lower()
        if not wanted:
            return None
        store = ICloudAccountRepository._store()
        if store is not None:
            record = store.first(email=wanted)
            return {k: record.get(k) for k in _FIELDS} if record else None

        from sqlmodel import select

        from .registry import platform_session

        with platform_session("icloud") as s:
            row = s.exec(
                select(ICloudAccountModel).where(ICloudAccountModel.email == wanted)
            ).first()
            return _from_model(row) if row is not None else None

    @staticmethod
    def find_remote(*, base_url: str, remote_account_id: str) -> Optional[dict[str, Any]]:
        """按 `(base_url, remote_account_id)` 找已接入的远程账号（查重用）。"""
        wanted_url = str(base_url or "").strip()
        wanted_id = str(remote_account_id or "").strip()
        if not wanted_url or not wanted_id:
            return None

        store = ICloudAccountRepository._store()
        if store is not None:
            for row in store.all():
                if (str(row.get("base_url") or "").strip() == wanted_url
                        and str(row.get("remote_account_id") or "").strip() == wanted_id):
                    return {k: row.get(k) for k in _FIELDS}
            return None

        from sqlmodel import select

        from .registry import platform_session

        with platform_session("icloud") as s:
            row = s.exec(
                select(ICloudAccountModel)
                .where(ICloudAccountModel.source == "remote")
                .where(ICloudAccountModel.base_url == wanted_url)
                .where(ICloudAccountModel.remote_account_id == wanted_id)
            ).first()
            return _from_model(row) if row is not None else None

    # ---------------------------------------------------------------- 写

    @staticmethod
    def add(**fields: Any) -> dict[str, Any]:
        """新建主号。`email` 归一成小写（与既有行为一致）。"""
        payload = {k: v for k, v in fields.items() if k in _FIELDS}
        if "email" in payload:
            payload["email"] = str(payload["email"] or "").strip().lower()

        store = ICloudAccountRepository._store()
        if store is not None:
            created = store.insert(payload)
            return {k: created.get(k) for k in _FIELDS}

        from .registry import platform_session

        with platform_session("icloud") as s:
            row = ICloudAccountModel(**payload)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _from_model(row)

    @staticmethod
    def set_fields(account_id: Any, **fields: Any) -> Optional[dict[str, Any]]:
        """改若干字段（含 `updated_at` 自动刷新）。"""
        from datetime import datetime, timezone

        payload = {k: v for k, v in fields.items() if k in _FIELDS}
        payload["updated_at"] = datetime.now(timezone.utc)

        store = ICloudAccountRepository._store()
        if store is not None:
            updated = store.update(account_id, payload)
            return {k: (updated or {}).get(k) for k in _FIELDS} if updated else None

        from .registry import platform_session

        with platform_session("icloud") as s:
            row = s.get(ICloudAccountModel, int(account_id))
            if row is None:
                return None
            for key, value in payload.items():
                setattr(row, key, value)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _from_model(row)

    @staticmethod
    def delete(account_id: Any) -> bool:
        store = ICloudAccountRepository._store()
        if store is not None:
            return store.delete(account_id)

        from .registry import platform_session

        with platform_session("icloud") as s:
            row = s.get(ICloudAccountModel, int(account_id))
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True


icloud_account_repository = ICloudAccountRepository()

__all__ = ["ICloudAccountRepository", "icloud_account_repository", "TABLE"]
