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
        # SQLite 变量上限 32766：id 列表超过后 IN 查询直接炸（实测 33000 个
        # id → OperationalError: too many SQL variables；调用方大多有 1000
        # 上限，但上限检查在**查询之后**，先炸就轮不到它）。分块查询，
        # 500 一批，与仓储层 get_many 同口径。
        row_map: dict[int, AccountModel] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            rows = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == "chatgpt")
                .where(AccountModel.id.in_(chunk))
            ).all()
            for row in rows:
                row_map[int(row.id or 0)] = row
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
