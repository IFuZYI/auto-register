"""iCloud 隐私邮箱的号池语义。

与 Outlook 号池（`core/mailboxes/channels/outlook/`）对齐：别名不只是「生成过」，
还有「被注册任务领过没有」的记账。这里验证三件事：

  1. `claim_alias` 优先复用池里 `available` 的，而不是每次都向 Apple 新生成
     （生成额度每小时只有 5 个，复用能省下来）
  2. 领取会把状态推到 `in_use`，避免两个任务领到同一个地址
  3. `mark_alias_used` / `release_alias_claim` 的流转，以及
     `set_alias_pool_status` 对非法取值的拒绝

用一个真的 SQLite 库（不是 mock）跑，因为要验证的正是「状态真的落库了」。
"""

from __future__ import annotations

import pytest
from sqlmodel import SQLModel, Session, create_engine

from platforms.icloud import (
    POOL_STATUS_AVAILABLE,
    POOL_STATUS_IN_USE,
    POOL_STATUS_USED,
)


@pytest.fixture
def icloud_db(tmp_path, monkeypatch):
    """把 iCloud 分库指到临时文件，并建好表。"""
    import core.db as db_module
    from core.db import platform_database_registry

    engine = create_engine(f"sqlite:///{tmp_path / 'icloud.db'}")
    SQLModel.metadata.create_all(engine)
    platform_database_registry.configure({"icloud": str(engine.url)})
    monkeypatch.setattr(db_module, "engine", engine)
    return engine


def _seed_alias(engine, *, address: str, pool_status: str, status: str = "active") -> int:
    from sqlmodel import select as sm_select

    from core.db.models_platform import ICloudAccountModel, ICloudAliasModel

    with Session(engine) as session:
        # 主号邮箱有唯一约束：多次 seed 要复用同一个，不能重复插入
        account = session.exec(
            sm_select(ICloudAccountModel).where(ICloudAccountModel.email == "owner@icloud.com")
        ).first()
        if account is None:
            account = ICloudAccountModel(email="owner@icloud.com", enabled=True)
            session.add(account)
            session.commit()
            session.refresh(account)

        alias = ICloudAliasModel(
            account_id=account.id,
            address=address,
            status=status,
            pool_status=pool_status,
        )
        session.add(alias)
        session.commit()
        session.refresh(alias)
        return int(alias.id)


def test_pool_summary_counts_each_status(icloud_db):
    from services import icloud_service

    _seed_alias(icloud_db, address="a@icloud.com", pool_status=POOL_STATUS_AVAILABLE)
    _seed_alias(icloud_db, address="b@icloud.com", pool_status=POOL_STATUS_IN_USE)
    _seed_alias(icloud_db, address="c@icloud.com", pool_status=POOL_STATUS_USED)
    _seed_alias(icloud_db, address="d@icloud.com", pool_status=POOL_STATUS_USED)

    summary = icloud_service.pool_summary()

    assert summary[POOL_STATUS_AVAILABLE] == 1
    assert summary[POOL_STATUS_IN_USE] == 1
    assert summary[POOL_STATUS_USED] == 2
    assert summary["total"] == 4


def test_claim_prefers_an_available_alias_over_generating(icloud_db):
    """池里有闲号时必须复用 —— 这是省下 Apple 生成额度的关键。

    没有这条保证，`get_email()` 会每次都新生成，5 个/小时一满就报限流，
    哪怕池里躺着一堆没用过的地址。
    """
    from services import icloud_service

    alias_id = _seed_alias(icloud_db, address="idle@icloud.com", pool_status=POOL_STATUS_AVAILABLE)

    generated: list[str] = []

    def _fail_if_called(*args, **kwargs):
        generated.append("called")
        raise AssertionError("池里有未使用的地址，不该去调 Apple 生成")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(icloud_service, "generate_alias", _fail_if_called)
        claimed = icloud_service.claim_alias(1)

    assert claimed["address"] == "idle@icloud.com"
    assert claimed["pool_status"] == POOL_STATUS_IN_USE
    assert generated == []

    # 状态真的落库了（不是只在返回值里）
    with Session(icloud_db) as session:
        from core.db.models_platform import ICloudAliasModel

        row = session.get(ICloudAliasModel, alias_id)
        assert row is not None
        assert row.pool_status == POOL_STATUS_IN_USE


def test_second_claim_does_not_take_the_same_alias(icloud_db):
    """领取要独占：两个任务不能拿到同一个地址。"""
    from services import icloud_service

    _seed_alias(icloud_db, address="one@icloud.com", pool_status=POOL_STATUS_AVAILABLE)

    first = icloud_service.claim_alias(1)

    # 池子空了，第二次会去生成 —— 让生成抛错，证明它确实没再捞到同一个
    with pytest.MonkeyPatch.context() as mp:
        def _raise(*args, **kwargs):
            raise RuntimeError("池子空了，走生成路径（预期）")

        mp.setattr(icloud_service, "generate_alias", _raise)
        with pytest.raises(RuntimeError, match="池子空了"):
            icloud_service.claim_alias(1)

    assert first["address"] == "one@icloud.com"


def test_claim_skips_disabled_aliases(icloud_db):
    """停用的别名不该被领走 —— 它收不到信，注册会卡在等验证码。"""
    from services import icloud_service

    _seed_alias(
        icloud_db,
        address="off@icloud.com",
        pool_status=POOL_STATUS_AVAILABLE,
        status="disabled",
    )

    with pytest.MonkeyPatch.context() as mp:
        def _raise(*args, **kwargs):
            raise RuntimeError("只有停用的地址，走生成路径（预期）")

        mp.setattr(icloud_service, "generate_alias", _raise)
        with pytest.raises(RuntimeError, match="只有停用的"):
            icloud_service.claim_alias(1)


def test_mark_used_then_release_round_trip(icloud_db):
    from services import icloud_service

    alias_id = _seed_alias(icloud_db, address="cycle@icloud.com", pool_status=POOL_STATUS_AVAILABLE)
    icloud_service.claim_alias(1)

    icloud_service.mark_alias_used(alias_id)
    assert icloud_service.pool_summary()[POOL_STATUS_USED] == 1

    # 放回池子后又能被领到（人工改回 available 的场景）
    icloud_service.set_alias_pool_status(alias_id, POOL_STATUS_AVAILABLE)
    assert icloud_service.pool_summary()[POOL_STATUS_AVAILABLE] == 1

    claimed = icloud_service.claim_alias(1)
    assert claimed["address"] == "cycle@icloud.com"
    assert claimed["pool_status"] == POOL_STATUS_IN_USE


def test_release_claim_only_touches_in_use(icloud_db):
    """`used` 是落定状态，不该被「释放未完成领取」顺手改回 available。"""
    from services import icloud_service

    alias_id = _seed_alias(icloud_db, address="done@icloud.com", pool_status=POOL_STATUS_USED)

    icloud_service.release_alias_claim(alias_id)

    assert icloud_service.pool_summary()[POOL_STATUS_USED] == 1


def test_set_pool_status_rejects_unknown_value(icloud_db):
    from services import icloud_service
    from platforms.icloud import ICloudError

    alias_id = _seed_alias(icloud_db, address="x@icloud.com", pool_status=POOL_STATUS_AVAILABLE)

    with pytest.raises(ICloudError) as ctx:
        icloud_service.set_alias_pool_status(alias_id, "nonsense")

    assert ctx.value.code == "invalid_pool_status"


def test_claim_uses_the_configured_account_only(icloud_db):
    """多主号时取号只碰指定的那个主号，不能串号。"""
    from services import icloud_service
    from core.db.models_platform import ICloudAccountModel, ICloudAliasModel

    # 主号 1 有一个闲号，主号 2 没有
    _seed_alias(icloud_db, address="owner1@icloud.com", pool_status=POOL_STATUS_AVAILABLE)
    with Session(icloud_db) as session:
        second = ICloudAccountModel(email="second@icloud.com", enabled=True)
        session.add(second)
        session.commit()

    with pytest.MonkeyPatch.context() as mp:
        def _raise(*args, **kwargs):
            raise RuntimeError("主号 2 池子空，走生成路径（预期）")

        mp.setattr(icloud_service, "generate_alias", _raise)
        with pytest.raises(RuntimeError, match="主号 2"):
            icloud_service.claim_alias(2)


def test_claim_does_not_deadlock_on_empty_pool(icloud_db):
    """池子捞空时 `claim_alias` 必须能走完生成分支，不能自锁。

    回归测试：主号锁一度是 `threading.Lock`，而 `claim_alias` 在持锁状态下
    调 `generate_alias`（后者自己也要拿同一把锁）—— 池子一空就永久阻塞，
    任务卡在建邮箱这一步且不报错。锁改 `RLock` 后同线程可重入。

    用一个**真的** `generate_alias`（不是 mock）才能覆盖到那次加锁；
    这里让它抛错即可，重点是「调用返回了」而不是「返回了什么」。
    """
    import threading

    from services import icloud_service

    # 池里没有任何 available 别名 → 必然走生成分支
    _seed_alias(icloud_db, address="busy@icloud.com", pool_status=POOL_STATUS_USED)

    done = threading.Event()
    errors: list[BaseException] = []

    def call():
        try:
            icloud_service.claim_alias(1)
        except BaseException as exc:      # noqa: BLE001 - 记录后交给主线程断言
            errors.append(exc)
        finally:
            done.set()

    t = threading.Thread(target=call, daemon=True)
    t.start()

    assert done.wait(timeout=10), (
        "claim_alias 在空池上超时未返回 —— 锁又变回不可重入了？"
        "（services/icloud_service.py 的 _account_lock 必须是 RLock）"
    )
    # 走到生成分支后，凭据是空的/假的，必然报错 —— 但必须「报错」而不是「卡住」
    assert errors, "预期生成分支会抛错（主号凭据不可用），但没有异常"


def test_release_stale_claims_returns_abandoned_aliases(icloud_db):
    """领了但超时未落定的 `in_use` 号会被放回 `available`。

    没有这层兜底的话，任务中途崩掉会让号永远停在 `in_use`，池子只出不进。
    """
    from datetime import timedelta

    from sqlmodel import select as sm_select

    from core.db.models_platform import ICloudAliasModel
    from services import icloud_service

    stale_id = _seed_alias(icloud_db, address="stale@icloud.com", pool_status=POOL_STATUS_IN_USE)
    fresh_id = _seed_alias(icloud_db, address="fresh@icloud.com", pool_status=POOL_STATUS_IN_USE)

    # 把 stale 那条的 updated_at 拨回 3 小时前（超过默认 2 小时阈值）
    with Session(icloud_db) as session:
        row = session.exec(
            sm_select(ICloudAliasModel).where(ICloudAliasModel.id == stale_id)
        ).first()
        assert row is not None
        row.updated_at = icloud_service._utcnow() - timedelta(hours=3)
        session.add(row)
        session.commit()

    released = icloud_service.release_stale_claims()

    assert released == 1, "只有超时那条该被放回"
    summary = icloud_service.pool_summary()
    assert summary[POOL_STATUS_AVAILABLE] == 1
    assert summary[POOL_STATUS_IN_USE] == 1, "未超时的那条必须留着（可能还在跑）"

    # 放回的号要真的能被再领到
    claimed = icloud_service.claim_alias(1)
    assert claimed["address"] == "stale@icloud.com"
    assert claimed["pool_status"] == POOL_STATUS_IN_USE

    # fresh 那条不受影响
    with Session(icloud_db) as session:
        row = session.exec(
            sm_select(ICloudAliasModel).where(ICloudAliasModel.id == fresh_id)
        ).first()
        assert row is not None and row.pool_status == POOL_STATUS_IN_USE


def test_release_stale_claims_does_not_overwrite_a_concurrent_status_change(icloud_db):
    """回收必须是条件 UPDATE —— 不能把并发改过的状态静默覆盖回 available。

    场景：某条 `in_use` 已经超时，但就在回收读出来之后、写回之前，有人
    （池页面点「已使用」，或注册流程刚把它标成 `used`）改了它的状态。
    早先的「SELECT 出行 → 改 ORM 对象 → commit」实现会把对方的结果整个
    覆盖掉（把刚标好的 `used` 又打回 `available`）；换成带
    `WHERE pool_status = 'in_use'` 的条件 UPDATE 后，数据库层保证这类
    变更不会被误改。
    """
    from datetime import timedelta

    from sqlmodel import select as sm_select

    from core.db.models_platform import ICloudAliasModel
    from services import icloud_service

    alias_id = _seed_alias(icloud_db, address="racy@icloud.com", pool_status=POOL_STATUS_IN_USE)
    with Session(icloud_db) as session:
        row = session.exec(
            sm_select(ICloudAliasModel).where(ICloudAliasModel.id == alias_id)
        ).first()
        assert row is not None
        row.updated_at = icloud_service._utcnow() - timedelta(hours=3)
        session.add(row)
        session.commit()

    # 模拟「并发变更恰好落在回收的 SELECT 与写回之间」：
    # 状态被改成 used，但 updated_at 仍是超时值（对方改状态时没刷新它，
    # 或刷新发生在回收读出之后）。这正是旧实现会覆盖的窗口 ——
    # 旧实现按 updated_at 选中它，再把整行写回，把 used 打回 available。
    with Session(icloud_db) as session:
        row = session.exec(
            sm_select(ICloudAliasModel).where(ICloudAliasModel.id == alias_id)
        ).first()
        assert row is not None
        row.pool_status = POOL_STATUS_USED
        row.updated_at = icloud_service._utcnow() - timedelta(hours=3)
        session.add(row)
        session.commit()

    released = icloud_service.release_stale_claims()

    assert released == 0, "条件 UPDATE 不该命中已被改成 used 的行"
    with Session(icloud_db) as session:
        row = session.exec(
            sm_select(ICloudAliasModel).where(ICloudAliasModel.id == alias_id)
        ).first()
        assert row is not None
        assert row.pool_status == POOL_STATUS_USED, (
            f"回收覆盖了并发写入的状态：{row.pool_status!r}（应保持 used）"
        )


# ---------------------------------------------------------------------------
# 「选择导入才能入池」——用户要求：账号邮箱要选择导入才能导入邮箱池，
# 不是全部导入邮箱池。
#
# 与 Outlook 号池（tests/test_outlook_pool_selection.py）同一套语义：
# 生成/同步下来的别名默认落 `unpooled`，注册取号跳过；勾选后才转 available。
# ---------------------------------------------------------------------------


def test_new_aliases_land_unpooled_not_available(icloud_db):
    """新建/同步下来的别名默认是「未入池」，不是可直接领取。

    这是用户要求的落点。以前生成出来的别名一律直接 available，等于
    「从 Apple 拉下来多少就自动全部可领」——主号下可能已有历史别名，
    自动全部入池会让注册任务悄悄消耗掉用户本来留着自己用的地址。
    """
    from sqlmodel import select as sm_select

    from core.db.models_platform import ICloudAliasModel

    # 不带 pool_status 建行 —— 走模型默认值，正是生成/同步路径的形状
    with Session(icloud_db) as session:
        row = ICloudAliasModel(account_id=1, address="brandnew@icloud.com")
        session.add(row)
        session.commit()
        session.refresh(row)
        alias_id = int(row.id)

    with Session(icloud_db) as session:
        stored = session.exec(
            sm_select(ICloudAliasModel).where(ICloudAliasModel.id == alias_id)
        ).first()
        assert stored is not None
        assert stored.pool_status == "unpooled", (
            f"新别名落成了 {stored.pool_status!r} —— 应当默认「未入池」，"
            "否则等于绕过「选择导入才能入池」的要求"
        )


def test_claim_skips_unpooled_aliases(icloud_db):
    """未入池的别名不能被注册任务领走，必须走生成分支。"""
    from services import icloud_service

    _seed_alias(icloud_db, address="notyet@icloud.com", pool_status="unpooled")

    with pytest.MonkeyPatch.context() as mp:
        def _raise(*args, **kwargs):
            raise RuntimeError("只有未入池的地址，走生成路径（预期）")

        mp.setattr(icloud_service, "generate_alias", _raise)
        with pytest.raises(RuntimeError, match="只有未入池的地址"):
            icloud_service.claim_alias(1)


def test_import_aliases_to_pool_only_touches_unpooled(icloud_db):
    """勾选入池只动 unpooled：in_use / used 必须原样保留。

    把 `in_use` 改回 `available` 会让同一个号被领两次 —— 两个注册任务互相
    顶掉验证码邮件，表现为「两个任务都收不到码」，而池子看着一切正常。
    """
    from services import icloud_service

    fresh_id = _seed_alias(icloud_db, address="fresh@icloud.com", pool_status="unpooled")
    busy_id = _seed_alias(icloud_db, address="busy@icloud.com", pool_status=POOL_STATUS_IN_USE)
    done_id = _seed_alias(icloud_db, address="done@icloud.com", pool_status=POOL_STATUS_USED)

    result = icloud_service.import_aliases_to_pool([fresh_id, busy_id, done_id])

    # changed/skipped 是 id 列表（前端据此给精确提示），不是计数
    assert result["changed"] == [fresh_id], "只有未入池那条该被改动"
    assert sorted(result["skipped"]) == sorted([busy_id, done_id])
    summary = icloud_service.pool_summary()
    assert summary[POOL_STATUS_AVAILABLE] == 1
    assert summary[POOL_STATUS_IN_USE] == 1, "in_use 被改动了 —— 会被重复领取"
    assert summary[POOL_STATUS_USED] == 1
    assert summary["unpooled"] == 0


def test_import_to_pool_is_idempotent(icloud_db):
    """重复点「导入邮箱池」要安全：第二次改 0 条，不能报错也不能重复计数。"""
    from services import icloud_service

    alias_id = _seed_alias(icloud_db, address="twice@icloud.com", pool_status="unpooled")

    first = icloud_service.import_aliases_to_pool([alias_id])
    second = icloud_service.import_aliases_to_pool([alias_id])

    assert first["changed"] == [alias_id]
    assert second["changed"] == []
    assert second["skipped"] == [alias_id], "第二次该报「跳过」而不是「改了 0 条」"
    assert icloud_service.pool_summary()[POOL_STATUS_AVAILABLE] == 1


def test_unpool_returns_available_aliases_to_unpooled(icloud_db):
    """「移出邮箱池」是导入的反向操作，只动 available。"""
    from services import icloud_service

    avail_id = _seed_alias(icloud_db, address="out@icloud.com", pool_status=POOL_STATUS_AVAILABLE)
    busy_id = _seed_alias(icloud_db, address="keepbusy@icloud.com", pool_status=POOL_STATUS_IN_USE)

    result = icloud_service.unpool_aliases([avail_id, busy_id])

    assert result["changed"] == [avail_id]
    assert result["skipped"] == [busy_id]
    summary = icloud_service.pool_summary()
    assert summary["unpooled"] == 1
    assert summary[POOL_STATUS_IN_USE] == 1, "使用中的不该被移出"


def test_pool_summary_separates_unpooled_from_available(icloud_db):
    """`unpooled` 必须单独计数，不能并进 available。

    并进去的话界面会显示「未使用 N」而注册一个都取不到 —— 用户完全无法
    理解为什么池子里有闲号却报「没有可用邮箱」。
    """
    from services import icloud_service

    _seed_alias(icloud_db, address="pooled@icloud.com", pool_status=POOL_STATUS_AVAILABLE)
    _seed_alias(icloud_db, address="waiting@icloud.com", pool_status="unpooled")

    summary = icloud_service.pool_summary()

    assert summary[POOL_STATUS_AVAILABLE] == 1
    assert summary["unpooled"] == 1
    assert summary["total"] == 2


def test_unpooled_is_not_a_manually_settable_status(icloud_db):
    """`unpooled` 由生成/同步产生，不该能通过 set_alias_pool_status 手工设置。

    `POOL_STATUSES` 是「可手动设置的值」白名单；`unpooled` 不在里面是有意的
    ——入池/出池各有专用函数（import_aliases_to_pool / unpool_aliases），
    它们能带计数与跳过逻辑，比通用 setter 安全。
    """
    from services import icloud_service
    from platforms.icloud import ICloudError

    alias_id = _seed_alias(icloud_db, address="manual@icloud.com", pool_status=POOL_STATUS_AVAILABLE)

    with pytest.raises(ICloudError) as ctx:
        icloud_service.set_alias_pool_status(alias_id, "unpooled")

    assert ctx.value.code == "invalid_pool_status"
