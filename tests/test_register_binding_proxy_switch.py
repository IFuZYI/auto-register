"""注册完成后，账号绑定与上传的代理必须是带 `bind` 标签的代理。

用户要求（2026-11-02）：GPT 注册流程最后绑定、上传的代理不能还是注册代理，
应当是带绑定标签（`bind`）的代理。

背景：注册链路按 `register` 标签取代理（可能只带 `register`），而账号的
`register_proxy` 同时是「绑定关系」和「上传代理」的唯一来源。注册结束直接写
注册代理，会让账号绑在不可绑定的出口上、随后上传也从注册出口出去 ——
`resolve_for_account` 的绑定规则（有 `bind` 标签才保留，否则换到可绑定池）
正好是这里要的语义，本测试钉住它在注册成功路径上真的被调用。
"""

from __future__ import annotations

from unittest import mock

import pytest
from sqlmodel import Session, select

from core.base_platform import Account, BasePlatform
from core.db import ProxyModel, current_engine
from core.proxy_utils import normalize_proxy_url

REGISTER_ONLY = "http://user:pw@10.0.0.1:8001"
BIND_A = "http://user:pw@10.0.0.2:8002"
BIND_B = "http://user:pw@10.0.0.3:8003"


class _FakeMailbox:
    """`_run_register` 只要求 mailbox 能被建出来；fake 平台不读信。"""


class _FakeChatGPT(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    _counter = 0

    def __init__(self, config=None, mailbox=None):
        super().__init__(config)
        self.mailbox = mailbox

    def register(self, email: str = None, password: str = None) -> Account:
        type(self)._counter += 1
        return Account(
            platform="chatgpt",
            email=f"binding-{type(self)._counter}@example.com",
            password=password or "pw",
            extra={},
        )

    def check_valid(self, account: Account) -> bool:
        return True


@pytest.fixture
def proxy_pool_db():
    """清空代理表（会话级共享测试库），用完再清一次。"""
    def _clear() -> None:
        with Session(current_engine()) as s:
            for row in s.exec(select(ProxyModel)).all():
                s.delete(row)
            s.commit()

    _clear()
    yield
    _clear()


def _add_proxy(url: str, *, tags: str, active: bool = True) -> None:
    with Session(current_engine()) as s:
        s.add(ProxyModel(url=normalize_proxy_url(url), tags=tags, is_active=active))
        s.commit()


def _run_once(*, proxy: str) -> tuple[list[Account], list[str]]:
    from api.tasks import RegisterTaskRequest, _create_task_record, _run_register, _task_store

    _FakeChatGPT._counter = 0
    captured: list[Account] = []
    task_id = f"task-binding-{abs(hash(proxy))}"

    def _capture(account):
        captured.append(account)
        return None

    req = RegisterTaskRequest(
        platform="chatgpt",
        count=1,
        concurrency=1,
        proxy=proxy,
        register_retry_times=0,
        extra={"mail_provider": "fake"},
    )
    _create_task_record(task_id, req, "manual", None)

    with (
        mock.patch("core.registry.get", return_value=_FakeChatGPT),
        mock.patch("modules.mail.create_mailbox", return_value=_FakeMailbox()),
        mock.patch("core.db.save_account", side_effect=_capture),
        mock.patch("services.task_service.auto_upload_integrations"),
        mock.patch("services.task_service.save_task_log"),
    ):
        _run_register(task_id, req)

    snapshot = _task_store.snapshot(task_id)
    assert snapshot["success"] == 1, snapshot.get("errors")
    return captured, [str(line) for line in snapshot.get("logs", [])]


def _saved_extra(captured: list[Account]) -> dict:
    assert captured, "注册成功但没有走到 save_account"
    return dict(captured[-1].extra or {})


def test_register_only_proxy_is_switched_to_a_bind_tagged_proxy(proxy_pool_db):
    """注册代理只有 `register` 标签 → 账号绑定必须换到 `bind` 代理。"""
    _add_proxy(REGISTER_ONLY, tags="register")
    _add_proxy(BIND_A, tags="bind")
    _add_proxy(BIND_B, tags="bind")

    captured, logs = _run_once(proxy=REGISTER_ONLY)
    extra = _saved_extra(captured)
    bound = normalize_proxy_url(extra.get("register_proxy") or "")

    assert bound in {normalize_proxy_url(BIND_A), normalize_proxy_url(BIND_B)}, (
        f"账号绑定还是注册代理: {bound}"
    )
    joined = "\n".join(logs)
    assert "绑定代理切换" in joined, f"注册完成日志里没有切换记录: {joined}"

    # 自动上传读的就是 `register_proxy`：开关打开时必须交出新绑定的代理，
    # 而不是注册出口。
    from services.chatgpt_sync import upload_proxy_for

    with mock.patch(
        "core.config_store.config_store.get",
        side_effect=lambda key, default="": "1" if key == "sub2api_upload_proxy_enabled" else "",
    ):
        assert upload_proxy_for("sub2api", extra) == bound


def test_proxy_tagged_register_and_bind_is_kept(proxy_pool_db):
    """注册代理本身也带 `bind` 标签 → 保留，不白换出口。"""
    _add_proxy(REGISTER_ONLY, tags="register,bind")

    captured, logs = _run_once(proxy=REGISTER_ONLY)
    bound = normalize_proxy_url(_saved_extra(captured).get("register_proxy") or "")

    assert bound == normalize_proxy_url(REGISTER_ONLY)
    assert "绑定代理切换" not in "\n".join(logs)


def test_no_bind_pool_keeps_register_proxy_for_old_deployments(proxy_pool_db):
    """一个 `bind` 标签都没有（存量部署）→ 保留注册代理，不丢绑定。"""
    _add_proxy(REGISTER_ONLY, tags="register")

    captured, logs = _run_once(proxy=REGISTER_ONLY)
    bound = normalize_proxy_url(_saved_extra(captured).get("register_proxy") or "")

    assert bound == normalize_proxy_url(REGISTER_ONLY)
    assert "绑定代理切换" not in "\n".join(logs)
