"""存储后端抽象：SQLite 与 JSON 两种实现。

用户要求（2026-10-27）：「增加数据存储方式（json、sqlite），在 env 中配置，
默认 json，现在使用 sqlite」。

## 为什么要有这一层

现有 129 个数据访问点直接使用 SQLModel 的 `Session`（含跨库查询、唯一约束、
事务）。JSON 没有 SQL —— 把 JSON 塞进 SQLAlchemy 方言层是假抽象。所以：

- `StorageBackend` 定义**业务需要的操作**（不是 SQL 操作）；
- `sqlite_backend` / `json_backend` 各自实现；
- 业务层逐步改调后端，而不是直接 `Session`。

## 默认值为什么不是「一律 json」

用户说「默认 json」，但**已有 SQLite 库的部署不能静默换库** —— 那会让用户
的账号、任务、配置全部「消失」（实际是读不到）。所以：

1. 显式 `STORAGE_BACKEND` 优先；
2. 没配时：已有库 → sqlite（保住数据）；全新部署 → json（用户要的默认）。

这是「默认 json」与「不丢数据」之间的正确取舍 —— 见
`resolve_backend_name` 的测试。
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

#: 支持的后端名
BACKEND_SQLITE = "sqlite"
BACKEND_JSON = "json"
SUPPORTED_BACKENDS = (BACKEND_SQLITE, BACKEND_JSON)

#: 环境变量名
ENV_BACKEND = "STORAGE_BACKEND"

#: 账号领域的**独立开关**。理由见 `accounts_backend_enabled`。
ENV_ACCOUNTS_BACKEND = "STORAGE_BACKEND_ACCOUNTS"

#: 用户要求的默认（全新部署时）
DEFAULT_BACKEND = BACKEND_JSON


def resolve_backend_name(
    env: Optional[Mapping[str, str]] = None,
    *,
    db_exists: bool,
) -> str:
    """解析该用哪个后端。

    `db_exists`：SQLite 库文件是否已存在（决定「没配置」时的行为）。

    规则：
    - 显式配置且合法 → 用它；
    - 显式配置但非法 → 按「没配置」处理（不因为一个拼写错误换后端）；
    - 没配置 → 已有库用 sqlite，全新部署用 json。
    """
    source = os.environ if env is None else env
    raw = str(source.get(ENV_BACKEND, "") or "").strip().lower()

    if raw in SUPPORTED_BACKENDS:
        return raw

    # 没配置 / 配置非法：已有库就保住 sqlite（不静默换库 = 不丢数据），
    # 全新部署才用用户要求的默认。
    return BACKEND_SQLITE if db_exists else DEFAULT_BACKEND


def accounts_backend_enabled(backend: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """账号领域是否走该后端。

    **账号的 JSON 后端要单独显式开启**（`STORAGE_BACKEND_ACCOUNTS=json`）。

    为什么不能跟着总开关一起生效：账号有约 60 处原生写路径
    （`sess.add(model); sess.commit()`），而仓储只覆盖了读写的一部分。
    直接生效会得到：

        create_account：仓储**读**（JSON）→ 没找到 → 原生 session **写**（SQLite）
        下一次 find_by_email：仓储**读**（JSON）→ 仍然没找到

    **新建的账号查不到** —— 静默的数据不一致，比报错严重得多。

    所以默认只让**配置**走 JSON（那条路径完整），账号留给显式开关。
    """
    if backend != BACKEND_JSON:
        return False
    source = os.environ if env is None else env
    raw = str(source.get(ENV_ACCOUNTS_BACKEND, "") or "").strip().lower()
    return raw == BACKEND_JSON


__all__ = [
    "BACKEND_JSON",
    "BACKEND_SQLITE",
    "DEFAULT_BACKEND",
    "ENV_BACKEND",
    "SUPPORTED_BACKENDS",
    "resolve_backend_name",
]
