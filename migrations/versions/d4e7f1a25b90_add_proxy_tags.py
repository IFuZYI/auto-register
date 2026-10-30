"""proxies.tags —— 代理标签（register / bind）

一个代理现在有两种用途，用标签表达：

- `register`：可用于**注册链路**（新号从这些出口出去）
- `bind`：可**绑定给账号**（账号的 `register_proxy` 只落在这一类上）

存逗号串（`"register,bind"`）而不是 JSON 数组：SQLite 与 JSON 文件两种后端
都能原样存，不需要为 JSON 后端单独做序列化分支。

`server_default=''`：存量代理升级后 tags 为空 —— 语义是「既不能注册也不能
绑定」，但它们**仍在池里**，`get_next()` 在没有带标签的代理时会回退到全部
活跃代理，所以升级瞬间不会把注册链路掐断（见 `core/proxy_pool.get_next`）。

Revision ID: d4e7f1a25b90
Revises: c3f8a1d64b27
Create Date: 2026-10-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "d4e7f1a25b90"
down_revision = "c3f8a1d64b27"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.add_column(
            sa.Column("tags", sa.String(), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("proxies") as batch_op:
        batch_op.drop_column("tags")
