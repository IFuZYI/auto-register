"""分库合并（T1.3）的行为契约。

合并是**不可逆的数据操作**，所以这里盯的不是「跑通」，而是三件事：

1. **行数守恒** —— 源库每一行都必须有交代（新增 / 更新 / 较旧跳过 / 源内重复）；
2. **冲突口径** —— 同键保留 `updated_at` 最新的（H3 已定），且空值不覆盖非空；
3. **幂等** —— 同一批源库跑两遍，第二遍必须什么都不改（不能来回翻烙饼）。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from core.db.merge import MergeConservationError, merge_accounts

SCHEMA = """
CREATE TABLE accounts (
    id VARCHAR NOT NULL PRIMARY KEY,
    legacy_id INTEGER,
    platform VARCHAR NOT NULL,
    email VARCHAR NOT NULL,
    password VARCHAR NOT NULL,
    user_id VARCHAR NOT NULL,
    region VARCHAR NOT NULL,
    token VARCHAR NOT NULL,
    status VARCHAR NOT NULL,
    cashier_url VARCHAR NOT NULL,
    extra_json VARCHAR NOT NULL,
    created_at VARCHAR NOT NULL,
    updated_at VARCHAR NOT NULL
)
"""


def _make_db(path: Path, rows: list[dict]) -> Path:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(SCHEMA)
        for row in rows:
            payload = {
                "id": row.get("id", f"id-{row['email']}-{row['platform']}"),
                "legacy_id": row.get("legacy_id"),
                "platform": row["platform"],
                "email": row["email"],
                "password": row.get("password", ""),
                "user_id": row.get("user_id", ""),
                "region": row.get("region", ""),
                "token": row.get("token", ""),
                "status": row.get("status", "registered"),
                "cashier_url": row.get("cashier_url", ""),
                "extra_json": row.get("extra_json", "{}"),
                "created_at": row.get("created_at", "2026-01-01T00:00:00+00:00"),
                "updated_at": row.get("updated_at", "2026-01-01T00:00:00+00:00"),
            }
            cols = ", ".join(payload)
            marks = ", ".join("?" for _ in payload)
            conn.execute(
                f"insert into accounts ({cols}) values ({marks})", list(payload.values())
            )
        conn.commit()
    finally:
        conn.close()
    return path


def _rows(path: Path) -> list[dict]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute("select * from accounts").fetchall()]
    finally:
        conn.close()


class MergeBasicsTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_inserts_into_empty_target(self):
        target = _make_db(self.dir / "target.db", [])
        source = _make_db(
            self.dir / "src.db",
            [
                {"platform": "chatgpt", "email": "a@x.ai"},
                {"platform": "grok", "email": "b@x.ai"},
            ],
        )
        report = merge_accounts(target, [source])
        self.assertEqual(report.inserted, 2)
        self.assertEqual(len(_rows(target)), 2)
        self.assertEqual(report.source_total, 2)

    def test_dry_run_does_not_write(self):
        target = _make_db(self.dir / "target.db", [])
        source = _make_db(self.dir / "src.db", [{"platform": "chatgpt", "email": "a@x.ai"}])

        report = merge_accounts(target, [source], dry_run=True)

        self.assertEqual(report.inserted, 1, "dry-run 也要算出真实数字")
        self.assertEqual(len(_rows(target)), 0, "dry-run 不许写库")
        self.assertEqual(report.target_after, report.target_before + 1)


class ConflictResolutionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_newer_source_wins(self):
        target = _make_db(
            self.dir / "target.db",
            [
                {
                    "platform": "chatgpt",
                    "email": "a@x.ai",
                    "status": "expired",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        )
        source = _make_db(
            self.dir / "src.db",
            [
                {
                    "platform": "chatgpt",
                    "email": "A@X.AI",
                    "status": "registered",
                    "updated_at": "2026-02-01T00:00:00+00:00",
                }
            ],
        )

        report = merge_accounts(target, [source])

        rows = _rows(target)
        self.assertEqual(len(rows), 1, "大小写不同的同一地址必须并成一行")
        self.assertEqual(rows[0]["status"], "registered", "较新的一份应当胜出")
        self.assertEqual(report.updated, 1)

    def test_older_source_is_skipped(self):
        target = _make_db(
            self.dir / "target.db",
            [
                {
                    "platform": "chatgpt",
                    "email": "a@x.ai",
                    "status": "registered",
                    "updated_at": "2026-03-01T00:00:00+00:00",
                }
            ],
        )
        source = _make_db(
            self.dir / "src.db",
            [
                {
                    "platform": "chatgpt",
                    "email": "a@x.ai",
                    "status": "banned",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        )

        report = merge_accounts(target, [source])

        self.assertEqual(_rows(target)[0]["status"], "registered")
        self.assertEqual(report.skipped_older, 1)
        self.assertEqual(report.updated, 0)

    def test_empty_fields_do_not_overwrite_non_empty(self):
        """A 库有密码没 RT，B 库有 RT 没密码 —— 合并后两样都要在。"""
        target = _make_db(
            self.dir / "target.db",
            [
                {
                    "platform": "chatgpt",
                    "email": "a@x.ai",
                    "password": "Pw-1",
                    "token": "",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        )
        source = _make_db(
            self.dir / "src.db",
            [
                {
                    "platform": "chatgpt",
                    "email": "a@x.ai",
                    "password": "",
                    "token": "AT-1",
                    "updated_at": "2026-02-01T00:00:00+00:00",
                }
            ],
        )

        merge_accounts(target, [source])

        row = _rows(target)[0]
        self.assertEqual(row["password"], "Pw-1", "空串不得覆盖已有密码")
        self.assertEqual(row["token"], "AT-1", "新值要写进去")

    def test_duplicate_inside_one_source_is_counted(self):
        target = _make_db(self.dir / "target.db", [])
        source = _make_db(
            self.dir / "src.db",
            [
                {"platform": "chatgpt", "email": "a@x.ai", "id": "id-1"},
                {"platform": "chatgpt", "email": "A@X.ai", "id": "id-2"},
            ],
        )

        report = merge_accounts(target, [source])

        self.assertEqual(report.sources[0].duplicate_in_source, 1)
        self.assertEqual(len(_rows(target)), 1)
        # 守恒：2 行 = 新增 1 + 源内重复 1
        self.assertEqual(report.source_total, 2)


class IdempotencyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_second_run_changes_nothing(self):
        target = _make_db(self.dir / "target.db", [])
        source = _make_db(
            self.dir / "src.db",
            [
                {"platform": "chatgpt", "email": "a@x.ai", "status": "registered"},
                {"platform": "grok", "email": "b@x.ai", "status": "expired"},
            ],
        )

        first = merge_accounts(target, [source])
        snapshot = _rows(target)
        second = merge_accounts(target, [source])

        self.assertEqual(first.inserted, 2)
        self.assertEqual(second.inserted, 0, "第二遍不该再新增")
        self.assertEqual(second.updated, 0, "第二遍不该再更新")
        self.assertEqual(second.skipped_older, 2)
        self.assertEqual(_rows(target), snapshot, "数据必须一模一样")

    def test_id_collision_gets_a_fresh_uuid(self):
        target = _make_db(
            self.dir / "target.db",
            [{"platform": "chatgpt", "email": "a@x.ai", "id": "same-id"}],
        )
        source = _make_db(
            self.dir / "src.db",
            [{"platform": "grok", "email": "b@x.ai", "id": "same-id"}],
        )

        report = merge_accounts(target, [source])

        ids = {row["id"] for row in _rows(target)}
        self.assertEqual(len(ids), 2, "两行不能共用主键")
        self.assertEqual(report.sources[0].id_reassigned, 1)


if __name__ == "__main__":
    unittest.main()
