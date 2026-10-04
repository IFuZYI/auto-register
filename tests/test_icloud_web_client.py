"""iCloud Web 客户端：请求构造、响应解析与上游错误归一化。"""

import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from requests.adapters import BaseAdapter

from platforms.icloud.build_info import BuildInfo, BuildInfoCache, parse_app_build
from platforms.icloud.constants import DEFAULT_ALIAS_LABEL
from platforms.icloud.constants import FALLBACK_CLOUD_BUILD
from platforms.icloud.credentials import ICloudCredentials
from platforms.icloud.errors import ICloudError
from platforms.icloud.models import SessionImportRequest
from platforms.icloud.transport import WebTransport, envelope_error
from platforms.icloud.web_client import ICloudWebClient, _parse_private_email_list


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


class _FixedBuildCache(BuildInfoCache):
    def get(self, http, region):
        return BuildInfo()


def _client(handler) -> tuple[ICloudWebClient, _StubAdapter]:
    adapter = _StubAdapter(handler)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    transport = WebTransport(session=session)
    return ICloudWebClient(transport, _FixedBuildCache()), adapter


def _credentials(**overrides) -> ICloudCredentials:
    base = {
        "region": "global",
        "dsid": "123456",
        "cookies": 'session=value; X-APPLE-WEBAUTH-USER="v=1%3As=1%3Ad=123456"',
        "hme_service_url": "https://p1-maildomainws.icloud.com/v1/hme",
        "client_id": "client-1",
        "client_build_number": "2626Build21",
        "client_mastering_number": "2626Build21",
    }
    base.update(overrides)
    return ICloudCredentials.from_dict(base)


def test_generate_private_email_reserves_and_returns_address():
    def handler(request):
        if request.url.split("?")[0].endswith("/v1/hme/generate"):
            return 200, {"success": True, "result": {"hme": "abc@icloud.com"}}, {}
        return 200, {"success": True, "result": {"hme": "abc@icloud.com", "anonymousId": "anon-1"}}, {}

    client, adapter = _client(handler)
    private_email = client.generate_private_email(_credentials(), label="任务", note="备注")

    assert private_email.address == "abc@icloud.com"
    assert private_email.provider_id == "anon-1"
    reserve_body = json.loads(adapter.requests[-1].body)
    assert reserve_body == {"hme": "abc@icloud.com", "label": "任务", "note": "备注"}


def test_blank_label_is_replaced_before_reserve():
    """标签在界面上是选填的，但 Apple 的 reserve 收到空标签会回 invalid Label。"""

    def handler(request):
        if request.url.split("?")[0].endswith("/v1/hme/generate"):
            return 200, {"success": True, "result": {"hme": "abc@icloud.com"}}, {}
        return 200, {"success": True, "result": {"hme": "abc@icloud.com"}}, {}

    client, adapter = _client(handler)
    private_email = client.generate_private_email(_credentials(), label="   ", note="")

    assert json.loads(adapter.requests[-1].body)["label"] == DEFAULT_ALIAS_LABEL
    assert private_email.label == DEFAULT_ALIAS_LABEL


def test_hme_endpoint_carries_dsid_and_build_numbers():
    client, adapter = _client(lambda _request: (200, {"success": True, "result": {"hmeEmails": []}}, {}))
    client.list_private_emails(_credentials())

    parsed = urlparse(adapter.requests[0].url)
    query = parse_qs(parsed.query)
    assert parsed.path == "/v1/hme/v2/hme/list"
    assert query["dsid"] == ["123456"]
    assert query["clientBuildNumber"] == [FALLBACK_CLOUD_BUILD]
    assert adapter.requests[0].headers["Cookie"] == (
        'session="value"; X-APPLE-WEBAUTH-USER="v=1%3As=1%3Ad=123456"'
    )


def test_hme_endpoint_recovers_exact_dsid_rounded_by_legacy_decoder():
    credentials = _credentials(
        dsid="9007199254740992",
        cookies='X-APPLE-WEBAUTH-USER="v=1%3As=1%3Ad=9007199254740993"',
    )
    client, adapter = _client(lambda _request: (200, {"success": True, "result": {"hmeEmails": []}}, {}))
    client.list_private_emails(credentials)

    query = parse_qs(urlparse(adapter.requests[0].url).query)
    assert query["dsid"] == ["9007199254740993"]


def test_hme_endpoint_rejects_unrelated_cookie_dsid():
    credentials = _credentials(
        dsid="111111111", cookies='X-APPLE-WEBAUTH-USER="v=1%3As=1%3Ad=222222222"'
    )
    client, _adapter = _client(lambda _request: (200, {}, {}))
    with pytest.raises(ICloudError, match="账号标识不一致"):
        client.list_private_emails(credentials)


def test_unauthorized_hme_response_marks_session_expired():
    client, _adapter = _client(lambda _request: (401, {}, {}))
    with pytest.raises(ICloudError) as excinfo:
        client.list_private_emails(_credentials())
    assert excinfo.value.code == "session_expired"


def test_private_email_list_requires_an_explicit_list_field():
    with pytest.raises(ICloudError, match="缺少邮箱列表"):
        _parse_private_email_list({"unrelated": []})

    parsed = _parse_private_email_list(
        {
            "hmeEmails": [
                {"hme": "A@icloud.com", "label": "x", "anonymousId": "1", "isActive": True},
                {"hme": "a@icloud.com", "label": "duplicate"},
                {"hme": "b@icloud.com", "isActive": False},
            ]
        }
    )
    assert [(item.address, item.status) for item in parsed] == [
        ("a@icloud.com", "active"),
        ("b@icloud.com", "disabled"),
    ]


def test_import_session_extracts_dsid_and_service_url():
    def handler(_request):
        return (
            200,
            {
                "dsInfo": {"dsid": "654321", "primaryEmail": "Owner@icloud.com"},
                "webservices": {
                    "premiummailsettings": {"url": "https://p1-maildomainws.icloud.com:443/v1/hme/"},
                    "mccgateway": {"url": "https://p1-mailws.icloud.com:443/"},
                },
            },
            {},
        )

    client, _adapter = _client(handler)
    imported = client.import_session(SessionImportRequest(cookie_header="a=1"))

    assert imported.account_email == "owner@icloud.com"
    assert imported.credentials.dsid == "654321"
    assert imported.credentials.hme_service_url == "https://p1-maildomainws.icloud.com/v1/hme"
    assert imported.credentials.mail_gateway_url == "https://p1-mailws.icloud.com"
    assert imported.masked_dsid == "65**21"


def test_import_session_reports_pending_terms():
    client, _adapter = _client(lambda _request: (200, {"dsInfo": {"termsUpdateNeeded": True}}, {}))
    with pytest.raises(ICloudError, match="服务条款"):
        client.import_session(SessionImportRequest(cookie_header="a=1"))


def test_envelope_error_normalizes_rate_limit_without_leaking_upstream_text():
    error = envelope_error(
        {"success": False, "error": {"errorCode": 429, "errorMessage": "slow down", "retryAfter": 12}},
        "fallback",
    )
    assert error.code == "provider_rate_limited"
    assert error.retry_after == 12
    assert "slow down" not in error.message


def test_parse_app_build_reads_icloud_page_attributes():
    html = (
        '<html><body><div data-cw-private-build-number="2700Build1" '
        'data-cw-private-mastering-number="2700Build2"></div></body></html>'
    )
    assert parse_app_build(html) == ("2700Build1", "2700Build2")

    with pytest.raises(ValueError):
        parse_app_build("<html><body></body></html>")
