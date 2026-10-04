"""平台数据库注册表：每个平台可以有自己的库。

为什么
------
不同平台的账号结构、生命周期、量级都不同（Grok 只有 SSO/token，ChatGPT 还有
Plus 状态、2FA、支付链接，iCloud 有主号+别名两层）。把它们的表塞在同一个库
里，既不利于隔离（一个平台写爆不影响别人），也不利于迁移和备份。

设计
----
- **默认库**（`DATABASE_URL`）承载跨平台基础设施：任务、日志、代理、配置。
- **平台库**（可选，`DATABASE_URL_<PLATFORM>` 或 `PLATFORM_DATABASE_URLS`）：
  该平台的账号表。未配置时透明回落到默认库，所以是纯增量能力，不配也能跑。

用法
----
    from core.db import platform_session, platform_engine

    with platform_session("grok") as session:      # 有配置用平台库，没有用默认库
        ...

表结构由各平台自己声明（`platforms/<name>/models.py` 的 `ensure_schema`），
平台库首次使用时会自动建表。
"""
from __future__ import annotations

import threading
from typing import Iterator

from sqlalchemy.engine import Engine
from sqlmodel import Session, create_engine

from .base import DATABASE_URL, parse_platform_database_urls

_lock = threading.Lock()


class PlatformDatabaseRegistry:
    """平台 → 数据库 URL / Engine 的映射与惰性建表。"""

    def __init__(self) -> None:
        self._urls: dict[str, str] = {}
        self._engines: dict[str, Engine] = {}
        self._schema_ready: set[str] = set()

    # ------------------------------------------------------------ 配置

    def configure(self, mapping: dict[str, str] | None = None) -> None:
        """设置平台分库映射（会清掉已缓存 engine，便于测试与热更新）。"""
        resolved = dict(mapping if mapping is not None else parse_platform_database_urls())
        with _lock:
            self._urls = {
                str(k).strip().lower(): str(v).strip()
                for k, v in resolved.items()
                if str(k).strip() and str(v).strip()
            }
            self._engines.clear()
            self._schema_ready.clear()

    def reload_from_env(self) -> None:
        self.configure(parse_platform_database_urls())

    def url_for(self, platform: str) -> str:
        """平台库 URL；未单独配置则用默认库。"""
        name = str(platform or "").strip().lower()
        if not name:
            return DATABASE_URL
        with _lock:
            if not self._urls:
                # 首次访问时从环境补齐，避免 import 顺序影响
                self._urls = {
                    str(k).strip().lower(): str(v).strip()
                    for k, v in parse_platform_database_urls().items()
                    if str(k).strip() and str(v).strip()
                }
            return self._urls.get(name) or DATABASE_URL

    def is_dedicated(self, platform: str) -> bool:
        """该平台是否配置了独立库（未配置时所有平台共库）。"""
        name = str(platform or "").strip().lower()
        if not name:
            return False
        return self.url_for(name) != DATABASE_URL

    def configured_platforms(self) -> list[str]:
        with _lock:
            return sorted(self._urls)

    # ------------------------------------------------------------ Engine

    def engine_for(self, platform: str) -> Engine:
        """取平台 engine（未分库时即默认 engine）。"""
        from .base import current_engine

        name = str(platform or "").strip().lower()
        url = self.url_for(name)
        if url == DATABASE_URL and not self.is_dedicated(name):
            return current_engine()
        # 注意：锁内不调用 url_for/is_dedicated（它们也会取 _lock）。
        # _lock 是普通 Lock（非重入），锁内重入会直接自死锁。
        with _lock:
            cached = self._engines.get(name)
            if cached is not None:
                return cached
            created = create_engine(url)
            self._engines[name] = created
            return created

    def register_schema(self, platform: str, tables: list) -> None:
        """在平台库中建表（幂等，每个平台库只跑一次）。

        平台自己的模型定义在 `platforms/<name>/models.py`，这里不 import 它们，
        由调用方把表对象传进来，避免 core 反向依赖 platforms。
        """
        name = str(platform or "").strip().lower()
        if not name or not tables:
            return
        key = f"{name}:{self.url_for(name)}"

        # 先无锁判断是否已建过，再取 engine（engine_for 自己会加锁）——
        # 持锁调用 engine_for 会自死锁
        with _lock:
            if key in self._schema_ready:
                return
        engine = self.engine_for(name)

        from sqlmodel import SQLModel

        # 只建本平台的表：SQLModel.metadata 里可能混着别的平台/基础设施表，
        # 直接用 create_all 会顺手把它们建到错误的库里
        SQLModel.metadata.create_all(engine, tables=list(tables))

        with _lock:
            self._schema_ready.add(key)

    def session_for(self, platform: str) -> Session:
        """打开平台库会话（调用方负责关闭，或用 platform_session 上下文）。"""
        return Session(self.engine_for(platform))

    def dispose_all(self) -> None:
        with _lock:
            for engine in self._engines.values():
                try:
                    engine.dispose()
                except Exception:
                    pass
            self._engines.clear()
            self._schema_ready.clear()


platform_database_registry = PlatformDatabaseRegistry()


def platform_engine(platform: str) -> Engine:
    return platform_database_registry.engine_for(platform)


def platform_session(platform: str) -> Session:
    """平台库会话（支持 `with`，未分库时即默认库）。

    用法::

        with platform_session("grok") as session:
            ...

    或作为 FastAPI 依赖::

        def endpoint(session: Session = Depends(platform_session_dep("grok"))):
    """
    return platform_database_registry.session_for(platform)


def platform_session_dep(platform: str):
    """生成一个 FastAPI 依赖函数（按平台取库）。"""

    def _dep() -> Iterator[Session]:
        with platform_database_registry.session_for(platform) as session:
            yield session

    return _dep


def platform_session_from_path(platform: str) -> Iterator[Session]:
    """FastAPI 依赖：从路径参数 `platform` 解析分库。

    用于路由形如 `/{platform}/...` 的接口 —— 平台是路径参数时，装饰期无法
    知道是哪个平台，只能在请求期解析。FastAPI 会把路径参数 `platform`
    注入到这里的同名形参上。

    用法::

        @router.post("/{platform}/{account_id}/action")
        def endpoint(platform: str, session: Session = Depends(platform_session_from_path)):
            ...
    """
    with platform_database_registry.session_for(platform) as session:
        yield session


__all__ = [
    "PlatformDatabaseRegistry",
    "platform_database_registry",
    "platform_engine",
    "platform_session",
    "platform_session_dep",
    "platform_session_from_path",
]
