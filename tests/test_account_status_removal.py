"""账号状态精简：删除「试用中（trial）/ 已订阅（subscribed）」两个状态。

用户要求：「状态只保留已注册、已过期、已失效、已封禁。试用中和已订阅不要了。」

删除后的状态集合：registered / expired / invalid / banned。随状态一起退场的：

- `AccountStatus.TRIAL` / `SUBSCRIBED` 两个枚举成员；
- 调度器的 `check_trial_expiry`（唯一职责是 trial → expired，删除后永远空转）；
- `accounts.trial_end_time` 列（零写入、零读取，试用状态的存储）；
- `Account.trial_end_time` 字段与 `BasePlatform.get_trial_url`（零调用死钩子）；
- 前端三处状态选项与仪表盘卡片里的这两个状态。

保留：`cashier_url`「试用链接」（注册产物，活跃功能）、Plus 试用检测
（`trial_eligible`，是另一个功能，与账号状态无关）。

读侧兜底（见 docs/MAINTENANCE.md §6）：`AccountStatus.normalize` 把已删除的
值归一成 registered；启动迁移把历史行归一、把 trial_end_time 列删掉。
"""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.base_platform import Account, AccountStatus  # noqa: E402
from core.db.models_account import AccountModel  # noqa: E402

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def _fresh_engine(tmp: str, name: str):
    """建一个只含账号表的新库（与生产 create_all 同源）。"""
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel

    from core.db.models_account import ACCOUNT_TABLES

    engine = create_engine(f"sqlite:///{Path(tmp) / name}")
    SQLModel.metadata.create_all(engine, tables=ACCOUNT_TABLES)
    return engine


def _insert_legacy_row(engine, email: str, status: str) -> None:
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO accounts (platform, email, password, user_id, region, "
            "token, status, cashier_url, extra_json, created_at, updated_at) "
            "VALUES ('chatgpt', ?, 'p', '', '', '', ?, '', '{}', "
            "'2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (email, status),
        )


class AccountStatusValuesTests(unittest.TestCase):
    """枚举只保留四个状态。"""

    def test_exactly_four_statuses_remain(self):
        self.assertEqual(
            AccountStatus.values(),
            {"registered", "expired", "invalid", "banned"},
            "状态集合应为 registered/expired/invalid/banned —— trial 与 subscribed 已删除",
        )

    def test_active_values_only_registered(self):
        """「还能用」的白名单只剩 registered（另三个都是失效态）。"""
        self.assertEqual(AccountStatus.active_values(), {"registered"})

    def test_is_active_accepts_registered_and_rejects_removed(self):
        self.assertTrue(AccountStatus.is_active("registered"))
        for gone in ("trial", "subscribed", "expired", "invalid", "banned"):
            self.assertFalse(AccountStatus.is_active(gone), gone)

    def test_removed_members_are_gone(self):
        """枚举里不能再有 TRIAL / SUBSCRIBED 成员。"""
        self.assertFalse(hasattr(AccountStatus, "TRIAL"), "TRIAL 成员应已删除")
        self.assertFalse(hasattr(AccountStatus, "SUBSCRIBED"), "SUBSCRIBED 成员应已删除")


class NormalizeRemovedStatusTests(unittest.TestCase):
    """读侧兜底：已删除的值归一成 registered。"""

    def test_removed_values_normalize_to_registered(self):
        for gone in ("trial", "subscribed", "TRIAL", "Subscribed", " trial "):
            self.assertEqual(
                AccountStatus.normalize(gone),
                "registered",
                f"已删除状态 {gone!r} 应归一成 registered",
            )

    def test_kept_values_pass_through(self):
        for kept in ("registered", "expired", "invalid", "banned"):
            self.assertEqual(AccountStatus.normalize(kept), kept)

    def test_empty_value_defaults_to_registered(self):
        for empty in ("", "   ", None):
            self.assertEqual(AccountStatus.normalize(empty), "registered")

    def test_unknown_values_are_left_alone(self):
        """枚举外的历史值不在这里猜 —— 只有已删除的两个值被归一。"""
        for other in ("active", "some-legacy-value"):
            self.assertEqual(AccountStatus.normalize(other), other)


class DeadTrialSurfaceTests(unittest.TestCase):
    """随「试用」状态退场的死面：列 / 字段 / 钩子。"""

    def test_account_model_has_no_trial_end_time_column(self):
        self.assertNotIn(
            "trial_end_time",
            AccountModel.__table__.columns,
            "trial_end_time 列应随试用状态一起删除",
        )

    def test_account_dataclass_has_no_trial_end_time_field(self):
        names = {f.name for f in dataclasses.fields(Account)}
        self.assertNotIn("trial_end_time", names)

    def test_get_trial_url_hook_is_gone(self):
        """`get_trial_url` 零调用零实现（初始版本带来后就没人用过）。"""
        from core.base_platform import BasePlatform

        self.assertFalse(
            hasattr(BasePlatform, "get_trial_url"),
            "get_trial_url 是死钩子，随试用功能一起删除",
        )


class SchedulerTrialCheckRemovedTests(unittest.TestCase):
    """调度器不再有 trial 到期检查（状态删除后它永远空转）。"""

    def test_check_trial_expiry_is_gone(self):
        from core.scheduler import Scheduler

        self.assertFalse(
            hasattr(Scheduler, "check_trial_expiry"),
            "check_trial_expiry 的职责是把 trial → expired，trial 删除后应整块移除",
        )

    def test_loop_has_no_trial_timer(self):
        """调度器实例上不再有 trial 检查的计时器字段，源码里也没有对应引用。

        断言计时器**字段名**（而不是方法名）—— 方法名允许出现在「已删除」
        的说明注释里（那是刻意的迁移记录），字段名只存在于可执行代码中。
        """
        from core import scheduler as mod
        from core.scheduler import Scheduler

        instance = Scheduler()
        for gone in ("_trial_check_interval_seconds", "_last_trial_check_at"):
            self.assertFalse(hasattr(instance, gone), f"Scheduler 实例还留着 {gone}")

        src = Path(mod.__file__).read_text(encoding="utf-8")
        for gone in ("_trial_check_interval_seconds", "_last_trial_check_at"):
            self.assertNotIn(gone, src, f"调度器源码里还引用 {gone}")

    def test_scheduler_still_starts_and_stops(self):
        """删除后调度器本身照常工作（生命周期不受影响）。"""
        import time

        from core.scheduler import Scheduler

        scheduler = Scheduler()
        scheduler._loop_interval_seconds = 1
        try:
            scheduler.start()
            time.sleep(0.2)
            self.assertTrue(scheduler._running)
        finally:
            scheduler.stop()


class MigrationNormalizesRemovedStatusTests(unittest.TestCase):
    """启动迁移把历史行里的已删状态归一（老库升级不残留）。"""

    def test_legacy_rows_are_normalized(self):
        import tempfile

        from sqlmodel import Session, select

        from core.db.migrations import _normalize_removed_account_statuses

        with tempfile.TemporaryDirectory() as tmp:
            engine = _fresh_engine(tmp, "legacy-status.db")
            _insert_legacy_row(engine, "a@x.ai", "trial")
            _insert_legacy_row(engine, "b@x.ai", "subscribed")
            _insert_legacy_row(engine, "c@x.ai", "registered")
            _insert_legacy_row(engine, "d@x.ai", "invalid")

            _normalize_removed_account_statuses(engine)

            with Session(engine) as session:
                rows = session.exec(select(AccountModel).order_by(AccountModel.email)).all()
            statuses = {row.email: row.status for row in rows}
            self.assertEqual(statuses["a@x.ai"], "registered", "trial 行应归一成 registered")
            self.assertEqual(statuses["b@x.ai"], "registered", "subscribed 行应归一成 registered")
            self.assertEqual(statuses["c@x.ai"], "registered", "保留值不动")
            self.assertEqual(statuses["d@x.ai"], "invalid", "保留值不动")

    def test_migration_is_idempotent(self):
        """迁移可重复执行（每次启动都会跑）。"""
        import tempfile

        from sqlmodel import Session, select

        from core.db.migrations import _normalize_removed_account_statuses

        with tempfile.TemporaryDirectory() as tmp:
            engine = _fresh_engine(tmp, "idem-status.db")
            _insert_legacy_row(engine, "a@x.ai", "trial")
            for _ in range(3):
                _normalize_removed_account_statuses(engine)
            with Session(engine) as session:
                rows = session.exec(select(AccountModel)).all()
            self.assertEqual([row.status for row in rows], ["registered"])

    def test_wired_into_startup_migrations(self):
        """归一迁移必须接进启动链 —— 函数写好了但没接线等于没做。"""
        import inspect

        from core.db.migrations import _run_one

        src = inspect.getsource(_run_one)
        self.assertIn(
            "_normalize_removed_account_statuses(engine)",
            src,
            "_run_one 没有调用 _normalize_removed_account_statuses —— 老库不会归一",
        )


class TrialEndTimeColumnDropTests(unittest.TestCase):
    """老库的 trial_end_time 列由启动迁移删除。"""

    def test_column_is_dropped_from_legacy_db(self):
        import tempfile

        from core.db.migrations import _drop_trial_end_time_column

        with tempfile.TemporaryDirectory() as tmp:
            engine = _fresh_engine(tmp, "legacy-column.db")
            # 模拟老库：新表建好后手工把列加回来（老版本模型有这一列）
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "ALTER TABLE accounts ADD COLUMN trial_end_time INTEGER NOT NULL DEFAULT 0"
                )

            _drop_trial_end_time_column(engine)

            with engine.begin() as conn:
                cols = {
                    str(row[1])
                    for row in conn.exec_driver_sql("PRAGMA table_info('accounts')").fetchall()
                }
            self.assertNotIn("trial_end_time", cols, "老库的 trial_end_time 列应被删除")

    def test_noop_on_fresh_db_and_idempotent(self):
        """新库本来就没有这列；重复执行不报错。"""
        import tempfile

        from core.db.migrations import _drop_trial_end_time_column

        with tempfile.TemporaryDirectory() as tmp:
            engine = _fresh_engine(tmp, "fresh-column.db")
            for _ in range(3):
                _drop_trial_end_time_column(engine)
            with engine.begin() as conn:
                cols = {
                    str(row[1])
                    for row in conn.exec_driver_sql("PRAGMA table_info('accounts')").fetchall()
                }
            self.assertNotIn("trial_end_time", cols)

    def test_wired_into_startup_migrations(self):
        import inspect

        from core.db.migrations import _run_one

        src = inspect.getsource(_run_one)
        self.assertIn(
            "_drop_trial_end_time_column(engine)",
            src,
            "_run_one 没有调用 _drop_trial_end_time_column —— 老库的列不会删",
        )


class WritePathNormalizationTests(unittest.TestCase):
    """写侧兜底：旧客户端提交已删除的值 → 落库归一成 registered。"""

    def test_repository_upsert_normalizes(self):
        from core.db import account_repository

        model = AccountModel(
            platform="chatgpt", email="upsert-norm@example.com",
            password="p", status="trial",
        )
        row = account_repository.upsert(model)
        try:
            self.assertEqual(row.status, "registered", "upsert 应把 trial 归一成 registered")
            fresh = account_repository.find_by_email("chatgpt", "upsert-norm@example.com")
            self.assertEqual(fresh.status, "registered", "读回也应是归一后的值")
        finally:
            account_repository.delete(row.id, "chatgpt")


class WritePathNormalizationApiTests(unittest.TestCase):
    """API 边界：PATCH / POST 提交已删除的值同样归一。"""

    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlmodel import Session, delete

        from api.accounts import router
        from core.db import AccountModel, engine

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add(AccountModel(
                platform="chatgpt", email="norm-me@example.com",
                password="pw", status="registered",
            ))
            session.commit()

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app, raise_server_exceptions=False)
        self._engine = engine

    def _stored_status(self, email: str) -> str:
        from sqlmodel import Session, select

        with Session(self._engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == email)
            ).first()
            return row.status if row else ""

    def test_patch_normalizes_removed_status(self):
        from sqlmodel import Session, select

        with Session(self._engine) as session:
            account_id = session.exec(
                select(AccountModel).where(AccountModel.email == "norm-me@example.com")
            ).first().id

        response = self.client.patch(
            f"/accounts/{account_id}?platform=chatgpt",
            json={"status": "trial"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._stored_status("norm-me@example.com"), "registered")

    def test_create_normalizes_removed_status(self):
        response = self.client.post(
            "/accounts",
            json={
                "platform": "chatgpt",
                "email": "norm-new@example.com",
                "password": "pw",
                "status": "subscribed",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self._stored_status("norm-new@example.com"), "registered")


class FrontendStatusRemovalTests(unittest.TestCase):
    """前端不再出现「试用中 / 已订阅」这两个状态（源码扫描契约）。"""

    def _src(self, rel: str) -> str:
        return (FRONTEND / "src" / rel).read_text(encoding="utf-8")

    def test_dashboard_has_no_removed_statuses(self):
        src = self._src("pages/Dashboard.tsx")
        self.assertNotIn("试用中", src, "仪表盘还有「试用中」卡片/标签")
        self.assertNotIn("已订阅", src, "仪表盘还有「已订阅」卡片/标签")
        self.assertNotIn("trial", src, "仪表盘还引用 trial 状态")
        self.assertNotIn("subscribed", src, "仪表盘还引用 subscribed 状态")

    def test_accounts_page_has_no_removed_statuses(self):
        src = self._src("pages/Accounts.tsx")
        self.assertNotIn("试用中", src, "账号页还有「试用中」选项")
        self.assertNotIn("已订阅", src, "账号页还有「已订阅」选项")
        self.assertNotIn("subscribed", src, "账号页还引用 subscribed 状态")
        # 状态值是带引号的字符串 —— 只查 `'trial'`，不误伤 Plus 试用检测
        # （`plusTrialMeta` / `check_plus_trial` / `trial_eligible` 是另一个功能）。
        self.assertNotIn("'trial'", src, "账号页还有 trial 状态值")

    def test_dashboard_shows_registered_card_instead(self):
        """删掉的两张卡由「已注册」替代（保留总数 / 好 / 坏三档结构）。"""
        src = self._src("pages/Dashboard.tsx")
        self.assertIn("title: '已注册'", src)
        self.assertIn("by_status?.registered", src)

    def test_add_modal_has_no_status_field(self):
        """新增弹窗不再有状态字段 —— 可选项只剩 registered 一个，字段是噪声。"""
        src = self._src("pages/Accounts.tsx")
        block = src.split('title="手动新增账号"', 1)[1].split("</Modal>", 1)[0]
        self.assertNotIn(
            'name="status"', block,
            "新增弹窗的状态字段应删除（唯一可选项就是默认值）",
        )

    def test_detail_modal_keeps_all_four_status_options(self):
        """详情弹窗仍可改状态，且只给保留的四个值。"""
        src = self._src("pages/Accounts.tsx")
        self.assertEqual(
            src.count('name="status"'), 1,
            "状态字段应只剩详情弹窗一处",
        )
        for kept in ("'registered'", "'expired'", "'invalid'", "'banned'"):
            self.assertIn(
                f"value: {kept}", src,
                f"详情/筛选的选项里缺少保留值 {kept}",
            )


if __name__ == "__main__":
    unittest.main()
