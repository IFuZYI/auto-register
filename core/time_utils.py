"""时间输入的归一：naive datetime 按 UTC 处理。

背景（复审发现，2026-10-07）：`created_at_start/end` 这类查询/请求参数此前
直接标 `datetime` —— 客户端发 naive 值（无时区，如 `2026-10-07T16:00:00`）时
SQL 比较会炸 `StatementError`（HTTP 500）。库里的时间全部是 UTC-aware、
UI 总是发 `Date.toISOString()`（带 Z）—— naive 输入按 UTC 归一（与存储口径
一致），而不是 500。

用法：把 `Optional[datetime]` 换成 `Optional[UtcDatetime]`（请求模型与
FastAPI 查询参数都适用；`AfterValidator` 在 pydantic v2 下对两者都生效）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import AfterValidator

__all__ = ["UtcDatetime", "ensure_utc"]


def ensure_utc(value: datetime) -> datetime:
    """naive datetime → 视为 UTC；aware 原样返回。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


#: 归一后的 datetime 类型：naive 输入按 UTC 处理（不炸 SQL 比较）。
UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
