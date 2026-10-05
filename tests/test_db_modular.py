"""数据库模块化：分库、邮箱唯一键、判重。

覆盖用户需求：
  1. 不同平台可以有不同数据库
  2. 以邮箱作为唯一账号 key
  3. 已注册的不再重复注册
"""
from __future__ import annotations

import os
import tempfile

import pytest
from sqlmodel import Session, select

from core.paths import DEFAULT_PLATFORM_DB_FILES
from core.db import (
    AccountModel,
    account_repository,
    normalize_email,
    parse_platform_database_urls,
    platform_database_registry,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """每个测试用独立的默认库 + 平台库。"""
    default_url = f"sqlite:///{tmp_path / 'default.db'}"
    grok_url = f"sqlite:///{tmp_path / 'grok.db'}"
    chatgpt_url = f"sqlite:///{tmp_path / 'chatgpt.db'}"

    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    # 建库：默认库放全部表；平台库只放账号表
    default_engine = create_engine(default_url)
    SQLModel.metadata.create_all(default_engine)
    from core.db.models_account import ACCOUNT_TABLES

    SQLModel.metadata.create_all(create_engine(grok_url), tables=ACCOUNT_TABLES)
    SQLModel.metadata.create_all(create_engine(chatgpt_url), tables=ACCOUNT_TABLES)

    # 默认库也要隔离：current_engine() 优先读 core.db.engine，
    # 不 patch 就会读到真实库、混进其它测试的数据
    import core.db as _db
    monkeypatch.setattr(_db, "engine", default_engine)

    platform_database_registry.configure({
        "grok": grok_url,
        "chatgpt": chatgpt_url,
    })
    yield {"default": default_url, "grok": grok_url, "chatgpt": chatgpt_url}
    platform_database_registry.configure({})


class _Account:
    """最小 Account 替身（避免依赖 base_platform 的枚举）。"""

    def __init__(self, platform, email, password="pw", status="registered", **kw):
        self.platform = platform
        self.email = email
        self.password = password
        self.status = status
        self.user_id = kw.get("user_id", "")
        self.region = kw.get("region", "")
        self.token = kw.get("token", "")
        self.extra = kw.get("extra", {})


# ------------------------------------------------------------------ 邮箱键

def test_normalize_email_is_case_and_space_insensitive():
    """邮箱键大小写与首尾空白无关。"""
    assert normalize_email("  User@X.AI ") == "user@x.ai"
    assert normalize_email("USER@X.AI") == normalize_email("user@x.ai")
    assert normalize_email("") == ""
    assert normalize_email(None) == ""


# ------------------------------------------------------------------ 分库配置

def test_parse_platform_urls_from_json(monkeypatch):
    monkeypatch.setenv(
        "PLATFORM_DATABASE_URLS",
        '{"grok": "sqlite:///g.db", "ChatGPT": "sqlite:///c.db"}',
    )
    monkeypatch.delenv("DATABASE_URL_GROK", raising=False)
    mapping = parse_platform_database_urls()
    assert mapping["grok"] == "sqlite:///g.db"
    assert mapping["chatgpt"] == "sqlite:///c.db", "平台名应规范化为小写"


def test_parse_platform_urls_from_env_per_platform(monkeypatch):
    monkeypatch.delenv("PLATFORM_DATABASE_URLS", raising=False)
    monkeypatch.setenv("DATABASE_URL_GROK", "sqlite:///env_grok.db")
    mapping = parse_platform_database_urls()
    assert mapping["grok"] == "sqlite:///env_grok.db"


def test_bad_json_is_ignored(monkeypatch):
    """坏 JSON 不该让解析炸掉，也不该影响内置默认分库。

    内置默认（邮箱池 icloud / outlook）永远存在，所以断言的是「坏 JSON 没被
    当成配置读进去」，而不是「结果为空」。
    """
    monkeypatch.setenv("PLATFORM_DATABASE_URLS", "{not json")
    monkeypatch.delenv("DATABASE_URL_GROK", raising=False)
    mapping = parse_platform_database_urls()
    assert "grok" not in mapping
    assert set(mapping) == set(DEFAULT_PLATFORM_DB_FILES), (
        "只应有内置默认的邮箱池分库，坏 JSON 不应产生任何平台项"
    )


def test_mailbox_pools_are_dedicated_by_default():
    """邮箱池默认分库（data/platforms/{icloud,outlook}.db）。

    它们不是注册平台而是「邮箱来源」，被多个平台的注册流程共用；单独成库
    才能一个文件备份/清空。
    """
    mapping = parse_platform_database_urls()
    for pool in ("icloud", "outlook"):
        assert pool in mapping
        assert mapping[pool].endswith(f"/platforms/{pool}.db")


def test_explicit_config_overrides_builtin_pool_split(monkeypatch):
    """显式配置优先级高于内置默认，可以改指到别处。"""
    monkeypatch.setenv("PLATFORM_DATABASE_URLS", '{"outlook": "sqlite:///custom.db"}')
    assert parse_platform_database_urls()["outlook"] == "sqlite:///custom.db"


def test_unconfigured_platform_falls_back_to_default(isolated_db):
    """未分库的平台透明回落默认库（纯增量能力）。"""
    assert platform_database_registry.is_dedicated("icloud") is False
    from core.db import DATABASE_URL

    assert platform_database_registry.url_for("icloud") == DATABASE_URL


def test_configured_platform_is_dedicated(isolated_db):
    assert platform_database_registry.is_dedicated("grok") is True
    assert platform_database_registry.is_dedicated("chatgpt") is True


# ------------------------------------------------------------------ 分库落库

def test_accounts_land_in_their_own_database(isolated_db):
    """不同平台的账号分别落到各自库。"""
    from sqlalchemy import create_engine

    account_repository.upsert(_Account("grok", "a@x.ai"))
    account_repository.upsert(_Account("chatgpt", "b@x.ai"))

    with Session(create_engine(isolated_db["grok"])) as s:
        grok_rows = s.exec(select(AccountModel)).all()
    with Session(create_engine(isolated_db["chatgpt"])) as s:
        gpt_rows = s.exec(select(AccountModel)).all()

    assert [r.email for r in grok_rows] == ["a@x.ai"]
    assert [r.email for r in gpt_rows] == ["b@x.ai"]
    assert all(r.platform == "grok" for r in grok_rows)
    assert all(r.platform == "chatgpt" for r in gpt_rows)


def test_cross_platform_isolation(isolated_db):
    """一个平台的库不应看到另一个平台的账号。"""
    from sqlalchemy import create_engine

    account_repository.upsert(_Account("grok", "only-grok@x.ai"))
    with Session(create_engine(isolated_db["chatgpt"])) as s:
        assert s.exec(select(AccountModel)).all() == []


# ------------------------------------------------------------------ 邮箱唯一键

def test_email_is_unique_key_per_platform(isolated_db):
    """同平台同邮箱只保留一行（重复注册覆盖而非新增）。"""
    account_repository.upsert(_Account("grok", "dup@x.ai", password="first"))
    account_repository.upsert(_Account("grok", "dup@x.ai", password="second"))

    rows = account_repository.list_accounts("grok")
    assert len(rows) == 1
    assert rows[0].password == "second"


def test_email_key_is_case_insensitive(isolated_db):
    """大小写不同的同一邮箱视为同一账号。"""
    account_repository.upsert(_Account("grok", "Case@X.ai", password="p1"))
    assert account_repository.is_registered("grok", "case@x.ai") is True
    assert account_repository.is_registered("grok", "CASE@X.AI") is True

    account_repository.upsert(_Account("grok", "CASE@X.AI", password="p2"))
    rows = account_repository.list_accounts("grok")
    assert len(rows) == 1
    assert rows[0].email == "case@x.ai", "落库时应规范化为小写"


def test_same_email_on_different_platforms_are_separate(isolated_db):
    """不同平台可以用同一个邮箱（各自一行）。"""
    account_repository.upsert(_Account("grok", "shared@x.ai"))
    account_repository.upsert(_Account("chatgpt", "shared@x.ai"))

    assert account_repository.is_registered("grok", "shared@x.ai") is True
    assert account_repository.is_registered("chatgpt", "shared@x.ai") is True
    assert len(account_repository.list_accounts("grok")) == 1
    assert len(account_repository.list_accounts("chatgpt")) == 1


# ------------------------------------------------------------------ 判重

def test_is_registered_detects_existing(isolated_db):
    assert account_repository.is_registered("grok", "new@x.ai") is False
    account_repository.upsert(_Account("grok", "new@x.ai"))
    assert account_repository.is_registered("grok", "new@x.ai") is True


def test_is_registered_ignores_empty_email(isolated_db):
    assert account_repository.is_registered("grok", "") is False
    assert account_repository.is_registered("grok", "   ") is False


def test_is_registered_can_exclude_invalid(isolated_db):
    """默认把失效账号也算已注册（避免重复占用邮箱）；可显式放行重试。"""
    account_repository.upsert(_Account("grok", "dead@x.ai", status="invalid"))
    assert account_repository.is_registered("grok", "dead@x.ai") is True
    assert account_repository.is_registered(
        "grok", "dead@x.ai", include_invalid=False
    ) is False


def test_registered_emails_batch(isolated_db):
    """批量判重：一次查出已注册的邮箱。"""
    account_repository.upsert(_Account("grok", "one@x.ai"))
    account_repository.upsert(_Account("grok", "three@x.ai"))
    hits = account_repository.registered_emails(
        "grok", ["one@x.ai", "two@x.ai", "THREE@X.AI", ""]
    )
    assert hits == {"one@x.ai", "three@x.ai"}


# ------------------------------------------------------------------ 统计

def test_stats_spans_databases(isolated_db):
    """统计要跨所有库汇总，不能只看默认库。"""
    account_repository.upsert(_Account("grok", "g1@x.ai"))
    account_repository.upsert(_Account("grok", "g2@x.ai", status="invalid"))
    account_repository.upsert(_Account("chatgpt", "c1@x.ai"))

    stats = account_repository.stats()
    assert stats["by_platform"]["grok"] == 2
    assert stats["by_platform"]["chatgpt"] == 1
    assert stats["by_status"]["registered"] == 2
    assert stats["by_status"]["invalid"] == 1
    assert stats["total"] == 3


# ------------------------------------------------------------------ 迁移

def test_migration_creates_unique_index(tmp_path):
    """迁移为 (platform, email) 建唯一索引，并合并历史重复行。"""
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    from core.db.migrations import _ensure_account_email_unique

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(url)
    from core.db.models_account import ACCOUNT_TABLES

    SQLModel.metadata.create_all(engine, tables=ACCOUNT_TABLES)

    # 造历史脏数据：大小写不同 + 完全重复
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO accounts (platform, email, password, user_id, region, "
            "token, status, cashier_url, extra_json, "
            "created_at, updated_at) "
            "VALUES ('grok', 'Dup@X.ai', 'p1', '', '', '', 'registered', '', '{}', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00'), "
            "('grok', 'dup@x.ai', 'p2', 'u2', '', 'tok', 'invalid', '', '{\"k\":1}', "
            "'2026-01-02 00:00:00', '2026-01-02 00:00:00')"
        )

    _ensure_account_email_unique(engine)

    with Session(engine) as s:
        rows = s.exec(select(AccountModel)).all()
    assert len(rows) == 1, "重复行应被合并"
    assert rows[0].email == "dup@x.ai"

    # 唯一索引生效：再插重复应失败
    with engine.begin() as conn:
        with pytest.raises(Exception):
            conn.exec_driver_sql(
                "INSERT INTO accounts (platform, email, password) "
                "VALUES ('grok', 'dup@x.ai', 'p3')"
            )


def test_migration_is_idempotent(tmp_path):
    """迁移可重复执行（每次启动都会跑）。"""
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    from core.db.migrations import _ensure_account_email_unique

    url = f"sqlite:///{tmp_path / 'idem.db'}"
    engine = create_engine(url)
    from core.db.models_account import ACCOUNT_TABLES

    SQLModel.metadata.create_all(engine, tables=ACCOUNT_TABLES)
    for _ in range(3):
        _ensure_account_email_unique(engine)

    with engine.begin() as conn:
        idx = conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name='uq_accounts_platform_email'"
        ).fetchall()
    assert len(idx) == 1


class TestCrossDatabaseQueries:
    """跨库查询：分库后上层仍能看到全部账号（否则会静默漏读）。"""

    def test_list_all_accounts_spans_databases(self, isolated_db):
        account_repository.upsert(_Account("grok", "g1@x.ai"))
        account_repository.upsert(_Account("chatgpt", "c1@x.ai"))
        account_repository.upsert(_Account("icloud", "i1@x.ai"))  # 未分库 → 默认库

        rows = account_repository.list_all_accounts()
        assert {r.email for r in rows} == {"g1@x.ai", "c1@x.ai", "i1@x.ai"}

    def test_list_all_accounts_filters_by_platform(self, isolated_db):
        account_repository.upsert(_Account("grok", "g1@x.ai"))
        account_repository.upsert(_Account("chatgpt", "c1@x.ai"))
        account_repository.upsert(_Account("icloud", "i1@x.ai"))

        rows = account_repository.list_all_accounts(platform="grok")
        assert [r.email for r in rows] == ["g1@x.ai"], "指定平台不应混入其它平台"

    def test_list_all_accounts_email_filter_is_case_insensitive(self, isolated_db):
        account_repository.upsert(_Account("grok", "Mixed@X.ai"))
        account_repository.upsert(_Account("chatgpt", "other@x.ai"))

        rows = account_repository.list_all_accounts(email_contains="MIXED@x")
        assert [r.email for r in rows] == ["mixed@x.ai"]

    def test_list_all_accounts_status_filter(self, isolated_db):
        account_repository.upsert(_Account("grok", "ok@x.ai", status="registered"))
        account_repository.upsert(_Account("chatgpt", "bad@x.ai", status="invalid"))

        rows = account_repository.list_all_accounts(status="invalid")
        assert [r.email for r in rows] == ["bad@x.ai"]

    def test_get_finds_account_in_platform_database(self, isolated_db):
        """GET /accounts/{id} 只有 id：必须跨库找，否则找不到分库账号。"""
        row = account_repository.upsert(_Account("grok", "g@x.ai"))
        found = account_repository.get(row.id)
        assert found is not None and found.email == "g@x.ai"

    def test_get_prefers_platform_database(self, isolated_db):
        row = account_repository.upsert(_Account("chatgpt", "c@x.ai"))
        assert account_repository.get(row.id, "chatgpt").email == "c@x.ai"

    def test_delete_spans_databases(self, isolated_db):
        row = account_repository.upsert(_Account("grok", "gone@x.ai"))
        assert account_repository.delete(row.id) is True
        assert account_repository.is_registered("grok", "gone@x.ai") is False

    def test_delete_missing_returns_false(self, isolated_db):
        assert account_repository.delete(999999) is False

    def test_cross_database_duplicate_is_deduplicated(self, isolated_db):
        """分库迁移遗留：同 (platform,email) 同时在平台库与默认库 → 只返回一条。"""
        from core.db import platform_session

        account_repository.upsert(_Account("grok", "both@x.ai"))
        # 模拟迁移前遗留在默认库的历史行（platform_session("") = 默认库）
        with platform_session("") as s:
            s.add(AccountModel(platform="grok", email="both@x.ai", password="old"))
            s.commit()

        rows = account_repository.list_all_accounts(platform="grok")
        assert len(rows) == 1, f"跨库重复应去重，实际 {len(rows)} 条"

    def test_upsert_accepts_account_model(self, isolated_db):
        """API 层把 AccountModel 传给 upsert 时要保留 extra_json。"""
        model = AccountModel(
            platform="grok", email="m@x.ai", password="p",
            extra_json='{"keep": "me", "cashier_url": "https://pay"}',
        )
        saved = account_repository.upsert(model)
        assert saved.cashier_url == "https://pay"
        assert "keep" in saved.extra_json, "extra_json 不应被清空"


class TestExtraJsonRobustness:
    """extra_json 的脏数据容错。

    历史行可能是空串、半截 JSON（导入中断）、或非字典 JSON。这些行一旦被
    读出来就抛 JSONDecodeError 会打断整条流程（测活、导出、列表全挂），
    所以解析必须容错——仓储层一直有保护，模型层此前没有。
    """

    @pytest.mark.parametrize(
        "raw",
        ["", "   ", "{not json", "null", "[]", '"a string"', "123", "{'single': 1}"],
    )
    def test_get_extra_survives_dirty_data(self, raw):
        model = AccountModel(platform="grok", email="d@x.ai", password="p",
                             extra_json=raw)
        assert model.get_extra() == {}

    def test_get_extra_parses_valid_json(self):
        model = AccountModel(platform="grok", email="v@x.ai", password="p",
                             extra_json='{"a": 1, "b": {"c": 2}}')
        assert model.get_extra() == {"a": 1, "b": {"c": 2}}

    def test_set_extra_rejects_non_dict(self):
        model = AccountModel(platform="grok", email="s@x.ai", password="p")
        model.set_extra(["not", "a", "dict"])  # type: ignore[arg-type]
        assert model.get_extra() == {}

    def test_get_extra_value_reads_single_key(self):
        model = AccountModel(platform="grok", email="k@x.ai", password="p",
                             extra_json='{"token": "abc"}')
        assert model.get_extra_value("token") == "abc"
        assert model.get_extra_value("missing", "fallback") == "fallback"

    def test_update_extra_merges_and_deletes(self):
        model = AccountModel(platform="grok", email="u@x.ai", password="p",
                             extra_json='{"keep": 1, "drop": 2}')
        merged = model.update_extra(keep=10, added="new", drop=None)
        assert merged == {"keep": 10, "added": "new"}
        assert model.get_extra() == {"keep": 10, "added": "new"}

    def test_export_survives_dirty_extra_json(self, isolated_db):
        """导出接口遇到脏 extra_json 不应 500。"""
        account_repository.upsert(
            AccountModel(platform="grok", email="dirty@x.ai", password="p",
                         extra_json="{broken")
        )
        from api.accounts import export_accounts

        resp = export_accounts(platform="grok", status=None)
        assert resp is not None


# ------------------------------------------------- 邮箱池键不得被当成注册平台扫

def test_account_platform_keys_excludes_pools_even_when_named():
    """显式传邮箱池键也要剔除，否则会去查池库的 `accounts` 表。

    池库（icloud.db / outlook.db）里只有 `icloud_accounts` / `icloud_aliases`
    / `outlook_accounts`，没有 `accounts` —— 拿它去查必然 `no such table:
    accounts`，把一个「这个平台没有账号」的正常语义变成 500。

    可达路径不是假设：`/accounts/:platform` 是能直接打开的深链（菜单里不列
    iCloud，但旧书签和手输地址都会走到），实测 `/api/accounts?platform=icloud`
    与 `?platform=outlook` 都返回过 500。
    """
    from core.db.repository import account_platform_keys

    assert account_platform_keys("icloud") == []
    assert account_platform_keys("outlook") == []
    assert account_platform_keys("ICloud") == []      # 大小写与空格同样处理
    assert account_platform_keys(" icloud ") == []
    # 普通平台不受影响
    assert account_platform_keys("grok") == ["grok"]
    # 不指定平台时照旧返回全部非池平台
    assert not (set(account_platform_keys()) & {"icloud", "outlook"})


def test_account_db_key_maps_pools_to_default():
    """池键 → 默认库键；其它平台原样返回。"""
    from core.db.repository import account_db_key

    assert account_db_key("icloud") == ""
    assert account_db_key("outlook") == ""
    assert account_db_key(" ICloud ") == ""
    assert account_db_key("grok") == "grok"
    assert account_db_key("") == ""


def test_accounts_api_returns_empty_for_pool_platforms(isolated_db):
    """池键走账号接口要返回 200 空列表，不是 500。

    覆盖整条链路，不只是一个函数 —— 修复前实测 500 的入口有三个：
    `GET /api/accounts?platform=…`、`POST /api/accounts`（按平台开库会话）、
    `GET /api/accounts/{id}?platform=…`。它们走的是仓储里不同的会话打开点，
    只修其中一个另外两个照样炸。
    """
    from core.db.repository import account_repository

    for pool in ("icloud", "outlook"):
        assert account_repository.count_all_accounts(platform=pool) == 0
        assert account_repository.list_all_accounts(platform=pool) == []
        # 这三个都曾经因为按池键开库会话而 500
        assert account_repository.find_by_email(pool, "nobody@example.com") is None
        assert account_repository.get(1, platform=pool) is None
        account_repository.delete(1, platform=pool)  # 不该抛

    # 同时确认正常平台仍能查到账号（修复不能把正常路径一起挡掉）
    account_repository.upsert(
        AccountModel(platform="grok", email="pool-check@x.ai", password="p")
    )
    assert account_repository.count_all_accounts(platform="grok") == 1
    assert account_repository.find_by_email("grok", "pool-check@x.ai") is not None


def test_pool_key_count_matches_list(isolated_db):
    """池键的 count 与 list 口径必须一致（否则分页 total 与 items 对不上）。

    池键只扫默认库 —— 分库迁移前的历史遗留行可能就留在那里，count 直接
    return 0 会让「total=0 但 items 非空」。
    """
    from core.db.repository import account_repository

    # 往默认库塞一行 platform=icloud 的历史遗留账号
    from core.db.base import current_engine
    from sqlalchemy.orm import Session as _S

    with _S(current_engine()) as s:
        s.add(AccountModel(platform="icloud", email="legacy@icloud.com", password="p"))
        s.commit()

    listed = account_repository.list_all_accounts(platform="icloud")
    counted = account_repository.count_all_accounts(platform="icloud")
    assert len(listed) == counted == 1
