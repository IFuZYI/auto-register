"""接码自检接口：前端设置页依赖的响应结构与失败时的状态码。"""

from unittest import mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.sms import router


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


@pytest.fixture(autouse=True)
def saved_settings():
    """默认没有任何已保存配置，测试各自决定要不要给。"""
    with mock.patch("api.sms.resolve_sms_settings", return_value={}) as resolved:
        yield resolved


def test_provider_list_exposes_labels_and_defaults(client):
    body = client.get("/api/sms/providers").json()

    values = {item["value"] for item in body["items"]}
    assert values == {"smsbower", "herosms"}
    assert body["default_service"] == "dr"
    assert body["openai_sms_countries"] == ["52"]


def test_country_options_carry_chinese_names(client):
    body = client.get("/api/sms/country-options").json()

    by_value = {item["value"]: item for item in body["items"]}
    assert by_value["52"]["name"] == "泰国"
    # 标签里保留 ID，下拉既能按中文搜也能按数字搜
    assert by_value["52"]["label"] == "泰国 (52)"
    assert by_value["52"]["openai_sms_whitelisted"] is True
    assert by_value["12"]["openai_sms_whitelisted"] is False
    assert body["default_country"] == "52"
    assert len(body["items"]) > 100


def test_balance_probe_uses_request_credentials(client):
    provider = mock.Mock()
    provider.get_balance.return_value = 12.5

    with mock.patch("api.sms.create_sms_provider", return_value=provider) as factory:
        body = client.post(
            "/api/sms/balance", json={"provider": "herosms", "api_key": "k"}
        ).json()

    assert body == {"provider": "herosms", "balance": 12.5}
    provider_key, settings = factory.call_args.args
    assert provider_key == "herosms"
    assert settings["sms_api_key"] == "k"


def test_balance_probe_falls_back_to_saved_settings(client, saved_settings):
    saved_settings.return_value = {"sms_provider": "smsbower", "sms_api_key": "saved"}
    provider = mock.Mock()
    provider.get_balance.return_value = 1.0

    with mock.patch("api.sms.create_sms_provider", return_value=provider) as factory:
        client.post("/api/sms/balance", json={})

    assert factory.call_args.args[1]["sms_api_key"] == "saved"


def test_missing_api_key_is_a_client_error(client):
    response = client.post("/api/sms/balance", json={"provider": "smsbower"})

    assert response.status_code == 400
    assert "API Key" in response.json()["detail"]


def test_upstream_failure_is_reported_as_bad_gateway(client):
    provider = mock.Mock()
    provider.get_balance.side_effect = RuntimeError("BAD_KEY")

    with mock.patch("api.sms.create_sms_provider", return_value=provider):
        response = client.post("/api/sms/balance", json={"api_key": "k"})

    assert response.status_code == 502
    assert "BAD_KEY" in response.json()["detail"]


def test_country_ranking_is_annotated_and_truncated(client):
    provider = mock.Mock()
    provider.get_top_countries.return_value = [
        {"country": "52", "price": 0.3, "count": 300},
        {"country": "16", "price": 0.4, "count": 20},
    ]

    with mock.patch("api.sms.create_sms_provider", return_value=provider):
        body = client.post(
            "/api/sms/countries", json={"api_key": "k", "service": "dr", "limit": 1}
        ).json()

    assert body["service"] == "dr"
    assert body["items"] == [
        {
            "country": "52",
            "name": "泰国",
            "price": 0.3,
            "count": 300,
            "openai_sms_whitelisted": True,
        }
    ]


def test_non_whitelisted_country_is_flagged(client):
    provider = mock.Mock()
    provider.get_top_countries.return_value = [{"country": "16", "price": 0.4, "count": 20}]

    with mock.patch("api.sms.create_sms_provider", return_value=provider):
        body = client.post("/api/sms/countries", json={"api_key": "k"}).json()

    assert body["items"][0]["openai_sms_whitelisted"] is False
    assert body["items"][0]["name"] == "英国"


def test_country_ranking_failure_is_reported_as_bad_gateway(client):
    provider = mock.Mock()
    provider.get_top_countries.side_effect = RuntimeError("超时")

    with mock.patch("api.sms.create_sms_provider", return_value=provider):
        response = client.post("/api/sms/countries", json={"api_key": "k"})

    assert response.status_code == 502
