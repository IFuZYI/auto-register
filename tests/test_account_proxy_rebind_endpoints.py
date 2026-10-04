"""代理绑定的**端到端**验证：无绑定 / 绑定不可用时，后续动作要能补上并写回。

用户要求：「账号的注册代理绑定功能不再是仅有注册才绑定，若无绑定、绑定代理
不可用，后续也能更新。」

与 `test_account_proxy_reuse.py` 的分工：
- 那个测的是**决策函数**（`resolve_for_account`）的返回值语义；
- 这个测的是**真的写进库了没有** —— 走真实 API 端点，动作执行后回查账号行。

只测决策不测落库是不够的：调用方拿到 `should_bind=True` 却忘了 commit，
决策层测试全绿而功能实际没生效。

用 conftest 建好的测试库（它已含 `proxies` 与 `accounts` 两张表），
不另起 engine —— 换 engine 会让另一张表消失（`no such table: accounts`）。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from core.db import ProxyModel, account_repository, engine
from core.db.models_account import AccountModel
from core.proxy_utils import normalize_proxy_url

PROXY_A = "http://user:pw@1.1.1.1:8080"
PROXY_B = "http://user:pw@2.2.2.2:9090"


@pytest.fixture(scope="module")
def client():
    """起真实 app —— 必须进上下文，否则 lifespan 不跑、平台注册表为空。"""
    import main as main_mod

    with TestClient(main_mod.app) as c:
        yield c


@pytest.fixture(autouse=True)
def clean_proxies():
    """每个用例前后清空代理池，避免互相污染轮转游标。"""
    def _clear():
        with Session(engine) as s:
            for row in s.exec(select(ProxyModel)).all():
                s.delete(row)
            s.commit()

    _clear()
    yield
    _clear()


def _add_proxy(url: str, *, is_active: bool = True) -> None:
    with Session(engine) as s:
        s.add(ProxyModel(url=normalize_proxy_url(url), is_active=is_active))
        s.commit()


def _create_account(email: str, *, register_proxy: str = "") -> int:
    account = AccountModel(platform="chatgpt", email=email, password="pw")
    extra = {}
    if register_proxy:
        extra["register_proxy"] = register_proxy
    account.set_extra(extra)
    return account_repository.upsert(account).id


def _read_register_proxy(email: str) -> str:
    row = account_repository.find_by_email("chatgpt", email)
    if row is None:
        return ""
    return str(row.get_extra().get("register_proxy") or "")


def _delete_account(email: str) -> None:
    row = account_repository.find_by_email("chatgpt", email)
    if row is not None:
        account_repository.delete(row.id, "chatgpt")


def _run_probe(client, account_id: int):
    """跑一次动作。

    动作本身会因为账号没有可用凭证而失败 —— 这不影响断言：代理的**决策与
    写回**发生在动作执行之前（`_execute_platform_action` 里先 resolve 再执行）。
    """
    return client.post(
        f"/api/actions/chatgpt/{account_id}/probe_local_status",
        json={"params": {}},
    )


class TestBindOnFirstReuse:
    """无绑定 → 复用动作时补上绑定并写回。"""

    def test_probe_action_binds_a_proxy_when_account_has_none(self, client):
        """账号没绑过代理 → 跑一次动作后字段被填上。"""
        _add_proxy(PROXY_A, is_active=True)
        email = "bind-on-reuse@example.com"
        account_id = _create_account(email, register_proxy="")
        try:
            _run_probe(client, account_id)
            bound = _read_register_proxy(email)
            assert bound == normalize_proxy_url(PROXY_A), (
                "无绑定的账号跑完动作后应补上代理绑定（用户要求「不再是仅有注册才绑定」）"
            )
        finally:
            _delete_account(email)

    def test_second_call_reuses_the_newly_bound_proxy(self, client):
        """补绑之后第二次调用要**用回**它，而不是再换一个。"""
        _add_proxy(PROXY_A, is_active=True)
        _add_proxy(PROXY_B, is_active=True)
        email = "bind-then-reuse@example.com"
        account_id = _create_account(email, register_proxy="")
        try:
            _run_probe(client, account_id)
            first = _read_register_proxy(email)
            assert first, "第一次应当补上绑定"

            # 推两格轮转游标：若第二次重新轮询，会拿到不同的代理
            from core.proxy_pool import proxy_pool

            proxy_pool.get_next()
            proxy_pool.get_next()

            _run_probe(client, account_id)
            second = _read_register_proxy(email)
            assert second == first, (
                "第二次必须复用已绑定的代理，不能又换一个（出口 IP 会漂）"
            )
        finally:
            _delete_account(email)


class TestRebindWhenDead:
    """绑定不可用 → 复用动作时换新并写回。"""

    def test_dead_binding_is_replaced_and_persisted(self, client):
        """原代理被停用 → 换池里的新代理，字段更新。"""
        _add_proxy(PROXY_A, is_active=False)  # 已停用
        _add_proxy(PROXY_B, is_active=True)
        email = "rebind-dead@example.com"
        account_id = _create_account(email, register_proxy=PROXY_A)
        try:
            _run_probe(client, account_id)
            bound = _read_register_proxy(email)
            assert bound == normalize_proxy_url(PROXY_B), (
                "原绑定不可用时应当换成可用代理并写回（用户要求「绑定代理不可用，后续也能更新」）"
            )
        finally:
            _delete_account(email)

    def test_healthy_binding_is_left_alone(self, client):
        """原绑定还能用 → 一个字都不改（避免无意义写库）。"""
        _add_proxy(PROXY_A, is_active=True)
        _add_proxy(PROXY_B, is_active=True)
        email = "keep-healthy@example.com"
        account_id = _create_account(email, register_proxy=PROXY_A)
        try:
            _run_probe(client, account_id)
            assert _read_register_proxy(email) == normalize_proxy_url(PROXY_A), (
                "原代理可用时不该被换掉"
            )
        finally:
            _delete_account(email)
