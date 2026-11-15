"""跨进程取号互斥：`claim_available` 的裁决必须在数据库层，不在进程内锁。

背景（docs/MAINTENANCE.md §1.6）：iCloud 取号原本靠
`services/icloud/_locks.py` 里的 `threading.RLock` 保证「读池 → 标 in_use」
互斥。那把锁只在一个进程内有效 —— 起两个 worker 时两边各持自己那把锁，
于是从池里挑中同一个 `available` 别名，两个注册任务都撞「邮箱已被占用」。

这里用**真进程**（不是线程）钉住修复：N 个进程同时抢同一行，只能有一个
拿到。旧实现（无条件 `set_fields`）会让 N 个进程全部返回成功。
"""
from __future__ import annotations

import multiprocessing as mp
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: 必须钉死后端：全新目录（无 account_manager.db）默认会解析成 **json**，
#: 那时 `_store()` 不是 None，走的是 JSON 分支 —— 跨进程保证只存在于 SQL 分支，
#: 测错分支会「通过」得毫无意义。
os.environ["STORAGE_BACKEND"] = "sqlite"


def _point_icloud_at(url: str) -> None:
    """把注册表指向临时库，并**断言真的指过去了**。

    为什么需要这个断言：`platform_database_registry` 在 **import 时**就
    `configure()` 过一次，之后改环境变量对它无效。不重新 configure 的话，
    这些测试会连到真实的 `data/platforms/icloud.db` 上 —— 测的是生产数据，
    而且可能把它改脏。宁可在这里炸，也不要在真库上「碰巧通过」。
    """
    from core.db.registry import platform_database_registry

    platform_database_registry.configure({"icloud": url})
    resolved = platform_database_registry.url_for("icloud")
    if str(resolved) != str(url):
        raise AssertionError(f"注册表没指向临时库，拒绝继续：{resolved}")


def _claim_in_child(db_url: str, alias_id: int, queue) -> None:  # pragma: no cover
    """子进程入口：设好环境 → 走仓储抢号 → 把结果丢回队列。"""
    data_dir = str(Path(db_url.replace("sqlite:///", "")).parent)
    os.environ["DATA_DIR"] = data_dir
    os.environ["DATABASE_URL_ICLOUD"] = db_url
    os.environ["STORAGE_BACKEND"] = "sqlite"
    try:
        _point_icloud_at(db_url)
        from core.db.icloud_alias_repository import icloud_alias_repository

        queue.put(bool(icloud_alias_repository.claim_available(alias_id)))
    except Exception as exc:  # noqa: BLE001 - 子进程异常要能被父进程看见
        queue.put(f"ERROR: {type(exc).__name__}: {exc}")


class CrossProcessClaimTests(unittest.TestCase):
    def _make_db(self) -> tuple[str, int]:
        from sqlalchemy import create_engine
        from sqlmodel import Session

        from core.db.models_platform import ICloudAliasModel

        tmp = tempfile.mkdtemp(prefix="iclaim-")
        db = Path(tmp) / "icloud.db"
        url = f"sqlite:///{db}"
        engine = create_engine(url)
        ICloudAliasModel.metadata.create_all(
            engine, tables=[ICloudAliasModel.__table__]
        )
        with Session(engine) as session:
            row = ICloudAliasModel(
                account_id=1, address="race@example.com", pool_status="available"
            )
            session.add(row)
            session.commit()
            alias_id = int(row.id)
        engine.dispose()
        return url, alias_id

    def test_exactly_one_of_eight_processes_wins(self):
        url, alias_id = self._make_db()
        ctx = mp.get_context("fork")  # Linux：fork 最快，且能带上已导入的模块
        queue = ctx.Queue()
        procs = [
            ctx.Process(target=_claim_in_child, args=(url, alias_id, queue))
            for _ in range(8)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(60)

        results = [queue.get(timeout=10) for _ in procs]
        errors = [r for r in results if isinstance(r, str)]
        self.assertEqual(errors, [], f"子进程报错: {errors}")
        wins = sum(1 for r in results if r is True)
        self.assertEqual(
            wins, 1, f"应恰好一个进程领到，实际 {wins} 个: {results}"
        )

    def test_second_claim_on_same_row_is_refused(self):
        """同一进程里再抢一次必须失败（幂等的拒绝面）。"""
        url, alias_id = self._make_db()
        os.environ["DATA_DIR"] = str(Path(url.replace("sqlite:///", "")).parent)
        os.environ["DATABASE_URL_ICLOUD"] = url
        _point_icloud_at(url)
        from core.db.icloud_alias_repository import icloud_alias_repository

        # 正控：确实在 SQL 分支上（JSON 分支的锁不跨进程，测它没有意义）
        self.assertIsNone(
            icloud_alias_repository._store(), "后端不是 sqlite，跨进程断言无效"
        )
        self.assertTrue(icloud_alias_repository.claim_available(alias_id))
        self.assertFalse(icloud_alias_repository.claim_available(alias_id))

    def test_blank_pool_status_counts_as_available(self):
        """老行的 `pool_status` 是 NULL/空串时也要能领（与 find_candidates 同口径）。"""
        from sqlalchemy import create_engine, text
        from sqlmodel import Session

        from core.db.models_platform import ICloudAliasModel

        tmp = tempfile.mkdtemp(prefix="iclaim-blank-")
        url = f"sqlite:///{Path(tmp) / 'icloud.db'}"
        engine = create_engine(url)
        ICloudAliasModel.metadata.create_all(
            engine, tables=[ICloudAliasModel.__table__]
        )
        with Session(engine) as session:
            row = ICloudAliasModel(account_id=1, address="blank@example.com")
            session.add(row)
            session.commit()
            alias_id = int(row.id)
        with engine.begin() as conn:  # 模拟迁移前的老行（列是 NOT NULL，用空串）
            conn.execute(
                text("UPDATE icloud_aliases SET pool_status = '' WHERE id = :i"),
                {"i": alias_id},
            )
        engine.dispose()

        os.environ["DATA_DIR"] = tmp
        os.environ["DATABASE_URL_ICLOUD"] = url
        _point_icloud_at(url)
        from core.db.icloud_alias_repository import icloud_alias_repository

        # 正控：确实在 SQL 分支上（JSON 分支的锁不跨进程，测它没有意义）
        self.assertIsNone(
            icloud_alias_repository._store(), "后端不是 sqlite，跨进程断言无效"
        )
        self.assertTrue(icloud_alias_repository.claim_available(alias_id))


    def test_used_row_is_claimable(self):
        """`used` 也要能领 —— 别的平台用过的地址还能给本平台用。

        这条口径来自 `find_candidates`（它把 `used` 算候选，同平台复用由
        `used_platforms` 单独挡）。抢号的前提写窄成只剩 `available` 的话，
        跨平台复用会**永远抢不到**，直接去新生成，白烧 Apple 每小时 5 个的额度。
        """
        from sqlalchemy import create_engine, text
        from sqlmodel import Session

        from core.db.models_platform import ICloudAliasModel

        tmp = tempfile.mkdtemp(prefix="iclaim-used-")
        url = f"sqlite:///{Path(tmp) / 'icloud.db'}"
        engine = create_engine(url)
        ICloudAliasModel.metadata.create_all(
            engine, tables=[ICloudAliasModel.__table__]
        )
        with Session(engine) as session:
            row = ICloudAliasModel(
                account_id=1,
                address="used@example.com",
                pool_status="used",
                used_platforms="chatgpt",
            )
            session.add(row)
            session.commit()
            alias_id = int(row.id)
        engine.dispose()

        os.environ["DATA_DIR"] = tmp
        os.environ["DATABASE_URL_ICLOUD"] = url
        _point_icloud_at(url)
        from core.db.icloud_alias_repository import icloud_alias_repository

        self.assertTrue(icloud_alias_repository.claim_available(alias_id))


if __name__ == "__main__":
    unittest.main()
