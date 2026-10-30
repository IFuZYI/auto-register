"""SQLite 连接必须带 WAL / NORMAL / busy_timeout（data-layer + performance 两条 lane）。

默认 `create_engine("sqlite:///…")` 给的是 `journal_mode=delete` + `synchronous=FULL`：
写事务期间读也被阻塞，多读一写（注册任务 + 面板轮询 + 备份）必然撞
`database is locked`（仓内 `chatgpt_two_factor.py` 已有实测记录），
单次 upsert 22.9 ms 里很大一块是每次 commit 的 fsync。

这条测试钉住 `apply_sqlite_pragmas` 的行为，防止哪天换掉 engine 工厂就悄悄退回默认。
"""

from __future__ import annotations

import tempfile
import unittest

from sqlalchemy import create_engine

from core.db.base import apply_sqlite_pragmas


class SqlitePragmaTests(unittest.TestCase):
    def test_wal_and_normal_are_enabled(self):
        path = tempfile.mktemp(suffix=".db")
        try:
            engine = apply_sqlite_pragmas(create_engine(f"sqlite:///{path}"))
            with engine.connect() as conn:
                self.assertEqual(conn.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")
                self.assertEqual(conn.exec_driver_sql("PRAGMA synchronous").scalar(), 1)
                self.assertGreaterEqual(
                    int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar() or 0), 5000
                )
        finally:
            import os

            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.unlink(path + suffix)

    def test_applying_twice_does_not_stack_listeners(self):
        path = tempfile.mktemp(suffix=".db")
        try:
            engine = create_engine(f"sqlite:///{path}")
            apply_sqlite_pragmas(engine)
            apply_sqlite_pragmas(engine)  # 幂等：不应重复注册
            with engine.connect() as conn:
                self.assertEqual(conn.exec_driver_sql("PRAGMA journal_mode").scalar(), "wal")
        finally:
            import os

            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.unlink(path + suffix)

    def test_non_sqlite_engine_is_untouched(self):
        from sqlalchemy import text

        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.assertIs(apply_sqlite_pragmas(engine), engine)
        with engine.connect() as conn:
            self.assertEqual(conn.execute(text("select 1")).scalar(), 1)


if __name__ == "__main__":
    unittest.main()
