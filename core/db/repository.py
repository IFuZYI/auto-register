"""账号仓储：账号池的唯一读写入口。

设计目标
--------
1. **邮箱是唯一业务键**：`(platform, normalize_email(email))` 唯一。
2. **已注册不重复注册**：`is_registered()` 供任务层在动手前判重。
3. **透明分库**：仓储按账号所属平台选库，调用方不需要知道库在哪。

为什么不让调用方直接 `select(AccountModel)`
------------------------------------------
分库之后「账号在哪张表」取决于平台配置。散落的裸查询会在分库时静默漏读
（比如 Grok 账号进了 Grok 库，但导出还在默认库查）。集中到仓储层，分库对
上层就是透明的。
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Iterable, Optional

from sqlalchemy import func
from sqlmodel import Session, select

from .base import _utcnow, normalize_email
from .models_account import AccountModel
from .registry import platform_database_registry

#: 邮箱池库：这些库里没有 `accounts` 表（它们是「邮箱来源」，不是注册平台）。
#: 遍历平台库找账号时必须跳过，否则会撞上 "no such table: accounts"。
MAILBOX_POOL_KEYS: frozenset[str] = frozenset({"icloud", "outlook"})


def account_db_key(platform: str) -> str:
    """把平台名映射到「存账号的库」的键：邮箱池键 → `""`（默认库）。

    池库里没有 `accounts` 表（只有 `icloud_accounts` / `icloud_aliases` /
    `outlook_accounts`），任何按平台去开池库会话查账号的代码都会撞上
    `no such table: accounts`，把一个「这个平台没有账号」的正常语义变成 500。

    映射到默认库而不是报错，是因为历史遗留行可能就在默认库里（分库迁移前
    写入的），查默认库才既安全又完整。池库自己的会话不走这里 —— 渠道代码用
    `mailbox_pool_session`。
    """
    name = str(platform or "").strip().lower()
    return "" if name in MAILBOX_POOL_KEYS else platform


def account_platform_keys(include: str = "") -> list[str]:
    """要扫描 `accounts` 表的平台库键（已排除邮箱池）。

    `include` 非空时只返回它（调用方明确指定了平台，不去猜）。

    但邮箱池键**即使被显式指定也要剔除**：池库里根本没有 `accounts` 表，
    拿它去查账号必然撞上 `no such table: accounts`。可达路径不是假设 ——
    `/accounts/:platform` 是个能直接打开的深链（菜单里不列 iCloud 了，但旧
    书签、手输地址都会走到），实测 `/api/accounts?platform=icloud` 与
    `?platform=outlook` 都返回过 500。
    """
    name = str(include or "").strip().lower()
    if name:
        return [] if name in MAILBOX_POOL_KEYS else [include]
    return [
        key
        for key in platform_database_registry.configured_platforms()
        if key not in MAILBOX_POOL_KEYS
    ]


class AccountRepository:
    """账号池读写。所有方法都接受可选 `session`（默认自动按平台选库）。"""

    # ------------------------------------------------------------ 会话

    @staticmethod
    def session_for(platform: str, session: Session | None = None) -> Session:
        """取该平台的库会话（公开 API，供 API 层直接写入用）。

        邮箱池键会落到默认库：池库里没有 `accounts` 表，按池键去查账号会 500。
        池库自己的会话用 `mailbox_pool_session`。
        """
        return session if session is not None else platform_database_registry.session_for(
            account_db_key(platform)
        )

    # 兼容内部旧名
    _session_for = session_for

    # ------------------------------------------------------------ 判重

    @staticmethod
    def find_by_email(
        platform: str,
        email: str,
        *,
        session: Session | None = None,
    ) -> Optional[AccountModel]:
        """按 (平台, 邮箱) 查账号。邮箱大小写无关。"""
        key = normalize_email(email)
        if not key:
            return None
        own = session is None
        sess = AccountRepository._session_for(platform, session)
        try:
            return sess.exec(
                select(AccountModel)
                .where(AccountModel.platform == platform)
                .where(AccountModel.email == key)
            ).first()
        finally:
            if own:
                sess.close()

    @staticmethod
    def is_registered(
        platform: str,
        email: str,
        *,
        session: Session | None = None,
        include_invalid: bool = True,
    ) -> bool:
        """该邮箱在此平台是否已注册过（默认含失效账号，避免重复占用邮箱）。

        `include_invalid=False` 时只把「还有效」的账号视为已注册，用于需要重试
        失效账号的场景。有效性判定统一走 `AccountStatus.is_active`。
        """
        row = AccountRepository.find_by_email(platform, email, session=session)
        if row is None:
            return False
        if include_invalid:
            return True
        # 局部导入：AccountStatus 定义在 core.base_platform，模块级导入会让
        # core.db 包初始化期就拉进插件基类，增加循环导入风险
        from core.base_platform import AccountStatus

        # counts_as_registered 与改动前的 `status not in ("invalid","expired")`
        # 完全等价——未知状态仍算「已注册」，保守地避免重复注册
        return AccountStatus.counts_as_registered(row.status)

    @staticmethod
    def registered_emails(
        platform: str,
        emails: Iterable[str],
        *,
        session: Session | None = None,
    ) -> set[str]:
        """批量判重：返回其中已注册的邮箱（规范化后）。"""
        keys = {normalize_email(e) for e in emails if normalize_email(e)}
        if not keys:
            return set()
        own = session is None
        sess = AccountRepository._session_for(platform, session)
        try:
            rows = sess.exec(
                select(AccountModel.email)
                .where(AccountModel.platform == platform)
                .where(AccountModel.email.in_(keys))
            ).all()
            return {normalize_email(r if isinstance(r, str) else r[0]) for r in rows}
        finally:
            if own:
                sess.close()

    # ------------------------------------------------------------ 写入

    @staticmethod
    def upsert(
        account,
        *,
        session: Session | None = None,
    ) -> AccountModel:
        """按 (平台, 邮箱) upsert 一个账号。

        接受两种对象：
        - `core.base_platform.Account`（插件产出，字段是 `extra` 字典）
        - `core.db.AccountModel`（API 层已有行，字段是 `extra_json` 字符串）

        传入 `AccountModel` 时会保留其 `extra_json`（不解析再序列化，避免
        丢掉仓储不认识的键）。
        """
        platform = str(getattr(account, "platform", "") or "")
        email = normalize_email(getattr(account, "email", "") or "")
        if not platform or not email:
            raise ValueError("账号缺少 platform 或 email，无法落库")

        # 区分两种输入：AccountModel 自带 extra_json，Account 用 extra 字典
        is_model = isinstance(account, AccountModel)
        if is_model:
            extra_json = str(getattr(account, "extra_json", "") or "{}")
            try:
                extra = json.loads(extra_json) if extra_json else {}
            except (json.JSONDecodeError, TypeError):
                extra = {}
        else:
            extra = getattr(account, "extra", None) or {}
            extra_json = json.dumps(extra, ensure_ascii=False)

        own = session is None
        sess = AccountRepository._session_for(platform, session)
        try:
            existing = sess.exec(
                select(AccountModel)
                .where(AccountModel.platform == platform)
                .where(AccountModel.email == email)
            ).first()
            status = getattr(account, "status", None)
            status_value = getattr(status, "value", None) or str(status or "registered")
            # 状态精简的写侧兜底：旧客户端/旧导入文件可能提交已删除的
            # trial / subscribed —— 落库前统一归一。历史行由启动迁移
            # （`_normalize_removed_account_statuses`）收敛；写侧（创建/导入/
            # 更新）在这里归一，防止旧客户端/旧导入文件把已删值写回库。
            from core.base_platform import AccountStatus

            status_value = AccountStatus.normalize(status_value)
            # cashier_url 有两个来源：账号表的列（API 层导入/创建写这里）与
            # extra（插件形态的账号走这里）。**列上有值时以列为准** —— 旧实现
            # 无条件用 `extra.get("cashier_url")` 重算，而 JSON 导入恰好把该字段
            # 写在列上、又把它排除在 extra 之外，于是刚导入的值被就地抹掉
            # （带值导入丢失、不带值导入清空旧值，实测两端都中）。
            # 两个来源都没提这个字段时保持原值：沉默不等于清空。
            incoming_cashier = str(getattr(account, "cashier_url", "") or "")
            fallback_cashier = (
                str(extra.get("cashier_url") or "") if isinstance(extra, dict) else ""
            )
            if existing is not None:
                existing.password = getattr(account, "password", "") or existing.password
                existing.user_id = getattr(account, "user_id", "") or ""
                existing.region = getattr(account, "region", "") or ""
                existing.token = getattr(account, "token", "") or ""
                existing.status = status_value
                existing.extra_json = extra_json
                if incoming_cashier.strip():
                    existing.cashier_url = incoming_cashier
                elif isinstance(extra, dict) and "cashier_url" in extra:
                    existing.cashier_url = fallback_cashier
                existing.updated_at = _utcnow()
                sess.add(existing)
                sess.commit()
                sess.refresh(existing)
                return existing

            row = AccountModel(
                platform=platform,
                email=email,
                password=getattr(account, "password", "") or "",
                user_id=getattr(account, "user_id", "") or "",
                region=getattr(account, "region", "") or "",
                token=getattr(account, "token", "") or "",
                status=status_value,
                extra_json=extra_json,
                cashier_url=incoming_cashier if incoming_cashier.strip() else fallback_cashier,
            )
            # 调用方显式带 `created_at` 时保留它（导入导出往返要留住注册时间；
            # 排序与筛选都依赖这个值）。不带则用模型的默认（当前时间）。
            #
            # **只认 datetime**：插件产出的 `core.base_platform.Account.created_at`
            # 是 unix 秒（int），直接塞进 datetime 列会让 SQLAlchemy 在绑定参数时
            # 抛 `'int' object has no attribute 'utcoffset'`。那条路本来就该用
            # 当前时间，不该被覆盖。
            created_at = getattr(account, "created_at", None)
            if isinstance(created_at, datetime):
                row.created_at = created_at
            sess.add(row)
            sess.commit()
            sess.refresh(row)
            return row
        finally:
            if own:
                sess.close()

    # ------------------------------------------------------------ 查询

    @staticmethod
    def list_accounts(
        platform: str = "",
        *,
        status: str = "",
        email_contains: str = "",
        session: Session | None = None,
    ) -> list[AccountModel]:
        """列出账号。给了 platform 就查该平台的库，否则查默认库。"""
        own = session is None
        sess = AccountRepository._session_for(platform or "", session)
        try:
            q = select(AccountModel)
            if platform:
                q = q.where(AccountModel.platform == platform)
            if status:
                q = q.where(AccountModel.status == status)
            if email_contains:
                q = q.where(AccountModel.email.contains(normalize_email(email_contains)))
            return list(sess.exec(q).all())
        finally:
            if own:
                sess.close()

    @staticmethod
    def get(account_id: int, platform: str = "", *, session: Session | None = None) -> Optional[AccountModel]:
        """按 id 取账号。

        给了 `platform` 只查该平台的库；没给就跨库查找（默认库 + 所有平台库）。
        API 层的 `GET /accounts/{id}` 只有 id，不跨库会找不到分库平台的账号。

        **ID 撞号**：账号 id 是**每库各自自增**的，不同库里的同一个数字指向不同
        账号。所以指定 `platform` 时，默认库那一步必须再按 platform 过滤，否则
        会命中默认库里另一个平台的同 id 行（实测会把别的平台账号取出来）。
        """
        if session is not None:
            return session.get(AccountModel, account_id)

        if platform:
            # 池键没有 `accounts` 表，走默认库（account_db_key 把池键映射成 ""）
            with platform_database_registry.session_for(account_db_key(platform)) as sess:
                row = sess.get(AccountModel, account_id)
                if row is not None:
                    return row
            # 平台库里没有 → 再找默认库的历史遗留行（必须是同平台，见上文撞号说明）
            return AccountRepository._get_in_default(account_id, platform)

        for key in account_platform_keys():
            with platform_database_registry.session_for(key) as sess:
                row = sess.get(AccountModel, account_id)
                if row is not None:
                    return row

        return AccountRepository._get_in_default(account_id, "")

    @staticmethod
    def _get_in_default(account_id: int, platform: str) -> Optional[AccountModel]:
        """在默认库里按 id 取，`platform` 非空时附加同平台约束（防 ID 撞号）。"""
        from .base import current_engine

        with Session(current_engine()) as sess:
            if not platform:
                return sess.get(AccountModel, account_id)
            return sess.exec(
                select(AccountModel)
                .where(AccountModel.id == account_id)
                .where(func.lower(AccountModel.platform) == platform.strip().lower())
            ).first()

    @staticmethod
    def delete(account_id: int, platform: str = "", *, session: Session | None = None) -> bool:
        """按 id 删除账号（没给 platform 时跨库查找）。

        与 `get` 同样的 ID 撞号问题：指定 `platform` 时默认库那一步要再按
        platform 过滤，否则会删掉默认库里另一个平台的同 id 账号。
        """
        if session is not None:
            row = session.get(AccountModel, account_id)
            if row is None:
                return False
            session.delete(row)
            session.commit()
            return True

        targets = account_platform_keys(platform) + [""]
        for key in targets:
            sess = platform_database_registry.session_for(key)
            try:
                if key or not platform:
                    row = sess.get(AccountModel, account_id)
                else:
                    # 默认库 + 指定 platform：必须带 platform 过滤
                    row = sess.exec(
                        select(AccountModel)
                        .where(AccountModel.id == account_id)
                        .where(
                            func.lower(AccountModel.platform)
                            == platform.strip().lower()
                        )
                    ).first()
                if row is None:
                    continue
                sess.delete(row)
                sess.commit()
                return True
            finally:
                sess.close()
        return False

    # ------------------------------------------------------------ 统计

    @staticmethod
    def get_many(
        ids: list[int], platform: str = "", *, session: Session | None = None
    ) -> list[AccountModel]:
        """按 id 批量取账号，保持传入顺序。

        为什么需要：`get()` 是「每个 id 一个新会话、逐库试」，调用方在循环里
        调它就成了 N+1 —— 100 个 id × 2 个库 = 最多 200 次开会话。这里改成
        「每库一次 IN 查询」，跨库合并后按传入顺序回填。

        去重语义与 `get()` 一致：平台库优先于默认库的历史遗留行。
        """
        wanted = [int(v) for v in ids]
        if not wanted:
            return []

        found: dict[int, AccountModel] = {}
        wanted_platform = platform.strip().lower()

        # SQLite 变量上限 32766：id 列表超过后 IN 查询直接炸
        # （实测 40000 个 id → OperationalError: too many SQL variables）。
        # 分块查询，调用方传多大都安全；500 与 upsert_batch 同口径。
        _CHUNK = 500

        def _collect(sess: Session, *, enforce_platform: bool) -> None:
            base = select(AccountModel)
            # ID 是每库自增的，同一数字在不同库里指向不同账号。指定 platform 时
            # 默认库那一步必须再按 platform 过滤，否则会取到别的平台的同 id 行。
            if enforce_platform and wanted_platform:
                base = base.where(func.lower(AccountModel.platform) == wanted_platform)
            keys = sorted(set(wanted))
            for i in range(0, len(keys), _CHUNK):
                chunk = keys[i : i + _CHUNK]
                q = base.where(AccountModel.id.in_(chunk))
                for row in sess.exec(q).all():
                    # 先到先得：平台库先扫，默认库的历史行不覆盖
                    found.setdefault(int(row.id or 0), row)

        if session is not None:
            _collect(session, enforce_platform=False)
        else:
            targets = account_platform_keys(wanted_platform)
            for key in targets:
                with platform_database_registry.session_for(key) as sess:
                    _collect(sess, enforce_platform=False)

            from .base import current_engine

            with Session(current_engine()) as sess:
                _collect(sess, enforce_platform=True)

        # 按传入顺序回填，并去掉重复 id（保留首次出现位置）
        ordered = list(dict.fromkeys(wanted))
        return [found[i] for i in ordered if i in found]

    @staticmethod
    def delete_many(
        ids: list[int], platform: str = "", *, session: Session | None = None
    ) -> tuple[list[int], list[int]]:
        """按 id 批量删除，返回 `(deleted, not_found)`。

        与逐个 `delete()` 的区别：每库只开一次会话、分块 IN 查询，避免
        N+1（批量删除接口一次最多 1000 个 id；仓储层不假设调用方已限流，
        分块到 SQLite 变量上限之下，传多大都安全）。
        """
        wanted = list(dict.fromkeys(int(v) for v in ids))
        if not wanted:
            return [], []
        deleted: list[int] = []
        remaining = set(wanted)
        wanted_platform = platform.strip().lower()

        # SQLite 变量上限 32766：与 get_many 同口径分块，500 一批。
        _CHUNK = 500

        def _drop(sess: Session, *, enforce_platform: bool) -> None:
            if not remaining:
                return
            base = select(AccountModel)
            # 同 get_many：指定 platform 时默认库那一步要带 platform 过滤，
            # 否则 ID 撞号会删掉别的平台的账号（数据丢失，不可恢复）。
            if enforce_platform and wanted_platform:
                base = base.where(func.lower(AccountModel.platform) == wanted_platform)
            keys = sorted(remaining)
            for i in range(0, len(keys), _CHUNK):
                if not remaining:
                    return
                chunk = [k for k in keys[i : i + _CHUNK] if k in remaining]
                if not chunk:
                    continue
                rows = sess.exec(base.where(AccountModel.id.in_(chunk))).all()
                for row in rows:
                    remaining.discard(int(row.id or 0))
                    deleted.append(int(row.id or 0))
                    sess.delete(row)
                if rows:
                    sess.commit()

        if session is not None:
            _drop(session, enforce_platform=False)
        else:
            targets = account_platform_keys(wanted_platform)
            for key in targets:
                with platform_database_registry.session_for(key) as sess:
                    _drop(sess, enforce_platform=False)

            from .base import current_engine

            with Session(current_engine()) as sess:
                _drop(sess, enforce_platform=True)

        # 保持传入顺序，未命中的按同样顺序返回
        deleted_set = set(deleted)
        return [i for i in wanted if i in deleted_set], [i for i in wanted if i not in deleted_set]

    @staticmethod
    def stats(*, session: Session | None = None) -> dict[str, Any]:
        """各平台账号数与状态分布。

        分库时逐个平台库统计，再合并——否则只能看到默认库里的平台。
        """
        by_platform: dict[str, int] = {}
        by_status: dict[str, int] = {}
        total = 0
        seen: set[str] = set()

        def _accumulate(rows: Iterable[AccountModel]) -> None:
            nonlocal total
            for acc in rows:
                total += 1
                by_platform[acc.platform] = by_platform.get(acc.platform, 0) + 1
                by_status[acc.status] = by_status.get(acc.status, 0) + 1

        def _scan(platform_key: str, sess: Session) -> None:
            rows = list(sess.exec(select(AccountModel)).all())
            if platform_key:
                # 平台库只应包含本平台账号；万一混入别的平台（老库迁移），
                # 让它归属真实 platform 字段，而不是被重复统计
                rows = [r for r in rows if str(r.platform or "").lower() == platform_key]
            _accumulate(rows)

        if session is not None:
            _scan("", session)
            return {"total": total, "by_platform": by_platform, "by_status": by_status}

        for platform in account_platform_keys():
            with platform_database_registry.session_for(platform) as sess:
                _scan(platform, sess)
            seen.add(platform)

        # 默认库：包含未分库平台的账号 + 分库平台遗留的历史行
        from .base import current_engine

        with Session(current_engine()) as sess:
            rows = list(sess.exec(select(AccountModel)).all())
            rows = [
                r for r in rows
                if str(r.platform or "").lower() not in seen
            ]
            _accumulate(rows)

        return {"total": total, "by_platform": by_platform, "by_status": by_status}

    # ------------------------------------------------------------ 跨库列表

    @staticmethod
    def _list_filters(
        *,
        platform: str = "",
        status: str = "",
        email_contains: str = "",
        created_at_start=None,
        created_at_end=None,
    ) -> list:
        """构造跨库列表的公共 WHERE 条件（各库 SQL 一致）。"""
        conds = []
        if platform:
            conds.append(AccountModel.platform == platform.strip().lower())
        if status:
            conds.append(AccountModel.status == status)
        if email_contains:
            conds.append(AccountModel.email.contains(normalize_email(email_contains)))
        # created_at 是普通列，能下推到 SQL；之前只在 Python 里过滤，
        # 导致「按时间筛选」也必须把整表读出来。
        if created_at_start is not None:
            conds.append(AccountModel.created_at >= created_at_start)
        if created_at_end is not None:
            conds.append(AccountModel.created_at <= created_at_end)
        return conds

    @staticmethod
    def platforms_by_email(
        emails: Iterable[str] | None = None,
        *,
        platform: str = "",
        session: Session | None = None,
    ) -> dict[str, set[str]]:
        """`地址 → 已注册平台集合`，**跨所有库**扫描 `accounts` 表。

        用途：邮箱池页面要显示「这个地址注册过哪些平台」，取号时也要拿它当
        **权威证据**再比对一次（池自己的 `used_platforms` 只是记账，可能因为
        任务中途崩掉、或老数据回填不准而缺项）。

        为什么不能只查默认库：账号跟随平台分库（`DATABASE_URL_GROK` 等），
        分库之后同一个地址在默认库里查不到 —— 表现为「明明注册过，取号还是
        把同一个地址发出去」，然后撞「邮箱已被占用」白跑一轮。

        `emails` 给定则只关心这些地址（取号热路径上只问候选的那几个）；
        不给定则返回全部。`platform` 给定则只统计该平台的账号 —— 邮箱只对
        用掉它的那个平台一次性，跨平台复用是设计内行为。

        池库里没有 `accounts` 表（`account_platform_keys` 已把池键剔除），
        某个库缺表时跳过而不是抛错：缺表 = 这个库里没有账号记录。
        """
        wanted = {
            normalize_email(e)
            for e in (emails if emails is not None else [])
            if normalize_email(e)
        }
        only = str(platform or "").strip().lower()
        found: dict[str, set[str]] = {}

        def _collect(sess: Session) -> None:
            try:
                q = select(AccountModel.platform, AccountModel.email)
                if only:
                    q = q.where(func.lower(AccountModel.platform) == only)
                if wanted:
                    # SQLite 变量上限 999，分批问
                    keys = sorted(wanted)
                    for i in range(0, len(keys), 500):
                        chunk = keys[i : i + 500]
                        for name, email in sess.exec(q.where(AccountModel.email.in_(chunk))).all():
                            key = normalize_email(email)
                            value = str(name or "").strip().lower()
                            if key and value:
                                found.setdefault(key, set()).add(value)
                    return
                for name, email in sess.exec(q).all():
                    key = normalize_email(email)
                    value = str(name or "").strip().lower()
                    if key and value:
                        found.setdefault(key, set()).add(value)
            except Exception:
                # 该库没有 accounts 表（池库/新库）或库不可用 —— 当作没有记录。
                # 宁可少判重也不能让取号因为一次查询失败整体挂掉。
                return

        if session is not None:
            _collect(session)
            return found

        for key in account_platform_keys(only):
            with platform_database_registry.session_for(key) as sess:
                _collect(sess)

        # 默认库：未分库平台的账号 + 分库迁移前的历史遗留行
        from .base import current_engine

        with Session(current_engine()) as sess:
            _collect(sess)
        return found

    @staticmethod
    def count_all_accounts(
        *,
        platform: str = "",
        status: str = "",
        email_contains: str = "",
        created_at_start=None,
        created_at_end=None,
    ) -> int:
        """跨库计数（不下拉整行），供分页接口算 total。

        与 `list_all_accounts` 用同一组条件。
        - 指定 platform：单库 COUNT(*)，直接可信。
        - 未指定：跨库相加会把「平台库 + 默认库的历史遗留行」算两次，
          而列表接口按 (platform, email) 去重，所以改按唯一键去重计数
          （只取两列，不加载整行）。
        """
        conds = AccountRepository._list_filters(
            platform=platform,
            status=status,
            email_contains=email_contains,
            created_at_start=created_at_start,
            created_at_end=created_at_end,
        )

        if not platform:
            return len(AccountRepository._distinct_keys(conds))

        from sqlalchemy import func

        # 邮箱池键没有 `accounts` 表：平台库那一步跳过，直接数默认库。
        # 不能直接 return 0 —— `list_all_accounts` 对池键同样只扫默认库（分库
        # 迁移前的历史遗留行可能在那里），count 与 list 必须口径一致，否则分页
        # 的 total 与 items 会对不上。
        keys = account_platform_keys(platform.strip().lower())
        if not keys:
            return AccountRepository._count_in_default(conds)

        with platform_database_registry.session_for(keys[0]) as sess:
            q = select(func.count()).select_from(AccountModel)
            for c in conds:
                q = q.where(c)
            return int(sess.exec(q).one() or 0)

    @staticmethod
    def _count_in_default(conds: list) -> int:
        """在默认库里按条件计数（池键走这条路）。"""
        from sqlalchemy import func

        from .base import current_engine

        with Session(current_engine()) as sess:
            q = select(func.count()).select_from(AccountModel)
            for c in conds:
                q = q.where(c)
            return int(sess.exec(q).one() or 0)

    @staticmethod
    def _distinct_keys(conds: list) -> set[tuple[str, str]]:
        """跨库收集 (platform, email) 去重键，只取两列不加载整行。"""
        seen: set[tuple[str, str]] = set()

        def _collect(sess: Session) -> None:
            q = select(AccountModel.platform, AccountModel.email)
            for c in conds:
                q = q.where(c)
            for row in sess.exec(q).all():
                seen.add(
                    (
                        str(row[0] or "").strip().lower(),
                        str(row[1] or "").strip().lower(),
                    )
                )

        for key in account_platform_keys():
            with platform_database_registry.session_for(key) as sess:
                _collect(sess)

        from .base import current_engine

        with Session(current_engine()) as sess:
            _collect(sess)
        return seen

    @staticmethod
    def list_all_accounts(
        *,
        platform: str = "",
        status: str = "",
        email_contains: str = "",
        created_at_start=None,
        created_at_end=None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AccountModel]:
        """列出账号，**跨所有库**。

        指定 `platform` 时只查该平台的库（快）；不指定时扫描默认库 + 所有
        已配置的平台库并合并 —— 否则列表接口在分库后会漏掉平台账号。

        `limit`/`offset` 会把分页下推到 SQL（每库取 `offset+limit` 行再合并
        截断），避免「整表读进内存只为了切一页」。跨库合并语义下 offset 需要
        在合并后统一施加，所以每库先多取 `offset + limit` 行。

        结果按 `created_at` 倒序（新的在前），与前端列表预期一致。
        """
        conds = AccountRepository._list_filters(
            platform=platform,
            status=status,
            email_contains=email_contains,
            created_at_start=created_at_start,
            created_at_end=created_at_end,
        )
        rows: list[AccountModel] = []
        # 跨库去重：同一 (platform, email) 可能同时存在于平台库与默认库
        # （分库迁移前的历史遗留行）。平台库是权威源，先扫、优先保留。
        seen_keys: set[tuple[str, str]] = set()

        def _add(row: AccountModel) -> None:
            key = (
                str(row.platform or "").strip().lower(),
                str(row.email or "").strip().lower(),
            )
            if key in seen_keys:
                return
            seen_keys.add(key)
            rows.append(row)

        # 每库最多需要 offset+limit 行（跨库合并后可能全被前面的库占满）
        per_db = None if limit is None else max(0, int(offset)) + int(limit)

        targets = account_platform_keys(platform.strip().lower())
        for key in targets:
            with platform_database_registry.session_for(key) as sess:
                q = select(AccountModel)
                for c in conds:
                    q = q.where(c)
                q = q.order_by(AccountModel.created_at.desc())
                if per_db is not None:
                    q = q.limit(per_db)
                for row in sess.exec(q).all():
                    _add(row)

        # 默认库：未分库平台的账号 + 分库平台的历史遗留行（去重后并入）
        from .base import current_engine

        with Session(current_engine()) as sess:
            q = select(AccountModel)
            for c in conds:
                q = q.where(c)
            q = q.order_by(AccountModel.created_at.desc())
            if per_db is not None:
                q = q.limit(per_db)
            for row in sess.exec(q).all():
                _add(row)

        rows.sort(key=lambda r: (r.created_at is None, r.created_at), reverse=True)
        if limit is not None:
            start = max(0, int(offset))
            rows = rows[start : start + int(limit)]
        return rows


account_repository = AccountRepository()


__all__ = ["AccountRepository", "account_repository"]
