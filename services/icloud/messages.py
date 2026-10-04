"""iCloud 收件业务：主号收件箱（IMAP 优先、Web API 兜底）、别名收件、分享收件。

`fetch_inbox_web` 采用函数内延迟 import（原实现如此），避免与
`platforms.icloud` 在包加载期形成重依赖。
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlmodel import select

from core.db import ICloudAliasModel, platform_session
from platforms.icloud import ICloudCredentials, ICloudError, MailMessage, fetch_inbox
from services.icloud._locks import DEFAULT_MESSAGE_LIMIT
from services.icloud._facade import _facade
from services.icloud.accounts import get_account, load_credentials

logger = logging.getLogger(__name__)


def fetch_account_messages(
    account_id: int, *, limit: int = DEFAULT_MESSAGE_LIMIT, recipient: str = ""
) -> list[MailMessage]:
    """读主号收件箱（IMAP 优先、Web API 兜底），只回邮件列表。"""
    messages, _method, _warning = fetch_account_messages_detailed(
        account_id, limit=limit, recipient=recipient
    )
    return messages


def fetch_account_messages_detailed(
    account_id: int,
    *,
    limit: int = DEFAULT_MESSAGE_LIMIT,
    recipient: str = "",
    proxy: str | None = None,
) -> tuple[list[MailMessage], str, str]:
    """读主号收件箱，IMAP 优先、Web API 兜底。

    返回 `(messages, method, warning)`：`method` 是 `imap` 或 `web_api`，
    `warning` 只在降级时非空，说明为什么没用 IMAP。

    为什么要有回退：IMAP 需要用户去 appleid.apple.com 生成 App 专用密码，
    而导入 Cookie 的主号天然带 Web 会话。只有 Web 会话时不该完全读不了信。
    """
    row = get_account(account_id)
    credentials = load_credentials(row)

    if credentials.has_imap:
        try:
            messages = fetch_inbox(
                credentials, row.email, limit=limit, recipient=recipient
            )
            return messages, "imap", ""
        except ICloudError as exc:
            # 有 App 密码但 IMAP 不通（密码被撤销、服务器拒绝）时降级，
            # 而不是直接把错误抛给用户——Web 会话往往还能用。
            if not credentials.has_web_session:
                raise
            logger.warning("iCloud IMAP 收件失败，回退 Web API: %s", exc)
            return _fetch_via_web(
                credentials,
                limit=limit,
                recipient=recipient,
                proxy=proxy,
                warning=f"IMAP 不可用，已回退 Web API：{exc}",
            )

    if credentials.has_web_session:
        return _fetch_via_web(
            credentials,
            limit=limit,
            recipient=recipient,
            proxy=proxy,
            warning="未配置 IMAP 应用专用密码，已回退 Web API（Web API 不含正文，只有摘要）",
        )

    raise ICloudError(
        "invalid_config",
        f"主号 {row.email} 既没有 IMAP 应用专用密码，也没有可用的 Web 会话，无法收件",
    )


def _fetch_via_web(
    credentials: ICloudCredentials,
    *,
    limit: int,
    recipient: str,
    proxy: str | None,
    warning: str,
) -> tuple[list[MailMessage], str, str]:
    from platforms.icloud.web_mail import fetch_inbox_web

    messages = fetch_inbox_web(
        credentials, limit=limit, recipient=recipient, proxy=proxy
    )
    return messages, "web_api", warning


def fetch_alias_messages(
    alias_id: int, *, limit: int = DEFAULT_MESSAGE_LIMIT
) -> list[MailMessage]:
    messages, _method, _warning = fetch_alias_messages_detailed(alias_id, limit=limit)
    return messages


def fetch_alias_messages_detailed(
    alias_id: int, *, limit: int = DEFAULT_MESSAGE_LIMIT
) -> tuple[list[MailMessage], str, str]:
    with platform_session("icloud") as session:
        alias = session.get(ICloudAliasModel, int(alias_id))
        if alias is None:
            raise ICloudError("alias_not_found", "隐私邮箱不存在")
        account_id, address = alias.account_id, alias.address
    return fetch_account_messages_detailed(account_id, limit=limit, recipient=address)


def fetch_latest_shared_message(
    share_token: str, *, limit: int = DEFAULT_MESSAGE_LIMIT
) -> tuple[str, Optional[MailMessage]]:
    """按分享 token 取该隐私邮箱的最新一封邮件。

    给免登录页面用，所以只认 token、只回一封，拿不到地址以外的任何账号信息。
    """
    token = str(share_token or "").strip()
    if not token:
        raise ICloudError("alias_not_found", "隐私邮箱不存在")
    with platform_session("icloud") as session:
        alias = session.exec(
            select(ICloudAliasModel).where(ICloudAliasModel.share_token == token)
        ).first()
        if alias is None:
            raise ICloudError("alias_not_found", "隐私邮箱不存在")
        account_id, address = alias.account_id, alias.address
    messages = _facade.fetch_account_messages(account_id, limit=limit, recipient=address)
    return address, messages[0] if messages else None
