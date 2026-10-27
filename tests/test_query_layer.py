"""查询层：一份谓词定义，两种后端执行。

## 为什么需要它

邮箱池（iCloud / outlook）的查询含 `OR` 状态条件、`func.lower(email)`
归一、账号类型匹配。JSON 没有 SQL —— 直接移植就得**在 Python 里重写一遍
同样的规则**，两份定义必然漂移（本仓已因同类问题踩过 `_export_names`
漏 sso）。

所以把「过滤什么」抽成**中立形式**（`Clause`），两个执行器：

    Clause  ──to_predicate──→  Python 谓词（JSON 后端）
            └─to_sql────────→  SQLModel 表达式（SQLite 后端）

**这条测试的核心断言是「两个执行器对同一批数据给出同样结果」** ——
只测各自的话，漂移照样发生。
"""

from __future__ import annotations

import unittest


class ClausePredicateTests(unittest.TestCase):
    def test_eq(self):
        from core.storage.query import eq

        self.assertTrue(eq("status", "available").matches({"status": "available"}))
        self.assertFalse(eq("status", "available").matches({"status": "used"}))

    def test_eq_none_matches_none_and_missing(self):
        """`== None` 在 SQL 里是 `IS NULL`；缺失键也算 NULL。"""
        from core.storage.query import eq

        self.assertTrue(eq("status", None).matches({"status": None}))
        self.assertTrue(eq("status", None).matches({}))

    def test_eq_empty_string_is_not_none(self):
        """空串与 NULL 是两回事（SQL 里也是）。"""
        from core.storage.query import eq

        self.assertTrue(eq("status", "").matches({"status": ""}))
        self.assertFalse(eq("status", "").matches({"status": None}))

    def test_eq_true(self):
        from core.storage.query import eq

        self.assertTrue(eq("enabled", True).matches({"enabled": True}))
        self.assertFalse(eq("enabled", True).matches({"enabled": False}))

    def test_in_(self):
        from core.storage.query import in_

        clause = in_("status", ["available", "used"])
        self.assertTrue(clause.matches({"status": "available"}))
        self.assertTrue(clause.matches({"status": "used"}))
        self.assertFalse(clause.matches({"status": "failed"}))

    def test_not_in_lower(self):
        """`func.lower(email) NOT IN (...)` —— 邮箱大小写无关。"""
        from core.storage.query import not_in_lower

        clause = not_in_lower("email", ["a@b.c"])
        self.assertFalse(clause.matches({"email": "A@B.C"}))
        self.assertTrue(clause.matches({"email": "x@y.z"}))

    def test_or(self):
        from core.storage.query import any_of, eq

        clause = any_of(eq("status", "available"), eq("status", None))
        self.assertTrue(clause.matches({"status": "available"}))
        self.assertTrue(clause.matches({"status": None}))
        self.assertFalse(clause.matches({"status": "failed"}))

    def test_and_implicit_within_a_clause(self):
        """多个 clause 之间是 AND（与 `.where().where()` 一致）。"""
        from core.storage.query import eq, matches_all

        row = {"status": "available", "enabled": True}
        self.assertTrue(matches_all([eq("status", "available"), eq("enabled", True)], row))
        self.assertFalse(matches_all([eq("status", "used"), eq("enabled", True)], row))


class SqlExecutorAgreesWithPredicateTests(unittest.TestCase):
    """**两个执行器必须给出同样结果** —— 这是这一层存在的唯一理由。"""

    def setUp(self):
        import tempfile

        from sqlalchemy import create_engine
        from sqlmodel import Session, SQLModel

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._engine = create_engine(f"sqlite:///{self._tmp.name}/q.db")
        SQLModel.metadata.create_all(self._engine)

    def _sql_matches(self, clause, rows):
        from sqlmodel import Session, select

        from core.storage.query import to_sql
        from core.db.models_platform import ICloudAliasModel

        with Session(self._engine) as s:
            for i, row in enumerate(rows):
                s.add(ICloudAliasModel(
                    id=i + 1, account_id=1,
                    address=row.get("address") or f"a{i}@x.c",
                    status=row.get("status") or "active",
                    pool_status=row.get("pool_status") or "available",
                    used_platforms=row.get("used_platforms") or "",
                    share_token=f"tok{i}",
                ))
            s.commit()

        with Session(self._engine) as s:
            found = s.exec(
                select(ICloudAliasModel).where(to_sql(ICloudAliasModel, clause))
            ).all()
            return {r.address for r in found}

    def test_eq_agrees(self):
        from core.storage.query import eq

        rows = [
            {"address": "a@x.c", "pool_status": "available"},
            {"address": "b@x.c", "pool_status": "used"},
        ]
        clause = eq("pool_status", "available")
        expected = {r["address"] for r in rows if clause.matches(r)}
        self.assertEqual(self._sql_matches(clause, rows), expected)

    def test_in_agrees(self):
        from core.storage.query import in_

        rows = [
            {"address": "a@x.c", "pool_status": "available"},
            {"address": "b@x.c", "pool_status": "used"},
            {"address": "c@x.c", "pool_status": "unpooled"},
        ]
        clause = in_("pool_status", ["available", "used"])
        expected = {r["address"] for r in rows if clause.matches(r)}
        self.assertEqual(self._sql_matches(clause, rows), expected)

    def test_eq_none_agrees(self):
        """`eq(field, None)` 在两侧都必须命中「值为 NULL」的行。

        SQL 里写 `= NULL` 恒假，必须用 `IS NULL` —— 这条用例专门盯住它
        （第一版的 SQL 执行器只测了普通等值，变异掉 `is_(None)` 时测试仍绿）。
        """
        from core.storage.query import eq

        rows = [
            {"address": "a@x.c", "pool_status": "available"},
            {"address": "b@x.c", "pool_status": ""},   # 空串不是 NULL
        ]
        clause = eq("pool_status", None)
        expected = {r["address"] for r in rows if clause.matches(r)}
        self.assertEqual(self._sql_matches(clause, rows), expected)
        self.assertEqual(expected, set(), "空白样本里没有 NULL 行，用例失去意义")

    def test_not_in_lower_agrees(self):
        """`lower(email) NOT IN (...)` 必须大小写无关。

        第一版没测这个算子，变异掉 `func.lower` 时测试仍绿。
        """
        from core.storage.query import not_in_lower

        rows = [
            {"address": "A@X.C", "pool_status": "available"},
            {"address": "b@x.c", "pool_status": "available"},
        ]
        clause = not_in_lower("address", ["a@x.c"])
        expected = {r["address"] for r in rows if clause.matches(r)}
        self.assertEqual(self._sql_matches(clause, rows), expected)
        self.assertEqual(expected, {"b@x.c"}, "大小写归一没生效")

    def test_or_agrees(self):
        from core.storage.query import any_of, eq

        rows = [
            {"address": "a@x.c", "pool_status": "available"},
            {"address": "b@x.c", "pool_status": ""},
            {"address": "c@x.c", "pool_status": "failed"},
        ]
        clause = any_of(eq("pool_status", "available"), eq("pool_status", ""))
        expected = {r["address"] for r in rows if clause.matches(r)}
        self.assertEqual(self._sql_matches(clause, rows), expected)


if __name__ == "__main__":
    unittest.main()
