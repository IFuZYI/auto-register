from __future__ import annotations

"""全局配置持久化 - 存储在 SQLite，并在缺省时回退到环境变量/.env。"""
import os
import re
from pathlib import Path
from typing import Optional
from sqlmodel import Field, SQLModel, Session, select
from .db import current_engine
from .mail_import_sources import normalize_mail_provider


_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


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
    """简单 key-value 配置存储"""

    def get(self, key: str, default: str = "") -> str:
        env_values = _runtime_env_values()
        with Session(current_engine()) as s:
            item = s.get(ConfigItem, key)
            value = str(item.value if item else "" or "").strip()
            if value:
                return value
        fallback = _get_env_fallback_value(key, env_values=env_values)
        return fallback or default

    def set(self, key: str, value: str) -> None:
        with Session(current_engine()) as s:
            item = s.get(ConfigItem, key)
            if item:
                item.value = value
            else:
                item = ConfigItem(key=key, value=value)
            s.add(item)
            s.commit()

    def get_all(self) -> dict:
        with Session(current_engine()) as s:
            items = s.exec(select(ConfigItem)).all()
            values = {i.key: i.value for i in items}
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
        with Session(current_engine()) as s:
            for key, value in data.items():
                item = s.get(ConfigItem, key)
                if item:
                    item.value = value
                else:
                    item = ConfigItem(key=key, value=value)
                s.add(item)
            s.commit()


config_store = ConfigStore()
