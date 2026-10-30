"""把复合索引同步进迁移链（模型 ↔ 生产库 ↔ 空库三方对齐）

背景：`accounts` 列表页走 `WHERE platform=? ORDER BY created_at DESC`，
`task_logs` 列表页走 `WHERE platform=? ORDER BY id DESC` —— 这两条复合索引
是生产库由 `core/db/migrations.py` 的 legacy runner 建的，**迁移链里没有**。

后果有两个，都是真问题：

1. 空库 `alembic upgrade head` 建出来的表**没有**这两个索引，列表页一上来
   就是全表扫 + 排序；而生产库有 —— 两边性能不一样，测试也发现不了。
2. 模型里没声明它们，`alembic check` 会把它们当成「多余」并生成 `drop_index`。
   谁照着 autogenerate 跑一次，生产库的承重索引就没了。

同时顺手删掉 `ix_task_logs_platform`（单列）：复合索引的前缀就是 `platform`，
单列索引纯属重复占空间，生产库里本来也没有它。

幂等：先用 inspector 查一遍再动，重复执行/在已有索引的库上执行都不会报错。

Revision ID: e5b2c8f71a34
Revises: d4e7f1a25b90
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "e5b2c8f71a34"
down_revision = "d4e7f1a25b90"
branch_labels = None
depends_on = None


def _index_names(inspector, table: str) -> set[str]:
    try:
        return {item["name"] for item in inspector.get_indexes(table)}
    except Exception:  # noqa: BLE001 - 表不存在时 inspector 会抛，按「没有」处理
        return set()


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    accounts = _index_names(inspector, "accounts")
    if "ix_accounts_platform_created_at" not in accounts:
        op.create_index(
            "ix_accounts_platform_created_at", "accounts", ["platform", "created_at"]
        )

    task_logs = _index_names(inspector, "task_logs")
    if "ix_task_logs_platform" in task_logs:
        op.drop_index("ix_task_logs_platform", table_name="task_logs")
    if "ix_task_logs_platform_id" not in task_logs:
        op.create_index("ix_task_logs_platform_id", "task_logs", ["platform", "id"])


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    accounts = _index_names(inspector, "accounts")
    if "ix_accounts_platform_created_at" in accounts:
        op.drop_index("ix_accounts_platform_created_at", table_name="accounts")

    task_logs = _index_names(inspector, "task_logs")
    if "ix_task_logs_platform_id" in task_logs:
        op.drop_index("ix_task_logs_platform_id", table_name="task_logs")
    if "ix_task_logs_platform" not in task_logs:
        op.create_index("ix_task_logs_platform", "task_logs", ["platform"])
