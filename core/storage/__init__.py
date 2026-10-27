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


def sqlite_db_exists() -> bool:
    """当前 engine 指向的 SQLite 库文件是否已存在。

    **必须从 engine 的 URL 解析，不能用固定路径**。用固定路径会在两种场景
    判错：

    - 测试（以及任何换过 engine 的部署）：库在临时目录，固定路径看不到它，
      于是判定成「全新部署」→ 静默切到 JSON（实测：代理表因此静默切了，
      3 个无关测试变红）；
    - 自定义 `DATABASE_URL`：同理。

    这是本仓第二次踩同一个坑（第一次在 `core/config_store`），所以抽到这里
    共用，不再各写一份。
    """
    try:
        from core.db import current_engine

        url = str(current_engine().url)
    except Exception as exc:  # noqa: BLE001 - 取不到就当没有
        import logging

        logging.getLogger(__name__).warning(
            "解析当前 engine 失败（按「无库」处理）: %s", exc
        )
        return False
    if not url.startswith("sqlite:///"):
        return False
    raw = url[len("sqlite:///"):]
    if not raw or raw == ":memory:":
        return False
    from pathlib import Path

    return Path(raw).exists()


def accounts_backend_enabled(backend: str, env: Optional[Mapping[str, str]] = None) -> bool:
    """账号领域是否走该后端。

    历史上这里是一道**显式门槛**（`STORAGE_BACKEND_ACCOUNTS=json`）：当时账号
    有约 60 处原生写路径（`sess.add(model); sess.commit()`），而仓储只覆盖了
    读写的一部分。直接跟着总开关生效会得到：

        create_account：仓储**读**（JSON）→ 没找到 → 原生 session **写**（SQLite）
        下一次 find_by_email：仓储**读**（JSON）→ 仍然没找到

    也就是**新建的账号查不到** —— 静默的数据不一致，比报错严重得多。

    **2026-10-27：门槛已撤。** 账号的写路径已全部收敛到
    `AccountRepository.save()`（23 处），原生 `session.add(账号)` 一个不剩，
    所以跟着总开关生效是安全的。

    `STORAGE_BACKEND_ACCOUNTS=sqlite` 仍可用于「配置用 JSON、账号留 SQLite」
    的混合部署 —— 门槛从「必须显式开启」变成「可以显式关掉」。
    """
    if backend != BACKEND_JSON:
        return False
    source = os.environ if env is None else env
    raw = str(source.get(ENV_ACCOUNTS_BACKEND, "") or "").strip().lower()
    # 显式关掉：配置走 JSON、账号留 SQLite
    if raw == BACKEND_SQLITE:
        return False
    return True


__all__ = [
    "BACKEND_JSON",
    "BACKEND_SQLITE",
    "DEFAULT_BACKEND",
    "ENV_BACKEND",
    "SUPPORTED_BACKENDS",
    "resolve_backend_name",
]
