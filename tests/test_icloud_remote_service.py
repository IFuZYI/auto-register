"""远程 iCloud 仓库服务层：接入、同步、去重（本地优先）、生成、删除、收件。

核心契约（用户需求 2026-10-09）：
- 远程账号按「服务地址 + 管理员密码 + 远端账号 id」接入，一行一个远端账号；
- 同步把远端别名拉进本地库（source=remote），**同地址去重时本地优先**：
  本地已有该地址则不建远程行、不改本地行；
- 远程行可以像本地一样生成/停用/激活/删除（走远程 API）；
- 收件走远端 /api/inbox，映射成本项目 MailMessage（字段名与本地链路一致）。

双端是真实 HTTP 服务（tests/remote_hme_double.py），不是 mock。
"""
from __future__ import annotations

import base64
import os

import pytest
from sqlmodel import SQLModel, create_engine, select

from platforms.icloud.errors import ICloudError
from core.db import platform_session
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
def service(tmp_path, monkeypatch, remote):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())

    import core.db as db
    import core.secret_box as secret_box_module
    from core.db import platform_database_registry
    from core.secret_box import SecretBox
    from services import icloud_service

    url = f"sqlite:///{tmp_path / 'icloud.db'}"
    engine = create_engine(url)
    SQLModel.metadata.create_all(engine)
    platform_database_registry.configure({"icloud": url})
    monkeypatch.setattr(secret_box_module, "secret_box", SecretBox())
    monkeypatch.setattr(icloud_service, "secret_box", secret_box_module.secret_box)

    # 不替换远程客户端：服务层经 `remote_client(row)` 真实连到双端（HTTP），
    # 密码用真实的 secret_box 加密/解密 —— 整条链没有打桩。
    yield icloud_service
    platform_database_registry.configure({})
    engine.dispose()


def _seed_local_account(email: str = "owner@icloud.com") -> int:
    """直接落一行本地主号（避开 Web 客户端导入链，本文件只测远程分支）。"""
    from core.db import ICloudAccountModel

    with platform_session("icloud") as session:
        row = ICloudAccountModel(email=email, source="local")
        session.add(row)
        session.commit()
        session.refresh(row)
        return int(row.id or 0)


# ------------------------------------------------------------------ 接入


class TestAddRemoteAccount:
    def test_add_remote_account_stores_encrypted_password(self, service, remote):
        account = service.add_remote_account(
            base_url=remote.base_url,
            admin_password=ADMIN_PASSWORD,
            remote_account_id="acc_1",
            name="远程主号",
        )
        assert account["source"] == "remote"
        assert account["email"] == "owner@icloud.com"
        assert account["base_url"] == remote.base_url
        assert account["remote_account_id"] == "acc_1"
        # 密码必须加密落库、绝不回显
        assert "admin_password" not in account
        row = service.get_account(account["id"])
        assert row.remote_password_cipher and ADMIN_PASSWORD not in row.remote_password_cipher
        assert service.remote_password(row) == ADMIN_PASSWORD

    def test_add_remote_account_rejects_duplicate_binding(self, service, remote):
        service.add_remote_account(
            base_url=remote.base_url, admin_password=ADMIN_PASSWORD, remote_account_id="acc_1"
        )
        with pytest.raises(ICloudError) as excinfo:
            service.add_remote_account(
                base_url=remote.base_url, admin_password=ADMIN_PASSWORD, remote_account_id="acc_1"
            )
        assert excinfo.value.code == "invalid_config"

    def test_add_remote_account_rejects_unknown_remote_id(self, service, remote):
        with pytest.raises(ICloudError) as excinfo:
            service.add_remote_account(
                base_url=remote.base_url, admin_password=ADMIN_PASSWORD, remote_account_id="acc_missing"
            )
        assert excinfo.value.code == "invalid_config"

    def test_add_remote_account_rejects_bad_password(self, service, remote):
        with pytest.raises(ICloudError) as excinfo:
            service.add_remote_account(
                base_url=remote.base_url, admin_password="wrong", remote_account_id="acc_1"
            )
        assert excinfo.value.code == "invalid_config"

    def test_remote_accounts_list_alongside_local(self, service, remote):
        service.add_remote_account(
            base_url=remote.base_url, admin_password=ADMIN_PASSWORD, remote_account_id="acc_1"
        )
        accounts = service.list_accounts()
        assert [item["source"] for item in accounts] == ["remote"]


# ------------------------------------------------------------------ 同步与去重


class TestSyncRemote:
    def _add(self, service, remote, **kwargs):
        return service.add_remote_account(
            base_url=remote.base_url,
            admin_password=ADMIN_PASSWORD,
            remote_account_id=kwargs.pop("remote_account_id", "acc_1"),
            **kwargs,
        )

    def test_sync_pulls_aliases_with_remote_source(self, service, remote):
        account = self._add(service, remote)
        remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-a", "label": "GitHub", "active": True, "createdAt": "2026-01-01T00:00:00Z"},
            {"email": "b@icloud.com", "anonymousId": "anon-b", "label": "Notion", "active": False, "createdAt": "2026-01-02T00:00:00Z"},
        ]
        result = service.sync_remote_account(account["id"])
        assert result["fetched"] == 2
        assert result["created"] == 2

        aliases = service.list_aliases(account["id"])
        assert {item["address"] for item in aliases} == {"a@icloud.com", "b@icloud.com"}
        assert all(item["source"] == "remote" for item in aliases)
        # 停用的别名同步后状态一致
        by_address = {item["address"]: item for item in aliases}
        assert by_address["b@icloud.com"]["status"] == "disabled"
        # 未入池（与本地语义一致：勾选导入才进池）
        assert all(item["pool_status"] == "unpooled" for item in aliases)

    def test_sync_dedupes_local_first(self, service, remote):
        """本地优先：远端别名与本地已有地址相同时，本地行保留、不建远程行。"""
        # 本地主号 + 本地别名
        local_account_id = _seed_local_account("owner@icloud.com")
        service._upsert_alias(
            local_account_id,
            {"address": "shared@icloud.com", "label": "本地", "status": "active"},
            "owner@icloud.com",
        )

        account = self._add(service, remote)
        remote.aliases["acc_1"] = [
            {"email": "shared@icloud.com", "anonymousId": "anon-s", "label": "远程", "active": True, "createdAt": "2026-01-01T00:00:00Z"},
            {"email": "only-remote@icloud.com", "anonymousId": "anon-r", "label": "远程独有", "active": True, "createdAt": "2026-01-01T00:00:00Z"},
        ]
        result = service.sync_remote_account(account["id"])
        assert result["created"] == 1
        assert result["skipped_local"] == 1

        with platform_session("icloud") as session:
            from core.db import ICloudAliasModel

            rows = session.exec(select(ICloudAliasModel)).all()
        by_address = {row.address: row for row in rows}
        # 同地址只有一行，且是本地行（来源没被改成远程、标签没被覆盖）
        assert by_address["shared@icloud.com"].source == "local"
        assert by_address["shared@icloud.com"].label == "本地"
        assert by_address["only-remote@icloud.com"].source == "remote"

    def test_sync_updates_existing_remote_rows(self, service, remote):
        """重复同步幂等：已有远程行更新标签/状态，不重复建行。"""
        account = self._add(service, remote)
        remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-a", "label": "旧", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        service.sync_remote_account(account["id"])
        remote.aliases["acc_1"][0]["label"] = "新"
        remote.aliases["acc_1"][0]["active"] = False
        result = service.sync_remote_account(account["id"])
        assert result["created"] == 0
        assert result["updated"] == 1
        aliases = service.list_aliases(account["id"])
        assert aliases[0]["label"] == "新"
        assert aliases[0]["status"] == "disabled"

    def test_sync_does_not_touch_pool_bookkeeping(self, service, remote):
        """同步不改已存在的池记账（用户可能已把地址标成 used/in_use）。"""
        account = self._add(service, remote)
        remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-a", "label": "x", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        service.sync_remote_account(account["id"])
        alias_id = service.list_aliases(account["id"])[0]["id"]
        service.set_alias_pool_status(alias_id, "used")
        remote.aliases["acc_1"][0]["label"] = "y"
        service.sync_remote_account(account["id"])
        assert service.list_aliases(account["id"])[0]["pool_status"] == "used"

    def test_sync_records_error_on_failure(self, service, remote):
        account = self._add(service, remote)
        remote.stop()
        with pytest.raises(ICloudError):
            service.sync_remote_account(account["id"])
        row = service.get_account(account["id"])
        assert row.sync_error


# ------------------------------------------------------------------ 生成/停用/删除（远程分支）


class TestRemoteAliasOperations:
    def _add(self, service, remote):
        return service.add_remote_account(
            base_url=remote.base_url, admin_password=ADMIN_PASSWORD, remote_account_id="acc_1"
        )

    def test_generate_alias_calls_remote_create(self, service, remote):
        account = self._add(service, remote)
        created = service.generate_alias(account["id"], label="GitHub")
        assert created["source"] == "remote"
        assert created["address"].endswith("@icloud.com")
        assert remote.last_create_label == "GitHub"
        # 匿名 id 从远端别名列表解析并落库（删除/停用要用）
        assert created["provider_id"] == "anon-1"

    def test_generate_alias_reports_rate_limit(self, service, remote):
        account = self._add(service, remote)
        remote.create_error = {"code": "CREATION_COOLDOWN", "message": "20 分钟冷却"}
        with pytest.raises(ICloudError) as excinfo:
            service.generate_alias(account["id"], label="GitHub")
        assert excinfo.value.code == "provider_rate_limited"

    def test_set_alias_active_calls_remote(self, service, remote):
        account = self._add(service, remote)
        remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-a", "label": "x", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        service.sync_remote_account(account["id"])
        alias_id = service.list_aliases(account["id"])[0]["id"]

        service.set_alias_active(alias_id, False)
        assert remote.active_calls[-1] == ("anon-a", False)
        assert service.list_aliases(account["id"])[0]["status"] == "disabled"

        service.set_alias_active(alias_id, True)
        assert remote.active_calls[-1] == ("anon-a", True)
        assert service.list_aliases(account["id"])[0]["status"] == "active"

    def test_delete_alias_calls_remote_and_removes_row(self, service, remote):
        account = self._add(service, remote)
        remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-a", "label": "x", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        service.sync_remote_account(account["id"])
        alias_id = service.list_aliases(account["id"])[0]["id"]

        service.delete_alias(alias_id, remote=True)
        assert remote.deleted_aliases == ["anon-a"]
        assert service.list_aliases(account["id"]) == []

    def test_delete_alias_without_remote_only_removes_row(self, service, remote):
        account = self._add(service, remote)
        remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-a", "label": "x", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        service.sync_remote_account(account["id"])
        alias_id = service.list_aliases(account["id"])[0]["id"]

        service.delete_alias(alias_id, remote=False)
        assert remote.deleted_aliases == []
        assert service.list_aliases(account["id"]) == []


# ------------------------------------------------------------------ 收件（远程分支）


class TestRemoteMessages:
    def _add_with_alias(self, service, remote):
        account = service.add_remote_account(
            base_url=remote.base_url, admin_password=ADMIN_PASSWORD, remote_account_id="acc_1"
        )
        remote.aliases["acc_1"] = [
            {"email": "a@icloud.com", "anonymousId": "anon-a", "label": "x", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        service.sync_remote_account(account["id"])
        return account, service.list_aliases(account["id"])[0]

    def test_alias_messages_map_to_mail_message(self, service, remote):
        account, alias = self._add_with_alias(service, remote)
        remote.inbox_messages["acc_1"] = [
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
        messages, method, warning = service.fetch_alias_messages_detailed(alias["id"])
        assert method == "remote"
        assert warning == ""
        message = messages[0]
        assert message.provider_message_id == "1042"
        assert message.subject == "验证码"
        assert message.snippet == "您的验证码是 123456"
        assert message.sender.email == "noreply@github.com"
        assert message.sender.name == "GitHub"
        assert message.mailbox == "INBOX"

    def test_account_messages_filter_by_recipient(self, service, remote):
        account, alias = self._add_with_alias(service, remote)
        remote.inbox_messages["acc_1"] = [
            {
                "id": "1042",
                "from": "no-reply@example.com",
                "to": "a@icloud.com",
                "subject": "验证码",
                "date": "2026-10-09T12:00:00Z",
                "preview": "验证码 123456",
                "folder": "INBOX",
            }
        ]
        messages, method, _ = service.fetch_account_messages_detailed(
            account["id"], recipient="a@icloud.com"
        )
        assert method == "remote"
        assert messages[0].provider_message_id == "1042"
        # 请求带上 alias 过滤（远端按收件人过滤）
        assert remote.requests[-1][2].get("alias") == "a@icloud.com"

    def test_shared_message_reads_remote(self, service, remote):
        account, alias = self._add_with_alias(service, remote)
        remote.inbox_messages["acc_1"] = [
            {
                "id": "1042",
                "from": "no-reply@example.com",
                "to": "a@icloud.com",
                "subject": "最新",
                "date": "2026-10-09T12:00:00Z",
                "preview": "验证码 123456",
                "folder": "INBOX",
            }
        ]
        address, message = service.fetch_latest_shared_message(alias["share_token"])
        assert address == "a@icloud.com"
        assert message is not None and message.provider_message_id == "1042"

    def test_missing_alias_raises(self, service):
        with pytest.raises(ICloudError) as excinfo:
            service.fetch_alias_messages_detailed(99999)
        assert excinfo.value.code == "alias_not_found"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
