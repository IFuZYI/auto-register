"""Outlook 号池的「选择导入才能入池」两步流程。

用户要求：**账号邮箱要选择导入才能导入邮箱池，不是全部导入。**

粘贴一批邮箱 → 全部落 `unpooled`（未入池）→ 注册取号跳过它们 →
在预览表里勾选 → 调 `/outlook/pool-status/import` → 才转 `available`。

与 iCloud 别名池（`platforms/icloud` + `services/icloud_service.py`）同一套三态
语义。这个文件守住 Outlook 这一侧，iCloud 那侧在
`tests/test_icloud_service.py` 的 pool 相关用例里。
"""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine


@pytest.fixture
def client(tmp_path, monkeypatch):
    import core.db as db
    from core.db import platform_database_registry

    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    # 号池表在 platforms/outlook.db，registry 指过去
    platform_database_registry.configure({"outlook": str(engine.url)})

    from main import app

    return TestClient(app)


def _import_accounts(client, lines: str) -> None:
    """走真实导入接口（会做 OAuth 可用性探测，这里用 MailAPI 格式绕开）。"""
    resp = client.post("/api/outlook/batch-import", json={"data": lines, "enabled": True})
    assert resp.status_code == 200, resp.text


def _statuses(client) -> dict:
    from core.mailboxes.channels.outlook import OutlookMailbox

    return OutlookMailbox.pool_status_summary()


def test_imported_accounts_land_unpooled(client):
    """粘贴进来的账号一律是 unpooled —— 这是「不全部导入邮箱池」的落点。"""
    _import_accounts(
        client,
        "a@outlook.com----https://mailapi.icu/k?a=1\n"
        "b@outlook.com----https://mailapi.icu/k?b=2\n",
    )

    summary = _statuses(client)
    assert summary["unpooled"] == 2
    assert summary["available"] == 0, "导入不该直接进可领取状态"
    assert summary["total"] == 2


def test_claim_skips_unpooled_accounts(client):
    """注册取号必须跳过 unpooled，并在报错里说清是「没入池」而不是「没导入」。"""
    _import_accounts(client, "a@outlook.com----https://mailapi.icu/k?a=1\n")

    from core.mailboxes.channels.outlook import OutlookMailbox

    mailbox = OutlookMailbox()
    with pytest.raises(RuntimeError) as ctx:
        mailbox.get_email()

    detail = str(ctx.value)
    assert "未入池" in detail, f"报错没说清是没入池，用户会以为导入失败：{detail}"
    assert "导入邮箱池" in detail


def test_selected_accounts_become_claimable(client):
    """勾选后入池：只有被勾的那些转 available，其余仍是 unpooled。"""
    _import_accounts(
        client,
        "a@outlook.com----https://mailapi.icu/k?a=1\n"
        "b@outlook.com----https://mailapi.icu/k?b=2\n"
        "c@outlook.com----https://mailapi.icu/k?c=3\n",
    )

    from core.mailboxes.channels.outlook import OutlookMailbox

    # 只勾第二个
    from core.db import OutlookAccountModel, mailbox_pool_session
    from sqlmodel import select

    with mailbox_pool_session("outlook") as session:
        rows = session.exec(select(OutlookAccountModel).order_by(OutlookAccountModel.id)).all()
        target_id = rows[1].id

    resp = client.post("/api/outlook/pool-status/import", json={"ids": [target_id]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["changed"] == 1
    assert body["remaining_unpooled"] == 2

    summary = _statuses(client)
    assert summary["available"] == 1
    assert summary["unpooled"] == 2

    # 现在能领到，而且领到的正是勾选那个
    mailbox = OutlookMailbox()
    account = mailbox.get_email()
    assert account.email == "b@outlook.com"


def test_import_to_pool_is_idempotent_and_never_revives_in_use(client):
    """重复点「导入邮箱池」不能把已在用的号改回可领取。

    这是**真实风险**：界面状态过期或用户连点两下时，如果把 `in_use` 改回
    `available`，同一个号会被两个注册任务同时领走 —— 两边互相顶掉验证码邮件，
    表现为「两个任务都收不到码」，而号池看着一切正常。
    """
    _import_accounts(client, "a@outlook.com----https://mailapi.icu/k?a=1\n")

    from core.db import OutlookAccountModel, mailbox_pool_session
    from sqlmodel import select

    from core.mailboxes.channels.outlook import OutlookMailbox

    with mailbox_pool_session("outlook") as session:
        row = session.exec(select(OutlookAccountModel)).one()
        account_id = row.id

    # 先入池再领走（状态变 in_use）
    assert client.post("/api/outlook/pool-status/import", json={"ids": [account_id]}).status_code == 200
    OutlookMailbox().get_email()

    summary = _statuses(client)
    assert summary["in_use"] == 1

    # 再点一次入池：必须跳过，不能复活
    body = client.post("/api/outlook/pool-status/import", json={"ids": [account_id]}).json()
    assert body["changed"] == 0
    assert body["skipped"] == 1

    summary = _statuses(client)
    assert summary["in_use"] == 1, "in_use 被改回可领取了 —— 会被重复领取"
    assert summary["available"] == 0


def test_empty_selection_is_rejected(client):
    """空勾选要报错说人话，不能静默什么都不做。"""
    resp = client.post("/api/outlook/pool-status/import", json={"ids": []})
    assert resp.status_code == 400
    assert "请先选择" in resp.json()["detail"]


def test_pool_summary_endpoint_exposes_unpooled(client):
    _import_accounts(client, "a@outlook.com----https://mailapi.icu/k?a=1\n")

    summary = client.get("/api/outlook/pool-summary").json()

    assert summary["unpooled"] == 1
    assert summary["total"] == 1


def test_import_to_pool_handles_ids_beyond_sqlite_variable_limit(client):
    """33000 个 id（超过 SQLite 变量上限 32766）不能炸。

    实测（修复前）：`import_accounts_to_pool` 的 IN 查询无界 →
    `sqlite3.OperationalError: too many SQL variables`。界面虽不会一次勾
    33000 行，但接口不限流、脚本调用也走这条路 —— 分块后传多大都安全。
    """
    _import_accounts(client, "a@outlook.com----https://mailapi.icu/k?a=1\n")

    from core.db import OutlookAccountModel, mailbox_pool_session
    from core.mailboxes.channels.outlook import OutlookMailbox
    from sqlmodel import select

    with mailbox_pool_session("outlook") as session:
        row = session.exec(select(OutlookAccountModel)).one()
        account_id = int(row.id or 0)

    # 真实 id + 33000 个不存在的 id
    ids = [account_id] + list(range(account_id + 1, account_id + 33001))
    result = OutlookMailbox.import_accounts_to_pool(ids)

    assert result["changed"] == 1
    assert result["skipped"] == 33000
    assert result["remaining_unpooled"] == 0
