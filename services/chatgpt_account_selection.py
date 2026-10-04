"""批量任务的选号入口：补 RT、绑 2FA 这些活儿共用同一套筛选规则。

页面上的批量按钮只有两种范围：勾了行就按 id 走，没勾就把当前筛选条件原样带过来。
两种范围之外，每个任务还会再按自己的条件筛一道（缺 RT、没绑 2FA），那部分由调用方
用 ``keep`` 传进来。
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional

from sqlmodel import Session, select

from core.db import AccountModel
from services.chatgpt_account_state import filter_accounts_by_plus_status

MAX_BATCH_ACCOUNTS = 1000


def normalize_account_ids(account_ids: Optional[Iterable[int]]) -> list[int]:
    """去重、去非法值，保持调用方给的顺序。"""
    ids: list[int] = []
    seen: set[int] = set()
    for raw in account_ids or []:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        ids.append(value)
    return ids


def select_chatgpt_accounts(
    session: Session,
    *,
    account_ids: Optional[Iterable[int]] = None,
    all_filtered: bool = False,
    email: str = "",
    status: str = "",
    plus_status: str = "",
    keep: Optional[Callable[[AccountModel], bool]] = None,
    max_accounts: int = MAX_BATCH_ACCOUNTS,
) -> tuple[list[AccountModel], list[int]]:
    """挑出要处理的 ChatGPT 账号，返回 ``(账号列表, 找不到的 id)``。"""
    ids = normalize_account_ids(account_ids)
    missing_ids: list[int] = []

    if ids:
        rows = session.exec(
            select(AccountModel)
            .where(AccountModel.platform == "chatgpt")
            .where(AccountModel.id.in_(ids))
        ).all()
        row_map = {row.id: row for row in rows}
        accounts = [row_map[account_id] for account_id in ids if account_id in row_map]
        missing_ids = [account_id for account_id in ids if account_id not in row_map]
    elif all_filtered:
        query = select(AccountModel).where(AccountModel.platform == "chatgpt")
        if status:
            query = query.where(AccountModel.status == status)
        if email:
            query = query.where(AccountModel.email.contains(email))
        accounts = list(session.exec(query).all())
        if plus_status:
            accounts = filter_accounts_by_plus_status(accounts, plus_status)
    else:
        raise ValueError("请提供 account_ids，或指定 all_filtered=true")

    if keep is not None:
        accounts = [row for row in accounts if keep(row)]
    if len(accounts) > max_accounts:
        raise ValueError(f"单次最多处理 {max_accounts} 个账号")
    return accounts, missing_ids
