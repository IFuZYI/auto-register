"""跨平台邮箱复用：同一个邮箱注册了 ChatGPT 还能注册 Grok。

用户要求：「邮箱的不同平台是否注册要确保是分开的，比如一个邮箱注册了 gpt
还能注册 grok。」

分两层，这两层的行为必须一致：
  ① **账号层**（`accounts` 表）：唯一索引是 `(platform, email)`，判重带平台
     过滤 —— 同一邮箱在两个平台各有一个账号是合法的。
  ② **邮箱号池**（iCloud 别名 / Outlook 池）：取号时只能跳过「**本平台**
     已用过」的地址，不能因为别的平台用过就一起锁死。

这一层此前是全局锁：iCloud 池 22 个 `used` 别名里 13 个 grok 从没用过，
却全都领不到；只剩 2 个 available，很快就要撞 Apple 每小时 5 个的生成额度。
"""

from __future__ import annotations

import pytest
from sqlmodel import SQLModel, Session, create_engine


@pytest.fixture
def icloud_db(tmp_path, monkeypatch):
    import core.db as db_module
    from core.db import platform_database_registry

    engine = create_engine(f"sqlite:///{tmp_path / 'icloud.db'}")
    SQLModel.metadata.create_all(engine)
    platform_database_registry.configure({"icloud": str(engine.url)})
    monkeypatch.setattr(db_module, "engine", engine)
    return engine


def _seed(engine, *, address: str, pool_status: str, used_platforms: str = "") -> int:
    from core.db.models_platform import ICloudAccountModel, ICloudAliasModel

    with Session(engine) as session:
        account = session.exec(
            __import__("sqlmodel").select(ICloudAccountModel).where(
                ICloudAccountModel.email == "owner@icloud.com"
            )
        ).first()
        if account is None:
            account = ICloudAccountModel(email="owner@icloud.com", enabled=True)
            session.add(account)
            session.commit()
            session.refresh(account)
        alias = ICloudAliasModel(
            account_id=account.id,
            address=address,
            status="active",
            pool_status=pool_status,
            used_platforms=used_platforms,
        )
        session.add(alias)
        session.commit()
        session.refresh(alias)
        return int(alias.id)


def _claim_blocked(monkeypatch, platform: str):
    """跑一次 claim，返回拿到的地址（池空时返回 None）。"""
    from services import icloud_service

    def _no_generate(*args, **kwargs):
        raise RuntimeError("池里没有可复用的号，走了生成分支")

    monkeypatch.setattr(icloud_service, "generate_alias", _no_generate)
    try:
        return icloud_service.claim_alias(1, platform=platform)["address"]
    except RuntimeError:
        return None


class TestCrossPlatformReuse:
    """被 A 平台用过的地址，B 平台还能领到。"""

    def test_used_by_other_platform_is_reusable(self, icloud_db, monkeypatch):
        """核心诉求：chatgpt 用过的别名，grok 还能领。"""
        _seed(
            icloud_db,
            address="shared@icloud.com",
            pool_status="used",
            used_platforms=",chatgpt,",
        )
        assert _claim_blocked(monkeypatch, "grok") == "shared@icloud.com", (
            "被 chatgpt 用过的地址必须还能给 grok —— 两个平台的账号体系互不相干"
        )

    def test_used_by_same_platform_is_not_reusable(self, icloud_db, monkeypatch):
        """同一个平台不能重复领已经注册过的地址（否则必然撞「邮箱已被占用」）。"""
        _seed(
            icloud_db,
            address="taken@icloud.com",
            pool_status="used",
            used_platforms=",grok,",
        )
        assert _claim_blocked(monkeypatch, "grok") is None

    def test_used_by_both_platforms_is_exhausted(self, icloud_db, monkeypatch):
        """两个平台都用过了 → 对谁都不能再发。"""
        _seed(
            icloud_db,
            address="spent@icloud.com",
            pool_status="used",
            used_platforms=",chatgpt,grok,",
        )
        assert _claim_blocked(monkeypatch, "grok") is None
        assert _claim_blocked(monkeypatch, "chatgpt") is None

    def test_available_still_wins_over_reusable_used(self, icloud_db, monkeypatch):
        """`available` 优先于「别的平台用过的 used」—— 全新地址先发。"""
        fresh = _seed(icloud_db, address="fresh@icloud.com", pool_status="available")
        _seed(
            icloud_db,
            address="reusable@icloud.com",
            pool_status="used",
            used_platforms=",chatgpt,",
        )
        assert _claim_blocked(monkeypatch, "grok") == "fresh@icloud.com"

    def test_unknown_platform_falls_back_to_global_lock(self, icloud_db, monkeypatch):
        """拿不到平台名时保守处理：`used` 一律不领（旧行为）。

        宁可多生成一个地址，也不要把已经注册过的地址再发给同一个平台。
        """
        _seed(
            icloud_db,
            address="any@icloud.com",
            pool_status="used",
            used_platforms=",chatgpt,",
        )
        assert _claim_blocked(monkeypatch, "") is None


class TestAccountTableEvidence:
    """取号前**必须**和 `accounts` 表比对：确保没注册才能开始注册。

    用户要求：「邮箱应该有标记是否注册某个平台的，先判断，再和平台已注册账号里
    进行比对，确保没注册才能开始注册，避免重复注册。」

    回归背景（实测）：池里两个别名 `quart.overdub-4e@icloud.com` /
    `ouster.donut-9b@icloud.com` 的 `used_platforms` 记着 `,chatgpt,`，
    但 `pool_status` 被人工放回了 `available`。只信池子记账时，chatgpt 的下一个
    任务会把它们再领出来 —— 而 `accounts` 表里明明已经有这两个地址的 chatgpt
    账号，注册必然撞「邮箱已被占用」白跑一轮。
    """

    def _seed_account(self, engine, *, platform: str, email: str) -> None:
        from core.db.models_account import AccountModel

        with Session(engine) as session:
            session.add(
                AccountModel(platform=platform, email=email.lower(), password="x")
            )
            session.commit()

    def test_claim_skips_address_already_registered_on_this_platform(
        self, icloud_db, monkeypatch
    ):
        from services import icloud_service

        _seed(
            icloud_db,
            address="taken@icloud.com",
            pool_status="available",
            used_platforms=",chatgpt,",
        )
        self._seed_account(icloud_db, platform="chatgpt", email="taken@icloud.com")

        assert _claim_blocked(monkeypatch, "chatgpt") is None, (
            "accounts 表里已有该地址的 chatgpt 账号，不能再领给 chatgpt"
        )

    def test_claim_still_offers_it_to_another_platform(self, icloud_db, monkeypatch):
        """本平台注册过 ≠ 对所有平台锁死：grok 还能领（邮箱按平台一次性）。"""
        from services import icloud_service

        _seed(icloud_db, address="shared2@icloud.com", pool_status="available")
        self._seed_account(icloud_db, platform="chatgpt", email="shared2@icloud.com")

        assert _claim_blocked(monkeypatch, "grok") == "shared2@icloud.com"

    def test_claim_blocks_even_when_bookkeeping_says_nothing(
        self, icloud_db, monkeypatch
    ):
        """池里记账是空的，但 `accounts` 表有记录 —— 也要挡住。

        这条正是「先判断，再和平台已注册账号里进行比对」的落点：池子记账可能
        因为任务中途崩掉而缺项，只有账号表才是权威证据。
        """
        _seed(icloud_db, address="evidence2@icloud.com", pool_status="available")
        self._seed_account(icloud_db, platform="grok", email="evidence2@icloud.com")

        assert _claim_blocked(monkeypatch, "grok") is None

    def test_claim_backfills_bookkeeping_when_evidence_is_found(
        self, icloud_db, monkeypatch
    ):
        """比对命中时顺手把 `used_platforms` 补上 —— 下次一眼可见。"""
        from core.db.models_platform import ICloudAliasModel
        from services import icloud_service

        alias_id = _seed(icloud_db, address="backfill@icloud.com", pool_status="available")
        self._seed_account(icloud_db, platform="grok", email="backfill@icloud.com")

        _claim_blocked(monkeypatch, "grok")

        with Session(icloud_db) as session:
            row = session.get(ICloudAliasModel, alias_id)
            assert row is not None
            assert row.used_platforms == ",grok,", (
                f"命中账号表证据时要把记账补上，实际是 {row.used_platforms!r}"
            )

    def test_list_aliases_exposes_registered_platforms(self, icloud_db):
        """列表接口要带上权威证据 —— 界面按它显示「已注册平台」并做平台筛选。"""
        from services import icloud_service

        _seed(icloud_db, address="listed@icloud.com", pool_status="used")
        self._seed_account(icloud_db, platform="chatgpt", email="listed@icloud.com")
        self._seed_account(icloud_db, platform="grok", email="listed@icloud.com")

        rows = icloud_service.list_aliases()
        row = next(r for r in rows if r["address"] == "listed@icloud.com")
        assert row["registered_platforms"] == ["chatgpt", "grok"]


class TestUsedPlatformsBookkeeping:
    """`used_platforms` 的读写语义。"""

    def test_mark_alias_used_records_the_platform(self, icloud_db):
        from services import icloud_service

        alias_id = _seed(icloud_db, address="a@icloud.com", pool_status="in_use")
        icloud_service.mark_alias_used(alias_id, platform="grok")

        with Session(icloud_db) as session:
            from core.db.models_platform import ICloudAliasModel

            row = session.get(ICloudAliasModel, alias_id)
            assert row.pool_status == "used"
            assert row.used_platforms == ",grok,"

    def test_two_platforms_accumulate(self, icloud_db):
        """两个平台先后用掉同一个地址 → 两个名字都记上。"""
        from services import icloud_service

        alias_id = _seed(icloud_db, address="b@icloud.com", pool_status="in_use")
        icloud_service.mark_alias_used(alias_id, platform="grok")
        icloud_service.mark_alias_used(alias_id, platform="chatgpt")

        with Session(icloud_db) as session:
            from core.db.models_platform import ICloudAliasModel

            row = session.get(ICloudAliasModel, alias_id)
            assert row.used_platforms == ",grok,chatgpt,"

    def test_marking_same_platform_twice_is_idempotent(self, icloud_db):
        from services import icloud_service

        alias_id = _seed(icloud_db, address="c@icloud.com", pool_status="in_use")
        icloud_service.mark_alias_used(alias_id, platform="grok")
        icloud_service.mark_alias_used(alias_id, platform="grok")

        with Session(icloud_db) as session:
            from core.db.models_platform import ICloudAliasModel

            row = session.get(ICloudAliasModel, alias_id)
            assert row.used_platforms == ",grok,"

    def test_used_platforms_surfaces_in_the_api_dict(self, icloud_db):
        """界面要看「谁用过」，别名列表必须带这个字段。"""
        from services import icloud_service

        _seed(
            icloud_db,
            address="d@icloud.com",
            pool_status="used",
            used_platforms=",grok,",
        )
        rows = icloud_service.list_aliases()
        row = next(r for r in rows if r["address"] == "d@icloud.com")
        assert row["used_platforms"] == ",grok,"


class TestHelperSemantics:
    """`mark_email_used_by` / `email_used_by_platform` 的边界。"""

    def test_substring_platform_does_not_match(self):
        """`,grok,` 不能匹配 `grok2` —— 首尾逗号就是为此。"""
        from core.db import email_used_by_platform, mark_email_used_by

        field = mark_email_used_by("", "grok")
        assert email_used_by_platform(field, "grok") is True
        assert email_used_by_platform(field, "grok2") is False

    def test_case_insensitive(self):
        from core.db import email_used_by_platform, mark_email_used_by

        field = mark_email_used_by("", "Grok")
        assert email_used_by_platform(field, "grok") is True
        assert field == ",grok,"

    def test_empty_platform_is_a_noop(self):
        """没有平台名时不能瞎记 —— 记错会把地址错误地锁给某个平台。"""
        from core.db import mark_email_used_by

        assert mark_email_used_by(",grok,", "") == ",grok,"
        assert mark_email_used_by("", "") == ""

    def test_empty_field_means_nobody_used_it(self):
        from core.db import email_used_by_platform

        assert email_used_by_platform("", "grok") is False
