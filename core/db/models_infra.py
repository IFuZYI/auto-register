"""基础设施模型：任务、日志、代理。这些是跨平台的，永远在默认库。"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from .base import _utcnow


class TaskLog(SQLModel, table=True):
    __tablename__ = "task_logs"

    #: 日志列表按 platform 过滤 + id 倒序翻页，两个条件都要走**同一个**索引，
    #: 否则日志积累到几万条后每次翻页都是全表扫描 + 排序。
    #:
    #: 复合索引的前缀就是 `platform`，所以不再单独给 platform 建单列索引
    #: （原来 `index=True` 会多出一个 `ix_task_logs_platform`，生产库里没有，
    #: `alembic check` 一直想把它加回来 —— 加了也是白占空间）。
    __table_args__ = (Index("ix_task_logs_platform_id", "platform", "id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = ""
    email: str = Field(index=True)
    status: str = Field(default="success", index=True)  # success | failed
    error: str = ""
    detail_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow, index=True)


class TaskRunModel(SQLModel, table=True):
    __tablename__ = "task_runs"

    id: str = Field(primary_key=True)
    platform: str = Field(index=True)
    source: str = Field(default="manual", index=True)
    status: str = Field(default="pending", index=True)
    total: int = 0
    progress: str = "0/0"
    success: int = 0
    registered: int = 0
    skipped: int = 0
    error: str = ""
    meta_json: str = "{}"
    logs_json: str = "[]"
    errors_json: str = "[]"
    cashier_urls_json: str = "[]"
    control_json: str = "{}"
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)


class ProxyModel(SQLModel, table=True):
    __tablename__ = "proxies"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(unique=True)
    region: str = ""
    success_count: int = 0
    fail_count: int = 0
    is_active: bool = True
    last_checked: Optional[datetime] = None
    #: 逗号分隔的标签。当前两个：
    #: - `register`：可用于**注册链路**（新号从这些出口出去）
    #: - `bind`：可**绑定给账号**（账号的 register_proxy 只能落在这类代理上）
    #:
    #: 存逗号串而不是 JSON 数组：两种后端（SQLite / JSON 文件）都能原样存，
    #: 不需要为 JSON 后端单独做序列化分支（这仓库已经踩过「JSON 分支丢字段」）。
    tags: str = ""


INFRA_TABLES = [m.__table__ for m in (TaskLog, TaskRunModel, ProxyModel)]


__all__ = ["TaskLog", "TaskRunModel", "ProxyModel", "INFRA_TABLES"]
