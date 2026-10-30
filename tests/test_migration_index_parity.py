"""迁移链建出来的库，必须和模型完全一致（索引也不例外）。

为什么单独立一条：模型与迁移链漂移过一次，而且漂得很危险 ——

- `ix_accounts_platform_created_at` / `ix_task_logs_platform_id` 是生产库由
  legacy runner 建的**承重索引**（列表页 `WHERE platform=? ORDER BY ...`），
  模型里没声明 → `alembic check` 认为它们多余，autogenerate 会生成 `drop_index`；
- 空库 `alembic upgrade head` 建出来的表又**没有**这两个索引 → 新部署一上来
  就是全表扫 + 排序，而测试（用 `create_all` 建表）永远发现不了。

两边都错、错的方向还不一样，所以既不能用「生产库能跑」证明，也不能用
「测试全绿」证明。这里直接从迁移链建一个空库，逐表比索引，再跑一次
`alembic check` —— 只有这条能同时钉住三方（模型 / 迁移 / 生产形态）一致。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 由 `data/platforms/<平台>.db` 各自持有的表 —— 主库不该有，`env.py` 里也
#: 明确跳过了它们的对比（见 `_PER_PLATFORM_TABLES`）。
PER_PLATFORM_TABLES = {"icloud_accounts", "icloud_aliases", "outlook_accounts"}


def _run_alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["ALEMBIC_DATABASE_URL"] = f"sqlite:///{db_path}"
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


class MigrationIndexParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = Path(ROOT) / ".pytest-migration-parity.db"
        if cls._tmp.exists():
            cls._tmp.unlink()
        result = _run_alembic(cls._tmp, "upgrade", "head")
        if result.returncode != 0:
            raise AssertionError(f"alembic upgrade head 失败:\n{result.stdout}\n{result.stderr}")

    @classmethod
    def tearDownClass(cls):
        if cls._tmp.exists():
            cls._tmp.unlink()

    def _db_indexes(self, table: str) -> set[str]:
        conn = sqlite3.connect(self._tmp)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
            ).fetchall()
        finally:
            conn.close()
        return {name for (name,) in rows if not name.startswith("sqlite_autoindex")}

    def test_accounts_indexes_match_the_model(self):
        from core.db.models_account import AccountModel

        expected = {index.name for index in AccountModel.__table__.indexes}
        actual = self._db_indexes("accounts")
        self.assertEqual(
            expected - actual, set(), "模型声明了但迁移没建的索引 —— 空库会缺索引"
        )
        self.assertEqual(
            actual - expected, set(), "迁移建了但模型没声明的索引 —— alembic check 会想删掉它"
        )

    def test_task_logs_indexes_match_the_model(self):
        from core.db.models_infra import TaskLog

        expected = {index.name for index in TaskLog.__table__.indexes}
        actual = self._db_indexes("task_logs")
        self.assertEqual(expected - actual, set())
        self.assertEqual(actual - expected, set())

    def test_load_bearing_composite_indexes_exist(self):
        """列表页的两条承重索引必须在（名字写死，防止被「优化」掉）。"""
        self.assertIn("ix_accounts_platform_created_at", self._db_indexes("accounts"))
        self.assertIn("ix_task_logs_platform_id", self._db_indexes("task_logs"))
        self.assertNotIn(
            "ix_task_logs_platform",
            self._db_indexes("task_logs"),
            "单列 platform 索引与复合索引前缀重复，不该再建回来",
        )

    def test_alembic_check_is_clean_on_a_fresh_database(self):
        result = _run_alembic(self._tmp, "check")
        self.assertEqual(
            result.returncode,
            0,
            f"空库上 alembic check 报漂移：\n{result.stdout}\n{result.stderr}",
        )
        self.assertIn("No new upgrade operations detected", result.stdout + result.stderr)

    def test_per_platform_tables_are_excluded_from_the_comparison(self):
        """平台表（icloud/outlook）不进主库的 autogenerate 对比。

        它们实际住在 `data/platforms/<平台>.db`，基线迁移仍会创建（单库部署
        与测试用得上）。如果 `env.py` 的 `include_object` 把过滤去掉了，
        真库上 `alembic check` 会立刻报「缺这三张表」—— 所以这里直接读源码
        常量，而不是 import env.py（那个模块只能在 alembic 上下文里执行）。
        """
        import re

        source = (ROOT / "migrations" / "env.py").read_text(encoding="utf-8")
        match = re.search(r"_PER_PLATFORM_TABLES = frozenset\(\{([^}]*)\}\)", source)
        self.assertIsNotNone(match, "env.py 里找不到 _PER_PLATFORM_TABLES")
        names = set(re.findall(r'"([^"]+)"', match.group(1)))
        self.assertEqual(names, PER_PLATFORM_TABLES)
        self.assertIn("include_object=include_object", source, "两个 configure 都要传过滤")


if __name__ == "__main__":
    unittest.main()


class LegacyProxyTagsUpgradeTests(unittest.TestCase):
    """老库升级路径：`init_db()` 不跑 Alembic，必须由手写迁移补 `proxies.tags`。

    这是「按文档升级」最常见的场景：`docs/MAINTENANCE.md` 说 `init_db()` 每次
    启动自动迁移，但 `proxies.tags` 是 **Alembic** 迁移加的，两条链没有交叉。
    不补的话，启动后第一次 `select(ProxyModel)` 就是
    `no such column: proxies.tags`，整个服务起不来（proxy 审计 P0-4）。
    """

    def test_missing_tags_column_is_added_idempotently(self):
        import sqlite3
        import tempfile

        from sqlalchemy import create_engine

        from core.db.migrations import _ensure_proxy_tags_column

        path = tempfile.mktemp(suffix=".db")
        try:
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE proxies (id INTEGER PRIMARY KEY, url TEXT, region TEXT, is_active BOOLEAN)"
            )
            conn.execute("INSERT INTO proxies (url) VALUES ('socks5://u:p@1.2.3.4:1080')")
            conn.commit()
            conn.close()

            engine = create_engine(f"sqlite:///{path}")
            _ensure_proxy_tags_column(engine)
            _ensure_proxy_tags_column(engine)  # 幂等：第二次是空操作

            conn = sqlite3.connect(path)
            try:
                columns = [row[1] for row in conn.execute("PRAGMA table_info('proxies')")]
                rows = conn.execute("SELECT count(*) FROM proxies").fetchone()[0]
            finally:
                conn.close()

            self.assertIn("tags", columns, "老库升级后仍然没有 proxies.tags —— 启动会炸")
            self.assertEqual(rows, 1, "补列过程中丢了数据")
        finally:
            import os

            if os.path.exists(path):
                os.unlink(path)
