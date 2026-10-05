"""轻量 schema 迁移（SQLite 为主，幂等）。

分库之后同一套迁移要在**每个库**跑一遍：默认库 + 每个平台库。所以迁移函数都
接受 `engine` 参数，`run_migrations()` 负责遍历所有库。
"""
from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from .base import current_engine
from .models_platform import ICloudAliasModel
from .registry import platform_database_registry


def _is_sqlite(engine: Engine) -> bool:
    try:
        return engine.url.get_backend_name() == "sqlite"
    except Exception:
        return False


def _migrate_outlook_accounts_schema(engine: Engine | None = None) -> None:
    """Outlook 邮箱池补列（老库缺 account_type / mailapi_url / status）。"""
    engine = engine or current_engine()
    if not _is_sqlite(engine):
        return
    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info('outlook_accounts')").fetchall()
        if not rows:
            return
        existing_columns = {str(row[1]) for row in rows}
        if "account_type" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN account_type TEXT DEFAULT 'microsoft_oauth'"
            )
        if "mailapi_url" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN mailapi_url TEXT DEFAULT ''"
            )
        if "status" not in existing_columns:
            conn.exec_driver_sql(
                "ALTER TABLE outlook_accounts ADD COLUMN status TEXT DEFAULT 'available'"
            )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET account_type = 'microsoft_oauth' "
            "WHERE account_type IS NULL OR TRIM(account_type) = ''"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET mailapi_url = '' WHERE mailapi_url IS NULL"
        )
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET status = 'available' "
            "WHERE status IS NULL OR TRIM(status) = ''"
        )


def _sync_outlook_used_status(engine: Engine | None = None) -> None:
    """邮箱池里已注册过的邮箱标记为 used。

    只在本库内比较：分库后 outlook 池与账号表可能不在同一个库，跨库子查询不成立。
    跨库场景由 `sync_outlook_used_across_databases()` 兜底。
    """
    engine = engine or current_engine()
    with engine.begin() as conn:
        tables = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "outlook_accounts" not in tables or "accounts" not in tables:
            return
        conn.exec_driver_sql(
            "UPDATE outlook_accounts SET status = 'used' "
            "WHERE EXISTS (SELECT 1 FROM accounts "
            "WHERE lower(accounts.email) = lower(outlook_accounts.email)) "
            "AND status = 'available'"
        )


def sync_outlook_used_across_databases() -> None:
    """把「账号池里已注册的邮箱」回写到 Outlook 邮箱池。

    分库后账号可能散落在多个平台库，所以要把所有库里的邮箱收集起来，再对
    outlook 池所在库做一次标记。未分库时等价于旧行为。
    """
    emails: set[str] = set()
    from sqlmodel import select as _select

    from .models_account import AccountModel

    engines: list[Engine] = []
    seen_urls: set[str] = set()

    def _add(eng: Engine) -> None:
        url = str(eng.url)
        if url in seen_urls:
            return
        seen_urls.add(url)
        engines.append(eng)

    _add(current_engine())
    for platform in platform_database_registry.configured_platforms():
        _add(platform_database_registry.engine_for(platform))

    for eng in engines:
        try:
            with Session(eng) as session:
                rows = session.exec(_select(AccountModel.email)).all()
        except Exception:
            continue
        for value in rows:
            email = value if isinstance(value, str) else (value[0] if value else "")
            email = str(email or "").strip().lower()
            if email:
                emails.add(email)

    if not emails:
        return

    outlook_engine = platform_database_registry.engine_for("outlook")
    if not _is_sqlite(outlook_engine):
        return
    with outlook_engine.begin() as conn:
        tables = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "outlook_accounts" not in tables:
            return
        # 逐批标记（SQLite 变量上限 999，分批稳妥）
        pending = [
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT email FROM outlook_accounts WHERE status = 'available'"
            ).fetchall()
            if str(row[0] or "").strip()
        ]
        hit = [e for e in pending if e.strip().lower() in emails]
        for i in range(0, len(hit), 500):
            chunk = hit[i : i + 500]
            placeholders = ",".join("?" for _ in chunk)
            conn.exec_driver_sql(
                f"UPDATE outlook_accounts SET status = 'used' "
                f"WHERE lower(email) IN ({placeholders})",
                tuple(e.lower() for e in chunk),
            )


def _migrate_used_platforms_columns(engine: Engine | None = None) -> None:
    """给两个邮箱池补 `used_platforms` 列，并按历史数据回填。

    背景：邮箱是一次性的，但**只对用掉它的那个平台**一次性 —— 同一个地址
    注册过 ChatGPT 之后还能注册 Grok。旧的记账只有一个全局
    `status='used'` / `pool_status='used'`，记不出「被哪个平台用掉的」，
    取号时只能对所有平台一起锁死（实测：iCloud 池 22 个 used 别名里
    13 个 grok 从没用过，却全领不到）。

    回填分两级，能用证据就用证据、拿不准才保守：
      ① **有账号记录**：`accounts` 表里这个地址属于哪些平台，就只填那些 ——
         这是确凿证据，能真正解锁跨平台复用（实测 22 个 used 里 16 个有记录）。
      ② **查不到记录**：填成「所有已知平台」（保守）。少填会让地址被重新发给
         一个已经注册过的平台，撞「邮箱已被占用」白跑一轮；多填只损失一点
         复用率，且用户可在池页面手动放回。
      ③ 非 `used` 的行：留空（= 没有平台用过它）。
    """
    engine = engine or current_engine()
    if not _is_sqlite(engine):
        return

    from .base import mark_email_used_by
    from .models_account import AccountModel

    all_token = ""
    for name in _known_platforms():
        all_token = mark_email_used_by(all_token, name)

    # ① 从**所有**库收集「地址 → 平台」的证据（账号可能散落在多个平台库）。
    owner: dict[str, str] = {}
    for candidate_engine in _all_engines(engine):
        try:
            with Session(candidate_engine) as session:
                for email, platform in session.exec(
                    select(AccountModel.email, AccountModel.platform)
                ).all():
                    key = str(email or "").strip().lower()
                    name = str(platform or "").strip().lower()
                    if key and name:
                        owner[key] = mark_email_used_by(owner.get(key, ""), name)
        except Exception:
            continue

    with engine.begin() as conn:
        tables = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table, used_value in (
            ("outlook_accounts", "used"),
            ("icloud_aliases", "used"),
        ):
            if table not in tables:
                continue
            columns = {
                str(row[1])
                for row in conn.exec_driver_sql(f"PRAGMA table_info('{table}')").fetchall()
            }
            if "used_platforms" not in columns:
                conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN used_platforms TEXT DEFAULT ''"
                )
            if not all_token:
                continue
            status_col = "pool_status" if table == "icloud_aliases" else "status"
            email_col = "address" if table == "icloud_aliases" else "email"
            # ② 先按证据逐条填（有账号记录的那些）
            for addr, token in owner.items():
                if not token:
                    continue
                conn.exec_driver_sql(
                    f"UPDATE {table} SET used_platforms = ? "
                    f"WHERE lower({email_col}) = ? AND {status_col} = ? "
                    f"AND (used_platforms IS NULL OR TRIM(used_platforms) = '')",
                    (token, addr, used_value),
                )
            # ③ 剩下查不到记录的，保守填「所有已知平台」
            conn.exec_driver_sql(
                f"UPDATE {table} SET used_platforms = ? "
                f"WHERE {status_col} = ? "
                f"AND (used_platforms IS NULL OR TRIM(used_platforms) = '')",
                (all_token, used_value),
            )


def _all_engines(primary: Engine) -> list[Engine]:
    """所有可能有 `accounts` 表的库：默认库 + 各平台库（去重）。"""
    from .base import current_engine

    engines: list[Engine] = []
    seen: set[str] = set()

    def _add(candidate: Engine) -> None:
        key = str(candidate.url)
        if key in seen:
            return
        seen.add(key)
        engines.append(candidate)

    _add(primary)
    _add(current_engine())
    for name in platform_database_registry.configured_platforms():
        try:
            _add(platform_database_registry.engine_for(name))
        except Exception:
            continue
    return engines


def _known_platforms() -> list[str]:
    """所有「可能消耗邮箱」的平台名（注册平台 + 邮箱池本身之外的）。

    取注册表里的平台名。注册表加载失败时回落到两个内置平台 —— 迁移不能
    因为插件加载问题整个跳过，那会让老库永远缺列。
    """
    names: list[str] = []
    try:
        from core.registry import SUPPORTED_PLATFORMS

        names = [str(n).strip().lower() for n in SUPPORTED_PLATFORMS if str(n).strip()]
    except Exception:
        names = []
    for fallback in ("chatgpt", "grok"):
        if fallback not in names:
            names.append(fallback)
    return names


def _migrate_icloud_aliases_schema(engine: Engine | None = None) -> None:
    """iCloud 别名补 share_token 与 pool_status。

    `share_token` 每行不同随机值，不能用一条 UPDATE。
    `pool_status` 给老库补列：**默认值取 'unpooled'（未入池）而不是 'available'**。
    用户要求「账号邮箱要选择导入才能导入邮箱池，不是全部导入邮箱池」——
    迁移前库里那批别名从没经过一次显式入池动作，直接当成可领等于绕过这个要求，
    下一次注册就会拿它们去跑。补成 unpooled 后，用户在池页面勾选导入即可放行。
    """
    engine = engine or current_engine()
    if _is_sqlite(engine):
        with engine.begin() as conn:
            rows = conn.exec_driver_sql("PRAGMA table_info('icloud_aliases')").fetchall()
            if not rows:
                return
            columns = {str(row[1]) for row in rows}
            if "share_token" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE icloud_aliases ADD COLUMN share_token TEXT DEFAULT ''"
                )
            if "pool_status" not in columns:
                conn.exec_driver_sql(
                    "ALTER TABLE icloud_aliases ADD COLUMN pool_status TEXT DEFAULT 'unpooled'"
                )

    with Session(engine) as session:
        pending = session.exec(
            select(ICloudAliasModel).where(
                (ICloudAliasModel.share_token == None)  # noqa: E711 - SQL 里要 IS NULL
                | (ICloudAliasModel.share_token == "")
            )
        ).all()
        from .base import new_alias_share_token

        for row in pending:
            row.share_token = new_alias_share_token()
            session.add(row)
        if pending:
            session.commit()


def _ensure_account_email_unique(engine: Engine | None = None) -> None:
    """给 (platform, email) 建唯一索引 —— 邮箱唯一键的数据库级兜底。

    老库里可能存在同一 (platform, email) 的重复行（重构前没有约束）。这里先
    合并重复行（保留 id 最小的一条，其余字段按「非空优先」补齐），再建唯一索引。
    索引建不上（仍有重复）时不阻塞启动，只跳过——业务层的判重仍然生效。
    """
    engine = engine or current_engine()
    if not _is_sqlite(engine):
        return
    with engine.begin() as conn:
        tables = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "accounts" not in tables:
            return

        # ① 规范化邮箱（历史行可能带大写/空格）
        conn.exec_driver_sql(
            "UPDATE accounts SET email = lower(trim(email)) "
            "WHERE email <> lower(trim(email))"
        )

        # ② 合并重复行：同 (platform, email) 只留 id 最小的一条
        dup_groups = conn.exec_driver_sql(
            "SELECT platform, email, COUNT(*) c, MIN(id) keep_id "
            "FROM accounts GROUP BY platform, email HAVING c > 1"
        ).fetchall()
        for platform, email, _count, keep_id in dup_groups:
            rows = conn.exec_driver_sql(
                "SELECT id, password, user_id, region, token, status, "
                "cashier_url, extra_json FROM accounts "
                "WHERE platform = ? AND email = ? ORDER BY id",
                (platform, email),
            ).fetchall()
            merged = {
                "password": "",
                "user_id": "",
                "region": "",
                "token": "",
                "status": "registered",
                "cashier_url": "",
                "extra_json": "{}",
            }
            for row in rows:
                (
                    _id, password, user_id, region, token, status,
                    cashier_url, extra_json,
                ) = row
                for key, value in (
                    ("password", password), ("user_id", user_id), ("region", region),
                    ("token", token), ("cashier_url", cashier_url),
                ):
                    if not merged[key] and value:
                        merged[key] = value
                if merged["extra_json"] in ("", "{}") and extra_json not in ("", "{}"):
                    merged["extra_json"] = extra_json
                if status and status != "registered":
                    merged["status"] = status
            conn.exec_driver_sql(
                "UPDATE accounts SET password=?, user_id=?, region=?, token=?, "
                "status=?, cashier_url=?, extra_json=? WHERE id=?",
                (
                    merged["password"], merged["user_id"], merged["region"],
                    merged["token"], merged["status"],
                    merged["cashier_url"], merged["extra_json"], keep_id,
                ),
            )
            conn.exec_driver_sql(
                "DELETE FROM accounts WHERE platform = ? AND email = ? AND id <> ?",
                (platform, email, keep_id),
            )

        # ③ 唯一索引（业务键）
        try:
            conn.exec_driver_sql(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_accounts_platform_email "
                "ON accounts (platform, email)"
            )
        except Exception:
            # 仍有重复（并发写入等极端情况）时不阻塞启动
            pass


def _ensure_hot_query_indexes(engine: Engine | None = None) -> None:
    """给既有库补建热点查询索引。

    新库由 `create_all` 按模型定义建好索引，但**已存在的表不会被 `create_all`
    改动**——老库升级上来时这些索引是缺的，列表接口会退化成全表扫描。
    这里按表存在与否幂等补建。

    覆盖的查询模式：
    - 日志列表：`WHERE platform = ? ORDER BY id DESC`（api/tasks.py）
    - 账号列表：`WHERE platform = ? [AND status = ?] ORDER BY created_at DESC`
      （repository.list_all_accounts）
    """
    engine = engine or current_engine()
    if not _is_sqlite(engine):
        return
    with engine.begin() as conn:
        tables = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        # 表名 → [(索引名, 建索引语句)]
        wanted: dict[str, list[tuple[str, str]]] = {
            "task_logs": [
                (
                    "ix_task_logs_platform_id",
                    "CREATE INDEX IF NOT EXISTS ix_task_logs_platform_id "
                    "ON task_logs (platform, id)",
                ),
                (
                    "ix_task_logs_created_at",
                    "CREATE INDEX IF NOT EXISTS ix_task_logs_created_at "
                    "ON task_logs (created_at)",
                ),
                (
                    "ix_task_logs_status",
                    "CREATE INDEX IF NOT EXISTS ix_task_logs_status "
                    "ON task_logs (status)",
                ),
                (
                    "ix_task_logs_email",
                    "CREATE INDEX IF NOT EXISTS ix_task_logs_email "
                    "ON task_logs (email)",
                ),
            ],
            "accounts": [
                (
                    "ix_accounts_platform_created_at",
                    "CREATE INDEX IF NOT EXISTS ix_accounts_platform_created_at "
                    "ON accounts (platform, created_at)",
                ),
                (
                    "ix_accounts_status",
                    "CREATE INDEX IF NOT EXISTS ix_accounts_status "
                    "ON accounts (status)",
                ),
                (
                    "ix_accounts_created_at",
                    "CREATE INDEX IF NOT EXISTS ix_accounts_created_at "
                    "ON accounts (created_at)",
                ),
                (
                    "ix_accounts_updated_at",
                    "CREATE INDEX IF NOT EXISTS ix_accounts_updated_at "
                    "ON accounts (updated_at)",
                ),
            ],
            "icloud_aliases": [
                (
                    "ix_icloud_aliases_status",
                    "CREATE INDEX IF NOT EXISTS ix_icloud_aliases_status "
                    "ON icloud_aliases (status)",
                ),
                (
                    "ix_icloud_aliases_created_at",
                    "CREATE INDEX IF NOT EXISTS ix_icloud_aliases_created_at "
                    "ON icloud_aliases (created_at)",
                ),
                # `pool_status` 在模型里声明了 index=True，新库由 create_all 建索引；
                # 但老库是 ALTER TABLE 补的列，create_all 不会改动已存在的表，
                # 于是升级上来的库没有这条索引 —— `claim_alias` 的
                # WHERE pool_status IN (...) 退化成全表扫描，且新旧部署行为不一致。
                (
                    "ix_icloud_aliases_pool_status",
                    "CREATE INDEX IF NOT EXISTS ix_icloud_aliases_pool_status "
                    "ON icloud_aliases (pool_status)",
                ),
            ],
        }

        for table, indexes in wanted.items():
            if table not in tables:
                continue
            for name, sql in indexes:
                try:
                    conn.exec_driver_sql(sql)
                except Exception:
                    # 索引建不上（列缺失等）不该阻塞启动
                    pass


def _drop_stale_pool_tables_from_default_db(engine: Engine) -> None:
    """把默认库里遗留的空邮箱池表删掉（分库改造的收尾）。

    背景：邮箱池（icloud / outlook）改为默认分库后，老库里还留着
    `icloud_accounts` / `icloud_aliases` / `outlook_accounts` 三张表。
    它们**永远是空的**（新数据都写到 data/platforms/*.db 去了），但看起来
    和真表一模一样 —— 排查「数据到底在哪」时非常误导。

    两道保险，任一不满足就原样保留：
    1. 只对默认库执行（分库的表就是它的正式住处，不能删）；
    2. 只删**空表**。非空说明这个部署还没迁移过数据，删了就是丢数据 ——
       那种情况留给人工确认，代码不替用户做这个决定。
    """
    from .base import DATABASE_URL

    if str(engine.url) != str(DATABASE_URL):
        return
    if not _is_sqlite(engine):
        return

    candidates = ("icloud_accounts", "icloud_aliases", "outlook_accounts")
    with engine.begin() as conn:
        existing = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        for table in candidates:
            if table not in existing:
                continue
            try:
                count = conn.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                continue
            if int(count or 0) == 0:
                conn.exec_driver_sql(f"DROP TABLE {table}")


def _normalize_removed_account_statuses(engine: Engine | None = None) -> None:
    """把历史行里的已删除状态（trial / subscribed）归一成 registered。

    用户要求删除「试用中 / 已订阅」两个状态后，老库里可能还存着它们 ——
    不归一会让界面上重新冒出这两个值（读侧 `AccountStatus.normalize` 只是
    兜底，数据本身也该收敛）。幂等：每次启动跑一遍，无匹配时零改动。
    """
    engine = engine or current_engine()
    if not _is_sqlite(engine):
        return
    with engine.begin() as conn:
        tables = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "accounts" not in tables:
            return
        conn.exec_driver_sql(
            "UPDATE accounts SET status = 'registered' "
            "WHERE status IN ('trial', 'subscribed')"
        )


def _drop_trial_end_time_column(engine: Engine | None = None) -> None:
    """删掉老库 accounts 表上的 `trial_end_time` 列（状态精简的收尾）。

    该列只服务于已删除的「试用中」状态（零写入、零读取）—— 模型里已经去掉，
    但 `create_all` 不会改动已存在的表，老库会一直留着它。SQLite 3.35+ 支持
    `DROP COLUMN`（实测 3.53）；列不存在或库不支持时安静跳过（幂等）。
    """
    engine = engine or current_engine()
    if not _is_sqlite(engine):
        return
    with engine.begin() as conn:
        tables = {
            str(row[0])
            for row in conn.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "accounts" not in tables:
            return
        columns = {
            str(row[1])
            for row in conn.exec_driver_sql("PRAGMA table_info('accounts')").fetchall()
        }
        if "trial_end_time" not in columns:
            return
        try:
            conn.exec_driver_sql("ALTER TABLE accounts DROP COLUMN trial_end_time")
        except Exception:  # noqa: BLE001 - 旧版 SQLite 不支持 DROP COLUMN 时保留该列
            pass


def run_migrations(engine: Engine | None = None) -> None:
    """在指定库跑全部迁移；不传则跑「默认库 + 所有平台库」。"""
    if engine is not None:
        _run_one(engine)
        return

    from .base import current_engine

    _run_one(current_engine())
    for platform in platform_database_registry.configured_platforms():
        _run_one(platform_database_registry.engine_for(platform))


def _run_one(engine: Engine) -> None:
    _ensure_account_email_unique(engine)
    _migrate_outlook_accounts_schema(engine)
    # 补列必须排在任何**用 ORM 读这两张表**的迁移之前：SQLModel 的
    # `select(ICloudAliasModel)` 会把新加的 `used_platforms` 一起写进 SELECT，
    # 列还没补上时整条语句直接抛 `no such column`，启动就炸。
    _migrate_used_platforms_columns(engine)
    _migrate_icloud_aliases_schema(engine)
    _sync_outlook_used_status(engine)
    _ensure_hot_query_indexes(engine)
    _drop_stale_pool_tables_from_default_db(engine)
    # 状态精简：老库里的 trial / subscribed 归一成 registered，trial_end_time 列删除
    _normalize_removed_account_statuses(engine)
    _drop_trial_end_time_column(engine)


__all__ = [
    "run_migrations",
    "sync_outlook_used_across_databases",
    "_drop_stale_pool_tables_from_default_db",
    "_drop_trial_end_time_column",
    "_ensure_account_email_unique",
    "_ensure_hot_query_indexes",
    "_migrate_icloud_aliases_schema",
    "_migrate_outlook_accounts_schema",
    "_migrate_used_platforms_columns",
    "_normalize_removed_account_statuses",
]
