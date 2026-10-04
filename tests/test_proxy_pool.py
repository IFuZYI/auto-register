from sqlmodel import Session, select

from core.db import ProxyModel, engine
from core.proxy_pool import ProxyPool


def test_report_success_matches_legacy_proxy_after_normalization():
    pool = ProxyPool()
    raw = "proxy.example:2000:user-name:secret-pass"
    normalized = "http://user-name:secret-pass@proxy.example:2000"

    with Session(engine) as session:
        proxy = ProxyModel(url=raw, is_active=False)
        session.add(proxy)
        session.commit()

    pool.report_success(normalized)

    with Session(engine) as session:
        proxy = session.exec(select(ProxyModel).where(ProxyModel.url == raw)).one()
        assert proxy.success_count == 1
        assert proxy.fail_count == 0
        assert proxy.is_active is True
        assert proxy.last_checked is not None


def test_report_fail_matches_legacy_proxy_after_normalization():
    pool = ProxyPool()
    raw = "proxy.example:2001:user-name:secret-pass"
    normalized = "http://user-name:secret-pass@proxy.example:2001"

    with Session(engine) as session:
        proxy = ProxyModel(url=raw, is_active=True)
        session.add(proxy)
        session.commit()

    pool.report_fail(normalized)

    with Session(engine) as session:
        proxy = session.exec(select(ProxyModel).where(ProxyModel.url == raw)).one()
        assert proxy.success_count == 0
        assert proxy.fail_count == 1
        assert proxy.is_active is True
        assert proxy.last_checked is not None


def test_report_fail_disables_never_successful_proxy_after_threshold():
    pool = ProxyPool()
    raw = "proxy.example:2002:user-name:secret-pass"

    with Session(engine) as session:
        proxy = ProxyModel(url=raw, is_active=True, fail_count=4)
        session.add(proxy)
        session.commit()

    pool.report_fail(raw)

    with Session(engine) as session:
        proxy = session.exec(select(ProxyModel).where(ProxyModel.url == raw)).one()
        assert proxy.success_count == 0
        assert proxy.fail_count == 5
        assert proxy.is_active is False
