"""设置里选的导入类型要管到取号那一步。

outlook_accounts 一张表里混着 OAuth 号和 MailAPI URL 号，以前取号只按 id 顺序捞第一个
可用的：设置页明明选着 MailAPI URL，注册任务照样发下来一个 outlook OAuth 号，日志里
has_mailapi_url=False、收信后端 graph。选错类型等于换了套取码方式，必然收不到验证码，
所以这里按视图筛 account_type，且两类之间不互相顶替。
"""

import sys
from pathlib import Path

import pytest
from sqlmodel import Session, SQLModel, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.base_mailbox import OutlookMailbox, create_mailbox
from services.mail_imports import resolve_pool_account_type


@pytest.fixture
def engine(tmp_path, monkeypatch):
    import core.db as db
    from core.db import platform_database_registry

    test_engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    SQLModel.metadata.create_all(test_engine)
    monkeypatch.setattr(db, "engine", test_engine)
    platform_database_registry.configure({"icloud": str(test_engine.url)})
    try:
        yield test_engine
    finally:
        test_engine.dispose()


def seed(engine, rows):
    from core.db import OutlookAccountModel

    with Session(engine) as session:
        for row in rows:
            session.add(OutlookAccountModel(**row))
        session.commit()


OAUTH_ROW = {
    "email": "oauth@outlook.com",
    "password": "pwd",
    "client_id": "cid",
    "refresh_token": "rt",
    "account_type": "microsoft_oauth",
}
MAILAPI_ROW = {
    "email": "mailapi@hotmail.com",
    "password": "",
    "account_type": "mailapi_url",
    "mailapi_url": "https://mailapi.icu/key?type=html&orderNo=1",
}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("mailapi", "mailapi_url"),
        ("outlook", "microsoft_oauth"),
        ("hotmail", "microsoft_oauth"),
        ("MailAPI", "mailapi_url"),
        # 旧库只存 microsoft/applemail，microsoft 就是 Outlook 视图
        ("microsoft", "microsoft_oauth"),
        # applemail 视图已弃用，收敛到 Outlook 视图 → 只取 OAuth 号
        ("applemail", "microsoft_oauth"),
        # 没存过视图的库不筛选，保持整池取号的旧行为
        ("", ""),
        ("nonsense", ""),
    ],
)
def test_view_decides_which_account_type_to_take(source, expected):
    assert resolve_pool_account_type(source) == expected


def test_create_mailbox_passes_the_configured_view_through():
    mailbox = create_mailbox(
        provider="microsoft",
        extra={"mail_import_source": "mailapi", "outlook_backend": "graph"},
    )

    assert mailbox._pool_account_type == "mailapi_url"


def test_mailapi_view_skips_oauth_accounts(engine):
    seed(engine, [OAUTH_ROW, MAILAPI_ROW])

    payload = OutlookMailbox(mail_import_source="mailapi")._pop_account()

    assert payload["email"] == "mailapi@hotmail.com"
    assert payload["account_type"] == "mailapi_url"


def test_outlook_view_skips_mailapi_accounts(engine):
    seed(engine, [MAILAPI_ROW, OAUTH_ROW])

    payload = OutlookMailbox(mail_import_source="outlook")._pop_account()

    assert payload["email"] == "oauth@outlook.com"


def test_outlook_view_still_takes_rows_imported_before_account_type_existed(engine):
    seed(engine, [MAILAPI_ROW, {"email": "legacy@outlook.com", "password": "pwd", "account_type": ""}])

    payload = OutlookMailbox(mail_import_source="outlook")._pop_account()

    assert payload["email"] == "legacy@outlook.com"


def test_mailapi_view_says_so_instead_of_handing_out_an_oauth_account(engine):
    seed(engine, [OAUTH_ROW])

    with pytest.raises(RuntimeError) as excinfo:
        OutlookMailbox(mail_import_source="mailapi")._pop_account()

    message = str(excinfo.value)
    assert "MailAPI URL" in message
    assert "不会拿来顶替" in message

    from core.db import OutlookAccountModel

    with Session(engine) as session:
        assert session.get(OutlookAccountModel, 1) is not None


def test_mailapi_view_distinguishes_already_registered_from_wrong_type(engine):
    from core.db import AccountModel

    seed(engine, [MAILAPI_ROW])
    with Session(engine) as session:
        session.add(
            AccountModel(platform="chatgpt", email="mailapi@hotmail.com", password="pwd")
        )
        session.commit()

    with pytest.raises(RuntimeError, match="都已经注册过了"):
        OutlookMailbox(mail_import_source="mailapi")._pop_account()


def test_pool_stays_unfiltered_when_no_view_was_ever_saved(engine):
    seed(engine, [MAILAPI_ROW])

    payload = OutlookMailbox()._pop_account()

    assert payload["email"] == "mailapi@hotmail.com"


def test_applied_filter_shows_up_in_the_task_log(engine):
    seed(engine, [MAILAPI_ROW])
    mailbox = OutlookMailbox(mail_import_source="mailapi")
    logs = []
    mailbox._log_fn = logs.append

    mailbox._pop_account()

    assert any("只取 account_type=mailapi_url" in line for line in logs)
