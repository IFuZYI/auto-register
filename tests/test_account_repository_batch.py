"""账号仓储批量读写：消除 N+1。

背景：`get()` / `delete()` 是「每个 id 一个新会话、逐库试」。调用方在循环里
调它们就成了 N+1 —— 批量导出 100 个账号会开最多 200 次会话。这里验证
`get_many` / `delete_many` 的正确性，重点是与逐个调用**语义完全等价**。
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, select

from core.db import AccountModel, init_db, platform_database_registry
from core.db.base import current_engine
from core.db.repository import account_repository


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """每个测试用独立的默认库 + 平台库（照搬 test_db_modular 的既有 fixture）。

    必须用显式 `configure()` 而不是 `reload_from_env()`：后者在测试进程里
    受其它测试的 env 状态影响，配不上时会**透明回落默认库**，于是所有账号
    都进默认库，跨库断言就失去意义了。
    """
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    default_url = f"sqlite:///{tmp_path / 'default.db'}"
    grok_url = f"sqlite:///{tmp_path / 'grok.db'}"
    chatgpt_url = f"sqlite:///{tmp_path / 'chatgpt.db'}"

    default_engine = create_engine(default_url)
    SQLModel.metadata.create_all(default_engine)
    from core.db.models_account import ACCOUNT_TABLES

    SQLModel.metadata.create_all(create_engine(grok_url), tables=ACCOUNT_TABLES)
    SQLModel.metadata.create_all(create_engine(chatgpt_url), tables=ACCOUNT_TABLES)

    # 默认库也要隔离：current_engine() 优先读 core.db.engine
    import core.db as _db

    monkeypatch.setattr(_db, "engine", default_engine)

    platform_database_registry.configure(
        {"grok": grok_url, "chatgpt": chatgpt_url}
    )
    yield
    platform_database_registry.dispose_all()
    platform_database_registry.configure({})


def _add(platform: str, email: str) -> int:
    row = account_repository.upsert(
        AccountModel(platform=platform, email=email, password="pw")
    )
    return int(row.id)


class TestGetMany:
    def test_returns_in_requested_order(self, isolated_db):
        first = _add("grok", "a@x.ai")
        second = _add("grok", "b@x.ai")
        third = _add("grok", "c@x.ai")

        rows = account_repository.get_many([third, first, second])
        assert [r.id for r in rows] == [third, first, second]

    def test_spans_platform_and_default_db(self, isolated_db):
        """跨库取：平台库与默认库的账号都要拿到。"""
        grok_id = _add("grok", "g@x.ai")
        default_id = _add("chatgpt", "c@x.ai")

        rows = account_repository.get_many([grok_id, default_id])
        assert {r.id for r in rows} == {grok_id, default_id}

    def test_skips_missing_ids(self, isolated_db):
        present = _add("grok", "a@x.ai")
        rows = account_repository.get_many([present, 99999])
        assert [r.id for r in rows] == [present]

    def test_deduplicates_repeated_ids(self, isolated_db):
        row_id = _add("grok", "a@x.ai")
        rows = account_repository.get_many([row_id, row_id, row_id])
        assert len(rows) == 1

    def test_empty_input(self, isolated_db):
        assert account_repository.get_many([]) == []

    def test_matches_individual_get(self, isolated_db):
        """批量结果必须与逐个 get 完全一致（含字段值）。"""
        ids = [_add("grok", f"u{i}@x.ai") for i in range(5)]
        ids.append(99999)

        bulk = account_repository.get_many(ids)
        one_by_one = [r for r in (account_repository.get(i) for i in ids) if r is not None]

        assert [r.id for r in bulk] == [r.id for r in one_by_one]
        assert [(r.email, r.platform, r.password) for r in bulk] == [
            (r.email, r.platform, r.password) for r in one_by_one
        ]

    def test_scoped_to_platform_when_given(self, isolated_db):
        grok_id = _add("grok", "g@x.ai")
        chatgpt_id = _add("chatgpt", "c@x.ai")

        rows = account_repository.get_many([grok_id, chatgpt_id], platform="grok")
        assert [r.id for r in rows] == [grok_id]

    def test_accepts_explicit_session(self, isolated_db):
        """显式 session 时按 id 直查（会话由调用方选定，不做跨库探测）。

        注意：显式 session 里 ID 是**该会话所属库**的 ID —— 账号在哪张表就得用
        哪个会话查，这是 ID 每库自增的必然结果。
        """
        grok_id = _add("grok", "a@x.ai")       # 进 grok 平台库
        other_id = _add("other", "o@x.ai")     # 未配置分库 → 进默认库

        with platform_database_registry.session_for("grok") as session:
            rows = account_repository.get_many([grok_id], session=session)
        assert [r.id for r in rows] == [grok_id]

        with Session(current_engine()) as session:
            rows = account_repository.get_many([other_id], session=session)
        assert [r.id for r in rows] == [other_id]


class TestDeleteMany:
    def test_deletes_across_databases(self, isolated_db):
        grok_id = _add("grok", "g@x.ai")
        default_id = _add("chatgpt", "c@x.ai")

        deleted, not_found = account_repository.delete_many([grok_id, default_id])
        assert set(deleted) == {grok_id, default_id}
        assert not_found == []

        with Session(current_engine()) as session:
            assert session.exec(select(AccountModel)).all() == []

    def test_reports_missing_ids(self, isolated_db):
        present = _add("grok", "a@x.ai")
        deleted, not_found = account_repository.delete_many([present, 4242])
        assert deleted == [present]
        assert not_found == [4242]

    def test_preserves_requested_order(self, isolated_db):
        ids = [_add("grok", f"u{i}@x.ai") for i in range(4)]
        deleted, _ = account_repository.delete_many([ids[3], ids[1], ids[0], ids[2]])
        assert deleted == [ids[3], ids[1], ids[0], ids[2]]

    def test_deduplicates_input(self, isolated_db):
        row_id = _add("grok", "a@x.ai")
        deleted, not_found = account_repository.delete_many([row_id, row_id])
        assert deleted == [row_id]
        assert not_found == []

    def test_empty_input(self, isolated_db):
        assert account_repository.delete_many([]) == ([], [])

    def test_matches_individual_delete(self, isolated_db):
        """批量删除的 deleted/not_found 口径必须与逐个 delete 一致。"""
        ids = [_add("grok", f"u{i}@x.ai") for i in range(4)]
        mixed = [ids[0], 99999, ids[2], 88888]

        deleted, not_found = account_repository.delete_many(mixed)
        assert deleted == [ids[0], ids[2]]
        assert not_found == [99999, 88888]

        # 逐个 delete 的口径：存在的删掉返回 True，不存在返回 False
        remaining = account_repository.get_many(ids)
        assert [r.id for r in remaining] == [ids[1], ids[3]]
        assert account_repository.delete(ids[1]) is True
        assert account_repository.delete(99999) is False

    def test_scoped_to_platform_when_given(self, isolated_db):
        """指定 platform 时不碰别的平台——即便 ID 撞号。

        ID 是每库自增的：grok 库里的 1 与默认库里 chatgpt 的 1 是两回事。
        """
        grok_id = _add("grok", "g@x.ai")
        chatgpt_id = _add("chatgpt", "c@x.ai")
        assert grok_id == chatgpt_id, "本测试前提：两库 ID 撞号"

        deleted, not_found = account_repository.delete_many(
            [grok_id], platform="grok"
        )
        assert deleted == [grok_id]
        assert not_found == []

        # grok 的账号没了，chatgpt 的同 id 账号必须完好
        assert account_repository.get(chatgpt_id, platform="chatgpt") is not None


class TestIdCollisionAcrossDatabases:
    """ID 每库自增 → 同一数字在不同库里指向不同账号。

    这是真实踩过的坑：指定 platform 时若不额外过滤 platform，默认库那一步
    会命中另一个平台的同 id 行 —— 读会读到别人的账号，删会**删掉别人的账号**。
    """

    def test_get_with_platform_does_not_cross_platforms(self, isolated_db):
        grok_id = _add("grok", "grok-a@x.ai")
        chatgpt_id = _add("chatgpt", "chatgpt-b@x.ai")
        assert grok_id == chatgpt_id, "本测试前提：两库 ID 撞号"

        # 指定 chatgpt：不能因为 grok 库先被扫到就把 grok 的行交出来
        row = account_repository.get(chatgpt_id, platform="chatgpt")
        assert row is not None
        assert row.platform == "chatgpt"
        assert row.email == "chatgpt-b@x.ai"

    def test_delete_with_platform_does_not_delete_other_platform(self, isolated_db):
        """核心回归：指定 platform 删除不得波及别的平台（数据丢失不可逆）。"""
        grok_id = _add("grok", "grok-a@x.ai")
        chatgpt_id = _add("chatgpt", "chatgpt-b@x.ai")
        assert grok_id == chatgpt_id

        assert account_repository.delete(chatgpt_id, platform="chatgpt") is True

        # grok 的账号必须还在
        survivor = account_repository.get(grok_id, platform="grok")
        assert survivor is not None, "grok 账号被误删了（ID 撞号）"
        assert survivor.email == "grok-a@x.ai"

    def test_get_many_with_platform_does_not_cross_platforms(self, isolated_db):
        grok_id = _add("grok", "grok-a@x.ai")
        chatgpt_id = _add("chatgpt", "chatgpt-b@x.ai")
        assert grok_id == chatgpt_id

        rows = account_repository.get_many([chatgpt_id], platform="chatgpt")
        assert len(rows) == 1
        assert rows[0].platform == "chatgpt"

    def test_delete_many_with_platform_does_not_cross_platforms(self, isolated_db):
        grok_id = _add("grok", "grok-a@x.ai")
        chatgpt_id = _add("chatgpt", "chatgpt-b@x.ai")
        assert grok_id == chatgpt_id

        account_repository.delete_many([chatgpt_id], platform="chatgpt")
        assert account_repository.get(grok_id, platform="grok") is not None


class TestNoNPlusOne:
    def test_get_many_opens_one_session_per_database(self, isolated_db, monkeypatch):
        """核心断言：会话数不随 id 数量增长。"""
        ids = [_add("grok", f"u{i}@x.ai") for i in range(20)]
        ids.append(_add("chatgpt", "c@x.ai"))

        opened = 0
        original = platform_database_registry.session_for

        def counting_session_for(platform, *args, **kwargs):
            nonlocal opened
            opened += 1
            return original(platform, *args, **kwargs)

        monkeypatch.setattr(
            platform_database_registry, "session_for", counting_session_for
        )
        account_repository.get_many(ids)

        # 2 个库（grok 平台库 + 默认库）→ 至多 2 次会话，而不是 21 次
        assert opened <= 2, f"打开了 {opened} 次会话，疑似 N+1"

    def test_delete_many_opens_one_session_per_database(self, isolated_db, monkeypatch):
        ids = [_add("grok", f"u{i}@x.ai") for i in range(20)]
        ids.append(_add("chatgpt", "c@x.ai"))

        opened = 0
        original = platform_database_registry.session_for

        def counting_session_for(platform, *args, **kwargs):
            nonlocal opened
            opened += 1
            return original(platform, *args, **kwargs)

        monkeypatch.setattr(
            platform_database_registry, "session_for", counting_session_for
        )
        account_repository.delete_many(ids)

        assert opened <= 2, f"打开了 {opened} 次会话，疑似 N+1"


class TestSqlVariableLimit:
    """SQLite 变量上限 32766：无界 IN 查询在超大 id 列表上会炸。

    实测（修复前）：`export-text` 传 40000 个 id → `sqlite3.OperationalError:
    too many SQL variables`（500）。删除接口有 1000 上限，但导出/回填接口
    没有 —— 直接把上限压到仓储层：分块查询，调用方多大都不炸。
    """

    def test_get_many_handles_ids_beyond_sqlite_variable_limit(self, isolated_db):
        # 33000 个 id：超过 32766 上限（其中只有 1 个真实存在）
        existing = _add("grok", "only-one@x.ai")
        ids = [existing] + list(range(existing + 1, existing + 33001))

        rows = account_repository.get_many(ids)
        assert [r.id for r in rows] == [existing]

    def test_delete_many_handles_ids_beyond_sqlite_variable_limit(self, isolated_db):
        existing = _add("grok", "only-one@x.ai")
        ids = [existing] + list(range(existing + 1, existing + 33001))

        deleted, not_found = account_repository.delete_many(ids)
        assert deleted == [existing]
        assert len(not_found) == 33000
