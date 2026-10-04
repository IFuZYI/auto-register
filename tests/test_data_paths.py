"""数据目录布局（core/paths.py）与旧布局迁移。

这层的关键性质是"数据的唯一真相源 + 迁移永不丢数据"：
路径解析错了会把库建到别处（甚至跟着工作目录跑），迁移写错了会覆盖真实数据。
两条都比普通功能 bug 贵得多，所以单独立测。
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


def _reload_paths(monkeypatch, tmp_path: Path, data_dir: str = ""):
    """按指定 DATA_DIR 重新求值 core.paths 的模块级常量。"""
    if data_dir:
        monkeypatch.setenv("DATA_DIR", data_dir)
    else:
        monkeypatch.delenv("DATA_DIR", raising=False)
    import core.paths

    return importlib.reload(core.paths)


@pytest.fixture(autouse=True)
def _restore_paths():
    """每个用例结束后把 core.paths 恢复到真实环境，避免污染其他测试。"""
    yield
    import core.paths

    importlib.reload(core.paths)


class TestLayout:
    def test_defaults_live_under_data_dir(self, monkeypatch, tmp_path):
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))

        assert paths.DEFAULT_DB_FILE == tmp_path / "data" / "account_manager.db"
        assert paths.PLATFORM_DB_DIR == tmp_path / "data" / "platforms"
        assert paths.CREDENTIAL_KEY_FILE == tmp_path / "data" / "secrets" / "credential_key"
        assert paths.LOG_DIR == tmp_path / "data" / "logs"

    def test_without_env_var_data_dir_is_repo_relative(self, monkeypatch, tmp_path):
        """没配 DATA_DIR 时落到仓库根下的 data/，而不是跟着工作目录跑。"""
        paths = _reload_paths(monkeypatch, tmp_path, "")

        assert paths.DATA_DIR == paths.project_root() / "data"
        assert paths.DATA_DIR.is_absolute()

    def test_relative_data_dir_is_resolved_against_repo_root(self, monkeypatch, tmp_path):
        paths = _reload_paths(monkeypatch, tmp_path, "custom-data")

        assert paths.DATA_DIR == paths.project_root() / "custom-data"

    def test_every_path_is_absolute(self, monkeypatch, tmp_path):
        """相对路径会被 SQLite 按工作目录解析 —— 从别处启动就换了个库。"""
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))

        for name in (
            "DATA_DIR",
            "DEFAULT_DB_FILE",
            "PLATFORM_DB_DIR",
            "SECRETS_DIR",
            "CREDENTIAL_KEY_FILE",
            "LOG_DIR",
        ):
            assert getattr(paths, name).is_absolute(), name

    def test_ensure_data_dirs_creates_every_directory(self, monkeypatch, tmp_path):
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))
        paths.ensure_data_dirs()

        for directory in (
            paths.DATA_DIR,
            paths.PLATFORM_DB_DIR,
            paths.SECRETS_DIR,
            paths.LOG_DIR,
        ):
            assert directory.is_dir(), directory

    def test_ensure_data_dirs_is_idempotent(self, monkeypatch, tmp_path):
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))
        paths.ensure_data_dirs()
        paths.ensure_data_dirs()  # 不该抛


class TestMigration:
    def test_moves_legacy_file_into_data_dir(self, monkeypatch, tmp_path):
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))
        legacy = tmp_path / "account_manager.db"
        legacy.write_text("real-data", encoding="utf-8")

        monkeypatch.setattr(
            paths, "_LEGACY_MOVES", ((legacy, paths.DEFAULT_DB_FILE),)
        )
        results = paths.migrate_legacy_paths()

        assert paths.DEFAULT_DB_FILE.read_text(encoding="utf-8") == "real-data"
        assert not legacy.exists()
        assert any("account_manager.db" in line for line in results)

    def test_never_overwrites_an_existing_target(self, monkeypatch, tmp_path):
        """目标已有数据时保留目标 —— 迁移最不能犯的错就是覆盖真数据。"""
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))
        legacy = tmp_path / "account_manager.db"
        legacy.write_text("stale", encoding="utf-8")
        paths.ensure_data_dirs()
        paths.DEFAULT_DB_FILE.write_text("current", encoding="utf-8")

        monkeypatch.setattr(
            paths, "_LEGACY_MOVES", ((legacy, paths.DEFAULT_DB_FILE),)
        )
        results = paths.migrate_legacy_paths()

        assert paths.DEFAULT_DB_FILE.read_text(encoding="utf-8") == "current"
        # 旧文件不删，留给使用者确认后再清理
        assert legacy.read_text(encoding="utf-8") == "stale"
        assert any("跳过" in line for line in results)

    def test_is_idempotent(self, monkeypatch, tmp_path):
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))
        legacy = tmp_path / "account_manager.db"
        legacy.write_text("real-data", encoding="utf-8")
        monkeypatch.setattr(
            paths, "_LEGACY_MOVES", ((legacy, paths.DEFAULT_DB_FILE),)
        )

        paths.migrate_legacy_paths()
        second = paths.migrate_legacy_paths()

        assert second == []
        assert paths.DEFAULT_DB_FILE.read_text(encoding="utf-8") == "real-data"

    def test_missing_legacy_path_is_a_no_op(self, monkeypatch, tmp_path):
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))
        monkeypatch.setattr(
            paths,
            "_LEGACY_MOVES",
            ((tmp_path / "nope.db", paths.DEFAULT_DB_FILE),),
        )

        assert paths.migrate_legacy_paths() == []

    def test_moves_a_whole_directory(self, monkeypatch, tmp_path):
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))
        legacy = tmp_path / "external_logs"
        legacy.mkdir()
        (legacy / "cliproxyapi.log").write_text("log", encoding="utf-8")

        monkeypatch.setattr(paths, "_LEGACY_MOVES", ((legacy, paths.LOG_DIR),))
        paths.migrate_legacy_paths()

        assert (paths.LOG_DIR / "cliproxyapi.log").read_text(encoding="utf-8") == "log"
        assert not legacy.exists()

    def test_directory_merge_does_not_nest_an_extra_level(self, monkeypatch, tmp_path):
        """目标目录已被 ensure_data_dirs 建好时，内容要并进它、而不是塞到它内部。

        回归：直接 `shutil.move(src, dst)` 在 dst 已存在时会产生
        `dst/<src名>/…` —— 文件看着搬成功了，实际多一层、谁也找不到。
        """
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))
        paths.ensure_data_dirs()  # 目标目录 LOG_DIR 此时已存在

        legacy = tmp_path / "external_logs"
        legacy.mkdir()
        (legacy / "cliproxyapi.log").write_text("log", encoding="utf-8")

        monkeypatch.setattr(paths, "_LEGACY_MOVES", ((legacy, paths.LOG_DIR),))
        paths.migrate_legacy_paths()

        assert (paths.LOG_DIR / "cliproxyapi.log").exists()
        assert not (paths.LOG_DIR / "external_logs").exists()
        assert not legacy.exists()

    def test_directory_merge_keeps_existing_files(self, monkeypatch, tmp_path):
        """同名文件保留新位置那份，旧的那份留在原地等人工确认。"""
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))
        paths.ensure_data_dirs()
        (paths.LOG_DIR / "cliproxyapi.log").write_text("new", encoding="utf-8")

        legacy = tmp_path / "external_logs"
        legacy.mkdir()
        (legacy / "cliproxyapi.log").write_text("old", encoding="utf-8")
        (legacy / "other.log").write_text("other", encoding="utf-8")

        monkeypatch.setattr(paths, "_LEGACY_MOVES", ((legacy, paths.LOG_DIR),))
        results = paths.migrate_legacy_paths()

        assert (paths.LOG_DIR / "cliproxyapi.log").read_text(encoding="utf-8") == "new"
        assert (paths.LOG_DIR / "other.log").read_text(encoding="utf-8") == "other"
        # 有冲突就不能删旧目录，否则被跳过的那份会凭空消失
        assert (legacy / "cliproxyapi.log").read_text(encoding="utf-8") == "old"
        assert any("保留在" in line for line in results)

    def test_failure_is_reported_not_raised(self, monkeypatch, tmp_path):
        """数据目录出问题时应用仍要能起来，并把原因说清楚。"""
        paths = _reload_paths(monkeypatch, tmp_path, str(tmp_path / "data"))
        legacy = tmp_path / "account_manager.db"
        legacy.write_text("x", encoding="utf-8")

        def _boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(paths.shutil, "move", _boom)
        monkeypatch.setattr(
            paths, "_LEGACY_MOVES", ((legacy, paths.DEFAULT_DB_FILE),)
        )

        results = paths.migrate_legacy_paths()
        assert any("失败" in line for line in results)


class TestWiredIntoStartup:
    """布局只有真的被业务代码用上才算数。"""

    def test_db_default_url_points_at_data_dir(self):
        from core import paths
        from core.db.base import DATABASE_URL

        # conftest 会把 DATABASE_URL 指向临时库，这里只断言"不是相对路径"
        assert not DATABASE_URL.startswith("sqlite:///account_manager.db")
        if DATABASE_URL.startswith("sqlite:///") and not os.getenv("DATABASE_URL"):
            assert str(paths.DEFAULT_DB_FILE) in DATABASE_URL

    def test_credential_key_default_is_under_data_dir(self):
        from core import paths
        from core.secret_box import DEFAULT_KEY_FILE

        assert DEFAULT_KEY_FILE == paths.CREDENTIAL_KEY_FILE

    def test_main_runs_migration_before_init_db(self):
        """迁移必须在建库之前跑，否则会先建出空库再来处理冲突。"""
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(
            encoding="utf-8"
        )
        assert source.index("migrate_legacy_paths()") < source.index("init_db()")
