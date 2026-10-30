"""dedupe accounts and add unique index

Revision ID: b7c1e4a92d30
Revises: 77cc4baded71
Create Date: 2026-11-02

账号的唯一业务键是 `(platform, email)`，但这条约束一直只靠应用层的
`repository.upsert` 的 select-then-write 保证 —— 并发注册同邮箱会插重复行，
进程在 select 与 insert 之间崩掉也一样。

本 revision 做三件事，顺序不能换：

1. **归一邮箱**：与 `core.db.base.normalize_email` 同口径（`strip().lower()`）。
   历史行可能带大写或首尾空格，不先归一会让「大小写不同的同一地址」逃过去重。
2. **去重**：同一 `(platform, email)` 只保留 id 最小的一行。
3. **建唯一索引**：`uq_accounts_platform_email`，建在 `(platform, lower(email))` 上。
   只建在原始 `email` 上不够 —— 大小写不同的同一地址会被当成两个值放过去
   （实测：`USER@X.AI` 能绕过 `(platform, email)` 的唯一索引）。

为什么必须在加密之前：`extra_json` 一旦加密，`lower(email)` 就无法在 SQL 里
计算，去重与唯一索引都做不了。
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "b7c1e4a92d30"
down_revision = "77cc4baded71"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_accounts_platform_email"


def upgrade() -> None:
    conn = op.get_bind()

    conn.execute(
        sa.text(
            "UPDATE accounts SET email = lower(trim(email)) "
            "WHERE email <> lower(trim(email))"
        )
    )

    conn.execute(
        sa.text(
            "DELETE FROM accounts WHERE id NOT IN ("
            "  SELECT MIN(id) FROM accounts GROUP BY platform, email"
            ")"
        )
    )

    # 老库可能已被手写迁移 `_ensure_account_email_unique` 建过**同名但不同定义**
    # 的索引（`(platform, email)` 裸列版）。同名索引不先删掉，下面这条表达式
    # 索引会直接 `index already exists`。
    conn.execute(sa.text(f"DROP INDEX IF EXISTS {INDEX_NAME}"))

    op.create_index(
        INDEX_NAME,
        "accounts",
        ["platform", sa.text("lower(email)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="accounts")
