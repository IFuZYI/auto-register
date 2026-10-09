"""平台专属模型：Outlook 邮箱池、iCloud 主号/别名。

这些表属于具体平台，跟随对应平台的分库配置：配置了 `DATABASE_URL_OUTLOOK` /
`DATABASE_URL_ICLOUD` 就落到那个库，否则落到默认库。

注意：Outlook 邮箱池被多个平台的注册流程当「发信邮箱来源」共用，所以它的分库
键是 `outlook` 而不是某个注册平台。
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import Index, text
from sqlmodel import Field, SQLModel

from .base import _utcnow, new_alias_share_token


class OutlookAccountModel(SQLModel, table=True):
    __tablename__ = "outlook_accounts"

    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, sa_column_kwargs={"unique": True})
    password: str
    client_id: str = ""
    refresh_token: str = ""
    account_type: str = "microsoft_oauth"
    mailapi_url: str = ""
    enabled: bool = True
    status: str = Field(default="available", index=True)
    #: 这个地址被**哪些平台**消耗过（`,grok,chatgpt,` 形式，空串 = 没有/未知）。
    #:
    #: 邮箱是「一次性」的，但**只对用掉它的那个平台**一次性 —— 同一个地址
    #: 注册过 ChatGPT 之后还能注册 Grok（两个平台的账号体系互不相干）。
    #: 只用一个全局 `status='used'` 记不出这层区别，会把地址对所有平台一起锁死。
    used_platforms: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_used: Optional[datetime] = None


class ICloudAccountModel(SQLModel, table=True):
    """iCloud 主号。

    Web Session 与 IMAP 凭据统一以 AES-256-GCM 密文保存。

    `source` 区分这条记录是**本地主号**（本机直连 Apple，凭据在
    `credentials_cipher`）还是**远程仓库账号**（凭据在独立的 icloud-hme 服务里，
    本机只存服务地址 + 管理员密码密文 + 远端账号 id）。同一邮箱允许两边各有一行
    （本地优先去重时本地行胜出，见 `services/icloud/remote.py`）。
    """

    __tablename__ = "icloud_accounts"

    #: 同一 (base_url, remote_account_id) 只能接入一次（partial index：只约束远程行）。
    #: 本地行的 base_url / remote_account_id 都是空串，不参与该约束。
    __table_args__ = (
        Index(
            "uq_icloud_accounts_remote",
            "base_url",
            "remote_account_id",
            unique=True,
            sqlite_where=text("source = 'remote'"),
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    #: email 不再单列唯一：同一个 Apple ID 可以同时是本地主号与远程账号
    #: （「本地优先去重」的部署形态），单列唯一会让远程账号加不进来。
    #: 唯一性由业务层按来源判断，远程按 (base_url, remote_account_id) 约束。
    email: str = Field(index=True)
    display_name: str = ""
    region: str = "global"
    status: str = "active"
    enabled: bool = True
    credentials_cipher: str = ""
    #: 来源：local（本机直连 Apple）/ remote（远程 icloud-hme 仓库）。
    source: str = Field(default="local", index=True)
    #: 远程服务地址（如 http://127.0.0.1:8081），仅 source=remote 使用。
    base_url: str = ""
    #: 远程服务里的账号 id（如 acc_45ea504a），仅 source=remote 使用。
    remote_account_id: str = ""
    #: 远程服务管理员密码（AES-256-GCM 密文），仅 source=remote 使用。
    remote_password_cipher: str = ""
    #: 访问远程服务用的代理（可选，原文保存 —— 它不是凭据，是网络出口配置）。
    proxy: str = ""
    sync_error: str = ""
    last_sync_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ICloudAliasModel(SQLModel, table=True):
    """从主号生成或同步得到的 Hide My Email 隐私邮箱。"""

    __tablename__ = "icloud_aliases"

    id: Optional[int] = Field(default=None, primary_key=True)
    account_id: int = Field(index=True, foreign_key="icloud_accounts.id")
    address: str = Field(index=True, sa_column_kwargs={"unique": True})
    label: str = ""
    note: str = ""
    status: str = Field(default="active", index=True)
    #: 号池取用状态（unpooled / available / in_use / used），与 `status` 是两个维度：
    #: `status` 管「在 Apple 那边还能不能收信」，这里管「进没进号池、注册任务用过没有」。
    #:
    #: 默认 `unpooled`（未入池）：生成/同步下来的别名不自动进池，必须由用户在
    #: 池页面勾选「导入邮箱池」才转成 available。老库补列时同样补 unpooled
    #: （见 core/db/migrations.py）。
    pool_status: str = Field(default="unpooled", index=True)
    #: 来源：local（本机直连 Apple 生成/同步）/ remote（从远程 icloud-hme 仓库同步）。
    #: 与 `pool_status` / `status` 都是正交维度：来源回答「这个地址从哪来」。
    #: 同地址去重时**本地优先**：远程同步遇到本地已有的地址不建行
    #: （见 `services/icloud/remote.py::sync_remote_aliases`）。
    source: str = Field(default="local", index=True)
    #: 这个别名被**哪些平台**消耗过（`,grok,chatgpt,` 形式，空串 = 没有/未知）。
    #:
    #: 邮箱是一次性的，但**只对用掉它的那个平台**一次性 —— 同一个别名注册过
    #: ChatGPT 之后还能注册 Grok（两个平台的账号体系互不相干）。只用一个全局
    #: `pool_status='used'` 记不出这层区别，会把别名对所有平台一起锁死
    #: （实测：22 个 `used` 别名里 13 个 grok 从没用过，却全都领不到）。
    used_platforms: str = ""
    provider_id: str = ""
    # 免登录查看最新邮件的凭证，链接本身就是权限，所以必须是猜不出来的随机串
    share_token: str = Field(default_factory=lambda: new_alias_share_token(), index=True)
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow)


OUTLOOK_TABLES = [OutlookAccountModel.__table__]
ICLOUD_TABLES = [m.__table__ for m in (ICloudAccountModel, ICloudAliasModel)]


__all__ = [
    "OutlookAccountModel",
    "ICloudAccountModel",
    "ICloudAliasModel",
    "OUTLOOK_TABLES",
    "ICLOUD_TABLES",
]
