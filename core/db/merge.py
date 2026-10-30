"""把分库的 `accounts` 合并进单一库（T1.3）。

## 背景

`PlatformDatabaseRegistry` 允许每个平台落一个独立 sqlite 文件
（`DATABASE_URL_GROK=…` / `PLATFORM_DATABASE_URLS={…}`）。分库当初是为了
「一个平台的数据坏了不牵连另一个」，代价是：

* 同一个账号在不同库里可能有不同状态，判重、统计、面板对比都要跨库拼；
* `accounts.id` 换成 UUID 之后跨库不再撞号，但**跨库查询**仍然要写一遍
  「两个库都扫」的逻辑（`repository.get_many` 里那段分块 IN 就是为此存在）。

这个模块做合并：把若干源库的 `accounts` 并进目标库。

## 冲突口径（H3，已定）

同一 `(platform, lower(email))` 在多个库里都有时，**保留 `updated_at` 最新的那份**。
`updated_at` 相同（或都为空）时保留目标库里已有的那份 —— 合并要幂等，
重复跑不能来回翻烙饼。

## 行数守恒

`source_total == inserted + updated + skipped_older + duplicate_in_source`

任何一条不成立就是数据丢了，函数直接抛 `MergeConservationError`，不写库
（先算后写：`dry_run` 与真实执行走同一套计算，只是最后不 commit）。

## 为什么不复用 SQLModel

合并脚本要能在**模型已经改过好几轮之后**仍然读得动老库。按 `PRAGMA table_info`
动态取列，少一列多一列都不会炸 —— 用 ORM 的话，模型加一个 NOT NULL 列就让
老库读不出来，而合并恰恰是老库唯一还在被读的时刻。
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

#: 判重键：平台 + 规范化邮箱（与 `uq_accounts_platform_email` 表达式索引同口径）。
_KEY_COLUMNS = ("platform", "email")


class MergeConservationError(RuntimeError):
    """行数不守恒 —— 合并过程丢了数据，必须中止。"""


@dataclass
class SourceStats:
    path: str
    total: int = 0
    inserted: int = 0
    updated: int = 0
    skipped_older: int = 0
    duplicate_in_source: int = 0
    id_reassigned: int = 0

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "total": self.total,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped_older": self.skipped_older,
            "duplicate_in_source": self.duplicate_in_source,
            "id_reassigned": self.id_reassigned,
        }


@dataclass
class MergeReport:
    target: str
    dry_run: bool
    sources: list[SourceStats] = field(default_factory=list)
    target_before: int = 0
    target_after: int = 0

    @property
    def inserted(self) -> int:
        return sum(s.inserted for s in self.sources)

    @property
    def updated(self) -> int:
        return sum(s.updated for s in self.sources)

    @property
    def skipped_older(self) -> int:
        return sum(s.skipped_older for s in self.sources)

    @property
    def source_total(self) -> int:
        return sum(s.total for s in self.sources)

    def as_dict(self) -> dict:
        return {
            "target": self.target,
            "dry_run": self.dry_run,
            "target_before": self.target_before,
            "target_after": self.target_after,
            "source_total": self.source_total,
            "inserted": self.inserted,
            "updated": self.updated,
            "skipped_older": self.skipped_older,
            "sources": [s.as_dict() for s in self.sources],
        }


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [row[1] for row in rows]


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "select name from sqlite_master where type='table' and name=?", (table,)
    ).fetchone()
    return row is not None


def _normalize_email(value: object) -> str:
    return str(value or "").strip().lower()


def _timestamp_key(value: object) -> float:
    """把 `updated_at` 归一成可比较的数字。解析不了按 0（最旧）算。"""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    # ISO（含 'Z' 与 '+00:00'）
    candidate = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate).timestamp()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _merge_row_fields(existing: dict, incoming: dict) -> dict:
    """同键合并：以 incoming 为底，**空值不覆盖**已有值。

    这条很重要：合并的常见形态是「A 库有密码没 RT，B 库有 RT 没密码」。
    整行替换会把另一边已有的字段抹成空串（实测过一次，代价是重新登录）。
    """
    merged = dict(existing)
    for key, value in incoming.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip() and str(existing.get(key) or "").strip():
            continue  # 空串不覆盖非空
        merged[key] = value
    return merged


def merge_accounts(
    target_path: Path | str,
    source_paths: Iterable[Path | str],
    *,
    dry_run: bool = False,
    table: str = "accounts",
) -> MergeReport:
    """把 `source_paths` 的 `accounts` 并进 `target_path`。

    `dry_run=True` 时跑完整套计算但不写库 —— 报告里的数字与真实执行一致。
    """
    target = Path(target_path)
    sources = [Path(p) for p in source_paths]
    report = MergeReport(target=str(target), dry_run=dry_run)

    if not target.exists():
        raise FileNotFoundError(f"目标库不存在: {target}")

    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    try:
        if not _has_table(conn, table):
            raise RuntimeError(f"目标库里没有 {table} 表: {target}")
        target_columns = _table_columns(conn, table)
        report.target_before = conn.execute(f"select count(*) from {table}").fetchone()[0]

        existing = {
            (_normalize_email(row["email"]), str(row["platform"] or "").strip().lower()): dict(row)
            for row in conn.execute(f"select * from {table}").fetchall()
        }
        taken_ids = {str(row["id"]) for row in existing.values()}

        for source in sources:
            stats = SourceStats(path=str(source))
            report.sources.append(stats)
            if not source.exists():
                continue
            src = sqlite3.connect(str(source))
            src.row_factory = sqlite3.Row
            try:
                if not _has_table(src, table):
                    continue
                source_columns = [c for c in _table_columns(src, table) if c in target_columns]
                rows = src.execute(f"select * from {table}").fetchall()
                stats.total = len(rows)
                seen_in_source: set[tuple[str, str]] = set()

                for raw in rows:
                    row = {c: raw[c] for c in source_columns}
                    key = (
                        _normalize_email(row.get("email")),
                        str(row.get("platform") or "").strip().lower(),
                    )
                    if key in seen_in_source:
                        # 同一个源库里就有重复（合并前没建唯一索引的老库）
                        stats.duplicate_in_source += 1
                        continue
                    seen_in_source.add(key)

                    current = existing.get(key)
                    if current is None:
                        row["id"] = _ensure_unique_id(row.get("id"), taken_ids, stats)
                        existing[key] = row
                        if not dry_run:
                            _insert(conn, table, row, target_columns)
                        stats.inserted += 1
                        continue

                    if _timestamp_key(row.get("updated_at")) > _timestamp_key(current.get("updated_at")):
                        merged = _merge_row_fields(current, row)
                        merged["id"] = current["id"]  # 主键不跟着源库走
                        existing[key] = merged
                        if not dry_run:
                            _update(conn, table, merged, target_columns)
                        stats.updated += 1
                    else:
                        stats.skipped_older += 1

                _check_conservation(stats)
            finally:
                src.close()

        if not dry_run:
            conn.commit()
        report.target_after = (
            report.target_before + report.inserted if dry_run
            else conn.execute(f"select count(*) from {table}").fetchone()[0]
        )
        return report
    finally:
        conn.close()


def _ensure_unique_id(row_id: object, taken: set[str], stats: SourceStats) -> str:
    value = str(row_id or "").strip()
    if value and value not in taken:
        taken.add(value)
        return value
    # 源库 id 为空或与目标库撞了：换一个新 UUID，旧值记到 legacy_id 之外不动
    fresh = str(uuid.uuid4())
    taken.add(fresh)
    stats.id_reassigned += 1
    return fresh


def _insert(conn: sqlite3.Connection, table: str, row: dict, columns: list[str]) -> None:
    cols = [c for c in columns if c in row]
    placeholders = ", ".join("?" for _ in cols)
    conn.execute(
        f"insert into {table} ({', '.join(cols)}) values ({placeholders})",
        [row[c] for c in cols],
    )


def _update(conn: sqlite3.Connection, table: str, row: dict, columns: list[str]) -> None:
    cols = [c for c in columns if c in row and c != "id"]
    assignments = ", ".join(f"{c}=?" for c in cols)
    conn.execute(
        f"update {table} set {assignments} where id=?",
        [row[c] for c in cols] + [row["id"]],
    )


def _check_conservation(stats: SourceStats) -> None:
    accounted = (
        stats.inserted + stats.updated + stats.skipped_older + stats.duplicate_in_source
    )
    if accounted != stats.total:
        raise MergeConservationError(
            f"{stats.path}: 源库 {stats.total} 行，只交代了 {accounted} 行"
            f"（新增 {stats.inserted} / 更新 {stats.updated} /"
            f" 跳过 {stats.skipped_older} / 源内重复 {stats.duplicate_in_source}）"
        )


__all__ = [
    "MergeConservationError",
    "MergeReport",
    "SourceStats",
    "merge_accounts",
]
