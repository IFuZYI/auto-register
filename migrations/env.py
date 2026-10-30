"""Alembic 环境：schema 的真相源是 SQLModel.metadata。

为什么不再手写补列：`core/db/migrations.py` 791 行手写幂等 DDL，每加一列都要
写「判断列是否存在 → ALTER」的分支，两种存储后端还要各来一套。Alembic 接管后，
schema 变更 = 一个 revision 文件。

数据库 URL 的解析顺序：
1. `ALEMBIC_DATABASE_URL`（CI / 一次性验证用）
2. `DATABASE_URL`（应用同一套配置，见 `core/db/base.py`）

注意 `render_as_batch=True`：SQLite 不支持大部分 `ALTER TABLE`，批处理模式会用
「建新表 → 拷数据 → 改名」的方式实现，缺了它改列会直接报错。
"""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlmodel import SQLModel

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 导入所有模型模块，让 SQLModel.metadata 收齐全部表。
# 少 import 一个模块 = autogenerate 会把它的表当成「需要删除」。
import core.config_store  # noqa: F401,E402
import core.db.models_account  # noqa: F401,E402
import core.db.models_infra  # noqa: F401,E402
import core.db.models_platform  # noqa: F401,E402
from core.db.base import DATABASE_URL  # noqa: E402

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata

#: 这三张表**不在主库**：它们由 `data/platforms/<平台>.db` 各自持有
#: （见 `core/db/base.py` 的 `platform_session`）。基线迁移里仍然会创建它们
#: （单库部署/测试用得上），但对**主库**做 autogenerate / `alembic check` 时
#: 必须跳过 —— 否则每次都会报「缺这三张表」，照着补一遍就等于在错误的库里
#: 建了一份永远不会被读到的副本。
_PER_PLATFORM_TABLES = frozenset({"icloud_accounts", "icloud_aliases", "outlook_accounts"})


def include_object(obj, name, type_, reflected, compare_to):
    """让 `alembic check` 只在「主库该有的东西」上报警。"""
    if type_ == "table" and name in _PER_PLATFORM_TABLES:
        return False
    if type_ == "index" and getattr(getattr(obj, "table", None), "name", "") in _PER_PLATFORM_TABLES:
        return False
    return True


def render_item(type_, obj, autogen_context):
    """把 SQLModel 的自定义类型渲染成原生 SQLAlchemy 类型。

    不这么做的话，autogenerate 会写出 `sqlmodel.sql.sqltypes.AutoString()`，
    而迁移脚本只 import 了 `sa` —— 空库 `upgrade head` 直接 NameError。
    迁移脚本不该依赖 SQLModel：它只是 SQLAlchemy 的一层皮。
    """
    if type_ == "type" and obj.__class__.__module__.startswith("sqlmodel"):
        name = obj.__class__.__name__
        if name == "AutoString":
            return "sa.String()"
        if name == "JSON":
            return "sa.JSON()"
        return "sa.String()"
    return False


def _url() -> str:
    return str(os.getenv("ALEMBIC_DATABASE_URL") or DATABASE_URL)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        render_as_batch=True,
        # 显式 False：Alembic 1.20 起 `compare_type` 默认是 True。
        # `core.time_utils.UtcDatetime` 是映射到 VARCHAR 的 TypeDecorator，
        # 开着它每张表的时间列都会被误报成类型漂移，`alembic check` 永远不干净。
        compare_type=False,
        render_item=render_item,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from sqlalchemy import create_engine

    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=False,
            render_item=render_item,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
