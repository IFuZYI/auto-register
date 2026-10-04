from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


MailImportProviderType = Literal["microsoft"]
MailImportAccountType = Literal["microsoft_oauth", "mailapi_url"]

DEFAULT_PREVIEW_LIMIT = 100
MAX_PREVIEW_LIMIT = 500


class MailImportProviderDescriptor(BaseModel):
    type: MailImportProviderType
    label: str
    description: str
    content_placeholder: str
    helper_text: str = ""
    supports_filename: bool = False
    filename_label: str = ""
    filename_placeholder: str = ""
    preview_empty_text: str = ""


class MailImportSnapshotItem(BaseModel):
    index: int
    id: int | None = None
    email: str
    mailbox: str = ""
    enabled: bool | None = None
    status: str = "available"
    has_oauth: bool | None = None
    account_type: MailImportAccountType | None = None
    #: 这个地址被**哪些平台**消耗过（`,grok,chatgpt,` 形式，空串 = 没有）。
    used_platforms: str = ""
    #: `accounts` 表里的权威注册证据（跨库查出来，已排序）。界面按它显示
    #: 「已注册平台」并做平台筛选 —— 池里的记账可能因任务中途崩掉而缺项。
    registered_platforms: list[str] = []


class MailImportSnapshotRequest(BaseModel):
    type: MailImportProviderType
    pool_dir: str = ""
    pool_file: str = ""
    preview_limit: int = Field(
        default=DEFAULT_PREVIEW_LIMIT,
        ge=1,
        le=MAX_PREVIEW_LIMIT,
    )


class MailImportExecuteRequest(BaseModel):
    type: MailImportProviderType
    content: str
    filename: str = ""
    pool_dir: str = ""
    pool_file: str = ""
    enabled: bool = True
    bind_to_config: bool = True
    alias_split_enabled: bool = False
    alias_split_count: int = Field(default=5, ge=1, le=5)
    alias_include_original: bool = False
    preview_limit: int = Field(
        default=DEFAULT_PREVIEW_LIMIT,
        ge=1,
        le=MAX_PREVIEW_LIMIT,
    )


class MailImportDeleteRequest(BaseModel):
    type: MailImportProviderType
    email: str
    mailbox: str = ""
    pool_dir: str = ""
    pool_file: str = ""
    preview_limit: int = Field(
        default=DEFAULT_PREVIEW_LIMIT,
        ge=1,
        le=MAX_PREVIEW_LIMIT,
    )


class MailImportDeleteItem(BaseModel):
    email: str
    mailbox: str = ""


class MailImportBatchDeleteRequest(BaseModel):
    type: MailImportProviderType
    items: list[MailImportDeleteItem] = Field(default_factory=list)
    pool_dir: str = ""
    pool_file: str = ""
    preview_limit: int = Field(
        default=DEFAULT_PREVIEW_LIMIT,
        ge=1,
        le=MAX_PREVIEW_LIMIT,
    )


class MailImportSnapshot(BaseModel):
    type: MailImportProviderType
    label: str
    count: int
    items: list[MailImportSnapshotItem] = Field(default_factory=list)
    truncated: bool = False
    filename: str = ""
    path: str = ""
    pool_dir: str = ""


class MailImportSummary(BaseModel):
    total: int
    success: int
    failed: int


class MailImportResponse(BaseModel):
    type: MailImportProviderType
    summary: MailImportSummary
    snapshot: MailImportSnapshot
    errors: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
