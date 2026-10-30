"""accounts.id -> uuid

Revision ID: c3f8a1d64b27
Revises: b7c1e4a92d30
Create Date: 2026-11-02

把账号主键从自增 `INTEGER` 换成 UUID 字符串（见 ADR-0008）。旧的整数主键
落到 `legacy_id`，保留一个版本用于回滚与老日志对账。

为什么必须**重建表**而不是 `ALTER COLUMN`：SQLite 改不了主键类型，Alembic 的
batch 模式底层也是「建新表 → 拷数据 → 改名」，这里直接写清楚。

UUID 在 SQL 里生成（`randomblob`），不依赖 Python 侧逐行回填 —— 663 行也要
一次 `INSERT ... SELECT` 搞定，不把数据搬进内存。

**没有任何外键指向 `accounts`**（实测 `grep foreign_key="accounts` 为空），
所以重建表不会牵连别的表。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "c3f8a1d64b27"
down_revision = "b7c1e4a92d30"
branch_labels = None
depends_on = None

#: SQLite 版 UUIDv4：8-4-4-4-12，版本位固定 4、变体位取 89ab 之一。
_UUID_EXPR = (
    "lower("
    "hex(randomblob(4)) || '-' || "
    "hex(randomblob(2)) || '-4' || substr(hex(randomblob(2)), 2) || '-' || "
    "substr('89ab', abs(random()) % 4 + 1, 1) || substr(hex(randomblob(2)), 2) || '-' || "
    "hex(randomblob(6))"
    ")"
)

_COLUMNS = (
    "platform, email, password, user_id, region, token, status, "
    "cashier_url, extra_json, created_at, updated_at"
)

_INDEXES = (
    ("ix_accounts_platform", "platform", False),
    ("ix_accounts_email", "email", False),
    ("ix_accounts_status", "status", False),
    ("ix_accounts_created_at", "created_at", False),
    ("ix_accounts_updated_at", "updated_at", False),
)


def upgrade() -> None:
    conn = op.get_bind()

    conn.exec_driver_sql("ALTER TABLE accounts RENAME TO accounts_legacy_id")
    conn.exec_driver_sql(
        f"""
        CREATE TABLE accounts (
            id VARCHAR NOT NULL PRIMARY KEY
                DEFAULT ({_UUID_EXPR}),
            legacy_id INTEGER,
            platform VARCHAR NOT NULL,
            email VARCHAR NOT NULL,
            password VARCHAR NOT NULL,
            user_id VARCHAR NOT NULL,
            region VARCHAR NOT NULL,
            token VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            cashier_url VARCHAR NOT NULL,
            extra_json VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL
        )
        """
    )
    conn.exec_driver_sql(
        f"INSERT INTO accounts (id, legacy_id, {_COLUMNS}) "
        f"SELECT {_UUID_EXPR}, id, {_COLUMNS} FROM accounts_legacy_id"
    )
    conn.exec_driver_sql("DROP TABLE accounts_legacy_id")

    for name, column, unique in _INDEXES:
        conn.exec_driver_sql(
            f"CREATE INDEX {name} ON accounts ({column})"
        )
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX ix_accounts_legacy_id ON accounts (legacy_id)"
    )
    # 唯一业务键（表达式索引，见 revision b7c1e4a92d30）
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_accounts_platform_email "
        "ON accounts (platform, lower(email))"
    )


def downgrade() -> None:
    """回到自增整数主键：用 `legacy_id` 还原，缺失的按行号补。"""
    conn = op.get_bind()

    conn.exec_driver_sql("ALTER TABLE accounts RENAME TO accounts_uuid")
    conn.exec_driver_sql(
        """
        CREATE TABLE accounts (
            id INTEGER NOT NULL PRIMARY KEY,
            platform VARCHAR NOT NULL,
            email VARCHAR NOT NULL,
            password VARCHAR NOT NULL,
            user_id VARCHAR NOT NULL,
            region VARCHAR NOT NULL,
            token VARCHAR NOT NULL,
            status VARCHAR NOT NULL,
            cashier_url VARCHAR NOT NULL,
            extra_json VARCHAR NOT NULL,
            created_at VARCHAR NOT NULL,
            updated_at VARCHAR NOT NULL
        )
        """
    )
    conn.exec_driver_sql(
        f"INSERT INTO accounts (id, {_COLUMNS}) "
        f"SELECT COALESCE(legacy_id, rowid), {_COLUMNS} FROM accounts_uuid"
    )
    conn.exec_driver_sql("DROP TABLE accounts_uuid")

    for name, column, unique in _INDEXES:
        conn.exec_driver_sql(f"CREATE INDEX {name} ON accounts ({column})")
    conn.exec_driver_sql(
        "CREATE UNIQUE INDEX uq_accounts_platform_email "
        "ON accounts (platform, lower(email))"
    )
