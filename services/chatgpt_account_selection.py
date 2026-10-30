"""批量任务的选号入口：补 RT、绑 2FA 这些活儿共用同一套筛选规则。

页面上的批量按钮只有两种范围：勾了行就按 id 走，没勾就把当前筛选条件原样带过来。
两种范围之外，每个任务还会再按自己的条件筛一道（缺 RT、没绑 2FA），那部分由调用方
用 ``keep`` 传进来。
"""

from __future__ import annotations

import re

from datetime import datetime
from typing import Callable, Iterable, Optional

from sqlmodel import Session, select

from core.db import AccountModel
from services.chatgpt_account_state import filter_accounts_by_plus_status

MAX_BATCH_ACCOUNTS = 1000


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_POSITIVE_INT_RE = re.compile(r"^[1-9][0-9]*$")


def normalize_account_ids(account_ids: Optional[Iterable[str]]) -> list[str]:
    """去重、去非法值，保持调用方给的顺序。

    主键换成 UUID 之后，合法形状有两种：

    * UUID（36 位，小写）—— 新前端与新脚本传的；
    * 正整数 —— 旧前端、老书签、外部脚本还在传 `legacy_id`。留着让它们能查，
      查不到自然会进 `missing`，比 422 把整批任务打掉友好。

    `0`、负数、非数字串、空值一律丢弃：那是「未选中」的占位，不是 id。
    """
    ids: list[str] = []
    seen: set[str] = set()
    for raw in account_ids or []:
        if raw is None:
            continue
        value = str(raw).strip().lower()
        if not value or value in seen:
            continue
        if not (_UUID_RE.match(value) or _POSITIVE_INT_RE.match(value)):
            continue
        seen.add(value)
        ids.append(value)
    return ids


def select_chatgpt_accounts(
    session: Session,
    *,
    account_ids: Optional[Iterable[str]] = None,
    all_filtered: bool = False,
    email: str = "",
    status: str = "",
    plus_status: str = "",
    created_at_start: Optional[datetime] = None,
    created_at_end: Optional[datetime] = None,
    keep: Optional[Callable[[AccountModel], bool]] = None,
    max_accounts: int = MAX_BATCH_ACCOUNTS,
    platform: str = "chatgpt",
) -> tuple[list[AccountModel], list[int]]:
    """挑出要处理的账号，返回 ``(账号列表, 找不到的 id)``。

    ``platform`` **必须由调用方传入**：这里原来硬编码 ``"chatgpt"``，于是
    Grok 账号永远被判成「不存在」—— `POST /api/tasks/batch-action` 传
    ``platform="grok"`` 时一个任务都建不起来（实测 2026-10-28：grok 平台库
    里 id=1 明明存在，端点返回「所选账号不存在」）。

    默认值 ``"chatgpt"`` 只为兼容既有调用方；新调用方一律显式传。

    `created_at_start/end` 与列表接口（`/accounts`）的日期筛选同口径 ——
    页面显示「处理当前筛选的 N 个账号」时，N 包含日期筛选，任务也必须
    按同一组条件选号，否则实际处理数会大于显示数。
    """
    ids = normalize_account_ids(account_ids)
    missing_ids: list[str] = []

    if ids:
        # SQLite 变量上限 32766：id 列表超过后 IN 查询直接炸（实测 33000 个
        # id → OperationalError: too many SQL variables；调用方大多有 1000
        # 上限，但上限检查在**查询之后**，先炸就轮不到它）。分块查询，
        # 500 一批，与仓储层 get_many 同口径。
        row_map: dict[str, AccountModel] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            rows = session.exec(
                select(AccountModel)
                .where(AccountModel.platform == platform)
                .where(AccountModel.id.in_(chunk))
            ).all()
            for row in rows:
                row_map[str(row.id)] = row
        accounts = [row_map[account_id] for account_id in ids if account_id in row_map]
        missing_ids = [account_id for account_id in ids if account_id not in row_map]
    elif all_filtered:
        query = select(AccountModel).where(AccountModel.platform == platform)
        if status:
            query = query.where(AccountModel.status == status)
        if email:
            query = query.where(AccountModel.email.contains(email))
        if created_at_start is not None:
            query = query.where(AccountModel.created_at >= created_at_start)
        if created_at_end is not None:
            query = query.where(AccountModel.created_at <= created_at_end)
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
