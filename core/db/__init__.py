"""数据库层（包）。

结构
----
- ``base``            : 时间/邮箱键工具、默认 engine、会话依赖
- ``models_infra``    : 跨平台基础设施表（任务、日志、代理）
- ``models_account``  : 账号池表（所有平台共用，随平台分库）
- ``models_platform`` : 平台专属表（Outlook 邮箱池、iCloud 主号/别名）
- ``registry``        : 平台分库注册表
- ``repository``      : 账号仓储（邮箱唯一键、判重、透明分库）
- ``migrations``      : 轻量 schema 迁移

向后兼容
--------
重构前所有代码都写 ``from core.db import AccountModel, engine, save_account``。
本包继续导出这些名字，且 `engine` 仍指默认库，所以老代码零改动可用。
"""
from __future__ import annotations

from sqlmodel import Session

from .base import (
    DATABASE_URL,
    _utcnow,
    current_engine,
    email_used_by_platform,
    engine,
    get_session,
    mark_email_used_by,
    new_alias_share_token,
    normalize_email,
    parse_platform_database_urls,
    resolve_database_url,
)
from .models_account import ACCOUNT_TABLES, AccountModel
from .models_infra import INFRA_TABLES, ProxyModel, TaskLog, TaskRunModel
from .models_platform import (
    ICLOUD_TABLES,
    OUTLOOK_TABLES,
    ICloudAccountModel,
    ICloudAliasModel,
    OutlookAccountModel,
)
from .registry import (
    platform_database_registry,
    platform_engine,
    platform_session,
    platform_session_dep,
    platform_session_from_path,
)
from .repository import AccountRepository, account_repository

__all__ = [
    "ACCOUNT_TABLES",
    "AccountModel",
    "AccountRepository",
    "DATABASE_URL",
    "ICLOUD_TABLES",
    "ICloudAccountModel",
    "ICloudAliasModel",
    "INFRA_TABLES",
    "MAILBOX_POOL_TABLES",
    "OUTLOOK_TABLES",
    "OutlookAccountModel",
    "ProxyModel",
    "TaskLog",
    "TaskRunModel",
    "_utcnow",
    "account_repository",
    "current_engine",
    "email_used_by_platform",
    "engine",
    "get_session",
    "init_db",
    "mailbox_pool_session",
    "mark_email_used_by",
    "new_alias_share_token",
    "normalize_email",
    "parse_platform_database_urls",
    "platform_database_registry",
    "platform_engine",
    "platform_session",
    "platform_session_dep",
    "platform_session_from_path",
    "resolve_database_url",
    "save_account",
]


def save_account(account) -> AccountModel:
    """把 base_platform.Account 存入账号池（同平台同邮箱则更新）。

    保留此函数是因为历史代码与测试大量直接调用；内部走仓储层，因此自动获得
    「邮箱唯一键 + 透明分库」行为。
    """
    return account_repository.upsert(account)


# --------------------------------------------------------------------- 邮箱池

#: 邮箱池平台键 → 该池的表。它们默认各占一个库（见 core.paths）。
MAILBOX_POOL_TABLES: dict[str, list] = {
    "icloud": ICLOUD_TABLES,
    "outlook": OUTLOOK_TABLES,
}


def mailbox_pool_session(pool: str):
    """打开某个邮箱池的会话（icloud / outlook）。

    用 `platform_session` 而不是 `Session(current_engine())`：这两个池默认分库
    （data/platforms/<pool>.db），直连默认库会读到一张空表 —— 表现为
    「明明导入过号，列表却是空的」，且不报错。
    """
    return platform_session(pool)


def init_db() -> None:
    """建表 + 迁移。

    - 默认库：基础设施表（任务/日志/代理/配置）+ 账号表（未分库平台用）；
    - 平台库：各平台自己的表。注册平台建账号表，邮箱池（icloud / outlook）
      建各自的池表 —— 它们默认分库，见 `core.paths.DEFAULT_PLATFORM_DB_FILES`。
    """
    from .migrations import run_migrations
    from .registry import platform_database_registry

    platform_database_registry.reload_from_env()

    # 默认库：基础设施 + 账号表（未分库平台用）+ 配置表。
    #
    # 这里显式列出表而不是 SQLModel.metadata.create_all()：metadata 里混着
    # 邮箱池的表（icloud_accounts / outlook_accounts），那两个池已默认分库，
    # 用 create_all 会在默认库里留下一批**永远为空**的同名表 —— 之后排查
    # 「数据到底在哪」时非常误导。分库的表由下面的循环建到各自库里。
    from sqlmodel import SQLModel

    from core.config_store import ConfigItem

    SQLModel.metadata.create_all(
        current_engine(),
        tables=[*INFRA_TABLES, *ACCOUNT_TABLES, ConfigItem.__table__],
    )

    # 平台库：注册平台建账号表，邮箱池建池表。
    # 注意别把池表也建到账号库里 —— 那正是「默认库里有张永远为空的
    # outlook_accounts」这种混乱的来源。
    for platform in platform_database_registry.configured_platforms():
        tables = MAILBOX_POOL_TABLES.get(platform, ACCOUNT_TABLES)
        platform_database_registry.register_schema(platform, tables)

    run_migrations()


def close_all() -> None:
    """释放所有连接（测试收尾、进程退出）。"""
    platform_database_registry.dispose_all()
    try:
        engine.dispose()
    except Exception:
        pass
