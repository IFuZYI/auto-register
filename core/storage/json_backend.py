"""JSON 文件存储：`{表名: {主键: 记录}}` 的单文件结构。

## 为什么是这个形状

`{"accounts": {"1": {...}, "2": {...}}}` —— 主键做内层键而不是数组：
按 id 取是 O(1)，不用遍历；`put` 幂等（同 id 覆盖）；不需要维护自增游标
（调用方给 id）。

## 三条硬要求

1. **原子写**：先写 `.tmp` 再 `os.replace`。直接覆盖时进程被杀会留下半截
   JSON，整个库读不出来。
2. **损坏不静默**：文件解析失败就抛，**绝不**当成空库继续 —— 那样下一次
   写入会把用户全部数据覆盖掉。
3. **读-改-写要加锁**：同一进程多线程（任务线程 + 请求线程）会并发写。
   锁在实例上；跨进程不在保证范围内（单进程部署是本项目的既有前提）。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Optional


class JsonStoreError(RuntimeError):
    """JSON 库文件损坏或结构非法。"""


class JsonStore:
    """单文件 JSON 存储。线程安全（进程内）。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- 内部

    def _read(self) -> dict[str, dict[str, Any]]:
        """读整个库。文件不存在 = 空库；文件损坏 = 抛（不静默）。"""
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise JsonStoreError(f"读取 {self.path} 失败: {exc}") from exc
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            # **不能** return {} —— 那会让下一次写入把全部数据覆盖掉。
            raise JsonStoreError(
                f"{self.path} 不是合法 JSON（{exc}）—— 拒绝把它当成空库，"
                f"否则下次写入会覆盖掉已有数据"
            ) from exc
        if not isinstance(data, dict):
            raise JsonStoreError(f"{self.path} 顶层应为对象，实际是 {type(data).__name__}")
        return data

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        """原子写：临时文件 → fsync → replace。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    # ---------------------------------------------------------------- 接口

    def all(self, table: str) -> list[dict[str, Any]]:
        """某张表的全部记录（按主键字符串排序，保证顺序稳定）。"""
        with self._lock:
            rows = self._read().get(str(table)) or {}
        if not isinstance(rows, dict):
            raise JsonStoreError(f"表 {table} 应为对象，实际是 {type(rows).__name__}")
        return [dict(rows[key]) for key in sorted(rows, key=_sort_key)]

    def get(self, table: str, key: Any) -> Optional[dict[str, Any]]:
        with self._lock:
            rows = self._read().get(str(table)) or {}
        record = rows.get(str(key))
        return dict(record) if isinstance(record, dict) else None

    def put(self, table: str, key: Any, record: dict[str, Any]) -> None:
        with self._lock:
            data = self._read()
            bucket = data.setdefault(str(table), {})
            if not isinstance(bucket, dict):
                raise JsonStoreError(f"表 {table} 应为对象")
            bucket[str(key)] = dict(record)
            self._write(data)

    def delete(self, table: str, key: Any) -> bool:
        with self._lock:
            data = self._read()
            bucket = data.get(str(table))
            if not isinstance(bucket, dict) or str(key) not in bucket:
                return False
            del bucket[str(key)]
            self._write(data)
            return True

    def update(self, table: str, key: Any, patch: dict[str, Any]) -> Optional[dict[str, Any]]:
        """局部更新；记录不存在时返回 None（不新建）。"""
        with self._lock:
            data = self._read()
            bucket = data.get(str(table))
            if not isinstance(bucket, dict):
                return None
            current = bucket.get(str(key))
            if not isinstance(current, dict):
                return None
            merged = {**current, **patch}
            bucket[str(key)] = merged
            self._write(data)
            return dict(merged)

    def count(self, table: str) -> int:
        with self._lock:
            rows = self._read().get(str(table)) or {}
        return len(rows) if isinstance(rows, dict) else 0


def _sort_key(value: str) -> tuple[int, Any]:
    """数字主键按数值排，其余按字符串 —— 与 SQLite 的 `ORDER BY id` 观感一致。"""
    text = str(value)
    return (0, int(text)) if text.isdigit() else (1, text)


__all__ = ["JsonStore", "JsonStoreError"]
