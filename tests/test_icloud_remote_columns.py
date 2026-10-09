"""远程 iCloud 仓库：账号/别名表的来源列与迁移。

背景（用户需求 2026-10-09）：iCloud 隐私邮箱增加「远程仓库」——把独立的
icloud-hme 服务接进来，账号与别名都带「本地 / 远程」来源标签，同地址去重时
**本地优先**（相同地址本地已有则不要远程行）。

本文件先钉住数据层契约：
- 模型新增 source / base_url / remote_account_id / remote_password_cipher（账号）
  与 source（别名）列；
- 老库升级时迁移补列并回填 `local`；
- email 的单列唯一索引被移除（同一邮箱允许同时以本地主号与远程账号存在 ——
  这正是「本地优先去重」场景：同一个 Apple ID 两边都有）；
- 远程账号按 (base_url, remote_account_id) 唯一（同一个服务里的同一个远端
  账号只能接入一次），本地账号不受该约束（其 base_url 为空）。
"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from core.db.models_platform import ICloudAccountModel, ICloudAliasModel


def _legacy_engine(tmp_path):
    """建一个「升级前」形态的 icloud 库：无 source 列，email 单列唯一。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'icloud.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """CREATE TABLE icloud_accounts (
            id INTEGER NOT NULL PRIMARY KEY,
            email VARCHAR NOT NULL,
            display_name VARCHAR NOT NULL DEFAULT '',
            region VARCHAR NOT NULL DEFAULT 'global',
            status VARCHAR NOT NULL DEFAULT 'active',
            enabled BOOLEAN NOT NULL DEFAULT 1,
            credentials_cipher VARCHAR NOT NULL DEFAULT '',
            sync_error VARCHAR NOT NULL DEFAULT '',
            last_sync_at DATETIME,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )"""
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX ix_icloud_accounts_email ON icloud_accounts (email)"
        )
        conn.exec_driver_sql(
            """CREATE TABLE icloud_aliases (
            id INTEGER NOT NULL PRIMARY KEY,
            account_id INTEGER NOT NULL,
            address VARCHAR NOT NULL,
            label VARCHAR NOT NULL DEFAULT '',
            note VARCHAR NOT NULL DEFAULT '',
            status VARCHAR NOT NULL DEFAULT 'active',
            provider_id VARCHAR NOT NULL DEFAULT '',
            share_token VARCHAR NOT NULL DEFAULT '',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            pool_status TEXT DEFAULT 'unpooled',
            used_platforms TEXT DEFAULT ''
        )"""
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX ix_icloud_aliases_address ON icloud_aliases (address)"
        )
        conn.exec_driver_sql(
            "INSERT INTO icloud_accounts (email) VALUES ('owner@icloud.com')"
        )
        conn.exec_driver_sql(
            "INSERT INTO icloud_aliases (account_id, address) VALUES (1, 'a@icloud.com')"
        )
    return engine


def _columns(engine, table: str) -> set[str]:
    with engine.begin() as conn:
        return {
            str(row[1])
            for row in conn.exec_driver_sql(f"PRAGMA table_info('{table}')").fetchall()
        }


class TestModelContract:
    def test_account_model_declares_remote_columns(self):
        """账号模型必须声明来源与远程服务字段（本地行为默认值不变）。"""
        account = ICloudAccountModel(email="owner@icloud.com")
        assert account.source == "local"
        assert account.base_url == ""
        assert account.remote_account_id == ""
        assert account.remote_password_cipher == ""
        assert account.proxy == ""

    def test_alias_model_declares_source(self):
        alias = ICloudAliasModel(account_id=1, address="a@icloud.com")
        assert alias.source == "local"


class TestMigration:
    def test_migration_adds_columns_and_backfills_local(self, tmp_path):
        """老库升级：补列 + 历史行回填 `local`。"""
        from core.db.migrations import run_migrations

        engine = _legacy_engine(tmp_path)
        run_migrations(engine)

        account_columns = _columns(engine, "icloud_accounts")
        for column in ("source", "base_url", "remote_account_id", "remote_password_cipher", "proxy"):
            assert column in account_columns, f"icloud_accounts 缺列 {column}"
        assert "source" in _columns(engine, "icloud_aliases")

        with engine.begin() as conn:
            source = conn.exec_driver_sql(
                "SELECT source FROM icloud_accounts WHERE id = 1"
            ).fetchone()[0]
            alias_source = conn.exec_driver_sql(
                "SELECT source FROM icloud_aliases WHERE id = 1"
            ).fetchone()[0]
        assert source == "local"
        assert alias_source == "local"

    def test_migration_allows_same_email_across_sources(self, tmp_path):
        """email 单列唯一被移除：同邮箱可同时是本地主号与远程账号。

        这正是「本地优先去重」的部署形态（同一个 Apple ID 两边都有），
        单列唯一会让远程账号根本加不进来。
        """
        from core.db.migrations import run_migrations

        engine = _legacy_engine(tmp_path)
        run_migrations(engine)

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO icloud_accounts (email, source, base_url, remote_account_id) "
                "VALUES ('owner@icloud.com', 'remote', 'http://hme.example', 'acc_1')"
            )
            count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM icloud_accounts WHERE email = 'owner@icloud.com'"
            ).fetchone()[0]
        assert count == 2

    def test_migration_keeps_remote_account_unique_per_service(self, tmp_path):
        """同一 (base_url, remote_account_id) 只能有一行 —— 防重复接入。"""
        from core.db.migrations import run_migrations

        engine = _legacy_engine(tmp_path)
        run_migrations(engine)

        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO icloud_accounts (email, source, base_url, remote_account_id) "
                "VALUES ('a@icloud.com', 'remote', 'http://hme.example', 'acc_1')"
            )
            with pytest.raises(IntegrityError):
                conn.exec_driver_sql(
                    "INSERT INTO icloud_accounts (email, source, base_url, remote_account_id) "
                    "VALUES ('b@icloud.com', 'remote', 'http://hme.example', 'acc_1')"
                )

    def test_migration_is_idempotent(self, tmp_path):
        """迁移幂等：重复跑不报错、不丢数据。"""
        from core.db.migrations import run_migrations

        engine = _legacy_engine(tmp_path)
        run_migrations(engine)
        run_migrations(engine)
        with engine.begin() as conn:
            count = conn.exec_driver_sql("SELECT COUNT(*) FROM icloud_accounts").fetchone()[0]
        assert count == 1

    def test_orm_read_works_after_migration(self, tmp_path):
        """迁移后 ORM 读必须可用（补列排在 ORM 读之前，否则 no such column）。"""
        from core.db.migrations import run_migrations

        engine = _legacy_engine(tmp_path)
        run_migrations(engine)
        with Session(engine) as session:
            row = session.exec(select(ICloudAccountModel)).first()
        assert row is not None and row.source == "local"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
