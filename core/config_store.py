from __future__ import annotations

import logging

"""全局配置持久化 - 存储在 SQLite，并在缺省时回退到环境变量/.env。"""
import os
import re
from pathlib import Path
from typing import Optional
from sqlmodel import Field, SQLModel, Session, select
from .db import current_engine
from .mail_import_sources import normalize_mail_provider
from core.config_schema import SECRET_CONFIG_KEYS
from core.credential_crypto import decrypt_text, encrypt_text


_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _seal(key: str, value):
    """配置口令落盘前加密（Q3：`configs` 表里不留明文口令）。

    非口令键、空值、已经是密文 → 原样返回（`encrypt_text` 自带幂等）。
    """
    if key not in SECRET_CONFIG_KEYS:
        return value
    return encrypt_text(value)


def _unseal(key: str, value):
    """读出来还原成明文。老数据是明文 → `decrypt_text` 原样放行。"""
    if key not in SECRET_CONFIG_KEYS:
        return value
    return decrypt_text(value)


def _normalize_config_value(value) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    return text


def _canonical_config_key(key: str) -> str:
    value = str(key or "").strip()
    if not value:
        return ""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _config_key_candidates(key: str) -> list[str]:
    raw = str(key or "").strip()
    if not raw:
        return []

    normalized = re.sub(r"[^A-Za-z0-9]+", "_", raw).strip("_")
    candidates: list[str] = []
    seen = set()
    for item in (
        raw,
        raw.lower(),
        raw.upper(),
        normalized,
        normalized.lower(),
        normalized.upper(),
    ):
        value = str(item or "").strip()
        if value and value not in seen:
            seen.add(value)
            candidates.append(value)
    return candidates


def _load_env_file(path: Path | str | None = None) -> dict[str, str]:
    env_path = Path(path or _ENV_FILE)
    if not env_path.exists():
        return {}

    try:
        lines = env_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return {}

    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _normalize_config_value(value)
    return values


def _runtime_env_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for key, value in _load_env_file().items():
        text = _normalize_config_value(value)
        if text:
            values[key] = text
    for key, value in os.environ.items():
        text = _normalize_config_value(value)
        if text:
            values[key] = text
    return values


def _get_env_fallback_value(key: str, env_values: Optional[dict[str, str]] = None) -> str:
    values = env_values if env_values is not None else _runtime_env_values()
    for candidate in _config_key_candidates(key):
        text = str(values.get(candidate, "") or "").strip()
        if text:
            return text
    return ""


#: 指向同一服务的重复配置键：`{别名: 规范键}`。
#:
#: 背景：CLIProxyAPI 在本应用里有两条使用路径 —— ① 把 ChatGPT 账号**上传**进去
#: （`platforms/chatgpt/cpa_upload.py`，键名 `cpa_*`）；② 读它的 auth-files
#: **同步账号状态**（`services/cliproxyapi_sync.py`，键名 `cliproxyapi_*`）。
#: 两条路径打的是同一个端点（`/v0/management/auth-files`），
#: `services/panel_registry.py` 的注释也写着「CPA 面板 = CLIProxyAPI 的 auth-files
#: 接口」。于是用户要在两个页面各填一遍同样的地址与口令 —— 面板管理页一份、
#: 平台配置页一份，改了一个另一个不生效。
#:
#: 收敛为单份：规范键是 `cpa_*`（历史更久、被上传路径与自动维护任务依赖），
#: 别名键只作**读取时的兜底**保留 —— 老用户库里只填过 `cliproxyapi_*`，
#: 直接删键会让他们的配置静默失效。
#: （界面上「平台配置」已并入「全局配置 → 面板配置」，两个页面的问题不复存在；
#: 这里保留的是**老数据**的兜底。）
_DUPLICATE_SERVICE_KEYS: dict[str, str] = {
    "cliproxyapi_base_url": "cpa_api_url",
    "cliproxyapi_management_key": "cpa_api_key",
}

#: 迁移映射：**旧键 → 新键**。旧键曾被界面写入、现在由新键取代。
#:
#: 与 `_DUPLICATE_SERVICE_KEYS` 的区别：那是「同一个服务的两个别名」，
#: 这是「功能搬家」（注册方式并入执行器）。共同点是**都必须在读取的必经
#: 之路迁移** —— 只改界面的话，用户已保存的值会被静默丢弃。
#:
#: 规则：新键**非空时优先**；新键为空而旧键有值，则把旧键的值迁移过去。
#: 旧键保留原值（仍有代码/脚本读它，见 `platforms/grok/plugin.py` 的兜底链）。
_LEGACY_VALUE_MIGRATIONS: dict[str, str] = {
    # Grok 的「注册方式」（browser/protocol）已并入「执行器」。
    # 不迁移的后果实测：库里存着 grok_register_mode='browser'（用户选的），
    # 新界面只读 grok_executor（空）→ 回落到旧全局 default_executor='protocol'
    # → 把默认任务打进协议路径，而那条路在 x.ai 上会被 CF 403。
    "grok_register_mode": "grok_executor",
}


def _migrate_legacy_values(values: dict[str, str]) -> None:
    """把已搬家功能的旧键值迁移到新键（原地修改）。

    只在**新键为空**时迁移 —— 用户在新键上的选择优先，不能被旧值覆盖。
    不做「新键有值时反写旧键」：旧键的读者（如 Grok 的兜底链）在
    `_executor_explicit` 为真时根本不看它，反写只会制造两个真相。
    """
    for legacy_key, new_key in _LEGACY_VALUE_MIGRATIONS.items():
        current = str(values.get(new_key, "") or "").strip()
        if current:
            continue
        legacy = str(values.get(legacy_key, "") or "").strip()
        if legacy:
            values[new_key] = legacy


def _collapse_duplicate_service_keys(values: dict[str, str]) -> None:
    """把重复服务的别名键收敛到规范键（原地修改）。

    规则：规范键**非空时优先**；规范键为空而别名键有值，则用别名键的值补上
    （老数据迁移）。同时把别名键也填成同一个值 —— 仍有代码直接读别名键
    （`services/cliproxyapi_sync.py`、`services/chatgpt_sync.py`），
    这里只改返回值的话，那些直读路径会拿不到用户新填的地址。

    放在 `get_all()` 里而不是各读取点：它是所有读取方的必经之路（界面走
    `GET /api/config`、任务走 `RegistrationContextBuilder(get_all())`）。
    """
    for alias_key, canonical_key in _DUPLICATE_SERVICE_KEYS.items():
        canonical = str(values.get(canonical_key, "") or "").strip()
        alias = str(values.get(alias_key, "") or "").strip()
        if canonical:
            # 规范键有值 → 以它为准，别名键跟着它（让直读别名的代码也能拿到）
            if alias != canonical:
                values[alias_key] = canonical
        elif alias:
            # 老数据只填过别名键 → 迁移到规范键
            values[canonical_key] = alias


def _merge_env_fallback(values: dict[str, str], env_values: Optional[dict[str, str]] = None) -> dict[str, str]:
    merged = dict(values or {})
    runtime_values = env_values if env_values is not None else _runtime_env_values()
    for env_key, env_value in runtime_values.items():
        text = str(env_value or "").strip()
        if not text:
            continue
        canonical_key = _canonical_config_key(env_key)
        for target_key in (env_key, canonical_key):
            if not target_key:
                continue
            if str(merged.get(target_key, "") or "").strip():
                continue
            merged[target_key] = text
    return merged


class ConfigItem(SQLModel, table=True):
    __tablename__ = "configs"
    key: str = Field(primary_key=True)
    value: str = ""


class ConfigStore:
    """简单 key-value 配置存储。

    支持两种后端（`STORAGE_BACKEND=sqlite|json`，见 `core.storage`）：

    - `sqlite`（默认）：`configs` 表；
    - `json`：`data/config.json`。

    **业务逻辑与存储无关**：`get()` 的环境变量回落、`get_all()` 的 provider
    名收敛与旧值迁移都挂在读取之后，两种后端共用 —— 否则切了后端，用户已
    保存的配置会「看起来丢了」。
    """

    # 类级默认：有些测试用 `ConfigStore.__new__` 绕过 `__init__`（直接塞
    # monkeypatch 的 session），实例上不会有这两个属性 —— 给默认值免得
    # 每次访问都要 `getattr`。
    _backend_override: str = ""
    _json_path_override = None

    def __init__(self, *, backend: str = "", json_path=None):
        """`backend` / `json_path` 只给测试用；生产走 `resolve_backend_name`。"""
        self._backend_override = str(backend or "").strip().lower()
        self._json_path_override = json_path

    # ---------------------------------------------------------------- 后端

    def _backend_name(self) -> str:
        if self._backend_override:
            return self._backend_override
        from core.paths import DATA_DIR
        from core.storage import BACKEND_JSON, resolve_backend_name

        # 判据是「**当前生效的 SQLite 库**是否已存在」，而不是固定路径。
        #
        # 两条踩过的坑：
        # 1. 把 `json_path.exists()` 也算进去 → 自反馈：`set()` 写了 JSON →
        #    下次判定看到 JSON → 返回 sqlite → `get()` 查不存在的表
        #    （`no such table: configs`）。JSON 存在只说明「用过 JSON 后端」。
        # 2. 用固定的 `DATA_DIR/account_manager.db` → 测试（以及任何换过
        #    engine 的部署）把 engine 指向临时库时，判定看不到它，于是走
        #    JSON 而数据在 SQLite 里，读回为空。
        #
        # 所以从当前 engine 的 URL 解析真实路径。
        return resolve_backend_name(db_exists=self._sqlite_db_exists())

    def _sqlite_db_exists(self) -> bool:
        """见 `core.storage.sqlite_db_exists` —— 共用一份，不再各写一遍。"""
        from core.storage import sqlite_db_exists

        return sqlite_db_exists()

    def _json_file(self):
        if self._json_path_override is not None:
            return self._json_path_override
        from core.paths import DATA_DIR

        return DATA_DIR / "config.json"

    def _json_store(self):
        from core.storage.config_store_json import JsonConfigStore

        return JsonConfigStore(self._json_file())

    # ---------------------------------------------------------------- 读写

    def get(self, key: str, default: str = "") -> str:
        env_values = _runtime_env_values()
        if self._backend_name() == "json":
            value = self._json_store().get(key, "").strip()
            if value:
                return _unseal(key, value)
        else:
            with Session(current_engine()) as s:
                item = s.get(ConfigItem, key)
                value = str(item.value if item else "" or "").strip()
                if value:
                    return _unseal(key, value)
        fallback = _get_env_fallback_value(key, env_values=env_values)
        return fallback or default

    def set(self, key: str, value: str) -> None:
        value = _seal(key, value)
        if self._backend_name() == "json":
            self._json_store().set(key, value)
            return
        with Session(current_engine()) as s:
            item = s.get(ConfigItem, key)
            if item:
                item.value = value
            else:
                item = ConfigItem(key=key, value=value)
            s.add(item)
            s.commit()

    def delete(self, key: str) -> bool:
        """删一个配置键。JSON 与 SQLite 两种后端都支持。"""
        if self._backend_name() == "json":
            return self._json_store().delete(key)
        with Session(current_engine()) as s:
            item = s.get(ConfigItem, key)
            if item is None:
                return False
            s.delete(item)
            s.commit()
            return True

    def get_all(self) -> dict:
        if self._backend_name() == "json":
            values = {
                k: _unseal(k, v) for k, v in self._json_store().get_all().items()
            }
        else:
            with Session(current_engine()) as s:
                items = s.exec(select(ConfigItem)).all()
                values = {i.key: _unseal(i.key, i.value) for i in items}
        merged = _merge_env_fallback(values)
        # 已删除的 provider 名在这里收敛（applemail → 空串，见
        # `core.mail_import_sources`）。放在 get_all 而不是各个调用点：这是
        # 所有读取方的必经之路 —— 界面走 `GET /api/config`、任务走
        # `RegistrationContextBuilder(get_all())`、周期任务走 `get_all()` 的
        # 兄弟路径，只在视图层收敛会让后两者拿到已删除的 provider 名，建邮箱
        # 时才炸，且报错里看不出是配置遗留。
        if "mail_provider" in merged:
            merged["mail_provider"] = normalize_mail_provider(merged.get("mail_provider"))
        _collapse_duplicate_service_keys(merged)
        # 功能搬家（注册方式 → 执行器）的旧值迁移。同上：必须挂在必经之路，
        # 否则用户已保存的值在新界面里等于丢了。
        _migrate_legacy_values(merged)
        return merged

    def set_many(self, data: dict) -> None:
        if self._backend_name() == "json":
            store = self._json_store()
            for key, value in data.items():
                store.set(key, _seal(key, value))
            return
        with Session(current_engine()) as s:
            for key, value in data.items():
                value = _seal(key, value)
                item = s.get(ConfigItem, key)
                if item:
                    item.value = value
                else:
                    item = ConfigItem(key=key, value=value)
                s.add(item)
            s.commit()


logger = logging.getLogger(__name__)

config_store = ConfigStore()
