"""iCloud 覆盖率补齐：web_mail / messages / build_info / utils 的未覆盖分支。

约定与既有测试一致：

* Web 传输用 `_StubAdapter` 挂到 requests.Session 上（见
  tests/test_icloud_web_client.py），绝不走真实网络；
* 业务层用 monkeypatch 打桩 get_account / load_credentials / fetch_inbox；
* 需要落库的用例（别名收件、分享 token）用 tmp_path 建独立 sqlite 并指向
  `platform_database_registry`，与 test_shared_mail_page.py 的做法相同。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from requests.adapters import BaseAdapter
from sqlmodel import Session, SQLModel, create_engine

from core.db import ICloudAccountModel, ICloudAliasModel
from platforms.icloud import web_mail as web_mail_module
from platforms.icloud.build_info import (
    MAIL_BUILD_PAGE_PATH,
    BuildInfo,
    BuildInfoCache,
    parse_app_build,
)
from platforms.icloud.constants import (
    FALLBACK_CLOUD_BUILD,
    FALLBACK_MAIL_BUILD,
    FALLBACK_MAIL_MASTERING,
)
from platforms.icloud.credentials import ICloudCredentials
from platforms.icloud.errors import ICloudError
from platforms.icloud.models import MailAddress, MailMessage, utcnow
from platforms.icloud.transport import WebTransport
from platforms.icloud.utils import (
    normalize_email_address,
    parse_address_list,
    six_digit_code,
    strip_html,
    truncate_text,
)
from platforms.icloud.web_mail import (
    _search_url,
    _strip_default_port,
    _thread_to_message,
    _timestamp,
    fetch_inbox_web,
)
from services.icloud import messages as messages_module
from services.icloud.messages import (
    fetch_account_messages,
    fetch_account_messages_detailed,
    fetch_alias_messages,
    fetch_alias_messages_detailed,
    fetch_latest_shared_message,
)


# --------------------------------------------------------------- web_mail.py


class _StubAdapter(BaseAdapter):
    """把每个请求交给测试提供的处理函数，同时记录调用序列。"""

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.requests = []

    def send(self, request, **_kwargs):
        self.requests.append(request)
        status, payload, headers = self.handler(request)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        response.url = request.url
        response.request = request
        response.headers.update({"Content-Type": "application/json", **(headers or {})})
        return response

    def close(self):
        pass


class _FakeResponse:
    """给自建 transport 替身用的最小响应对象。"""

    def __init__(self, payload, *, status=200):
        self._payload = payload
        self.status_code = status
        self.ok = 200 <= status < 400
        self.headers: dict[str, str] = {}

    def json(self):
        return self._payload


def _stub_transport(handler) -> tuple[WebTransport, _StubAdapter]:
    adapter = _StubAdapter(handler)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return WebTransport(session=session), adapter


def _credentials(**overrides) -> ICloudCredentials:
    base = {
        "region": "global",
        "dsid": "123456",
        "cookies": "session=value",
        "hme_service_url": "https://p1-maildomainws.icloud.com/v1/hme",
        "mail_gateway_url": "https://p1-mailws.icloud.com",
        "client_id": "client-1",
        "mail_client_build_number": "2700Build1",
        "mail_client_mastering_number": "2700Build2",
    }
    base.update(overrides)
    return ICloudCredentials.from_dict(base)


def _thread(thread_id: str = "t-1", **overrides) -> dict:
    base = {
        "threadId": thread_id,
        "subject": "验证码",
        "senders": ["sender@example.com"],
        "preview": "code 123456",
        "timestamp": 1700000000000,
    }
    base.update(overrides)
    return base


def _messages_handler(threads: list) -> "callable":
    def handler(_request):
        return 200, {"success": True, "threadList": threads}, {}

    return handler


def test_fetch_inbox_web_requires_a_web_session():
    with pytest.raises(ICloudError) as excinfo:
        fetch_inbox_web(_credentials(cookies="", dsid="", hme_service_url=""))

    assert excinfo.value.code == "invalid_config"
    assert "Web 会话" in str(excinfo.value)


def test_fetch_inbox_web_requires_a_mail_gateway_url():
    with pytest.raises(ICloudError) as excinfo:
        fetch_inbox_web(_credentials(mail_gateway_url=""))

    assert excinfo.value.code == "invalid_config"
    assert "mccgateway" in str(excinfo.value)


def test_fetch_inbox_web_maps_threads_to_messages():
    transport, _adapter = _stub_transport(_messages_handler([_thread("t-7")]))

    messages = fetch_inbox_web(_credentials(), limit=10, transport=transport)

    assert len(messages) == 1
    message = messages[0]
    assert message.provider_message_id == "web:t-7"
    assert message.mailbox == "INBOX"
    assert message.subject == "验证码"
    assert message.snippet == "code 123456"
    assert message.sender.email == "sender@example.com"
    assert message.received_at == datetime.fromtimestamp(1700000000000 / 1000, tz=timezone.utc)
    assert message.headers == {"web_api": "1"}
    assert message.alias_address == ""


def test_fetch_inbox_web_clamps_limit_and_doubles_it_for_alias_lookup():
    seen: list[dict] = []

    def handler(request):
        seen.append(json.loads(request.body))
        return 200, {"success": True, "threadList": []}, {}

    transport, _adapter = _stub_transport(handler)

    fetch_inbox_web(_credentials(), limit=500, transport=transport)
    fetch_inbox_web(_credentials(), limit=-3, transport=transport)
    fetch_inbox_web(_credentials(), limit=None, transport=transport)
    fetch_inbox_web(_credentials(), limit=20, recipient="alias@icloud.com", transport=transport)

    assert [body["maxResults"] for body in seen] == [100, 1, 20, 40]
    assert all(body["responseType"] == "THREAD_DIGEST" for body in seen)
    assert seen[-1]["sessionHeaders"]["folder"] == "INBOX"
    assert seen[-1]["sessionHeaders"]["threadmode"] == 1


def test_fetch_inbox_web_filters_by_alias_locally():
    """Web API 不支持服务端按收件人搜索，别名只能靠本地子串匹配。"""
    threads = [
        _thread("hit", subject="给 alias@icloud.com 的邮件"),
        _thread("miss", subject="无关邮件"),
    ]
    transport, _adapter = _stub_transport(_messages_handler(threads))

    messages = fetch_inbox_web(
        _credentials(), limit=10, recipient=" ALIAS@icloud.com ", transport=transport
    )

    assert [item.provider_message_id for item in messages] == ["web:hit"]
    assert messages[0].alias_address == "alias@icloud.com"


def test_thread_search_sends_session_headers_and_filters_extra_headers():
    captured: dict = {}

    def handler(request):
        captured["headers"] = dict(request.headers)
        return 200, {"success": True, "threadList": []}, {}

    transport, _adapter = _stub_transport(handler)
    credentials = _credentials(
        cookies='session="value"',
        web_auth_token="token-1",
        web_auth_token_header="X-APPLE-WEBAUTH-TOKEN",
        extra_headers={
            "X-Custom": "1",
            "Host": "evil.example.com",
            "Content-Length": "5",
            "Cookie": "attacker=1",
        },
    )

    fetch_inbox_web(credentials, transport=transport)

    headers = captured["headers"]
    assert headers["Cookie"] == 'session="value"'
    assert headers["X-APPLE-WEBAUTH-TOKEN"] == "token-1"
    assert headers["X-Custom"] == "1"
    # Host / Content-Length / Cookie 三个键被过滤：外部 extra_headers 不能覆盖会话
    assert "evil.example.com" not in headers.values()
    assert headers["Content-Length"] != "5"
    assert "attacker=1" not in headers["Cookie"]


def test_fetch_inbox_web_closes_transport_it_created(monkeypatch):
    created = []

    class _RecordingTransport:
        def __init__(self, *, proxy=None, timeout=None):
            self.proxy = proxy
            self.closed = False
            created.append(self)

        def request(self, method, url, **kwargs):
            return _FakeResponse({"success": True, "threadList": [_thread("t-1")]})

        def close(self):
            self.closed = True

    monkeypatch.setattr(web_mail_module, "WebTransport", _RecordingTransport)

    messages = fetch_inbox_web(_credentials(), limit=3, proxy="http://127.0.0.1:8080")

    assert [item.provider_message_id for item in messages] == ["web:t-1"]
    assert len(created) == 1
    assert created[0].proxy == "http://127.0.0.1:8080"
    assert created[0].closed is True


def test_thread_search_network_failure_becomes_upstream_unavailable():
    def handler(_request):
        raise requests.ConnectionError("connection reset by peer")

    transport, _adapter = _stub_transport(handler)

    with pytest.raises(ICloudError) as excinfo:
        fetch_inbox_web(_credentials(), transport=transport)

    assert excinfo.value.code == "upstream_unavailable"


def test_thread_search_http_error_is_normalized_for_mail_service():
    transport, _adapter = _stub_transport(lambda _request: (403, {"success": False}, {}))

    with pytest.raises(ICloudError) as excinfo:
        fetch_inbox_web(_credentials(), transport=transport)

    assert excinfo.value.code == "mail_access_denied"


def test_thread_search_rejects_invalid_json_body():
    transport, _adapter = _stub_transport(lambda _request: (200, b"<html>not json</html>", {}))

    with pytest.raises(ICloudError) as excinfo:
        fetch_inbox_web(_credentials(), transport=transport)

    assert excinfo.value.code == "invalid_response"


def test_thread_search_rejects_unsuccessful_envelope():
    transport, _adapter = _stub_transport(
        lambda _request: (200, {"success": False, "error": {"errorCode": 1}}, {})
    )

    with pytest.raises(ICloudError) as excinfo:
        fetch_inbox_web(_credentials(), transport=transport)

    assert excinfo.value.code == "upstream_rejected"


def test_thread_search_tolerates_missing_or_junk_thread_list():
    transport, _adapter = _stub_transport(
        lambda _request: (200, {"success": True, "threadList": ["junk", None, _thread("ok")]}, {})
    )
    messages = fetch_inbox_web(_credentials(), transport=transport)
    assert [item.provider_message_id for item in messages] == ["web:ok"]

    transport2, _adapter2 = _stub_transport(lambda _request: (200, {"success": True}, {}))
    assert fetch_inbox_web(_credentials(), transport=transport2) == []


def test_search_url_carries_build_numbers_and_strips_dsid_quotes():
    credentials = _credentials(
        dsid='"987654321"',
        mail_client_build_number="",
        mail_client_mastering_number="   ",
    )

    parsed = urlparse(_search_url(credentials, limit=10, recipient=""))
    query = parse_qs(parsed.query)

    assert parsed.netloc == "p1-mailws.icloud.com"
    assert parsed.path == "/mailws2/v1/thread/search"
    assert query["clientBuildNumber"] == [FALLBACK_MAIL_BUILD]
    assert query["clientMasteringNumber"] == [FALLBACK_MAIL_MASTERING]
    assert query["dsid"] == ["987654321"]
    assert query["clientId"] == ["client-1"]


def test_search_url_omits_blank_client_id():
    query = parse_qs(urlparse(_search_url(_credentials(client_id=""), limit=5, recipient="")).query)

    assert "clientId" not in query
    assert query["clientBuildNumber"] == ["2700Build1"]


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://p1-mailws.icloud.com:443", "https://p1-mailws.icloud.com"),
        ("https://p1-mailws.icloud.com:443/", "https://p1-mailws.icloud.com/"),
        ("https://p1-mailws.icloud.com/", "https://p1-mailws.icloud.com/"),
        ("https://p1-mailws.icloud.com:8443/", "https://p1-mailws.icloud.com:8443/"),
        ("  https://x.com:443  ", "https://x.com"),
        ("not a url", "not a url"),
        ("", ""),
    ],
)
def test_strip_default_port(value, expected):
    """带 :443 的 host 附加不上 Cookie，Apple 会回 403。"""
    assert _strip_default_port(value) == expected


def test_search_url_strips_the_default_port_from_the_gateway():
    parsed = urlparse(
        _search_url(_credentials(mail_gateway_url="https://p1-mailws.icloud.com:443/"), limit=5, recipient="")
    )

    assert parsed.netloc == "p1-mailws.icloud.com"


def test_thread_to_message_maps_fields_and_truncates_preview():
    message = _thread_to_message(
        _thread("t-9", subject="主题", senders=["A@X.com"], preview="<p>" + "x" * 400 + "</p>"),
        "",
    )

    assert message.provider_message_id == "web:t-9"
    assert message.sender.email == "A@X.com"
    assert message.snippet == "x" * 240 + "..."
    assert message.text_body == message.snippet
    assert message.headers == {"web_api": "1"}


def test_thread_to_message_tolerates_missing_fields():
    message = _thread_to_message({}, "")

    assert message.provider_message_id == "web:"
    assert message.sender.email == ""
    assert message.subject == ""
    assert message.snippet == ""
    assert message.received_at.tzinfo is timezone.utc


def test_thread_to_message_handles_odd_senders_and_alias_matching():
    odd = _thread_to_message({"senders": "not-a-list", "subject": "alias@icloud.com 的验证码"}, "alias@icloud.com")
    assert odd.sender.email == ""
    assert odd.alias_address == "alias@icloud.com"

    empty = _thread_to_message({"senders": [], "subject": "无关"}, "alias@icloud.com")
    assert empty.alias_address == ""

    in_preview = _thread_to_message({"preview": "to alias@icloud.com"}, "alias@icloud.com")
    assert in_preview.alias_address == "alias@icloud.com"


def test_timestamp_converts_milliseconds():
    assert _timestamp(1700000000000) == datetime.fromtimestamp(1700000000000 / 1000, tz=timezone.utc)


@pytest.mark.parametrize("value", [None, "abc", 0, "0", -5, int("9" * 30), 1e300])
def test_timestamp_falls_back_to_now_for_unusable_values(value):
    result = _timestamp(value)

    assert result.tzinfo is timezone.utc
    assert abs((result - utcnow()).total_seconds()) < 5


# ------------------------------------------------------- services/icloud/messages.py


def _msg(provider_id: str = "m-1") -> MailMessage:
    return MailMessage(provider_message_id=provider_id, mailbox="INBOX", subject="验证码 123456")


class _AccountRow:
    def __init__(self, email: str = "owner@icloud.com") -> None:
        self.email = email


@pytest.fixture
def icloud_db(tmp_path, monkeypatch):
    """独立 sqlite：别名收件与分享 token 要真的查库。"""
    import core.db as db
    from core.db import platform_database_registry

    engine = create_engine(f"sqlite:///{tmp_path / 'icloud_gaps.db'}")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db, "engine", engine)
    platform_database_registry.configure({"icloud": str(engine.url)})

    with Session(engine) as session:
        account = ICloudAccountModel(email="owner@icloud.com")
        session.add(account)
        session.commit()
        session.refresh(account)
        alias = ICloudAliasModel(
            account_id=account.id, address="alias@icloud.com", share_token="share-token-123"
        )
        session.add(alias)
        session.commit()
        session.refresh(alias)
        ids = {"account_id": account.id, "alias_id": alias.id}

    yield ids
    platform_database_registry.configure({})
    engine.dispose()


def _stub_account(monkeypatch, credentials: ICloudCredentials) -> None:
    monkeypatch.setattr(messages_module, "get_account", lambda _account_id: _AccountRow())
    monkeypatch.setattr(messages_module, "load_credentials", lambda _row: credentials)


def test_detailed_returns_imap_messages(monkeypatch):
    _stub_account(monkeypatch, _credentials(imap_password="app-specific"))
    sentinel = [_msg("imap-1")]
    monkeypatch.setattr(messages_module, "fetch_inbox", lambda creds, email, **kwargs: sentinel)

    messages, method, warning = fetch_account_messages_detailed(7, limit=5, recipient="alias@icloud.com")

    assert messages == sentinel
    assert (method, warning) == ("imap", "")


def test_fetch_account_messages_wrapper_returns_just_the_list(monkeypatch):
    _stub_account(monkeypatch, _credentials(imap_password="app-specific"))
    monkeypatch.setattr(messages_module, "fetch_inbox", lambda creds, email, **kwargs: [_msg("imap-2")])

    assert [item.provider_message_id for item in fetch_account_messages(7)] == ["imap-2"]


def test_imap_failure_falls_back_to_web_api(monkeypatch):
    _stub_account(monkeypatch, _credentials(imap_password="app-specific"))
    seen: dict = {}

    def _fail(*_args, **_kwargs):
        raise ICloudError("invalid_credentials", "App 专用密码已撤销")

    def _fetch_web(creds, *, limit, recipient, proxy=None):
        seen.update(credentials=creds, limit=limit, recipient=recipient, proxy=proxy)
        return [_msg("web-1")]

    monkeypatch.setattr(messages_module, "fetch_inbox", _fail)
    monkeypatch.setattr(web_mail_module, "fetch_inbox_web", _fetch_web)

    messages, method, warning = fetch_account_messages_detailed(
        7, limit=9, recipient="alias@icloud.com", proxy="http://127.0.0.1:9000"
    )

    assert [item.provider_message_id for item in messages] == ["web-1"]
    assert method == "web_api"
    assert "IMAP 不可用，已回退 Web API" in warning
    assert "App 专用密码已撤销" in warning
    assert seen["recipient"] == "alias@icloud.com"
    assert seen["limit"] == 9
    assert seen["proxy"] == "http://127.0.0.1:9000"


def test_imap_failure_without_web_session_is_reraised(monkeypatch):
    _stub_account(
        monkeypatch, _credentials(imap_password="app-specific", cookies="", dsid="", hme_service_url="")
    )

    def _fail(*_args, **_kwargs):
        raise ICloudError("invalid_credentials", "App 专用密码已撤销")

    monkeypatch.setattr(messages_module, "fetch_inbox", _fail)

    with pytest.raises(ICloudError) as excinfo:
        fetch_account_messages_detailed(7)

    assert excinfo.value.code == "invalid_credentials"


def test_web_only_account_uses_web_api_with_a_warning(monkeypatch):
    _stub_account(monkeypatch, _credentials(imap_password=""))
    monkeypatch.setattr(web_mail_module, "fetch_inbox_web", lambda creds, **kwargs: [_msg("web-2")])

    messages, method, warning = fetch_account_messages_detailed(7)

    assert [item.provider_message_id for item in messages] == ["web-2"]
    assert method == "web_api"
    assert "未配置 IMAP" in warning


def test_account_without_any_credential_raises_invalid_config(monkeypatch):
    _stub_account(
        monkeypatch, _credentials(imap_password="", cookies="", dsid="", hme_service_url="")
    )

    with pytest.raises(ICloudError) as excinfo:
        fetch_account_messages_detailed(7)

    assert excinfo.value.code == "invalid_config"
    assert "owner@icloud.com" in str(excinfo.value)


def test_alias_messages_look_up_the_alias_recipient(icloud_db, monkeypatch):
    captured: dict = {}

    def _detailed(account_id, *, limit, recipient):
        captured.update(account_id=account_id, limit=limit, recipient=recipient)
        return ([_msg("alias-1")], "web_api", "w")

    monkeypatch.setattr(messages_module, "fetch_account_messages_detailed", _detailed)

    messages = fetch_alias_messages(icloud_db["alias_id"], limit=11)

    assert [item.provider_message_id for item in messages] == ["alias-1"]
    assert captured == {
        "account_id": icloud_db["account_id"],
        "limit": 11,
        "recipient": "alias@icloud.com",
    }


def test_missing_alias_raises_alias_not_found(icloud_db):
    with pytest.raises(ICloudError) as excinfo:
        fetch_alias_messages_detailed(987654)

    assert excinfo.value.code == "alias_not_found"


def test_latest_shared_message_rejects_blank_tokens():
    for token in ("", "   ", None):
        with pytest.raises(ICloudError) as excinfo:
            fetch_latest_shared_message(token)
        assert excinfo.value.code == "alias_not_found"


def test_latest_shared_message_returns_address_and_newest(icloud_db, monkeypatch):
    import services.icloud_service as facade

    newest = _msg("newest")
    seen: dict = {}

    def _fetch(account_id, *, limit, recipient):
        seen.update(account_id=account_id, limit=limit, recipient=recipient)
        return [newest, _msg("older")]

    monkeypatch.setattr(facade, "fetch_account_messages", _fetch)

    address, message = fetch_latest_shared_message("share-token-123")

    assert address == "alias@icloud.com"
    assert message is newest
    assert seen["recipient"] == "alias@icloud.com"
    assert seen["account_id"] == icloud_db["account_id"]


def test_latest_shared_message_with_empty_inbox_returns_none(icloud_db, monkeypatch):
    import services.icloud_service as facade

    monkeypatch.setattr(facade, "fetch_account_messages", lambda *a, **k: [])

    address, message = fetch_latest_shared_message("share-token-123")

    assert address == "alias@icloud.com"
    assert message is None


def test_latest_shared_message_unknown_token(icloud_db):
    with pytest.raises(ICloudError) as excinfo:
        fetch_latest_shared_message("no-such-token")

    assert excinfo.value.code == "alias_not_found"


# ------------------------------------------------------------ build_info.py


def _page_html(build: str = "2700Build1", mastering: str = "2700Build2") -> str:
    return (
        "<html><body>"
        f'<div data-cw-private-build-number="{build}" '
        f'data-cw-private-mastering-number="{mastering}"></div>'
        "</body></html>"
    )


class _PageResponse:
    def __init__(self, text: str, *, status: int = 200) -> None:
        self.text = text
        self.status_code = status

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400


class _StubHTTP:
    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return self.handler(url)


def test_parse_app_build_prefers_the_first_complete_element():
    html = _page_html("A", "B") + _page_html("C", "D")

    assert parse_app_build(html) == ("A", "B")


def test_parse_app_build_ignores_elements_with_only_one_attribute():
    html = (
        '<div data-cw-private-build-number="A"></div>'
        '<div data-cw-private-mastering-number="B"></div>'
    )

    with pytest.raises(ValueError):
        parse_app_build(html)


def test_cache_discovers_builds_and_caches_per_region():
    http = _StubHTTP(lambda _url: _PageResponse(_page_html()))
    cache = BuildInfoCache()

    info = cache.get(http, "global")

    assert info == BuildInfo(
        cloud_build="2700Build1",
        cloud_mastering="2700Build2",
        mail_build="2700Build1",
        mail_mastering="2700Build2",
        discovered=True,
    )
    assert http.calls == [
        "https://www.icloud.com/",
        "https://www.icloud.com" + MAIL_BUILD_PAGE_PATH,
    ]

    # 第二次命中缓存，不再抓页面
    assert cache.get(http, "global") == info
    assert len(http.calls) == 2

    # 不同区域各自探测（cn 走 icloud.com.cn）
    cache.get(http, "cn")
    assert http.calls[-2:] == [
        "https://www.icloud.com.cn/",
        "https://www.icloud.com.cn" + MAIL_BUILD_PAGE_PATH,
    ]


def test_cache_returns_stale_entry_when_refresh_fails():
    state = {"fail": False}

    def handler(url):
        if state["fail"]:
            raise RuntimeError("timeout")
        return _PageResponse(_page_html())

    http = _StubHTTP(handler)
    cache = BuildInfoCache(ttl_seconds=0)

    fresh = cache.get(http, "global")
    state["fail"] = True
    stale = cache.get(http, "global")

    assert stale == fresh
    assert stale.discovered is True


def test_cache_falls_back_to_builtin_constants_without_any_cache():
    http = _StubHTTP(lambda _url: (_ for _ in ()).throw(RuntimeError("timeout")))
    cache = BuildInfoCache()

    info = cache.get(http, "global")

    assert info == BuildInfo()
    assert info.discovered is False
    assert info.cloud_build == FALLBACK_CLOUD_BUILD
    assert info.mail_build == FALLBACK_MAIL_BUILD


def test_cache_falls_back_when_only_the_mail_page_fails():
    def handler(url):
        if url.endswith("rootDomain=www"):
            return _PageResponse("missing", status=404)
        return _PageResponse(_page_html())

    info = BuildInfoCache().get(_StubHTTP(handler), "global")

    assert info.discovered is False


def test_cache_invalidate_forces_rediscovery():
    http = _StubHTTP(lambda _url: _PageResponse(_page_html()))
    cache = BuildInfoCache()

    cache.get(http, "cn")
    assert len(http.calls) == 2

    cache.invalidate("CN")  # 区域名先归一化再匹配
    cache.get(http, "cn")

    assert len(http.calls) == 4


def test_fetch_retries_once_after_a_transient_failure():
    attempts: list[str] = []

    def handler(url):
        attempts.append(url)
        if len(attempts) == 1:
            raise RuntimeError("connection reset")
        return _PageResponse(_page_html())

    result = BuildInfoCache._fetch(_StubHTTP(handler), "https://www.icloud.com/")

    assert result == ("2700Build1", "2700Build2")
    assert len(attempts) == 2


def test_fetch_gives_up_after_two_failed_attempts():
    def raising(url):
        raise RuntimeError("boom")

    http = _StubHTTP(raising)
    assert BuildInfoCache._fetch(http, "https://www.icloud.com/") is None
    assert len(http.calls) == 2

    http2 = _StubHTTP(lambda _url: _PageResponse("nope", status=500))
    assert BuildInfoCache._fetch(http2, "https://www.icloud.com/") is None
    assert len(http2.calls) == 2


def test_fetch_rejects_a_page_without_build_attributes():
    http = _StubHTTP(lambda _url: _PageResponse("<html><body>nothing</body></html>"))

    assert BuildInfoCache._fetch(http, "https://www.icloud.com/") is None


# ---------------------------------------------------------------- utils.py


@pytest.mark.parametrize(
    "value,expected",
    [
        ("123456", True),
        (" 123456 ", True),
        (123456, True),
        ("12345", False),
        ("1234567", False),
        ("12a456", False),
        ("", False),
        (None, False),
    ],
)
def test_six_digit_code(value, expected):
    assert six_digit_code(value) is expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("A@B.com", "a@b.com"),
        ("<a@b.com>", "a@b.com"),
        ("  a@b.com  ", "a@b.com"),
        ("not-an-email", ""),
        ("a b@c.com", ""),
        ("a@b", ""),
        ("a@b.c@d", ""),
        ("", ""),
    ],
)
def test_normalize_email_address(value, expected):
    assert normalize_email_address(value) == expected


def test_parse_address_list_keeps_valid_entries_and_drops_junk():
    parsed = parse_address_list('"OpenAI" <NoReply@OpenAI.com>, broken, c@d.com, <e@f.com>')

    assert parsed == [
        ("OpenAI", "noreply@openai.com"),
        ("", "c@d.com"),
        ("", "e@f.com"),
    ]
    assert parse_address_list("") == []


def test_strip_html_skips_non_visible_tags_and_collapses_whitespace():
    assert strip_html("<p>你好 <b>世界</b></p>") == "你好 世界"
    assert strip_html("<script>alert(1)</script><style>.a{}</style>ok") == "ok"
    assert strip_html("<head><title>t</title></head>body") == "body"
    assert strip_html("<noscript>n</noscript><template>t</template><svg><path/></svg>visible") == "visible"
    assert strip_html("a &amp; b") == "a & b"
    assert strip_html(None) == ""


def test_strip_html_returns_empty_string_when_the_parser_blows_up(monkeypatch):
    class _BoomParser:
        def feed(self, _value):
            raise RuntimeError("parser exploded")

    monkeypatch.setattr("platforms.icloud.utils._TextExtractor", _BoomParser)

    assert strip_html("<p>hi</p>") == ""


def test_truncate_text_collapses_and_truncates():
    assert truncate_text("a  b\nc", 10) == "a b c"
    assert truncate_text("abcdef", 6) == "abcdef"
    assert truncate_text("abcdef", 3) == "abc..."
    assert truncate_text(None, 5) == ""
