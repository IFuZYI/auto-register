"""远程 icloud-hme 客户端：API 秘钥鉴权、错误码映射、数据接口。

测试策略（按 TDD 技能「Doubles for external services」条）：
- 双端是**真实 HTTP 服务**（见 `tests/remote_hme_double.py`），不是 in-process
  mock —— 客户端如果没把请求发到双端（比如硬编码了生产地址），这里会直接
  连接失败，而不是拿到一句看似合理的上游报错。
- 双端**只认 `X-API-Key`**：缺头或值不对回 `401 INVALID_API_KEY`；客户端
  必须每个请求都带秘钥，且不再有 `/api/auth/login`、Cookie、CSRF 那套东西。
"""
from __future__ import annotations

import pytest

from platforms.icloud.errors import ICloudError
from platforms.icloud.remote_client import RemoteHMEClient
from tests.remote_hme_double import API_KEY, RemoteHMEDouble


@pytest.fixture
def remote():
    double = RemoteHMEDouble()
    double.start()
    try:
        yield double
    finally:
        double.stop()


@pytest.fixture
def client(remote):
    return RemoteHMEClient(remote.base_url, API_KEY, timeout=5)


# ------------------------------------------------------------------ 鉴权


class TestApiKeyAuth:
    def test_key_is_sent_on_every_request(self, remote, client):
        client.list_accounts()
        client.list_aliases("acc_1")
        assert remote.seen_keys == [API_KEY, API_KEY]

    def test_no_login_request_is_made(self, remote, client):
        client.create_alias("acc_1", "GitHub")
        assert all(path != "/api/auth/login" for _, path, _ in remote.requests)

    def test_bad_key_maps_to_invalid_config(self, remote):
        bad = RemoteHMEClient(remote.base_url, "sk-wrong-key", timeout=5)
        with pytest.raises(ICloudError) as excinfo:
            bad.list_accounts()
        assert excinfo.value.code == "invalid_config"

    def test_missing_key_fails_before_sending(self, remote):
        empty = RemoteHMEClient(remote.base_url, "", timeout=5)
        with pytest.raises(ICloudError) as excinfo:
            empty.list_accounts()
        assert excinfo.value.code == "invalid_config"
        assert remote.requests == []

    def test_unreachable_service_maps_to_upstream_unavailable(self):
        dead = RemoteHMEClient("http://127.0.0.1:1", API_KEY, timeout=1)
        with pytest.raises(ICloudError) as excinfo:
            dead.list_accounts()
        assert excinfo.value.code == "upstream_unavailable"

    def test_write_requests_work_without_csrf(self, remote, client):
        client.create_alias("acc_1", "GitHub")
        client.delete_alias("acc_1", "anon-9")
        client.set_alias_active("acc_1", "anon-9", False)
        client.set_alias_active("acc_1", "anon-9", True)
        assert remote.active_calls == [("anon-9", False), ("anon-9", True)]
        assert remote.deleted_aliases == ["anon-9"]
        assert ("POST", "/api/create") in [(m, p) for m, p, _ in remote.requests]


class TestApiKeyMode:
    """2026-11-02 新鉴权：`X-API-Key`，无登录/CSRF。"""

    @staticmethod
    def _response(data):
        class _Resp:
            status_code = 200

            def json(self):
                return {"success": True, "data": data}

        return _Resp()

    def test_api_key_is_sent_and_no_login_request(self):
        from unittest import mock

        client = RemoteHMEClient("http://hme.example", "sk-test-key", timeout=5)
        with mock.patch.object(
            client._session, "request", return_value=self._response([{"id": "acc_1"}])
        ) as request:
            assert client.list_accounts()[0]["id"] == "acc_1"
        assert request.call_args.kwargs["headers"]["X-API-Key"] == "sk-test-key"
        assert all(
            call.args[0] != "POST" or call.args[1] != "/api/auth/login"
            for call in request.call_args_list
        ), "API 秘钥模式不应再走登录接口"

    def test_inbox_passes_new_pagination_and_refresh_params(self):
        from unittest import mock

        client = RemoteHMEClient("http://hme.example", "sk-test-key", timeout=5)
        with mock.patch.object(
            client._session,
            "request",
            return_value=self._response({"messages": [], "count": 0, "total": 0}),
        ) as request:
            client.fetch_inbox(
                "acc_1",
                limit=20,
                offset=40,
                days=30,
                start="2026-10-01",
                end="2026-10-31",
                refresh=True,
            )
        params = request.call_args.kwargs["params"]
        assert params["offset"] == 40
        assert params["start"] == "2026-10-01"
        assert params["end"] == "2026-10-31"
        assert params["refresh"] == 1
        assert params["days"] == 30

    def test_previews_posts_ids_and_returns_mapping(self):
        from unittest import mock

        client = RemoteHMEClient("http://hme.example", "sk-test-key", timeout=5)
        with mock.patch.object(
            client._session,
            "request",
            return_value=self._response({"1042": "code 123456"}),
        ) as request:
            out = client.fetch_previews("acc_1", ["1042", "Junk:88"])
        assert out == {"1042": "code 123456"}
        assert request.call_args.kwargs["json"] == {"ids": ["1042", "Junk:88"]}


# ------------------------------------------------------------------ 错误码映射


class TestErrorMapping:
    def test_creation_cooldown_maps_to_rate_limited(self, remote, client):
        remote.create_error = {"code": "CREATION_COOLDOWN", "message": "至少等待 20 分钟"}
        with pytest.raises(ICloudError) as excinfo:
            client.create_alias("acc_1", "GitHub")
        assert excinfo.value.code == "provider_rate_limited"

    def test_creation_daily_limit_maps_to_rate_limited(self, remote, client):
        remote.create_error = {"code": "CREATION_LIMIT_REACHED", "message": "今日上限"}
        with pytest.raises(ICloudError) as excinfo:
            client.create_alias("acc_1", "GitHub")
        assert excinfo.value.code == "provider_rate_limited"

    def test_unknown_error_code_maps_to_upstream_error(self, remote, client):
        remote.create_error = {"code": "SOMETHING_NEW", "message": "新错误"}
        with pytest.raises(ICloudError) as excinfo:
            client.create_alias("acc_1", "GitHub")
        assert excinfo.value.code == "upstream_error"


# ------------------------------------------------------------------ 数据接口


class TestDataEndpoints:
    def test_list_accounts(self, remote, client):
        accounts = client.list_accounts()
        assert accounts[0]["id"] == "acc_1"

    def test_create_alias_passes_account_and_label(self, remote, client):
        created = client.create_alias("acc_1", "GitHub")
        assert created["account_id"] == "acc_1"
        assert created["label"] == "GitHub"

    def test_create_alias_defaults_label_from_library(self, remote, client):
        """没给 label 时从远端名称库挑一个（服务端要求 label 必填）。"""
        created = client.create_alias("acc_1")
        assert created["label"] == "GitHub"

    def test_list_aliases(self, remote, client):
        remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-1", "label": "GitHub", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        aliases = client.list_aliases("acc_1")
        assert aliases[0]["email"] == "a@icloud.com"

    def test_fetch_inbox(self, remote, client):
        remote.inbox_messages["acc_1"] = [
            {
                "id": "1042",
                "from": "GitHub <noreply@github.com>",
                "to": "newalias@icloud.com",
                "subject": "验证",
                "date": "2026-10-09T12:00:00Z",
                "preview": "验证码 123456",
                "folder": "INBOX",
            }
        ]
        data = client.fetch_inbox("acc_1", alias="newalias@icloud.com", limit=20)
        assert data["messages"][0]["id"] == "1042"
        assert remote.requests[-1][2].get("limit") == "20"

    def test_fetch_inbox_clamps_limit(self, remote, client):
        # 服务端 limit 上限是 50（以服务端代码为准，API.md 写的 100 是旧的）
        client.fetch_inbox("acc_1", limit=999)
        assert remote.requests[-1][2].get("limit") == "50"

    def test_fetch_message(self, remote, client):
        message = client.fetch_message("acc_1", "1042")
        assert message["body"] == "您的验证码是 123456"

    def test_check_account(self, remote, client):
        summary = client.check_account("acc_1")
        assert summary["status"] == "active"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
