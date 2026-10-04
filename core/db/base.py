"""数据库基础层：时间工具、默认 engine、会话依赖。

分库设计
--------
- **默认库**（`DATABASE_URL`）：任务、日志、代理、配置等跨平台基础设施。
- **平台库**（可选）：某平台账号可单独落到自己的库，见 `core.db.registry`。

`engine` 保持向后兼容（= 默认库），新代码请用 `get_session` / `platform_session`。
"""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from typing import Iterator, Optional

from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from core.paths import (
    DATA_DIR,
    DEFAULT_DB_FILE,
    DEFAULT_PLATFORM_DB_FILES,
    PLATFORM_DB_DIR,
)

# --------------------------------------------------------------------- 工具


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_alias_share_token() -> str:
    """128 位随机串，够长到不能枚举，又短到能塞进一行链接里。"""
    return secrets.token_urlsafe(16)


# --------------------------------------------------------------------- 默认库

# 默认落到 data/（见 core/paths.py）。用绝对路径而不是相对路径：
# SQLite 会把相对路径按**进程工作目录**解析，从别处启动就会连到另一个库上。
DATABASE_URL = str(os.getenv("DATABASE_URL", "") or "").strip() or (
    f"sqlite:///{DEFAULT_DB_FILE}"
)

# 建库前先确保 data/ 布局存在，否则 sqlite 会因为目录不存在而失败。
if DATABASE_URL.startswith("sqlite:///"):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PLATFORM_DB_DIR.mkdir(parents=True, exist_ok=True)

engine = create_engine(DATABASE_URL)


def current_engine() -> Engine:
    """当前生效的默认库 engine。

    解析顺序：`core.db.engine`（允许运行时替换，测试与热更新依赖这一点）
    → 本模块的 `engine`。重构前所有代码共享 `core.db.engine` 这一个全局，
    测试用 monkeypatch 换掉它就能整体切库；保留该语义避免破坏既有契约。
    """
    try:
        import core.db as _pkg

        patched = getattr(_pkg, "engine", None)
        if patched is not None:
            return patched
    except Exception:
        pass
    return engine


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：默认库会话。"""
    with Session(current_engine()) as session:
        yield session


# --------------------------------------------------------------------- 邮箱键

def normalize_email(email: str) -> str:
    """统一邮箱键的规范形式。

    邮箱作为账号唯一业务键，必须大小写无关：``User@X.ai`` 与 ``user@x.ai``
    是同一个账号，否则「已注册不再重复注册」会漏判。
    """
    return str(email or "").strip().lower()


# --------------------------------------------------- 邮箱的「按平台消耗」记账
#
# 邮箱是一次性的，但**只对用掉它的那个平台**一次性：同一个地址注册过 ChatGPT
# 之后还能注册 Grok —— 两个平台的账号体系互不相干。只用一个全局
# `status='used'` / `pool_status='used'` 记不出这层区别，会把地址对所有平台
# 一起锁死（实测：iCloud 池 22 个 used 别名里 13 个 grok 从没用过却全领不到，
# 只剩 2 个 available，很快就要撞 Apple 每小时 5 个的生成额度）。
#
# 存成 `,grok,chatgpt,` 而不是 JSON：这样「某平台用过没有」是
# `f",{platform}," in field`，一次字符串包含判断，不用解析 —— 取号是热路径。
# 两端都带逗号是为了避免子串误命中（`,grok,` 不会匹配 `,grok2,`）。

_USED_PLATFORMS_SEP = ","


def mark_email_used_by(field: str, platform: str) -> str:
    """把 `platform` 并进「已消耗平台」字段，返回新值（幂等）。

    字段格式固定为 `,name1,name2,`（首尾都带逗号），所以判断是
    `f",{name}," in field` —— 一次包含判断，取号热路径上不用解析。
    首尾逗号同时挡住子串误命中：`,grok,` 不会匹配 `,grok2,`。

    `platform` 为空时原样返回 —— 没有平台信息时不能瞎记，否则会把地址
    错误地锁给某个平台。
    """
    name = str(platform or "").strip().lower()
    if not name:
        return str(field or "")
    current = str(field or "")
    if f"{_USED_PLATFORMS_SEP}{name}{_USED_PLATFORMS_SEP}" in current:
        return current
    if not current:
        return f"{_USED_PLATFORMS_SEP}{name}{_USED_PLATFORMS_SEP}"
    if not current.startswith(_USED_PLATFORMS_SEP):
        current = f"{_USED_PLATFORMS_SEP}{current}"
    if not current.endswith(_USED_PLATFORMS_SEP):
        current = f"{current}{_USED_PLATFORMS_SEP}"
    return f"{current}{name}{_USED_PLATFORMS_SEP}"


def email_used_by_platform(field: str, platform: str) -> bool:
    """`field` 里记的已消耗平台是否包含 `platform`（大小写无关）。

    字段为空 = 没有记录：返回 False，即「这个平台还没用过它」。
    老数据迁移后会把历史 `used` 行填成「所有已知平台」，见 migrations。
    """
    name = str(platform or "").strip().lower()
    if not name:
        return False
    return f"{_USED_PLATFORMS_SEP}{name}{_USED_PLATFORMS_SEP}" in str(field or "")


def resolve_database_url(platform: str) -> str:
    """解析某平台应使用的数据库 URL（未配置则回落到默认库）。"""
    from .registry import platform_database_registry

    return platform_database_registry.url_for(platform)


def parse_platform_database_urls(raw: Optional[str] = None) -> dict[str, str]:
    """解析平台分库配置。

    支持两种写法（后者优先）：
      - ``DATABASE_URL_GROK=sqlite:///data/grok.db``（按平台名大写）
      - ``PLATFORM_DATABASE_URLS='{"grok": "sqlite:///data/grok.db"}'``（JSON 总表）

    另外有**内置默认分库**：``icloud`` 与 ``outlook`` 两个邮箱池各自落到
    ``data/platforms/<name>.db``。它们和注册平台不是一回事 —— 是「邮箱来源」，
    被多个平台的注册流程共用，混在默认库里既不好备份也不好单独清空。
    显式配置（上面两种写法）优先级更高，可以覆盖或改指到别处。
    """
    mapping: dict[str, str] = {}

    # 内置默认：邮箱池分库。放在最前面，后面显式配置会覆盖同名的键。
    for name, filename in DEFAULT_PLATFORM_DB_FILES.items():
        mapping[name] = f"sqlite:///{PLATFORM_DB_DIR / filename}"

    text = str(raw if raw is not None else os.getenv("PLATFORM_DATABASE_URLS", "") or "").strip()
    if text:
        try:
            data = json.loads(text)
        except (TypeError, ValueError):
            data = None
        if isinstance(data, dict):
            for key, value in data.items():
                name = str(key or "").strip().lower()
                url = str(value or "").strip()
                if name and url:
                    mapping[name] = url

    prefix = "DATABASE_URL_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        name = key[len(prefix):].strip().lower()
        url = str(value or "").strip()
        # PLATFORM_DATABASE_URLS 自身以 PLATFORM_ 开头，不会被这个前缀命中；
        # 这里再排除一次，避免将来有人写成 DATABASE_URL_PLATFORM_* 造成误解
        if name and url and not name.startswith("platform_"):
            mapping[name] = url
    return mapping


__all__ = [
    "DATABASE_URL",
    "engine",
    "get_session",
    "normalize_email",
    "new_alias_share_token",
    "parse_platform_database_urls",
    "resolve_database_url",
    "_utcnow",
]
