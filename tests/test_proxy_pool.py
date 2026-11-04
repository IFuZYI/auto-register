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


def test_balance_settings_raises_when_config_symbol_is_missing():
    """`core.config_store` 里没有 `config_store` 符号时必须立刻抛 ImportError。

    原先 `from core.config_store import config_store` 写在宽 `try` 里，
    符号消失会被吞成默认值 `(True, 0)` —— 与仓库其它位置「数据源函数不存在
    却被宽 except 吞掉」是同一形状：静默降级，毫无痕迹。修法是把 import
    挪出 try，只对真正的配置解析失败降级。这条测试钉住「符号缺失会抛」。
    """
    import sys
    import types
    from unittest import mock

    import pytest

    from core.proxy_pool import ProxyPool

    # 故意不带 `config_store` 属性的假模块 → `from ... import config_store` 会 ImportError。
    fake = types.ModuleType("core.config_store")
    with mock.patch.dict(sys.modules, {"core.config_store": fake}):
        with pytest.raises(ImportError):
            ProxyPool._balance_settings()

