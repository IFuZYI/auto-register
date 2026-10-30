"""账号的**唯一**领域模型。

以前有两个：

* `core.base_platform.Account` —— 平台侧用的 dataclass，没有 `id`、没有
  `cashier_url`、`created_at` 是 epoch 整数；
* `core.db.models_account.AccountModel` —— ORM 行，有 `id`/`cashier_url`，
  但 `extra` 是一个 JSON 字符串、时间字段是 `datetime`。

两者字段对不上，于是每个「从库里取一行喂给平台」的地方都手写一遍字段搬运：
`api/actions.py:_to_platform_account`、`api/accounts.py` 的探活分支、
`core/scheduler.py` 的巡检循环、`api/integrations.py` 的三处回调 —— 四份实现，
每份漏一两个字段（`region` 在一份里没有，`cashier_url` 在三份里没有）。

现在只有 `AccountRecord` 一个定义：

* 平台侧直接用它 —— `core.base_platform` 里 `Account = AccountRecord` 是**别名**，
  不是子类，所以 `isinstance` 与 `type()` 都只有一种；
* ORM 行用 `AccountModel.to_record()` / `AccountRecord.to_model_fields()` 互转；
* JSON 后端用 `to_dict()` / `from_dict()`。

## `Credentials` 为什么是「视图」而不是字段

凭证字段（`access_token` / `refresh_token` / `id_token` / `session_token` / `sso` /
`totp_secret`）历史上就存在 `extra` 字典里，几百处调用点直接读
`record.extra["access_token"]`。一次性搬成独立字段要同时改注册链路、刷新链路、
四个面板同步、检测探针 —— 那是阶段 1 的 T1.4（全量加密）该做的事，不是这次。

所以 `Credentials` 是挂在 `extra` 上的**类型化视图**：读写都落在同一个字典上，
新代码可以写 `record.credentials.refresh_token`，旧代码读
`record.extra["refresh_token"]` 拿到的是同一个值。T1.4 把 `extra` 换成加密列时，
只需要改这一个类。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

#: 未显式指定状态时的默认值。与 `AccountStatus.REGISTERED` 同值。
DEFAULT_STATUS = "registered"


@dataclass
class Credentials:
    """凭证的类型化视图 —— 不复制数据，读写都落在 `AccountRecord.extra` 上。"""

    _record: "AccountRecord" = field(repr=False, compare=False)

    # ---------------------------------------------------------------- 内部
    def _get(self, key: str) -> str:
        value = self._record.extra.get(key)
        return "" if value is None else str(value)

    def _set(self, key: str, value: Any) -> None:
        text = "" if value is None else str(value)
        if text:
            self._record.extra[key] = text
        else:
            # 置空 = 删除键：`extra` 里留着空串会让「有没有 RT」的判断全部失真
            self._record.extra.pop(key, None)

    # ---------------------------------------------------------------- 属性
    @property
    def access_token(self) -> str:
        return self._get("access_token")

    @access_token.setter
    def access_token(self, value: Any) -> None:
        self._set("access_token", value)

    @property
    def refresh_token(self) -> str:
        return self._get("refresh_token")

    @refresh_token.setter
    def refresh_token(self, value: Any) -> None:
        self._set("refresh_token", value)

    @property
    def id_token(self) -> str:
        return self._get("id_token")

    @id_token.setter
    def id_token(self, value: Any) -> None:
        self._set("id_token", value)

    @property
    def session_token(self) -> str:
        return self._get("session_token")

    @session_token.setter
    def session_token(self, value: Any) -> None:
        self._set("session_token", value)

    @property
    def sso(self) -> str:
        return self._get("sso")

    @sso.setter
    def sso(self, value: Any) -> None:
        self._set("sso", value)

    @property
    def totp_secret(self) -> str:
        return self._get("totp_secret")

    @totp_secret.setter
    def totp_secret(self, value: Any) -> None:
        self._set("totp_secret", value)

    @property
    def password(self) -> str:
        return str(self._record.password or "")

    @password.setter
    def password(self, value: Any) -> None:
        self._record.password = "" if value is None else str(value)


@dataclass
class AccountRecord:
    """一个账号的完整状态。字段顺序与旧的 `Account` 一致，位置参数不会错位。"""

    platform: str
    email: str
    password: str = ""
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: str = DEFAULT_STATUS
    #: 平台自定义字段 + 凭证（见 `Credentials`）。
    extra: dict[str, Any] = field(default_factory=dict)
    #: epoch 秒。旧 `Account` 就是这个形态，平台侧代码按整数用。
    created_at: Any = field(default_factory=lambda: int(time.time()))

    # ---- 下面四个是「仓储行」才有的字段，平台侧代码不该依赖 ----
    id: Optional[str] = None
    legacy_id: Optional[int] = None
    cashier_url: str = ""
    updated_at: Any = None

    def __post_init__(self) -> None:
        if self.extra is None:
            self.extra = {}
        if self.id is None:
            # 领域对象也带 id：`save()` 需要它来判断「新建还是更新」，
            # 而 JSON 后端要求两条后端落到同一个键上（见 ADR-0008）。
            self.id = str(uuid.uuid4())

    # ---------------------------------------------------------------- 视图
    @property
    def credentials(self) -> Credentials:
        return Credentials(self)

    # ---------------------------------------------------- 平台侧的鸭子类型
    # 平台代码（`status_probe` / `token_refresh` / `cpa_upload` / `chatgpt2api`
    # 上传器）历史上是拿「任意带这些属性的对象」的。以前每个调用点现造一个
    # 空壳类往里塞属性（`platforms/chatgpt/plugin.py` 两处、
    # `services/chatgpt_sync.py`、`services/external_sync.py`）。
    #
    # 把这几个名字挂到领域模型上，空壳就不需要了 —— 读写都落在 `extra` 上，
    # 与 `Credentials` 是同一份数据，不会出现「对象上有、落库没有」。
    @property
    def access_token(self) -> str:
        return self.credentials.access_token or str(self.token or "")

    @access_token.setter
    def access_token(self, value: Any) -> None:
        self.credentials.access_token = value

    @property
    def refresh_token(self) -> str:
        return self.credentials.refresh_token

    @refresh_token.setter
    def refresh_token(self, value: Any) -> None:
        self.credentials.refresh_token = value

    @property
    def id_token(self) -> str:
        return self.credentials.id_token

    @id_token.setter
    def id_token(self, value: Any) -> None:
        self.credentials.id_token = value

    @property
    def session_token(self) -> str:
        return self.credentials.session_token

    @session_token.setter
    def session_token(self, value: Any) -> None:
        self.credentials.session_token = value

    @property
    def cookies(self) -> str:
        return str(self.extra.get("cookies") or "")

    @cookies.setter
    def cookies(self, value: Any) -> None:
        text = "" if value is None else str(value)
        if text:
            self.extra["cookies"] = text
        else:
            self.extra.pop("cookies", None)

    @property
    def client_id(self) -> str:
        return str(self.extra.get("client_id") or "")

    @client_id.setter
    def client_id(self, value: Any) -> None:
        text = "" if value is None else str(value)
        if text:
            self.extra["client_id"] = text
        else:
            self.extra.pop("client_id", None)

    # ---------------------------------------------------------------- 转换
    def to_model_fields(self) -> dict[str, Any]:
        """喂给 `AccountModel(**fields)` 的字段（不含 `extra` —— 那是 JSON 串）。"""
        from core.db.base import _utcnow  # 延迟导入：避免 core.models ↔ core.db 循环

        created = self.created_at
        if isinstance(created, (int, float)):
            created = datetime.fromtimestamp(created, tz=timezone.utc)
        updated = self.updated_at
        if isinstance(updated, (int, float)):
            updated = datetime.fromtimestamp(updated, tz=timezone.utc)

        return {
            "id": self.id,
            "legacy_id": self.legacy_id,
            "platform": self.platform,
            "email": self.email,
            "password": self.password,
            "user_id": self.user_id,
            "region": self.region,
            "token": self.token,
            "status": str(getattr(self.status, "value", self.status)),
            "cashier_url": self.cashier_url,
            "extra_json": _dump_extra(self.extra),
            "created_at": created or _utcnow(),
            "updated_at": updated or _utcnow(),
        }

    @classmethod
    def from_model(cls, model: Any) -> "AccountRecord":
        """从 ORM 行还原。**唯一**的 ORM→领域对象转换点。"""
        created = getattr(model, "created_at", None)
        if isinstance(created, datetime):
            created = int(created.timestamp())
        updated = getattr(model, "updated_at", None)
        if isinstance(updated, datetime):
            updated = int(updated.timestamp())

        return cls(
            platform=str(getattr(model, "platform", "") or ""),
            email=str(getattr(model, "email", "") or ""),
            password=str(getattr(model, "password", "") or ""),
            user_id=str(getattr(model, "user_id", "") or ""),
            region=str(getattr(model, "region", "") or ""),
            token=str(getattr(model, "token", "") or ""),
            status=getattr(model, "status", DEFAULT_STATUS) or DEFAULT_STATUS,
            extra=(
                model.get_extra()
                if callable(getattr(model, "get_extra", None))
                else _load_extra(getattr(model, "extra_json", "") or "{}")
            ),
            created_at=created if created is not None else int(time.time()),
            id=getattr(model, "id", None),
            legacy_id=getattr(model, "legacy_id", None),
            cashier_url=str(getattr(model, "cashier_url", "") or ""),
            updated_at=updated,
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON 后端用的扁平字典（与 `AccountModel` 列同名，便于直接对拷）。"""
        return {
            "id": self.id,
            "legacy_id": self.legacy_id,
            "platform": self.platform,
            "email": self.email,
            "password": self.password,
            "user_id": self.user_id,
            "region": self.region,
            "token": self.token,
            "status": str(getattr(self.status, "value", self.status)),
            "cashier_url": self.cashier_url,
            "extra_json": _dump_extra(self.extra),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AccountRecord":
        return cls(
            platform=str(data.get("platform") or ""),
            email=str(data.get("email") or ""),
            password=str(data.get("password") or ""),
            user_id=str(data.get("user_id") or ""),
            region=str(data.get("region") or ""),
            token=str(data.get("token") or ""),
            status=data.get("status") or DEFAULT_STATUS,
            extra=_load_extra(data.get("extra_json") or data.get("extra") or "{}"),
            created_at=data.get("created_at") if data.get("created_at") is not None else int(time.time()),
            id=data.get("id"),
            legacy_id=data.get("legacy_id"),
            cashier_url=str(data.get("cashier_url") or ""),
            updated_at=data.get("updated_at"),
        )


def _load_extra(raw: Any) -> dict[str, Any]:
    """解析 extra 并**解密**凭证字段（老明文原样通过）。"""
    import json

    from core.credential_crypto import decrypt_extra

    if isinstance(raw, dict):
        return decrypt_extra(raw)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return decrypt_extra(parsed) if isinstance(parsed, dict) else {}


def _dump_extra(extra: dict[str, Any]) -> str:
    """序列化并**加密**凭证字段 —— 与 `_load_extra` 对称。

    两条后端（SQLite / JSON）都从这里拿 `extra_json`，所以加密只在这一处生效，
    不会出现「SQLite 是密文、JSON 是明文」。
    """
    import json

    from core.credential_crypto import encrypt_extra

    try:
        return json.dumps(encrypt_extra(extra or {}), ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"
