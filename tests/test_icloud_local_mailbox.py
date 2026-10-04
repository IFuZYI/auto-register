"""iCloud 隐私邮箱（本地）渠道：主号在本地库，别名由本项目生成。

与 `test_icloud_hme_mailbox.py` 的区别：那条链路的主号凭据在远程
icloud-hme 服务里，这条的凭据在本机 `data/platforms/icloud.db`。
用户诉求是「iCloud 隐私邮箱主号是一个东西，和 Outlook 一样是本地邮箱」，
所以这里重点验证三件事：
  1. 工厂能建出渠道（前端选了不能报「未知邮箱提供商」）
  2. 主号选取（留空自动挑、按 id 挑、按邮箱挑、停用的要拦）
  3. 收件与验证码提取走的是 `MailMessage` 的真实字段名
     （字段名猜错会静默只剩 subject，验证码永远提不出来）
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.base_mailbox import MailboxAccount, create_mailbox
from modules.mail.icloud_local import ICloudLocalMailbox
from platforms.icloud.models import MailAddress, MailMessage


def _message(provider_id: str, subject: str, snippet: str = "", body: str = "") -> MailMessage:
    return MailMessage(
        provider_message_id=provider_id,
        mailbox="INBOX",
        subject=subject,
        snippet=snippet,
        text_body=body,
        sender=MailAddress(email="no-reply@example.com"),
    )


def _account(account_id="7:abc@icloud.com", email="abc@icloud.com", icloud_id=7) -> MailboxAccount:
    return MailboxAccount(
        email=email,
        account_id=account_id,
        extra={"icloud_account_id": icloud_id, "icloud_alias_email": email},
    )


def _row(row_id=7, email="owner@icloud.com", enabled=True) -> MagicMock:
    row = MagicMock(id=row_id, email=email)
    row.enabled = enabled
    return row


# ------------------------------------------------------------------ 工厂接入


class TestFactoryRegistration:
    def test_factory_registers_provider(self):
        """渠道已接入邮箱工厂（否则前端选了也建不出来）。"""
        mailbox = create_mailbox("icloud_local", extra={})
        assert isinstance(mailbox, ICloudLocalMailbox)

    def test_factory_passes_config_through(self):
        mailbox = create_mailbox(
            "icloud_local",
            extra={
                "icloud_local_account_id": "9",
                "icloud_local_label": "GitHub",
                "icloud_local_note": "备注",
            },
        )
        assert isinstance(mailbox, ICloudLocalMailbox)
        assert mailbox._account_id == "9"
        assert mailbox._label == "GitHub"
        assert mailbox._note == "备注"

    def test_provider_is_listed_with_display_name(self):
        """注册表要能列出它，且显示名区分本地/远程（否则前端两个选项一样）。"""
        from core.mailboxes.registry import available_providers, provider_display_name

        assert "icloud_local" in available_providers()
        name = provider_display_name("icloud_local")
        assert "本地" in name


# -------------------------------------------------------------------- 主号选取


class TestAccountResolution:
    def test_blank_account_uses_first_available(self):
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.resolve_account.return_value = _row(3)
            service.return_value.claim_alias.return_value = {
                "id": 11,
                "address": "new@icloud.com",
                "label": "隐私邮箱",
            }
            account = mailbox.get_email()
        service.return_value.resolve_account.assert_called_once_with("")
        assert account.extra["icloud_account_id"] == 3

    def test_numeric_account_id_looks_up_by_id(self):
        mailbox = ICloudLocalMailbox(account_id="9")
        with patch.object(mailbox, "_service") as service:
            service.return_value.get_account.return_value = _row(9)
            service.return_value.claim_alias.return_value = {"id": 1, "address": "a@icloud.com"}
            mailbox.get_email()
        service.return_value.get_account.assert_called_once_with(9)

    def test_email_account_id_looks_up_by_email(self):
        mailbox = ICloudLocalMailbox(account_id="owner@icloud.com")
        with patch.object(mailbox, "_service") as service:
            service.return_value.find_account_by_email.return_value = _row(5)
            service.return_value.claim_alias.return_value = {"id": 1, "address": "a@icloud.com"}
            mailbox.get_email()
        service.return_value.find_account_by_email.assert_called_once_with("owner@icloud.com")

    def test_unknown_account_raises_actionable_error(self):
        """指了不存在的主号要立刻报错，别等到收码超时才暴露。"""
        mailbox = ICloudLocalMailbox(account_id="nobody@icloud.com")
        with patch.object(mailbox, "_service") as service:
            service.return_value.find_account_by_email.return_value = None
            with pytest.raises(RuntimeError, match="主号不存在"):
                mailbox.get_email()

    def test_disabled_account_is_rejected(self):
        mailbox = ICloudLocalMailbox(account_id="9")
        with patch.object(mailbox, "_service") as service:
            service.return_value.get_account.return_value = _row(9, enabled=False)
            with pytest.raises(RuntimeError, match="已停用"):
                mailbox.get_email()

    def test_no_account_at_all_is_explicit(self):
        """库里一个主号都没有时，报错要指向「先去登录」而不是空指针。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.resolve_account.side_effect = RuntimeError(
                "还没有可用的 iCloud 主号，请先完成 Apple ID 登录"
            )
            with pytest.raises(RuntimeError, match="请先完成 Apple ID 登录"):
                mailbox.get_email()


# -------------------------------------------------------------------- 取号语义


class TestGetEmail:
    def test_returns_alias_address(self):
        mailbox = ICloudLocalMailbox(label="Notion")
        with patch.object(mailbox, "_service") as service:
            service.return_value.resolve_account.return_value = _row(7)
            service.return_value.claim_alias.return_value = {
                "id": 21,
                "address": "abc123@icloud.com",
                "label": "Notion",
            }
            account = mailbox.get_email()

        assert account.email == "abc123@icloud.com"
        # account_id 要同时带上主号与别名地址：收件按前者取、过滤靠后者
        assert account.account_id == "7:abc123@icloud.com"
        assert account.extra["icloud_account_id"] == 7
        assert account.extra["icloud_alias_id"] == 21

    def test_passes_configured_label_and_note(self):
        mailbox = ICloudLocalMailbox(label="GitHub", note="给注册用")
        with patch.object(mailbox, "_service") as service:
            service.return_value.resolve_account.return_value = _row(7)
            service.return_value.claim_alias.return_value = {"id": 1, "address": "a@icloud.com"}
            mailbox.get_email()
            _args, kwargs = service.return_value.claim_alias.call_args
        assert kwargs["label"] == "GitHub"
        assert kwargs["note"] == "给注册用"

    def test_rejects_empty_address(self):
        """上游返回了但没地址：不能当成功，否则注册拿空邮箱跑下去。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.resolve_account.return_value = _row(7)
            service.return_value.claim_alias.return_value = {"id": 1, "address": ""}
            with pytest.raises(RuntimeError, match="未返回隐私邮箱地址"):
                mailbox.get_email()

    def test_rate_limit_error_propagates(self):
        """额度耗尽要原样抛出，上层据此换渠道（不能吞成空地址）。"""
        from platforms.icloud.errors import ICloudError

        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.resolve_account.return_value = _row(7)
            service.return_value.claim_alias.side_effect = ICloudError(
                "provider_rate_limited", "每小时最多 5 个"
            )
            with pytest.raises(ICloudError):
                mailbox.get_email()

    def test_releases_stale_claims_before_claiming(self):
        """取号前先做一次「超时未落定」的兜底回收。

        任务中途崩掉会让别名永远停在 `in_use`（池子只出不进），
        所以取号点要顺手放回超时的那些。
        """
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.resolve_account.return_value = _row(7)
            service.return_value.claim_alias.return_value = {"id": 1, "address": "a@icloud.com"}
            mailbox.get_email()
        service.return_value.release_stale_claims.assert_called_once_with()

    def test_stale_cleanup_failure_does_not_block_claiming(self):
        """兜底回收失败不该挡住取号 —— 池里还有可用号时任务要能继续。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.resolve_account.return_value = _row(7)
            service.return_value.release_stale_claims.side_effect = RuntimeError("库锁住了")
            service.return_value.claim_alias.return_value = {"id": 1, "address": "a@icloud.com"}
            account = mailbox.get_email()

        assert account.email == "a@icloud.com"
        service.return_value.claim_alias.assert_called_once()


# ---------------------------------------------------------------------- 收件


class TestMessageReading:
    def test_current_ids_uses_real_field_name(self):
        """`MailMessage` 的字段是 `provider_message_id`，不是 `id`。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.return_value = [
                _message("m1", "a"),
                _message("m2", "b"),
            ]
            ids = mailbox.get_current_ids(_account())
        assert ids == {"m1", "m2"}

    def test_fetch_passes_recipient_filter(self):
        """必须按别名过滤，否则会读到主号其它邮件。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.return_value = []
            mailbox.get_current_ids(_account())
            _args, kwargs = service.return_value.fetch_account_messages.call_args
        assert kwargs["recipient"] == "abc@icloud.com"
        assert _args[0] == 7

    def test_current_ids_survives_read_failure(self):
        """首轮探测失败不该打断注册，返回空集即可。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.side_effect = RuntimeError("IMAP 挂了")
            assert mailbox.get_current_ids(_account()) == set()

    def test_missing_account_id_raises(self):
        """`get_current_ids` 会吞掉读取失败（首轮探测不该打断注册），
        但缺主号 id 是配置错误，必须在取码时立刻炸出来而不是等超时。"""
        mailbox = ICloudLocalMailbox()
        bad = MailboxAccount(email="abc@icloud.com", account_id="", extra={})
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.return_value = []
            with pytest.raises(RuntimeError, match="缺少主号 id"):
                mailbox.wait_for_code(bad, timeout=1)

    def test_missing_address_raises(self):
        mailbox = ICloudLocalMailbox()
        bad = MailboxAccount(email="", account_id="7:", extra={"icloud_account_id": 7})
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.return_value = []
            with pytest.raises(RuntimeError, match="缺少地址"):
                mailbox.wait_for_code(bad, timeout=1)


class TestWaitForCode:
    def test_finds_code_in_snippet(self):
        """摘要里的验证码要能提出来（字段名错了这里就挂）。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.return_value = [
                _message("old", "旧邮件", "验证码 111111"),
                _message("new", "xAI", "您的验证码是 654321"),
            ]
            code = mailbox.wait_for_code(_account(), before_ids={"old"}, timeout=5)
        assert code == "654321"

    def test_finds_code_in_text_body(self):
        """摘要没命中时，正文（`text_body`）里的验证码也要能提出来。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.return_value = [
                _message("m1", "欢迎", snippet="", body="verification code 998877"),
            ]
            code = mailbox.wait_for_code(_account(), timeout=5)
        assert code == "998877"

    def test_skips_already_seen_ids(self):
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.return_value = [
                _message("seen", "xAI", "验证码 111111"),
            ]
            with pytest.raises(TimeoutError):
                mailbox.wait_for_code(_account(), before_ids={"seen"}, timeout=1)

    def test_honours_keyword_filter(self):
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.return_value = [
                _message("m1", "其它站点", "验证码 111111"),
                _message("m2", "xAI", "验证码 222222"),
            ]
            code = mailbox.wait_for_code(_account(), keyword="xai", timeout=5)
        assert code == "222222"

    def test_skips_excluded_codes(self):
        """上一轮用过的验证码不能重复命中（否则注册会卡在同一个码上）。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.return_value = [
                _message("m1", "xAI", "验证码 111111"),
            ]
            with pytest.raises(TimeoutError):
                mailbox.wait_for_code(_account(), timeout=1, exclude_codes={"111111"})

    def test_times_out_when_nothing_new(self):
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.fetch_account_messages.return_value = []
            with pytest.raises(TimeoutError):
                mailbox.wait_for_code(_account(), timeout=1)


class TestPoolAccountingHook:
    """号池记账必须挂在既有的 `set_account_status` 生命周期钩子上。

    上层（`platforms/chatgpt/registration_engine.py:239`）是用
    `getattr(mailbox, "set_account_status", None)` 回调渠道的 —— 没实现这个
    方法时回调被静默跳过，于是 `mark_alias_used` / `release_alias_claim`
    一个生产调用点都没有，领过的别名永远停在 `in_use`：池子只出不进，
    `claim_alias` 再也复用不到闲号，只能去消耗 Apple 每小时 5 个的生成额度。
    """

    def _account_with_alias(self, alias_id=42):
        return MailboxAccount(
            email="abc@icloud.com",
            account_id="7:abc@icloud.com",
            extra={"icloud_account_id": 7, "icloud_alias_id": alias_id},
        )

    def test_hook_exists_and_is_callable(self):
        """钩子必须存在 —— 否则上层 getattr 拿到 None 就静默跳过。"""
        mailbox = ICloudLocalMailbox()
        assert callable(getattr(mailbox, "set_account_status", None))

    def test_used_marks_alias_used(self):
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            mailbox.set_account_status(self._account_with_alias(), "used")
        # `platform` 必须一起传：邮箱只对用掉它的那个平台一次性，
        # 不记平台的话别的平台也领不到它（跨平台复用就废了）。
        service.return_value.mark_alias_used.assert_called_once_with(
            42, platform=mailbox._platform_name
        )

    def test_used_records_the_platform_that_consumed_it(self):
        """平台名要真的传下去 —— 它是「别的平台还能不能复用」的唯一依据。"""
        mailbox = ICloudLocalMailbox()
        mailbox._platform_name = "grok"
        with patch.object(mailbox, "_service") as service:
            mailbox.set_account_status(self._account_with_alias(), "used")
        service.return_value.mark_alias_used.assert_called_once_with(42, platform="grok")

    def test_failed_releases_claim(self):
        """失败要把号放回去复用，而不是标死 —— 地址本身还是好的。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            mailbox.set_account_status(self._account_with_alias(), "failed")
        service.return_value.release_alias_claim.assert_called_once_with(42)

    def test_available_maps_to_pool_status(self):
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            mailbox.set_account_status(self._account_with_alias(), "available")
        service.return_value.set_alias_pool_status.assert_called_once_with(42, "available")

    def test_account_without_alias_id_is_ignored(self):
        """不是从本渠道领来的账号（人工导入）没有记账对象，静默跳过。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            mailbox.set_account_status(MailboxAccount(email="x@y.com"), "used")
        service.return_value.mark_alias_used.assert_not_called()

    def test_unknown_status_is_ignored(self):
        """`in_use` 等中间态不是收尾语义，不该改动号池。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            mailbox.set_account_status(self._account_with_alias(), "in_use")
        service.return_value.mark_alias_used.assert_not_called()
        service.return_value.release_alias_claim.assert_not_called()

    def test_accounting_failure_does_not_raise(self):
        """记账炸了不能影响注册结果 —— 号已经注册出去了。"""
        mailbox = ICloudLocalMailbox()
        with patch.object(mailbox, "_service") as service:
            service.return_value.mark_alias_used.side_effect = RuntimeError("db down")
            mailbox.set_account_status(self._account_with_alias(), "used")
