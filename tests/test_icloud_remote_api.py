"""远程 iCloud 仓库的 HTTP 接口：前端依赖的响应结构与业务错误码映射。

覆盖（用户需求 2026-10-09「各功能都要齐全」）：
- 接入远程账号（POST /api/icloud/remote-accounts）
- 远程账号列表带 source/base_url/远端账号 id（脱敏，不回密码）
- 远端账号探测（POST .../check）
- 同步远端别名（POST /api/icloud/accounts/{id}/sync 的远程分支）
- 生成 / 停用 / 删除走远程（既有端点自动分流）
- 删除远程账号
- 错误码 → HTTP 状态映射（远程不可达 503、密码错 400、冷却 429）
"""
from __future__ import annotations

import base64
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, create_engine

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
def client(tmp_path, monkeypatch, remote):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())

    import core.secret_box as secret_box_module
    from api.icloud import router
    from core.db import platform_database_registry
    from core.secret_box import SecretBox
    from services import icloud_service

    url = f"sqlite:///{tmp_path / 'icloud.db'}"
    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)
    platform_database_registry.configure({"icloud": url})
    monkeypatch.setattr(secret_box_module, "secret_box", SecretBox())
    monkeypatch.setattr(icloud_service, "secret_box", secret_box_module.secret_box)

    app = FastAPI()
    app.include_router(router, prefix="/api")
    test_client = TestClient(app)
    test_client.remote = remote
    yield test_client
    platform_database_registry.configure({})
    engine.dispose()


def _add_remote(client) -> dict:
    response = client.post(
        "/api/icloud/remote-accounts",
        json={
            "base_url": client.remote.base_url,
            "admin_password": ADMIN_PASSWORD,
            "remote_account_id": "acc_1",
            "name": "远程主号",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ 接入


class TestAddRemoteAccount:
    def test_add_returns_redacted_summary(self, client):
        account = _add_remote(client)
        assert account["source"] == "remote"
        assert account["email"] == "owner@icloud.com"
        assert account["base_url"] == client.remote.base_url
        assert account["remote_account_id"] == "acc_1"
        # 密码绝不回显
        assert "admin_password" not in account
        assert "remote_password_cipher" not in account

    def test_bad_password_maps_to_400(self, client):
        response = client.post(
            "/api/icloud/remote-accounts",
            json={
                "base_url": client.remote.base_url,
                "admin_password": "wrong",
                "remote_account_id": "acc_1",
            },
        )
        assert response.status_code == 400

    def test_unreachable_service_maps_to_503(self, client):
        response = client.post(
            "/api/icloud/remote-accounts",
            json={
                "base_url": "http://127.0.0.1:9",
                "admin_password": ADMIN_PASSWORD,
                "remote_account_id": "acc_1",
            },
        )
        assert response.status_code == 503

    def test_duplicate_binding_rejected(self, client):
        _add_remote(client)
        response = client.post(
            "/api/icloud/remote-accounts",
            json={
                "base_url": client.remote.base_url,
                "admin_password": ADMIN_PASSWORD,
                "remote_account_id": "acc_1",
            },
        )
        assert response.status_code == 400


class TestDiscoverRemoteAccounts:
    def test_discover_lists_remote_accounts_without_persisting(self, client):
        """「获取账号列表」是只读探针：列出远端账号，但不落库。"""
        response = client.post(
            "/api/icloud/remote-accounts/discover",
            json={"base_url": client.remote.base_url, "admin_password": ADMIN_PASSWORD},
        )
        assert response.status_code == 200, response.text
        items = response.json()["items"]
        assert [item["id"] for item in items] == ["acc_1"]
        assert items[0]["icloud_email"] == "owner@icloud.com"
        # 没有落库
        assert client.get("/api/icloud/accounts").json()["items"] == []

    def test_discover_bad_password_maps_to_400(self, client):
        response = client.post(
            "/api/icloud/remote-accounts/discover",
            json={"base_url": client.remote.base_url, "admin_password": "wrong"},
        )
        assert response.status_code == 400

    def test_discover_unreachable_maps_to_503(self, client):
        response = client.post(
            "/api/icloud/remote-accounts/discover",
            json={"base_url": "http://127.0.0.1:9", "admin_password": ADMIN_PASSWORD},
        )
        assert response.status_code == 503


# ------------------------------------------------------------------ 列表与账号


class TestRemoteListing:
    def test_accounts_list_carries_source(self, client):
        _add_remote(client)
        response = client.get("/api/icloud/accounts")
        assert response.status_code == 200
        items = response.json()["items"]
        assert [item["source"] for item in items] == ["remote"]

    def test_check_remote_account(self, client):
        account = _add_remote(client)
        response = client.post(f"/api/icloud/accounts/{account['id']}/check")
        assert response.status_code == 200
        assert response.json()["status"] == "active"

    def test_delete_remote_account(self, client):
        account = _add_remote(client)
        response = client.delete(f"/api/icloud/accounts/{account['id']}")
        assert response.status_code == 200
        assert client.get("/api/icloud/accounts").json()["items"] == []


# ------------------------------------------------------------------ 同步与别名


class TestRemoteAliases:
    def _add_with_alias(self, client) -> tuple[dict, dict]:
        account = _add_remote(client)
        client.remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-a", "label": "GitHub", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        response = client.post(f"/api/icloud/accounts/{account['id']}/sync")
        assert response.status_code == 200
        aliases = client.get("/api/icloud/aliases").json()["items"]
        return account, aliases[0]

    def test_sync_reports_dedup_counts(self, client):
        account = _add_remote(client)
        client.remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-a", "label": "x", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        body = client.post(f"/api/icloud/accounts/{account['id']}/sync").json()
        assert body["created"] == 1
        assert body["skipped_local"] == 0

    def test_alias_list_carries_source(self, client):
        _account, alias = self._add_with_alias(client)
        assert alias["source"] == "remote"
        assert alias["pool_status"] == "unpooled"

    def test_generate_alias_refused_for_remote(self, client):
        """远程账号生成隐私邮箱 → 400（invalid_config）。

        改自旧断言（旧行为：远程生成走远端）。用户要求：注册机不许在远端新建。
        """
        account = _add_remote(client)
        response = client.post(
            "/api/icloud/aliases",
            json={"account_id": account["id"], "label": "GitHub", "count": 1},
        )
        assert response.status_code == 400
        assert client.remote.created_aliases == []

    def test_deactivate_alias_via_remote(self, client):
        _account, alias = self._add_with_alias(client)
        response = client.post(f"/api/icloud/aliases/{alias['id']}/deactivate")
        assert response.status_code == 200
        assert client.remote.active_calls[-1] == ("anon-a", False)

    def test_delete_alias_via_remote(self, client):
        _account, alias = self._add_with_alias(client)
        response = client.delete(f"/api/icloud/aliases/{alias['id']}")
        assert response.status_code == 200
        assert client.remote.deleted_aliases == ["anon-a"]
        assert client.get("/api/icloud/aliases").json()["items"] == []


# ------------------------------------------------------------------ 收件


class TestRemoteInbox:
    def test_alias_messages_via_remote(self, client):
        account = _add_remote(client)
        client.remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-a", "label": "x", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        client.post(f"/api/icloud/accounts/{account['id']}/sync")
        alias = client.get("/api/icloud/aliases").json()["items"][0]
        client.remote.inbox_messages["acc_1"] = [
            {
                "id": "1042",
                "from": "GitHub <noreply@github.com>",
                "to": "a@icloud.com",
                "subject": "验证码",
                "date": "2026-10-09T12:00:00Z",
                "preview": "您的验证码是 123456",
                "folder": "INBOX",
            }
        ]
        response = client.get(f"/api/icloud/aliases/{alias['id']}/messages")
        assert response.status_code == 200
        body = response.json()
        assert body["method"] == "remote"
        assert body["items"][0]["provider_message_id"] == "1042"
        assert body["items"][0]["snippet"] == "您的验证码是 123456"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
