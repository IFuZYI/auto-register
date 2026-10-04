"""邮箱导入面板选中的那一栏（导入类型）。

`mail_provider` 只区分到"微软邮箱池"，而微软那一侧在界面上又拆成 Outlook、
Hotmail、MailAPI URL 三个视图。视图选择以前没地方落库，退出设置页再回来
就只能按 `mail_provider` 反推，永远反推成 Outlook——选了 MailAPI URL 也留不住。
这里把视图本身作为一个配置项存下来，运行时既按它筛号池里该取哪一类账号，也仍旧按账号
自身的 account_type 决定取码方式。

实现在 `core/mail_import_sources.py`：`core/mailboxes/channels/outlook/`
需要按视图筛号池账号类型，而 core 不能反向依赖 services。本模块转出全部名字，
保持既有导入路径（`from services.mail_imports import resolve_pool_account_type`）可用。
"""

from __future__ import annotations

from core.mail_import_sources import (  # noqa: F401  (重导出，兼容旧路径)
    MAIL_IMPORT_PROVIDERS,
    MAIL_IMPORT_SOURCES,
    MAIL_IMPORT_SOURCE_HOTMAIL,
    MAIL_IMPORT_SOURCE_MAILAPI,
    MAIL_IMPORT_SOURCE_OUTLOOK,
    POOL_ACCOUNT_TYPE_LABELS,
    POOL_ACCOUNT_TYPE_MAILAPI_URL,
    POOL_ACCOUNT_TYPE_MICROSOFT_OAUTH,
    align_source_with_provider,
    describe_pool_account_type,
    normalize_mail_import_source,
    normalize_mail_provider,
    resolve_mail_provider_from_source,
    resolve_pool_account_type,
)

__all__ = [
    "MAIL_IMPORT_PROVIDERS",
    "MAIL_IMPORT_SOURCES",
    "MAIL_IMPORT_SOURCE_HOTMAIL",
    "MAIL_IMPORT_SOURCE_MAILAPI",
    "MAIL_IMPORT_SOURCE_OUTLOOK",
    "POOL_ACCOUNT_TYPE_LABELS",
    "POOL_ACCOUNT_TYPE_MAILAPI_URL",
    "POOL_ACCOUNT_TYPE_MICROSOFT_OAUTH",
    "align_source_with_provider",
    "describe_pool_account_type",
    "normalize_mail_import_source",
    "normalize_mail_provider",
    "resolve_mail_provider_from_source",
    "resolve_pool_account_type",
]
