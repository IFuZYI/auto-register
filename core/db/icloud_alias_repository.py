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


def new_alias_share_token() -> str:
    """分享令牌（别名行的 `share_token`）。与 `services/icloud/aliases.py`
    里的同名函数保持一致 —— 那边是唯一实现，这里只是避免循环 import。"""
    import secrets

    return secrets.token_urlsafe(16)


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

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
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
        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
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

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
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

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
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

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
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

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
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

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
            row = s.get(ICloudAliasModel, int(alias_id))
            if row is None:
                return None
            row.pool_status = str(pool_status)
            s.add(row)
            s.commit()
            s.refresh(row)
            return _from_model(row)

    @staticmethod
    def get_by_share_token(token: str) -> Optional[dict[str, Any]]:
        """按分享令牌查（分享链接打开时用）。"""
        wanted = str(token or "")
        if not wanted:
            return None
        store = ICloudAliasRepository._store()
        if store is not None:
            record = store.first(share_token=wanted)
            return {k: record.get(k) for k in _FIELDS} if record else None

        from sqlmodel import select

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
            row = s.exec(
                select(ICloudAliasModel).where(ICloudAliasModel.share_token == wanted)
            ).first()
            return _from_model(row) if row is not None else None

    @staticmethod
    def list_ids_for_pool(
        *, account_ids: list[int], only_available: bool = True, platform: str = "",
        session: Any = None,
    ) -> list[int]:
        """筛出可用于取号的别名 id（按主号集合 + 状态）。

        `only_available` 为真时只认「可用 / 空」—— `used` 不在内（与
        `remote.py` 原来那条查询一致）。
        """
        from core.storage.query import any_of, eq, matches_all, to_sql

        wanted_accounts = [int(a) for a in account_ids]
        wanted_platform = str(platform or "").strip().lower()
        clauses = [
            any_of(*(eq("account_id", a) for a in wanted_accounts)),
            eq("status", "active"),
        ]
        if only_available:
            clauses.append(
                any_of(
                    eq("pool_status", "available"),
                    eq("pool_status", None),
                    eq("pool_status", ""),
                )
            )

        store = ICloudAliasRepository._store()
        if store is not None:
            def _ok(row: dict) -> bool:
                if not matches_all(clauses, row):
                    return False
                if not wanted_platform:
                    return True
                # 还要看这个地址有没有被该平台用掉（代码审查 2026-10-27 I3）：
                # 真正取号时会按 `used_platforms` 过滤，这里不同步的话会挑到
                # 「别名都已被本平台用完」的主号 → 领不到 → 回落生成 → 远程
                # 拒绝生成 → 仍是用户报的那个失败。
                used = str(row.get("used_platforms") or "")
                return f",{wanted_platform}," not in used

            return sorted(
                int(r.get("id") or 0) for r in store.all() if _ok(r)
            )

        from sqlmodel import select

        def _run(s) -> list[int]:
            stmt = (
                select(ICloudAliasModel.id)
                .where(ICloudAliasModel.account_id.in_(wanted_accounts))
                .where(ICloudAliasModel.status == "active")
            )
            if only_available:
                stmt = stmt.where(to_sql(ICloudAliasModel, clauses[2]))
            if wanted_platform:
                from sqlalchemy import or_ as _or

                stmt = stmt.where(
                    _or(
                        ICloudAliasModel.used_platforms.is_(None),
                        ICloudAliasModel.used_platforms == "",
                        ~ICloudAliasModel.used_platforms.contains(f",{wanted_platform},"),
                    )
                )
            return [int(v) for v in s.exec(stmt).all() if v is not None]

        # **接受调用方的 session**：调用方（`resolve_remote_account`）已经开好
        # 了一个会话，复用它既省连接、也让测试能喂内存库
        # （`tests/test_remote_account_alias_availability.py` 就是这么做的）。
        if session is not None:
            return _run(session)

        from .registry import platform_session

        with platform_session("icloud") as s:
            return _run(s)

    @staticmethod
    def count_by_account() -> dict[int, int]:
        """每个主号下的别名数（列表页显示用）。"""
        store = ICloudAliasRepository._store()
        if store is not None:
            counts: dict[int, int] = {}
            for row in store.all():
                key = int(row.get("account_id") or 0)
                counts[key] = counts.get(key, 0) + 1
            return counts

        from sqlmodel import select

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
            counts = {}
            for row in s.exec(select(ICloudAliasModel)).all():
                key = int(getattr(row, "account_id", 0) or 0)
                counts[key] = counts.get(key, 0) + 1
            return counts

    @staticmethod
    def list_created_since(account_id: int, since: Any) -> list[dict[str, Any]]:
        """本主号在 `since` 之后创建的别名（每小时生成额度判定用）。"""
        store = ICloudAliasRepository._store()
        if store is not None:
            out = []
            for row in store.all():
                if int(row.get("account_id") or 0) != int(account_id):
                    continue
                stamp = row.get("created_at")
                try:
                    if stamp is not None and stamp >= since:
                        out.append({k: row.get(k) for k in _FIELDS})
                except TypeError:
                    continue  # naive 与 aware 混用：跳过而不是炸
            return out

        from sqlmodel import select

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
            rows = s.exec(
                select(ICloudAliasModel)
                .where(ICloudAliasModel.account_id == int(account_id))
                .where(ICloudAliasModel.created_at >= since)
            ).all()
            return [_from_model(r) for r in rows]

    @staticmethod
    def delete_by_account(account_id: int) -> int:
        """删掉某主号下的全部别名，返回条数（主号删除时级联）。"""
        store = ICloudAliasRepository._store()
        if store is not None:
            doomed = [
                r.get("id") for r in store.all()
                if int(r.get("account_id") or 0) == int(account_id)
            ]
            return store.delete_many(doomed)

        from sqlmodel import select

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
            rows = s.exec(
                select(ICloudAliasModel).where(
                    ICloudAliasModel.account_id == int(account_id)
                )
            ).all()
            removed = 0
            for row in rows:
                s.delete(row)
                removed += 1
            if removed:
                s.commit()
            return removed

    @staticmethod
    def get_by_address(address: str) -> Optional[dict[str, Any]]:
        wanted = str(address or "").strip().lower()
        if not wanted:
            return None
        store = ICloudAliasRepository._store()
        if store is not None:
            record = store.first(address=wanted)
            return {k: record.get(k) for k in _FIELDS} if record else None

        from sqlmodel import select

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
            row = s.exec(
                select(ICloudAliasModel).where(ICloudAliasModel.address == wanted)
            ).first()
            return _from_model(row) if row is not None else None

    @staticmethod
    def mirror_remote(
        *, account_id: int, address: str, label: str = "", status: str = "",
        provider_id: str = "", source: str = "remote", created_at: Any = None,
        backfill_provider_only: bool = False,
    ) -> str:
        """远程镜像的别名写入，返回 `created` / `updated` / `skipped_local` /
        `foreign` / `noop`。

        **本地优先**（ADR-0001）：
        - 同地址本地已有行 → `skipped_local`，不建行不改本地行；
        - 本账号的远程行 → 更新镜像字段，**不动池记账**（用户可能已标
          used/in_use）；
        - 其它远程账号持有的同地址 → `foreign`，保留现有行（地址全局唯一）；
        - `backfill_provider_only`：只补空的 `provider_id`，不动来源与标签。
        """
        wanted = str(address or "").strip().lower()
        store = ICloudAliasRepository._store()

        if store is not None:
            existing = store.first(address=wanted)
            if existing is None:
                payload = {
                    "account_id": int(account_id), "address": wanted,
                    "label": str(label or ""), "status": str(status or "active"),
                    "provider_id": str(provider_id or ""), "source": str(source or "remote"),
                    "pool_status": "unpooled",
                }
                if created_at is not None:
                    payload["created_at"] = created_at
                store.insert(payload)
                return "created"

            if str(existing.get("source") or "") == "local":
                return "skipped_local"

            if backfill_provider_only:
                if provider_id and not str(existing.get("provider_id") or "").strip():
                    store.update(existing.get("id"), {"provider_id": str(provider_id)})
                    return "updated"
                return "noop"

            if int(existing.get("account_id") or 0) != int(account_id):
                return "foreign"

            patch = {"status": str(status or "active")}
            if label:
                patch["label"] = str(label)
            if provider_id:
                patch["provider_id"] = str(provider_id)
            store.update(existing.get("id"), patch)
            return "updated"

        from sqlmodel import select

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
            existing = s.exec(
                select(ICloudAliasModel).where(ICloudAliasModel.address == wanted)
            ).first()

            if existing is None:
                fresh = ICloudAliasModel(
                    account_id=int(account_id), address=wanted,
                    label=str(label or ""), status=str(status or "active"),
                    provider_id=str(provider_id or ""), source=str(source or "remote"),
                )
                fresh.pool_status = "unpooled"
                if created_at is not None:
                    fresh.created_at = created_at
                s.add(fresh)
                s.commit()
                return "created"

            if str(getattr(existing, "source", "") or "") == "local":
                return "skipped_local"

            if backfill_provider_only:
                if provider_id and not str(existing.provider_id or "").strip():
                    existing.provider_id = str(provider_id)
                    s.add(existing)
                    s.commit()
                    return "updated"
                return "noop"

            if int(getattr(existing, "account_id", 0) or 0) != int(account_id):
                return "foreign"

            existing.label = label or existing.label
            existing.status = str(status or "active")
            if provider_id:
                existing.provider_id = str(provider_id)
            from datetime import datetime, timezone

            existing.updated_at = datetime.now(timezone.utc)
            s.add(existing)
            s.commit()
            return "updated"

    @staticmethod
    def find_candidates(*, account_id: int, active_status: str = "active") -> list[dict[str, Any]]:
        """取号候选：本主号下「启用中 + 池状态可用/已用/空」的别名，按 id 升序。

        筛选条件用**查询层**写一份（`OR` 状态 + 状态等值），两种后端各自执行。
        与 `claim_alias` 原查询等价 —— 那里是 `status == active AND
        pool_status IN (available, used, NULL, "") ORDER BY id`。
        """
        from core.storage.query import any_of, eq, matches_all

        clauses = [
            eq("account_id", int(account_id)),
            eq("status", active_status),
            any_of(
                eq("pool_status", "available"),
                eq("pool_status", "used"),
                eq("pool_status", None),
                eq("pool_status", ""),
            ),
        ]

        store = ICloudAliasRepository._store()
        if store is not None:
            rows = [
                r for r in store.all() if matches_all(clauses, r)
            ]
            rows.sort(key=lambda r: int(r.get("id") or 0))
            return [{k: r.get(k) for k in _FIELDS} for r in rows]

        from sqlmodel import select

        from core.storage.query import to_sql

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
            rows = s.exec(
                select(ICloudAliasModel)
                .where(ICloudAliasModel.account_id == int(account_id))
                .where(ICloudAliasModel.status == active_status)
                # **必须经 `to_sql`**：`where()` 要的是 SQL 表达式，直接传
                # Clause 会 ArgumentError（实测）。
                .where(to_sql(ICloudAliasModel, clauses[2]))
                .order_by(ICloudAliasModel.id)
            ).all()
            return [_from_model(r) for r in rows]

    @staticmethod
    def upsert_from_sync(
        *, account_id: int, address: str, payload: dict[str, Any], account_email: str,
        return_flag: bool = False,
    ):
        """同步下来的别名写入（`_upsert_alias` 的仓储版）。

        三条业务规则原样保留（都是踩过坑的）：
        1. **新建的默认 `unpooled`** —— 必须用户勾选才进池（用户明确要求）；
        2. **已存在的行不动 `pool_status`** —— 用户可能已标成 used/in_use，
           一次同步不该把记账打回；
        3. **同地址的远程行、归属不同账号时跳过** —— 抢走它的 account_id 会让
           远程行归到另一个本地账号名下而 source 仍是 remote，之后删除/停用
           会按远程分支调远端服务，账实不符（ADR-0001）。
        """
        wanted = str(address or "").strip().lower()
        store = ICloudAliasRepository._store()

        if store is not None:
            existing = store.first(address=wanted)
            is_new = existing is None
            if existing is None:
                created = store.insert({
                    "account_id": int(account_id),
                    "address": wanted,
                    "label": str(payload.get("label") or ""),
                    "note": str(payload.get("note") or ""),
                    "status": str(payload.get("status") or "active"),
                    "provider_id": str(payload.get("provider_id") or ""),
                    "share_token": new_alias_share_token(),
                    "pool_status": "unpooled",
                })
                result = {k: created.get(k) for k in _FIELDS}
                return (result, True) if return_flag else result

            if str(existing.get("source") or "") == "remote" and int(
                existing.get("account_id") or 0
            ) != int(account_id):
                result = {k: existing.get(k) for k in _FIELDS}
                return (result, False) if return_flag else result

            merged = {
                "account_id": int(account_id),
                "label": str(payload.get("label") or "") or existing.get("label") or "",
                "note": str(payload.get("note") or "") or existing.get("note") or "",
                "status": str(payload.get("status") or "active"),
                "provider_id": str(payload.get("provider_id") or "") or existing.get("provider_id") or "",
                "share_token": existing.get("share_token") or new_alias_share_token(),
            }
            updated = store.update(existing.get("id"), merged)
            result = {k: (updated or {}).get(k) for k in _FIELDS}
            return (result, False) if return_flag else result

        # SQLite：与 JSON 分支同样的三条规则，用原生会话实现
        from sqlmodel import select

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
            row = s.exec(
                select(ICloudAliasModel).where(ICloudAliasModel.address == wanted)
            ).first()
            is_new = row is None
            if row is None:
                row = ICloudAliasModel(account_id=int(account_id), address=wanted)
                row.pool_status = "unpooled"
            elif str(getattr(row, "source", "") or "") == "remote" and int(
                getattr(row, "account_id", 0) or 0
            ) != int(account_id):
                result = _from_model(row)
                return (result, False) if return_flag else result

            row.account_id = int(account_id)
            row.label = str(payload.get("label") or "") or row.label
            row.note = str(payload.get("note") or "") or row.note
            row.status = str(payload.get("status") or "active")
            row.provider_id = str(payload.get("provider_id") or "") or row.provider_id
            row.share_token = row.share_token or new_alias_share_token()
            from datetime import datetime, timezone

            row.updated_at = datetime.now(timezone.utc)
            s.add(row)
            s.commit()
            s.refresh(row)
            result = _from_model(row)
            return (result, is_new) if return_flag else result

    @staticmethod
    def delete(alias_id: Any) -> bool:
        store = ICloudAliasRepository._store()
        if store is not None:
            return store.delete(alias_id)

        from .registry import platform_session

        # **用 `platform_session` 而不是 `registry.session_for`**：前者是
        # 模块级函数，测试可以 patch 它（`tests/test_remote_account_alias_availability.py`
        # 就是这么喂内存库的）。用后者会让那些测试读到真实库 → 挑错主号（实测）。
        with platform_session("icloud") as s:
            row = s.get(ICloudAliasModel, int(alias_id))
            if row is None:
                return False
            s.delete(row)
            s.commit()
            return True


icloud_alias_repository = ICloudAliasRepository()

__all__ = ["ICloudAliasRepository", "icloud_alias_repository", "TABLE"]
