"""远程 icloud-hme 客户端：会话、CSRF、错误码映射、数据接口。

测试策略（按 TDD 技能「Doubles for external services」条）：
- 双端是**真实 HTTP 服务**（见 `tests/remote_hme_double.py`），不是 in-process
  mock —— 客户端如果没把请求发到双端（比如硬编码了生产地址），这里会直接
  连接失败，而不是拿到一句看似合理的上游报错。
- 双端**强制校验 CSRF**：写操作缺 `X-CSRF-Token` 或 token 不对时回
  `403 CSRF_INVALID`。若哪天客户端退化成不带 CSRF，写操作测试会立刻失败。
- 「服务重启」场景：双端换一个 csrf token 并让旧 token 失效（Cookie 仍在），
  客户端必须自动重登一次并重试，而不是把 401 抛给上层。
"""
from __future__ import annotations

import pytest

from platforms.icloud.errors import ICloudError
from platforms.icloud.remote_client import RemoteHMEClient
from tests.remote_hme_double import ADMIN_PASSWORD, RemoteHMEDouble


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
    return RemoteHMEClient(remote.base_url, ADMIN_PASSWORD, timeout=5)


# ------------------------------------------------------------------ 会话


class TestSession:
    def test_login_uses_admin_password_and_caches_csrf(self, remote, client):
        client.list_accounts()
        assert remote.login_count == 1
        # 登录请求确实到达了双端（而不是被某处短路）
        assert any(method == "POST" and path == "/api/auth/login" for method, path, _ in remote.requests)

    def test_session_is_reused_across_requests(self, remote, client):
        client.list_accounts()
        client.list_aliases("acc_1")
        assert remote.login_count == 1, "同一客户端内不应每个请求都重新登录（会踩登录限流）"

    def test_reauthentication_after_server_restart(self, remote, client):
        """服务重启后旧 CSRF 失效：客户端必须自动重登并重试，而不是抛错。"""
        client.list_accounts()
        remote.restart_with_new_csrf()
        created = client.create_alias("acc_1", "GitHub")
        assert created["email"].endswith("@icloud.com")
        assert remote.login_count == 2, "重启后应重新登录一次"

    def test_bad_password_maps_to_invalid_config(self, remote):
        bad = RemoteHMEClient(remote.base_url, "wrong-pass", timeout=5)
        with pytest.raises(ICloudError) as excinfo:
            bad.list_accounts()
        assert excinfo.value.code == "invalid_config"

    def test_unreachable_service_maps_to_upstream_unavailable(self):
        dead = RemoteHMEClient("http://127.0.0.1:9", ADMIN_PASSWORD, timeout=2)
        with pytest.raises(ICloudError) as excinfo:
            dead.list_accounts()
        assert excinfo.value.code == "upstream_unavailable"


# ------------------------------------------------------------------ CSRF 契约


class TestCsrfContract:
    def test_write_requests_carry_csrf_header(self, remote, client):
        client.create_alias("acc_1", "GitHub")
        # 双端强制校验 CSRF（缺头/错 token 回 403），能成功即证明带了正确头
        assert remote.last_create_label == "GitHub"

    def test_delete_carries_csrf(self, remote, client):
        client.delete_alias("acc_1", "anon-9")
        assert remote.deleted_aliases == ["anon-9"]

    def test_activate_carries_csrf(self, remote, client):
        client.set_alias_active("acc_1", "anon-9", False)
        client.set_alias_active("acc_1", "anon-9", True)
        assert remote.active_calls == [("anon-9", False), ("anon-9", True)]


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
