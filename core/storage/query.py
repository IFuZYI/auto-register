"""查询层：一份谓词定义，两种后端执行。

## 为什么需要它

邮箱池（iCloud / outlook）的查询含 `OR` 状态条件、`func.lower(email)` 归一、
账号类型匹配。JSON 没有 SQL —— 直接移植就得**在 Python 里重写一遍同样的
规则**，两份定义必然漂移（本仓已因同类问题踩过 `_export_names` 漏 sso、
列映射重复等）。

所以把「过滤什么」抽成**中立形式**，两个执行器共用一个定义：

    Clause  ──matches()──→  Python 谓词（JSON 后端）
            └─to_sql()───→  SQLModel 表达式（SQLite 后端）

`tests/test_query_layer.py` 的核心断言是**两个执行器对同一批数据给出同样
结果** —— 只测各自的话，漂移照样发生。

## 支持的算子

只做邮箱池实际用到的那些，不预先造轮子：

| 算子 | SQL 对应 | 用途 |
| --- | --- | --- |
| `eq(field, value)` | `= / IS NULL` | 状态、开关 |
| `in_(field, values)` | `IN (...)` | 状态集合 |
| `not_in_lower(field, values)` | `lower(f) NOT IN (...)` | 已注册邮箱排除 |
| `any_of(*clauses)` | `OR (...)` | 「可用或未入池」 |

多 clause 之间是 **AND**（与 `.where().where()` 一致）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class Clause:
    """一个过滤条件的中立形式。"""

    op: str
    field: str = ""
    value: Any = None
    values: tuple = ()
    clauses: tuple = ()

    # ------------------------------------------------------------ JSON 执行

    def matches(self, record: dict) -> bool:
        """Python 谓词（JSON 后端用）。"""
        if self.op == "or":
            return any(c.matches(record) for c in self.clauses)

        raw = record.get(self.field)

        if self.op == "eq":
            if self.value is None:
                # SQL 里 `= NULL` 永不成立，写 `== None` 的人想要的是 IS NULL；
                # 缺失键在 SQL 里也是 NULL，所以这里一并算命中。
                return raw is None
            if isinstance(self.value, bool):
                return bool(raw) is self.value
            return raw == self.value

        if self.op == "in":
            wanted = {str(v) for v in self.values}
            return str(raw if raw is not None else "") in wanted

        if self.op == "not_in_lower":
            lowered = {str(v).strip().lower() for v in self.values}
            return str(raw or "").strip().lower() not in lowered

        raise ValueError(f"未知算子: {self.op}")


def eq(field: str, value: Any) -> Clause:
    return Clause(op="eq", field=str(field), value=value)


def in_(field: str, values: Iterable[Any]) -> Clause:
    return Clause(op="in", field=str(field), values=tuple(values))


def not_in_lower(field: str, values: Iterable[Any]) -> Clause:
    return Clause(op="not_in_lower", field=str(field), values=tuple(values))


def any_of(*clauses: Clause) -> Clause:
    return Clause(op="or", clauses=tuple(clauses))


def matches_all(clauses: Iterable[Clause], record: dict) -> bool:
    """多 clause 之间是 AND。"""
    return all(c.matches(record) for c in clauses)


# ---------------------------------------------------------------- SQL 执行


def to_sql(model: Any, clause: Clause):
    """把中立 clause 翻成 SQLModel 表达式（SQLite 后端用）。

    与 `Clause.matches` **必须给出同样结果** —— 两边不一致时
    `tests/test_query_layer.py` 的对比断言会红。
    """
    from sqlalchemy import func, or_

    if clause.op == "or":
        return or_(*[to_sql(model, c) for c in clause.clauses])

    column = getattr(model, clause.field)

    if clause.op == "eq":
        if clause.value is None:
            # 用 `is_(None)` 而不是 `== None`：两者在 SQLAlchemy 下**生成的
            # SQL 相同**（都翻成 `IS NULL`，实测编译输出一致），所以这是
            # 等价写法而非行为差异 —— 取更明确的那个。裸 SQL 里 `= NULL`
            # 恒假，写错不会有测试红，靠这里的显式写法避免。
            return column.is_(None)
        if isinstance(clause.value, bool):
            return column.is_(clause.value)
        return column == clause.value

    if clause.op == "in":
        return column.in_([str(v) for v in clause.values])

    if clause.op == "not_in_lower":
        return func.lower(column).notin_(
            [str(v).strip().lower() for v in clause.values]
        )

    raise ValueError(f"未知算子: {clause.op}")


__all__ = [
    "Clause",
    "any_of",
    "eq",
    "in_",
    "matches_all",
    "not_in_lower",
    "to_sql",
]
