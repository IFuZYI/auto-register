"""iCloud IMAP 收件：连接参数解析、MIME 解析与隐私邮箱归类。"""

import imaplib
from email.message import EmailMessage

import pytest

from platforms.icloud import mailbox as mailbox_module
from platforms.icloud.credentials import ICloudCredentials
from platforms.icloud.delivery import delivered_to, delivery_addresses
from platforms.icloud.errors import ICloudError
from platforms.icloud.mailbox import fetch_inbox, resolve_imap_target


def _credentials(**overrides) -> ICloudCredentials:
    base = {"imap_password": "app-specific", "imap_username": "owner@icloud.com"}
    base.update(overrides)
    return ICloudCredentials.from_dict(base)


def _raw_message(*, subject: str, to: str, original_to: str = "", body: str = "验证码 123456") -> bytes:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = "OpenAI <noreply@openai.com>"
    message["To"] = to
    message["Date"] = "Mon, 03 Aug 2026 10:00:00 +0000"
    if original_to:
        message["X-Original-To"] = original_to
    message.set_content(body)
    return message.as_bytes()


class _FakeIMAP:
    """够用的 imaplib 替身，按 UID 返回预置邮件。"""

    instances: list["_FakeIMAP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.logged_in_as = None
        self.selected = None
        _FakeIMAP.instances.append(self)

    messages: list[tuple[int, bytes]] = []

    def login(self, username, password):
        if password != "app-specific":
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED")
        self.logged_in_as = username
        return "OK", [b"LOGIN completed"]

    def select(self, mailbox, readonly=False):
        self.selected = (mailbox, readonly)
        return "OK", [str(len(self.messages)).encode()]

    def response(self, name):
        return name, [b"77"] if name == "UIDVALIDITY" else [None]

    def fetch(self, _sequence, _items):
        payload = []
        for uid, raw in self.messages:
            descriptor = f'1 (UID {uid} FLAGS (\\Seen) INTERNALDATE "03-Aug-2026 10:00:00 +0000" BODY[] {{{len(raw)}}}'
            payload.append((descriptor.encode(), raw))
        return "OK", payload

    def logout(self):
        return "BYE", [b""]


@pytest.fixture
def fake_imap(monkeypatch):
    _FakeIMAP.instances = []
    _FakeIMAP.messages = []
    monkeypatch.setattr(mailbox_module.imaplib, "IMAP4_SSL", _FakeIMAP)
    return _FakeIMAP


def test_resolve_imap_target_defaults_to_icloud_tls_endpoint():
    target = resolve_imap_target(_credentials(), "owner@icloud.com")
    assert (target.host, target.port, target.username) == (
        "imap.mail.me.com",
        993,
        "owner@icloud.com",
    )


def test_resolve_imap_target_accepts_host_with_port():
    target = resolve_imap_target(_credentials(imap_host="mail.example.com:1993"), "owner@icloud.com")
    assert (target.host, target.port) == ("mail.example.com", 1993)


@pytest.mark.parametrize(
    "overrides",
    [
        {"imap_host": "https://mail.example.com"},
        {"imap_host": "mail.example.com:abc"},
        {"imap_password": "   "},
    ],
)
def test_resolve_imap_target_rejects_invalid_configuration(overrides):
    with pytest.raises(ICloudError):
        resolve_imap_target(_credentials(**overrides), "owner@icloud.com")


def test_fetch_inbox_parses_messages_and_builds_stable_ids(fake_imap):
    fake_imap.messages = [(202, _raw_message(subject="欢迎", to="alias@icloud.com"))]

    messages = fetch_inbox(_credentials(), "owner@icloud.com")

    assert len(messages) == 1
    message = messages[0]
    assert message.provider_message_id == "INBOX:77:202"
    assert message.subject == "欢迎"
    assert message.sender.email == "noreply@openai.com"
    assert "验证码 123456" in message.text_body
    assert message.is_read is True


def test_fetch_inbox_filters_by_privacy_alias_delivery_header(fake_imap):
    fake_imap.messages = [
        (1, _raw_message(subject="给别人", to="other@icloud.com")),
        (
            2,
            _raw_message(
                subject="给我", to="owner@icloud.com", original_to="alias@icloud.com"
            ),
        ),
    ]

    messages = fetch_inbox(_credentials(), "owner@icloud.com", recipient="alias@icloud.com")

    assert [message.subject for message in messages] == ["给我"]
    assert messages[0].alias_address == "alias@icloud.com"


def test_fetch_inbox_reports_invalid_app_specific_password(fake_imap):
    with pytest.raises(ICloudError) as excinfo:
        fetch_inbox(_credentials(imap_password="wrong"), "owner@icloud.com")
    assert excinfo.value.code == "invalid_credentials"


def test_delivery_addresses_prefer_alias_then_headers_then_recipients():
    addresses = delivery_addresses(
        alias_address="alias@icloud.com",
        headers={"X-Original-To": "rfc822;forwarded@icloud.com"},
        to=["owner@icloud.com"],
    )
    assert addresses == ["alias@icloud.com", "forwarded@icloud.com", "owner@icloud.com"]
    assert delivered_to(addresses, "FORWARDED@icloud.com") is True
    assert delivered_to(addresses, "stranger@icloud.com") is False
