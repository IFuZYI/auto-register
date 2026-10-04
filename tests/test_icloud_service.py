"""iCloud 业务层：凭据加密落库、额度控制与隐私邮箱去重。"""

import base64
import os

import pytest
from sqlmodel import SQLModel, create_engine

from platforms.icloud.credentials import ICloudCredentials
from platforms.icloud.errors import ICloudError
from platforms.icloud.models import ImportedSession, PrivateEmail, SessionImportRequest


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())

    import core.db as db
    from core.db import platform_database_registry
    from core.secret_box import SecretBox
    import core.secret_box as secret_box_module
    from services import icloud_service

    url = f"sqlite:///{tmp_path / 'icloud.db'}"
    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)
    # 分库 API：把 icloud 指向临时库（等价于生产里配置 DATABASE_URL_ICLOUD）
    platform_database_registry.configure({"icloud": url})
    monkeypatch.setattr(secret_box_module, "secret_box", SecretBox())
    monkeypatch.setattr(icloud_service, "secret_box", secret_box_module.secret_box)
    yield icloud_service
    platform_database_registry.configure({})
    engine.dispose()


class _StubWebClient:
    def __init__(self, *, imported=None, private_emails=(), generated=None):
        self.imported = imported
        self.private_emails = list(private_emails)
        self.generated = generated
        self.deleted = []

    def import_session(self, _request):
        return self.imported

    def list_private_emails(self, _credentials):
        return self.private_emails

    def generate_private_email(self, _credentials, *, label="", note=""):
        self.generated.label = label
        self.generated.note = note
        return self.generated

    def delete_private_email(self, _credentials, *, address, provider_id="", status=""):
        self.deleted.append(address)


@pytest.fixture
def stub_web_client(monkeypatch, service):
    from contextlib import contextmanager

    holder = {}

    @contextmanager
    def _factory(**_kwargs):
        yield holder["client"]

    monkeypatch.setattr(service, "web_client", _factory)
    return holder


def _imported(email="owner@icloud.com") -> ImportedSession:
    return ImportedSession(
        credentials=ICloudCredentials(
            region="global",
            dsid="123456",
            cookies="a=1",
            hme_service_url="https://hme.example.test",
            client_id="client-1",
            client_build_number="b",
            client_mastering_number="m",
            imap_password="app-specific",
        ),
        account_email=email,
        masked_dsid="12**56",
    )


def test_import_session_stores_encrypted_credentials(service, stub_web_client):
    stub_web_client["client"] = _StubWebClient(imported=_imported())

    account = service.import_session(SessionImportRequest(cookie_header="a=1"))

    assert account["email"] == "owner@icloud.com"
    assert account["credential_state"]["has_imap_credentials"] is True

    row = service.get_account(account["id"])
    assert "app-specific" not in row.credentials_cipher
    assert service.load_credentials(row).imap_password == "app-specific"


def test_reimport_keeps_existing_imap_password_when_not_resubmitted(service, stub_web_client):
    stub_web_client["client"] = _StubWebClient(imported=_imported())
    account = service.import_session(SessionImportRequest(cookie_header="a=1"))

    refreshed = _imported()
    refreshed.credentials.imap_password = ""
    refreshed.credentials.cookies = "a=2"
    stub_web_client["client"] = _StubWebClient(imported=refreshed)
    service.import_session(SessionImportRequest(cookie_header="a=2"))

    credentials = service.load_credentials(service.get_account(account["id"]))
    assert credentials.cookies == "a=2"
    assert credentials.imap_password == "app-specific"


def test_unreadable_credentials_report_a_relogin_hint(service, stub_web_client, monkeypatch):
    """密钥换掉后旧密文解不开，要给出"重新登录"而不是裸的 InvalidTag。"""
    stub_web_client["client"] = _StubWebClient(imported=_imported())
    account = service.import_session(SessionImportRequest(cookie_header="a=1"))

    import core.secret_box as secret_box_module

    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
    monkeypatch.setattr(service, "secret_box", secret_box_module.SecretBox())

    row = service.get_account(account["id"])
    with pytest.raises(ICloudError) as excinfo:
        service.load_credentials(row)

    assert excinfo.value.code == "credentials_unreadable"
    assert "重新登录" in str(excinfo.value)
    # InvalidTag 的 str() 是空的，不能在消息尾巴上留一个空括号
    assert not str(excinfo.value).endswith("（）")

    # 账号列表不能因此整个 500，要能标出这一条坏掉了
    assert service.list_accounts()[0]["credential_state"] == {"credentials_unreadable": True}


def test_generate_alias_enforces_hourly_quota(service, stub_web_client):
    stub_web_client["client"] = _StubWebClient(imported=_imported())
    account = service.import_session(SessionImportRequest(cookie_header="a=1"))

    for index in range(service.HOURLY_ALIAS_LIMIT):
        stub_web_client["client"] = _StubWebClient(
            generated=PrivateEmail(address=f"alias{index}@icloud.com", provider_id=f"anon-{index}")
        )
        service.generate_alias(account["id"], label="任务")

    assert service.alias_quota(account["id"])["remaining"] == 0
    stub_web_client["client"] = _StubWebClient(generated=PrivateEmail(address="overflow@icloud.com"))
    with pytest.raises(ICloudError) as excinfo:
        service.generate_alias(account["id"])
    assert excinfo.value.code == "provider_rate_limited"


def test_sync_aliases_deduplicates_by_address(service, stub_web_client):
    stub_web_client["client"] = _StubWebClient(imported=_imported())
    account = service.import_session(SessionImportRequest(cookie_header="a=1"))

    stub_web_client["client"] = _StubWebClient(
        private_emails=[
            PrivateEmail(address="a@icloud.com", provider_id="1"),
            PrivateEmail(address="b@icloud.com", provider_id="2"),
        ]
    )
    first = service.sync_aliases(account["id"])
    second = service.sync_aliases(account["id"])

    assert (first["created"], first["updated"]) == (2, 0)
    assert (second["created"], second["updated"]) == (0, 2)
    assert len(service.list_aliases(account["id"])) == 2


def test_resolve_account_skips_disabled_accounts(service, stub_web_client):
    stub_web_client["client"] = _StubWebClient(imported=_imported())
    account = service.import_session(SessionImportRequest(cookie_header="a=1"))
    service.set_account_enabled(account["id"], False)

    with pytest.raises(ICloudError, match="还没有可用的 iCloud 主号"):
        service.resolve_account()
    with pytest.raises(ICloudError) as excinfo:
        service.resolve_account("owner@icloud.com")
    assert excinfo.value.code == "account_disabled"
