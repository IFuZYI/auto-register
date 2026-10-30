"""账号模型：跨平台账号池的公共表定义。

分库说明
--------
`AccountModel` 是**所有平台共用**的账号表。它跟随「账号所属平台」落到对应库：

- 配置了 `DATABASE_URL_GROK` → Grok 账号进 Grok 库
- 没配置 → 进默认库（行为与重构前一致）

因此这张表在**每个平台库里都会各建一份**（`ensure_schema` 负责），查询时必须
带上 `platform` 条件——账号仓储层（`repository.py`）已经保证了这一点。
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Optional

from pydantic import computed_field
from sqlalchemy import Column, Index, String, text
from sqlmodel import Field, SQLModel

from .base import _utcnow


class AccountModel(SQLModel, table=True):
    __tablename__ = "accounts"
    #: 唯一业务键：同一平台同一邮箱只允许一行。
    #:
    #: 用**表达式索引** `(platform, lower(email))` 而不是裸列索引：大小写不同的
    #: 同一地址在 SQL 层是两个值，裸索引放得过去（实测 `USER@X.AI` 能插进
    #: `user@x.ai` 旁边）。生产库由 Alembic revision `b7c1e4a92d30` 创建；
    #: 这里声明是为了让 `create_all`（测试与全新部署）带上同一条约束 ——
    #: 测试库与生产库的约束不一致，等于没测。
    __table_args__ = (
        Index(
            "uq_accounts_platform_email",
            "platform",
            text("lower(email)"),
            unique=True,
        ),
    )

    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        sa_column=Column(
            "id",
            String,
            primary_key=True,
            default=lambda: str(uuid.uuid4()),
            # 服务端默认值：绕过 ORM 的裸 SQL INSERT（导入脚本、手工修数、
            # 测试里模拟老数据）也必须拿到一个合法主键，否则直接 NOT NULL 失败。
            server_default=text(
                "(lower(hex(randomblob(4))||'-'||hex(randomblob(2))||'-4'||"
                "substr(hex(randomblob(2)),2)||'-'||substr('89ab',abs(random())%4+1,1)||"
                "substr(hex(randomblob(2)),2)||'-'||hex(randomblob(6))))"
            ),
        ),
    )
    #: 旧的自增整数主键，保留一个版本：回滚时用它还原，老日志/老面板记录也靠它对账。
    #: 新代码不要再读它（除回滚脚本）。
    legacy_id: Optional[int] = Field(default=None, index=True, unique=True)
    platform: str = Field(index=True)
    # 邮箱是账号的唯一业务键（大小写无关，写入前经 normalize_email 规范化），
    # 与 platform 组成唯一约束：同一平台同一邮箱只允许一行。
    email: str = Field(index=True)
    #: 密码的**密文**。数据库列名仍是 `password`（不换列，免一次迁移），
    #: 但属性名改成 `password_cipher`，好让 `password` 变成一个透明加解密的
    #: property —— 67 处 `account.password` 一行都不用改。
    password_cipher: str = Field(
        default="",
        # 密文不进响应：对外契约里只有明文 `password`（见下面的 computed_field）。
        # 不排掉的话，列表/详情/创建三个响应都会多出一个 `password_cipher`，
        # 与改造前的形状不一致，还把密文白送给客户端。
        exclude=True,
        sa_column=Column("password", String, nullable=False, server_default=""),
    )
    user_id: str = ""
    region: str = ""
    token: str = ""
    # 列表默认「按状态筛选 + created_at 倒序」，两个条件都建索引；
    # 状态取值见 AccountStatus 枚举（registered/expired/invalid/banned），
    # 历史行可能带任意字符串。
    status: str = Field(default="registered", index=True)
    cashier_url: str = ""
    extra_json: str = "{}"   # JSON 存储平台自定义字段
    created_at: datetime = Field(default_factory=_utcnow, index=True)
    updated_at: datetime = Field(default_factory=_utcnow, index=True)

    def __init__(self, **data: Any) -> None:
        """构造时就把 `extra_json` 里的凭证加密。

        为什么必须在 `__init__` 里做：`set_extra()` 是写路径的正门，但**导入、
        测试、面板回填**这些地方直接 `AccountModel(extra_json=json.dumps({...}))`
        构造 —— 绕过正门，明文就落盘了（`test_chatgpt_two_factor_persistence`
        原先断言的正是这个泄漏）。这里是所有构造的必经点，补一道就够。

        `encrypt_extra` 对已是 `v1:` 的值跳过，所以「从库里读出来再构造一次」
        不会套娃。
        """
        # `password` 不是字段（是 property），pydantic 不认这个关键字 ——
        # 先摘出来，构造完再经 setter 加密写进 `password_cipher`。
        password = data.pop("password", None)
        super().__init__(**data)
        if password is not None:
            self.password = password
        raw = getattr(self, "extra_json", "") or ""
        if not raw.strip():
            return
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        from core.credential_crypto import encrypt_extra

        sealed = encrypt_extra(payload)
        if sealed != payload:
            self.extra_json = json.dumps(sealed, ensure_ascii=False)

    # ------------------------------------------------------------ 密码
    @computed_field  # type: ignore[misc]
    @property
    def password(self) -> str:
        """明文密码。落盘的是密文，这里透明解密（老明文原样通过）。"""
        from core.credential_crypto import decrypt_text

        return decrypt_text(self.password_cipher)

    @password.setter
    def password(self, value: Any) -> None:
        from core.credential_crypto import encrypt_text

        self.password_cipher = encrypt_text("" if value is None else str(value))

    def get_extra(self) -> dict:
        """解析 extra_json 为字典。

        脏数据不该打断流程：历史行可能是空串、半截 JSON（导入中断）、甚至是
        `"[]"` 这类非字典 JSON。仓储层（repository.upsert）已有同样保护，
        这里对齐——之前模型层直接 json.loads，遇到坏数据会抛 JSONDecodeError。
        """
        raw = self.extra_json or ""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        # 敏感凭证（AT/RT/IDT/ST/SSO/totp_secret）落盘是密文，这里解密。
        # 老数据是明文 —— `decrypt_extra` 对非 `v1:` 值原样通过。
        from core.credential_crypto import decrypt_extra

        return decrypt_extra(data)

    def set_extra(self, d: dict):
        """写回 extra_json。非字典输入按空字典处理，避免写出非法结构。

        **落盘前加密**：`access_token` / `refresh_token` / `id_token` /
        `session_token` / `sso` / `totp_secret` 六把凭证不进明文
        （见 `core/credential_crypto.py`）。读侧由 `get_extra` 解密。
        """
        from core.credential_crypto import encrypt_extra

        payload = d if isinstance(d, dict) else {}
        self.extra_json = json.dumps(encrypt_extra(payload), ensure_ascii=False)

    def get_extra_value(self, key: str, default: Any = None) -> Any:
        """读单个平台自定义字段（键不存在或数据损坏时返回 default）。"""
        return self.get_extra().get(key, default)

    def update_extra(self, **values: Any) -> dict:
        """合并更新若干字段并写回，返回合并后的完整字典。

        比「get_extra() → 改 → set_extra()」少一次漏写回的机会；
        值为 None 的键会被删除（用于清理字段）。
        """
        data = self.get_extra()
        for key, value in values.items():
            if value is None:
                data.pop(key, None)
            else:
                data[key] = value
        self.set_extra(data)
        return data

    # ------------------------------------------------------------ 领域模型
    def to_record(self):
        """转成领域对象（`core.models.account.AccountRecord`）。

        这是 ORM → 领域对象的**唯一**入口。以前每个调用点各写一遍字段搬运
        （`api/actions.py`、`api/accounts.py`、`core/scheduler.py`、
        `api/integrations.py`），四份实现里 `region` 和 `cashier_url` 各漏一处。
        """
        from core.models.account import AccountRecord

        return AccountRecord.from_model(self)


# 表对象供平台库建表用（`register_schema` 只建本平台的表）。
# 注意：必须取 `__table__`（SQLAlchemy Table），SQLModel 类对象本身不能直接
# 传给 `create_all(tables=...)` —— 它会去访问 `.name` 而模型类没有该属性。
ACCOUNT_TABLES = [AccountModel.__table__]


__all__ = ["AccountModel", "ACCOUNT_TABLES"]
