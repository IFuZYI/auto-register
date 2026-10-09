"""iCloud 隐私邮箱（远程）渠道：从远程仓库取号、收码。

与 `test_icloud_local_mailbox.py` 的对应关系：本地渠道的凭据在本机库
（直连 Apple），远程渠道的凭据在 icloud-hme 服务里（本机只调它的 API）。
用户诉求：远程仓库「各功能都要齐全，和原有的本地 icloud 邮箱一样，
只不过获取邮件改为调用远程接口」。

重点验证：
1. 工厂能建出渠道（前端选了不能报「未知邮箱提供商」）；
2. 主号选取（指定远程账号、留空自动挑、停用的要拦）；
3. 取号走远程仓库：优先复用池里可领的远程别名，池空才让远端新建；
4. 收码走远程收件（字段名与本地链路一致），并遵守「发码时间窗」；
5. 池记账钩子（used / failed）接上（同本地渠道）。
"""
from __future__ import annotations

import base64
import os
from unittest.mock import MagicMock

import pytest
from sqlmodel import SQLModel, create_engine

from core.base_mailbox import MailboxAccount, create_mailbox
from modules.mail.icloud_remote import ICloudRemoteMailbox
from platforms.icloud.errors import ICloudError
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

    account = icloud_service.add_remote_account(
        base_url=remote.base_url,
        admin_password=ADMIN_PASSWORD,
        remote_account_id="acc_1",
    )
    yield icloud_service, account
    platform_database_registry.configure({})
    engine.dispose()


# ------------------------------------------------------------------ 工厂接入


class TestFactoryRegistration:
    def test_factory_registers_provider(self):
        mailbox = create_mailbox("icloud_remote", extra={})
        assert isinstance(mailbox, ICloudRemoteMailbox)

    def test_factory_passes_config_through(self):
        mailbox = create_mailbox(
            "icloud_remote",
            extra={
                "icloud_remote_account_id": "9",
                "icloud_remote_label": "GitHub",
                "icloud_remote_note": "备注",
            },
        )
        assert mailbox._account_id == "9"
        assert mailbox._label == "GitHub"
        assert mailbox._note == "备注"

    def test_provider_is_listed_with_display_name(self):
        from core.mailboxes.registry import available_providers, provider_display_name

        assert "icloud_remote" in available_providers()
        name = provider_display_name("icloud_remote")
        assert "远程" in name
        # 与本地渠道的显示名要能区分
        assert provider_display_name("icloud_local") != name


# ------------------------------------------------------------------ 取号


class TestGetEmail:
    def test_get_email_reuses_pool_alias_before_creating(self, service, remote):
        """池里有可领的远程别名时复用，不消耗远端创建额度。"""
        icloud_service, account = service
        remote.aliases["acc_1"] = [
            {"email": "pooled@icloud.com", "anonymousId": "anon-p", "label": "x", "active": True, "createdAt": "2026-01-01T00:00:00Z"}
        ]
        icloud_service.sync_remote_account(account["id"])
        alias_id = icloud_service.list_aliases(account["id"])[0]["id"]
        icloud_service.import_aliases_to_pool([alias_id])

        mailbox = ICloudRemoteMailbox(account_id=str(account["id"]), platform_name="chatgpt")
        account_obj = mailbox.get_email()
        assert account_obj.email == "pooled@icloud.com"
        assert remote.created_aliases == [], "池里有可领的号时不该让远端新建"

    def test_get_email_creates_remote_alias_when_pool_empty(self, service, remote):
        icloud_service, account = service
        mailbox = ICloudRemoteMailbox(account_id=str(account["id"]), platform_name="chatgpt")
        account_obj = mailbox.get_email()
        assert account_obj.email.endswith("@icloud.com")
        assert remote.created_aliases and remote.created_aliases[0][0] == "acc_1"
        # account_id 存「本地账号 id : 别名地址」（收件按账号取、按别名过滤）
        assert account_obj.account_id == f"{account['id']}:{account_obj.email}"

    def test_get_email_reports_rate_limit(self, service, remote):
        icloud_service, account = service
        remote.create_error = {"code": "CREATION_COOLDOWN", "message": "20 分钟冷却"}
        mailbox = ICloudRemoteMailbox(account_id=str(account["id"]))
        with pytest.raises(ICloudError) as excinfo:
            mailbox.get_email()
        assert excinfo.value.code == "provider_rate_limited"

    def test_get_email_rejects_disabled_account(self, service, remote):
        icloud_service, account = service
        icloud_service.set_account_enabled(account["id"], False)
        mailbox = ICloudRemoteMailbox(account_id=str(account["id"]))
        with pytest.raises(RuntimeError):
            mailbox.get_email()

    def test_get_email_without_account_raises_when_none_configured(self, tmp_path, monkeypatch, remote):
        monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", base64.b64encode(os.urandom(32)).decode())
        import core.secret_box as secret_box_module
        from core.db import platform_database_registry
        from core.secret_box import SecretBox

        url = f"sqlite:///{tmp_path / 'empty.db'}"
        engine = create_engine(url)
        SQLModel.metadata.create_all(engine)
        platform_database_registry.configure({"icloud": url})
        monkeypatch.setattr(secret_box_module, "secret_box", SecretBox())
        try:
            mailbox = ICloudRemoteMailbox()
            with pytest.raises(RuntimeError):
                mailbox.get_email()
        finally:
            platform_database_registry.configure({})
            engine.dispose()


# ------------------------------------------------------------------ 收码


class TestWaitForCode:
    def _alias_account(self, service, remote) -> tuple:
        icloud_service, account = service
        mailbox = ICloudRemoteMailbox(account_id=str(account["id"]), platform_name="chatgpt")
        account_obj = mailbox.get_email()
        remote.inbox_messages["acc_1"] = [
            {
                "id": "2001",
                "from": "OpenAI <noreply@openai.com>",
                "to": account_obj.email,
                "subject": "Your code",
                "date": "2026-10-09T12:00:00Z",
                "preview": "Your verification code is 654321",
                "folder": "INBOX",
            }
        ]
        return mailbox, account_obj

    def test_wait_for_code_reads_remote_inbox(self, service, remote):
        mailbox, account_obj = self._alias_account(service, remote)
        code = mailbox.wait_for_code(account_obj, timeout=10)
        assert code == "654321"

    def test_wait_for_code_respects_before_ids(self, service, remote):
        """已见过的邮件不重复命中（补发后必须只认新邮件）。"""
        mailbox, account_obj = self._alias_account(service, remote)
        with pytest.raises(TimeoutError):
            mailbox.wait_for_code(account_obj, before_ids={"2001"}, timeout=3)

    def test_get_current_ids_reads_remote(self, service, remote):
        mailbox, account_obj = self._alias_account(service, remote)
        assert mailbox.get_current_ids(account_obj) == {"2001"}

    def test_wait_for_code_keyword_filter(self, service, remote):
        mailbox, account_obj = self._alias_account(service, remote)
        with pytest.raises(TimeoutError):
            mailbox.wait_for_code(account_obj, keyword="不存在的关键词", timeout=3)


# ------------------------------------------------------------------ 池记账


class TestPoolBookkeeping:
    def test_set_account_status_marks_used(self, service, remote):
        icloud_service, account = service
        mailbox = ICloudRemoteMailbox(account_id=str(account["id"]), platform_name="chatgpt")
        account_obj = mailbox.get_email()

        mailbox.set_account_status(account_obj, "used")
        aliases = icloud_service.list_aliases(account["id"])
        assert aliases[0]["pool_status"] == "used"
        assert "chatgpt" in aliases[0]["used_platforms"]

    def test_set_account_status_failed_releases_claim(self, service, remote):
        icloud_service, account = service
        mailbox = ICloudRemoteMailbox(account_id=str(account["id"]), platform_name="chatgpt")
        account_obj = mailbox.get_email()

        mailbox.set_account_status(account_obj, "failed")
        aliases = icloud_service.list_aliases(account["id"])
        assert aliases[0]["pool_status"] == "available"


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
