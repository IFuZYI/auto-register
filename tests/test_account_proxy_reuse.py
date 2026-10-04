"""账号保留注册代理，复用时优先复用，不可用才更新。

用户要求：「注册的账号保留注册IP（代理）字段，之后复用时优先使用原代理，
若原代理不可用，则更新代理字段。」

背景：同一个账号反复换出口 IP 容易被上游当成异常登录。此前复用路径
（测活按钮、补 RT、绑 2FA）**完全不用代理**（`config.proxy` 恒为 None），
每次都是直连出网 —— 与注册时的出口不一致。
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from core.proxy_pool import ProxyPool
from core.proxy_utils import normalize_proxy_url

PROXY_A = "http://user:pw@1.2.3.4:8080"
PROXY_B = "http://user:pw@5.6.7.8:9090"


@pytest.fixture
def db(tmp_path, monkeypatch):
    import core.db as db_module
    from core.db import ProxyModel

    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    SQLModel.metadata.create_all(engine, tables=[ProxyModel.__table__])
    monkeypatch.setattr(db_module, "engine", engine)
    return engine


def _add_proxy(engine, url: str, *, is_active: bool = True, fails: int = 0) -> None:
    from core.db import ProxyModel

    with Session(engine) as s:
        s.add(
            ProxyModel(
                url=normalize_proxy_url(url),
                is_active=is_active,
                fail_count=fails,
            )
        )
        s.commit()


class TestIsAvailable:
    """`is_available`：在池里且没被停用才算「可用」。"""

    def test_active_proxy_is_available(self, db):
        _add_proxy(db, PROXY_A, is_active=True)
        assert ProxyPool().is_available(PROXY_A) is True

    def test_disabled_proxy_is_not_available(self, db):
        _add_proxy(db, PROXY_A, is_active=False)
        assert ProxyPool().is_available(PROXY_A) is False

    def test_unknown_proxy_is_available(self, db):
        """不在池里的代理视为可用 —— 那是调用方手工指定的一次性代理，
        没有任何证据说它坏了，不该替用户把它换掉。"""
        assert ProxyPool().is_available("http://9.9.9.9:1234") is True

    def test_matches_across_normalization(self, db):
        """库里存 socks5h、传 socks5 也要认出来（同一代理的不同写法）。"""
        from core.db import ProxyModel

        with Session(db) as s:
            s.add(ProxyModel(url="socks5h://1.2.3.4:1080", is_active=False))
            s.commit()
        assert ProxyPool().is_available("socks5://1.2.3.4:1080") is False


class TestResolveForAccount:
    """`resolve_for_account`：优先原代理，不可用才换。"""

    def test_prefers_the_saved_proxy(self, db):
        """原代理可用 → 原样返回，不替换。"""
        _add_proxy(db, PROXY_A, is_active=True)
        chosen, replaced = ProxyPool().resolve_for_account(
            PROXY_A, fallback=PROXY_B
        )
        assert chosen == normalize_proxy_url(PROXY_A)
        assert replaced is False, "原代理可用时不该替换"

    def test_replaces_when_saved_proxy_is_dead(self, db):
        """原代理被停用 → 换 fallback，并标记「发生了替换」。"""
        _add_proxy(db, PROXY_A, is_active=False)
        _add_proxy(db, PROXY_B, is_active=True)
        chosen, replaced = ProxyPool().resolve_for_account(
            PROXY_A, fallback=PROXY_B
        )
        assert chosen == normalize_proxy_url(PROXY_B)
        assert replaced is True, "替换必须被标记，调用方据此写回字段"

    def test_no_saved_proxy_binds_the_fallback(self, db):
        """账号没记过代理 → 用 fallback，并标记**需要绑定**。

        用户要求「不再是仅有注册才绑定」：注册时没走代理（或池子当时是空的）
        的账号，之后第一次复用就补上绑定，此后固定走它。不补的话每次复用都
        从池里轮换取一个，出口 IP 一直在变。
        """
        _add_proxy(db, PROXY_B, is_active=True)
        chosen, should_bind = ProxyPool().resolve_for_account("", fallback=PROXY_B)
        assert chosen == normalize_proxy_url(PROXY_B)
        assert should_bind is True, "无绑定时要标记，调用方据此补写字段"

    def test_no_saved_proxy_and_no_fallback_is_a_no_op(self, db):
        """原来没绑定、池里也取不到 → 直连，不该写库（没什么可绑的）。"""
        chosen, should_bind = ProxyPool().resolve_for_account("", fallback="")
        assert chosen == ""
        assert should_bind is False

    def test_keeps_saved_proxy_when_no_fallback_available(self, db):
        """原代理坏了但没有替代 → **保留原值**，不能清空。

        代理可能只是被误停用（`report_fail` 的阈值很粗），清空字段等于丢掉
        唯一能回到它出生出口的线索；下次探活复活它之后就还能用。
        """
        _add_proxy(db, PROXY_A, is_active=False)
        chosen, replaced = ProxyPool().resolve_for_account(PROXY_A, fallback="")
        assert chosen == normalize_proxy_url(PROXY_A)
        assert replaced is False

    def test_keeps_saved_proxy_when_fallback_is_the_same(self, db):
        """fallback 就是原代理本身 → 不算替换（避免无意义的写库）。"""
        _add_proxy(db, PROXY_A, is_active=False)
        chosen, replaced = ProxyPool().resolve_for_account(
            PROXY_A, fallback=PROXY_A
        )
        assert chosen == normalize_proxy_url(PROXY_A)
        assert replaced is False

    def test_unknown_saved_proxy_is_kept(self, db):
        """不在池里的原代理视为可用 → 保留（手工指定的一次性代理）。"""
        chosen, replaced = ProxyPool().resolve_for_account(
            "http://9.9.9.9:1234", fallback=PROXY_B
        )
        assert chosen == "http://9.9.9.9:1234"
        assert replaced is False


class TestFallbackIsLazy:
    """原代理可用时**不该**去池里取备用代理。

    `get_next()` 有副作用（推轮转游标），白取一格会让真正需要备用的账号
    拿到偏离预期的代理。
    """

    def test_fallback_not_consulted_when_saved_proxy_works(self, db):
        _add_proxy(db, PROXY_A, is_active=True)
        calls = []

        def provider():
            calls.append(1)
            return PROXY_B

        chosen, replaced = ProxyPool().resolve_for_account(
            PROXY_A, fallback_provider=provider
        )
        assert chosen == normalize_proxy_url(PROXY_A)
        assert replaced is False
        assert calls == [], "原代理可用时不该调 fallback_provider"

    def test_fallback_consulted_when_saved_proxy_is_dead(self, db):
        _add_proxy(db, PROXY_A, is_active=False)
        calls = []

        def provider():
            calls.append(1)
            return PROXY_B

        chosen, replaced = ProxyPool().resolve_for_account(
            PROXY_A, fallback_provider=provider
        )
        assert chosen == normalize_proxy_url(PROXY_B)
        assert replaced is True
        assert calls == [1], "原代理坏了必须去取备用"

    def test_fallback_provider_exception_is_tolerated(self, db):
        """取备用代理抛异常时不能把整个动作带崩 —— 退回保留原值。"""
        _add_proxy(db, PROXY_A, is_active=False)

        def provider():
            raise RuntimeError("池子炸了")

        chosen, replaced = ProxyPool().resolve_for_account(
            PROXY_A, fallback_provider=provider
        )
        assert chosen == normalize_proxy_url(PROXY_A)
        assert replaced is False


class TestRegistrationRecordsProxy:
    """注册成功后要把用的代理写进账号。"""

    def test_source_records_register_proxy(self):
        import inspect

        from api import tasks as tasks_mod

        src = inspect.getsource(tasks_mod._run_register)
        assert 'account.extra["register_proxy"] = _proxy' in src, (
            "注册成功必须记下用的代理 —— 否则复用路径没有「原代理」可谈"
        )

    def test_batch_task_reads_register_proxy(self):
        """批量任务（补 RT / 绑 2FA）要从账号读代理并优先复用。"""
        import inspect

        from api import tasks as tasks_mod

        src = inspect.getsource(tasks_mod._load_account_fields)
        assert '"register_proxy"' in src, "读账号时要把代理带出来"

        src_batch = inspect.getsource(tasks_mod._run_account_batch_task)
        assert "_resolve_proxy_for_account(" in src_batch, (
            "批量任务必须按账号挑代理，不能直接用池里的下一个"
        )
        assert "_persist_register_proxy(" in src_batch, (
            "替换后要写回账号的代理字段"
        )

    def test_action_path_uses_account_proxy(self):
        """账号动作（测活 / 上传）要用账号自己的代理。

        此前 `config.proxy` 恒为 None —— 所有动作直连出网，与注册出口不一致。
        """
        import inspect

        from api import actions as actions_mod

        src = inspect.getsource(actions_mod._execute_platform_action)
        assert "_resolve_action_proxy(" in src
        assert "instance.config.proxy = action_proxy" in src

    def test_chatgpt_endpoints_fall_back_to_account_proxy(self):
        """测活端点在 proxy 参数为空时要回落到账号自己的代理。

        历史上这里钉的是 `api/chatgpt.py` 的端点 —— 该模块从未挂载
        （全历史无 include_router），已删除。现在钉真正可达的路径：
        `api/actions.py` 的 `_resolve_action_proxy` + 平台动作执行时
        把账号代理写进 `instance.config.proxy`。
        """
        import inspect

        from api import actions as actions_mod

        src = inspect.getsource(actions_mod._resolve_action_proxy)
        assert "resolve_for_account(" in src, (
            "动作路径没有走账号代理回落 —— 会直连出网"
        )
        # 决策函数拿到 should_bind=True 后必须写回账号的 register_proxy
        assert "register_proxy" in src, (
            "解析出的代理没有写回账号 —— 下次复用还会漂"
        )


class TestProxyNeverLeaksToLogs:
    """日志里的代理必须脱敏（凭据不能进日志）。"""

    def test_batch_task_redacts_before_logging(self):
        import inspect

        from api import tasks as tasks_mod

        src = inspect.getsource(tasks_mod._run_account_batch_task)
        assert "redact_proxy_url(chosen)" in src, (
            "替换代理时的日志要脱敏 —— 代理 URL 里带用户名密码"
        )
