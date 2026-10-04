"""iCloud 主号与隐私邮箱的业务层。实现在 `services/icloud/`。

对外提供主号登录落库、Hide My Email 生成/同步/删除以及 IMAP 实时收件；
凭据在这里完成加解密，调用方永远拿不到原文。

────────────────────────────────────────────────────────────────────────
本文件是**门面**（历史导入路径）：

* 全部业务函数由 `services/icloud/` 的子模块重导出。
* `_utcnow` 等时间工具从 `_locks` 重导出（测试用 `_utcnow` 做时间回拨）。
* 模块级锁字典 `_ACCOUNT_LOCKS` / `_ACCOUNT_LOCKS_GUARD` 的唯一持有处是
  `services/icloud/_locks.py` —— 这里只是别名，保证与实现读的是同一份。
* `claim_alias` 经门面调 `generate_alias`，因此
  `monkeypatch.setattr(icloud_service, "generate_alias", …)` 打桩即生效。

下方 import 的模块（logging/threading/datetime/sqlalchemy/sqlmodel 等）刻意保留：
旧单文件里它们是泄漏到模块命名空间的模块级名字，去掉会让导入面快照变化。
"""
from __future__ import annotations

import logging  # noqa: F401
import threading  # noqa: F401
from datetime import datetime, timedelta, timezone  # noqa: F401
from typing import Any, Optional  # noqa: F401

from sqlalchemy import or_, update  # noqa: F401
from sqlmodel import select  # noqa: F401

from core.db import (  # noqa: F401
    ICloudAccountModel,
    ICloudAliasModel,
    email_used_by_platform,
    mark_email_used_by,
    new_alias_share_token,
    platform_session,
)
from core.secret_box import secret_box  # noqa: F401
from platforms.icloud import (  # noqa: F401
    ALIAS_STATUS_ACTIVE,
    ALIAS_STATUS_DISABLED,
    POOL_STATUSES,
    POOL_STATUS_AVAILABLE,
    POOL_STATUS_IN_USE,
    POOL_STATUS_UNPOOLED,
    POOL_STATUS_USED,
    ICloudCredentials,
    ICloudError,
    LoginRequest,
    LoginState,
    MailMessage,
    SessionImportRequest,
    fetch_inbox,
    login_manager,
    normalize_region,
    web_client,
)

logger = logging.getLogger(__name__)

from services.icloud import (  # noqa: E402,F401  （必须在 ICloud* 等 import 之后）
    DEFAULT_MESSAGE_LIMIT,
    HOURLY_ALIAS_LIMIT,
    _account_lock,
    _account_to_dict,
    _alias_to_dict,
    _as_utc,
    _fetch_via_web,
    _record_sync_error,
    _registered_platforms_for,
    _registered_platforms_one,
    _set_pool_status_many,
    _upsert_alias,
    _utcnow,
    alias_quota,
    cancel_login,
    claim_alias,
    complete_login,
    delete_account,
    delete_alias,
    delete_aliases,
    fetch_account_messages,
    fetch_account_messages_detailed,
    fetch_alias_messages,
    fetch_alias_messages_detailed,
    fetch_latest_shared_message,
    find_account_by_email,
    generate_alias,
    get_account,
    import_aliases_to_pool,
    import_session,
    list_accounts,
    list_aliases,
    load_credentials,
    login_state,
    mark_alias_used,
    pool_summary,
    release_alias_claim,
    release_stale_claims,
    resend_login_code,
    resolve_account,
    send_login_sms,
    set_account_enabled,
    set_alias_active,
    set_alias_pool_status,
    start_login,
    sync_aliases,
    unpool_aliases,
    verify_login,
)
from services.icloud._locks import _ACCOUNT_LOCKS, _ACCOUNT_LOCKS_GUARD  # noqa: E402,F401

__all__ = [
    "claim_alias",
    "complete_login",
    "delete_account",
    "delete_alias",
    "delete_aliases",
    "fetch_account_messages",
    "fetch_account_messages_detailed",
    "fetch_alias_messages",
    "fetch_alias_messages_detailed",
    "fetch_latest_shared_message",
    "find_account_by_email",
    "generate_alias",
    "get_account",
    "import_aliases_to_pool",
    "import_session",
    "list_accounts",
    "list_aliases",
    "load_credentials",
    "mark_alias_used",
    "pool_summary",
    "release_alias_claim",
    "release_stale_claims",
    "resend_login_code",
    "resolve_account",
    "send_login_sms",
    "set_account_enabled",
    "set_alias_active",
    "set_alias_pool_status",
    "start_login",
    "sync_aliases",
    "unpool_aliases",
    "verify_login",
]
