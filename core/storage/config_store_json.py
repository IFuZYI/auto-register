"""配置的 JSON 后端。

对应 `core.config_store.ConfigStore` 的 SQLite 实现 —— 接口一致（`get` /
`set` / `get_all` / `delete`），底层换成 `JsonStore`。

## 为什么配置是第一个切换的领域

它只有四个操作、没有跨表查询、没有唯一约束，是验证「业务层改调后端」的
最小闭环。跑通它再动账号/任务那些带分页与多条件过滤的领域。

## 值一律是字符串

SQLite 版的 `value` 是 `VARCHAR` 列，调用方到处做 `str(...)` / `.strip()` /
比较 `"1"`。JSON 若把值存成 bool/int，`bool("0")` 与 `bool(0)` 的差异会让
那些判断**静默失效**。所以这里强制转成字符串 —— 语义与 SQLite 版一致。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.storage.json_backend import JsonStore

#: JSON 里的表名（与 SQLite 的 `configs` 表对齐，便于迁移脚本对照）
TABLE = "configs"


class JsonConfigStore:
    """配置的 JSON 实现。接口与 `core.config_store.ConfigStore` 的存储部分一致。"""

    def __init__(self, path: Path | str):
        self._store = JsonStore(path)

    def get(self, key: str, default: str = "") -> str:
        record = self._store.get(TABLE, str(key))
        if not isinstance(record, dict):
            return default
        value = record.get("value")
        return default if value is None else str(value)

    def set(self, key: str, value: Any) -> None:
        self._store.put(TABLE, str(key), {"key": str(key), "value": "" if value is None else str(value)})

    def get_all(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for record in self._store.all(TABLE):
            if not isinstance(record, dict):
                continue
            name = str(record.get("key") or "")
            if name:
                out[name] = "" if record.get("value") is None else str(record["value"])
        return out

    def delete(self, key: str) -> bool:
        return self._store.delete(TABLE, str(key))


__all__ = ["JsonConfigStore", "TABLE"]
