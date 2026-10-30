"""代理池仓储：代理表读写的**唯一入口**，按后端分派。

与 `AccountRepository` 同型（见那个模块的 docstring）：调用方不再直接
`select(ProxyModel)`，后端切换对上层透明。

**为什么连读也要收进来**：只接写不接读会得到

    写：save_model → JSON
    读：session.exec(select(Model)) → SQLite → 看不到刚写的行

这是账号那轮踩过的坑的同型（那次方向相反），所以接一张表就是「读写一起接」。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from sqlmodel import Session, select

from .base import current_engine
from .models_infra import ProxyModel
from core.models.proxy import ProxyRecord

#: 该表在 JSON 后端下的表名（也是文件名前缀，见 `json_table.table_path`）
TABLE = "proxies"


class ProxyRepository:
    """代理池读写。带 `session` 的调用方走原生 SQLite（它有自己的事务）。"""

    # ---------------------------------------------------------------- 后端

    @staticmethod
    def _store():
        """JSON 后端下的表存储；SQLite 后端返回 None。"""
        from core.storage.json_table import store_for

        return store_for(TABLE)

    # ---------------------------------------------------------------- 读写

    @staticmethod
    def list_all(*, session: Session | None = None) -> list[Any]:
        store = ProxyRepository._store()
        if store is not None:
            return [ProxyRecord.from_dict(r) for r in store.all()]
        own = session is None
        sess = session or Session(current_engine())
        try:
            return [ProxyRecord.from_model(m) for m in sess.exec(select(ProxyModel)).all()]
        finally:
            if own:
                sess.close()

    @staticmethod
    def find_by_url(url: str, *, session: Session | None = None) -> Optional[Any]:
        wanted = str(url or "").strip()
        if not wanted:
            return None
        store = ProxyRepository._store()
        if store is not None:
            record = store.first(url=wanted)
            return ProxyRecord.from_dict(record) if record else None
        own = session is None
        sess = session or Session(current_engine())
        try:
            model = sess.exec(
                select(ProxyModel).where(ProxyModel.url == wanted)
            ).first()
            return ProxyRecord.from_model(model) if model else None
        finally:
            if own:
                sess.close()

    @staticmethod
    def add(*, url: str, region: str = "", session: Session | None = None) -> Any:
        store = ProxyRepository._store()
        if store is not None:
            return ProxyRecord.from_dict(
                store.insert({"url": str(url or ""), "region": str(region or "")})
            )
        own = session is None
        sess = session or Session(current_engine())
        try:
            row = ProxyModel(url=str(url or ""), region=str(region or ""))
            sess.add(row)
            # **写即提交**：原端点就是 `add` 后立刻 `commit`。不提交的话
            # 新行没有 id（SQLite 在 flush 时才分配），调用方拿到 `id=None`
            # —— DELETE `/proxies/None` 直接 422（实测）。
            sess.commit()
            sess.refresh(row)
            return ProxyRecord.from_model(row)
        finally:
            if own:
                sess.close()

    @staticmethod
    def save(row: Any, *, session: Session | None = None) -> Any:
        """写回一行（改动后用）。**不看 `session` 是否为 None**：带 session 是
        调用方有自己的事务，不代表该走 SQLite。"""
        store = ProxyRepository._store()
        if store is not None:
            store.update(getattr(row, "id", 0), {
                "url": getattr(row, "url", ""),
                "region": getattr(row, "region", ""),
                "is_active": bool(getattr(row, "is_active", True)),
                "success_count": int(getattr(row, "success_count", 0) or 0),
                "fail_count": int(getattr(row, "fail_count", 0) or 0),
                "last_checked": (
                    row.last_checked.isoformat()
                    if getattr(row, "last_checked", None)
                    else None
                ),
            })
            return row
        own = session is None
        sess = session or Session(current_engine())
        try:
            model = sess.get(ProxyModel, getattr(row, "id", None))
            if model is None:
                return row
            model.url = str(getattr(row, "url", "") or "")
            model.region = str(getattr(row, "region", "") or "")
            model.is_active = bool(getattr(row, "is_active", True))
            model.success_count = int(getattr(row, "success_count", 0) or 0)
            model.fail_count = int(getattr(row, "fail_count", 0) or 0)
            model.last_checked = getattr(row, "last_checked", None)
            sess.add(model)
            sess.commit()
            sess.refresh(model)
            return ProxyRecord.from_model(model)
        finally:
            if own:
                sess.close()

    @staticmethod
    def delete(row: Any, *, session: Session | None = None) -> bool:
        store = ProxyRepository._store()
        if store is not None:
            return store.delete(getattr(row, "id", 0))
        own = session is None
        sess = session or Session(current_engine())
        try:
            model = sess.get(ProxyModel, getattr(row, "id", None))
            if model is None:
                return False
            sess.delete(model)
            sess.commit()
            return True
        finally:
            if own:
                sess.close()

    @staticmethod
    def get_many(ids: Iterable[int], *, session: Session | None = None) -> list[Any]:
        wanted = [int(i) for i in ids]
        if not wanted:
            return []
        store = ProxyRepository._store()
        if store is not None:
            return [
                ProxyRecord.from_dict(r)
                for r in store.all()
                if int(r.get("id") or 0) in wanted
            ]
        own = session is None
        sess = session or Session(current_engine())
        try:
            return list(sess.exec(select(ProxyModel).where(ProxyModel.id.in_(wanted))).all())
        finally:
            if own:
                sess.close()


proxy_repository = ProxyRepository()

__all__ = ["ProxyRepository", "proxy_repository", "TABLE"]
