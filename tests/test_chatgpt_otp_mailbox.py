"""补 RT 时按【已知地址】反查收件通道。"""

import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from core.base_mailbox import MailboxAccount
from core.task_runtime import StopTaskRequested
from platforms.chatgpt.protocol.mailbox_adapter import FixedAddressProviderAdapter
from services.chatgpt_otp_mailbox import (
    ICloudAliasMailProvider,
    resolve_otp_mail_provider,
)


class _StubControl:
    """记录 checkpoint 调用，可以在第 N 次抛停止。"""

    def __init__(self, raise_after: int | None = None):
        self.attempt_ids: list = []
        self._raise_after = raise_after

    def checkpoint(self, *, attempt_id=None, consume_skip=True):
        self.attempt_ids.append(attempt_id)
        if self._raise_after is not None and len(self.attempt_ids) > self._raise_after:
            raise StopTaskRequested()


class _Message:
    def __init__(self, message_id, text, *, received_at=None):
        self.provider_message_id = message_id
        self.subject = "OpenAI"
        self.text_body = text
        self.html_body = ""
        self.snippet = ""
        self.received_at = received_at or datetime.now(timezone.utc)


class _StubMailbox:
    def __init__(self, ids=None, code="123456"):
        self._ids = ids
        self._code = code
        self.wait_calls = []

    def get_email(self):
        raise AssertionError("补 RT 不该再向邮箱池要新地址")

    def get_current_ids(self, account):
        if self._ids is None:
            raise RuntimeError("列举失败")
        return self._ids

    def wait_for_code(self, account, **kwargs):
        self.wait_calls.append((account, kwargs))
        return self._code


class FixedAddressProviderAdapterTests(unittest.TestCase):
    def test_binds_to_given_address_instead_of_allocating(self):
        mailbox = _StubMailbox(ids={"m-1"})
        account = MailboxAccount(email="old@example.com", account_id="tok")
        adapter = FixedAddressProviderAdapter(mailbox, account)

        self.assertEqual(adapter.create_mailbox(), "old@example.com")
        self.assertEqual(adapter._before_ids, {"m-1"})
        self.assertTrue(adapter.accepts_existing_account)
        self.assertFalse(adapter.pooled)

    def test_prime_tolerates_listing_failure(self):
        adapter = FixedAddressProviderAdapter(
            _StubMailbox(ids=None), MailboxAccount(email="old@example.com")
        )
        adapter.prime()
        self.assertEqual(adapter._before_ids, set())

    def test_wait_for_otp_uses_bound_account(self):
        mailbox = _StubMailbox()
        account = MailboxAccount(email="old@example.com", account_id="tok")
        adapter = FixedAddressProviderAdapter(mailbox, account, otp_timeout=30)

        self.assertEqual(adapter.wait_for_otp("old@example.com", timeout=5), "123456")
        used_account, kwargs = mailbox.wait_calls[0]
        self.assertIs(used_account, account)
        self.assertEqual(kwargs["timeout"], 30)


class ICloudAliasProviderTests(unittest.TestCase):
    def test_returns_code_from_new_message(self):
        provider = ICloudAliasMailProvider(
            7, "alias@icloud.com", fetch=lambda: [_Message("m-1", "code 481920 expires")]
        )
        self.assertEqual(provider.wait_for_otp("alias@icloud.com", timeout=1), "481920")

    def test_primed_messages_are_not_read_again(self):
        messages = [_Message("m-1", "code 481920")]
        provider = ICloudAliasMailProvider(7, "alias@icloud.com", fetch=lambda: messages)
        provider.prime()

        with self.assertRaises(TimeoutError):
            provider.wait_for_otp("alias@icloud.com", timeout=1)

    def test_messages_older_than_the_window_are_skipped(self):
        old = datetime.now(timezone.utc) - timedelta(minutes=10)
        provider = ICloudAliasMailProvider(
            7, "alias@icloud.com", fetch=lambda: [_Message("m-1", "code 481920", received_at=old)]
        )

        with self.assertRaises(TimeoutError):
            provider.wait_for_otp(
                "alias@icloud.com",
                timeout=1,
                issued_after=datetime.now(timezone.utc).timestamp(),
            )

    def test_fetch_failure_does_not_abort_the_wait(self):
        calls = {"n": 0}

        def _fetch():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("IMAP 掉线")
            return [_Message("m-2", "code 224466")]

        provider = ICloudAliasMailProvider(
            7, "alias@icloud.com", fetch=_fetch, poll_interval=0.5
        )
        self.assertEqual(provider.wait_for_otp("alias@icloud.com", timeout=5), "224466")


class OtpWaitInterruptionTests(unittest.TestCase):
    """等码是补号里最长的一段，"停止任务"必须能在这期间生效。"""

    def test_alias_wait_is_interrupted_by_stop(self):
        control = _StubControl(raise_after=1)
        provider = ICloudAliasMailProvider(
            7, "alias@icloud.com", fetch=lambda: [], poll_interval=5
        )
        provider.bind_task_control(control, attempt_id=3)

        started = time.monotonic()
        with self.assertRaises(StopTaskRequested):
            provider.wait_for_otp("alias@icloud.com", timeout=120)

        # 别等满 5 秒的轮询间隔，更别等满 120 秒的超时
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(control.attempt_ids, [3, 3])

    def test_adapter_hands_control_and_log_to_the_mailbox(self):
        mailbox = _StubMailbox(ids=set())
        adapter = FixedAddressProviderAdapter(
            mailbox, MailboxAccount(email="old@example.com"), kind="微软邮箱"
        )
        lines: list[str] = []
        control = _StubControl()

        adapter.bind_task_control(control, attempt_id=9, log_fn=lines.append)
        mailbox._log_fn("[微软邮箱] OTP 收信后端: imap")

        self.assertIs(mailbox._task_control, control)
        self.assertEqual(mailbox._task_attempt_token, 9)
        self.assertEqual(lines, ["[微软邮箱] OTP 收信后端: imap"])

    def test_resolver_binds_control_onto_the_provider(self):
        control = _StubControl()
        with mock.patch.object(ICloudAliasMailProvider, "prime"), mock.patch(
            "services.chatgpt_otp_mailbox._build_icloud_provider",
            return_value=(ICloudAliasMailProvider(7, "alias@icloud.com", fetch=lambda: []), ""),
        ):
            provider, _ = resolve_otp_mail_provider(
                "alias@icloud.com", task_control=control, attempt_id=5
            )

        self.assertIs(provider._task_control, control)
        self.assertEqual(provider._attempt_id, 5)


class ResolveProviderTests(unittest.TestCase):
    def setUp(self):
        from sqlmodel import delete

        from core.db import (
            ICloudAliasModel,
            OutlookAccountModel,
            mailbox_pool_session,
        )

        # 邮箱池已分库（data/platforms/{icloud,outlook}.db），必须用池会话。
        # 用默认库 engine 会撞上 "no such table" —— 池表不在那里。
        with mailbox_pool_session("icloud") as session:
            session.exec(delete(ICloudAliasModel))
            session.commit()
        with mailbox_pool_session("outlook") as session:
            session.exec(delete(OutlookAccountModel))
            session.commit()

    def _add_alias(self, address):
        from core.db import ICloudAliasModel, mailbox_pool_session

        with mailbox_pool_session("icloud") as session:
            row = ICloudAliasModel(account_id=1, address=address)
            session.add(row)
            session.commit()
            session.refresh(row)
            return row.id

    def _add_outlook(self, email):
        from core.db import OutlookAccountModel, mailbox_pool_session

        with mailbox_pool_session("outlook") as session:
            session.add(
                OutlookAccountModel(
                    email=email,
                    password="pw",
                    client_id="cid",
                    refresh_token="rt",
                )
            )
            session.commit()

    def test_icloud_alias_wins(self):
        alias_id = self._add_alias("alias@icloud.com")

        with mock.patch.object(ICloudAliasMailProvider, "prime"):
            provider, reason = resolve_otp_mail_provider("alias@icloud.com")

        self.assertIsInstance(provider, ICloudAliasMailProvider)
        self.assertEqual(provider._alias_id, alias_id)
        self.assertEqual(reason, "")

    def test_outlook_pool_row_supplies_credentials(self):
        self._add_outlook("legacy@outlook.com")
        mailbox = _StubMailbox(ids=set())

        with mock.patch("core.base_mailbox.create_mailbox", return_value=mailbox) as factory:
            provider, reason = resolve_otp_mail_provider("legacy@outlook.com", config={})

        self.assertIsInstance(provider, FixedAddressProviderAdapter)
        self.assertEqual(factory.call_args.args[0], "outlook")
        self.assertEqual(provider._account.extra["refresh_token"], "rt")
        self.assertEqual(reason, "")

    def test_plus_alias_in_the_pool_still_matches_the_base_address(self):
        """号池存 xxx+abc@outlook.com，账号表记 xxx@outlook.com，是同一个信箱。"""
        self._add_outlook("legacy+tag1@outlook.com")
        mailbox = _StubMailbox(ids=set())

        with mock.patch("core.base_mailbox.create_mailbox", return_value=mailbox):
            provider, reason = resolve_otp_mail_provider("legacy@outlook.com", config={})

        self.assertIsInstance(provider, FixedAddressProviderAdapter)
        self.assertEqual(reason, "")
        # 登录身份用号池那条记录的地址，别名 IMAP 登不上
        self.assertEqual(provider._account.email, "legacy+tag1@outlook.com")
        self.assertEqual(provider._account.extra["refresh_token"], "rt")

    def test_base_address_in_the_pool_matches_a_plus_alias_account(self):
        self._add_outlook("legacy@outlook.com")
        mailbox = _StubMailbox(ids=set())

        with mock.patch("core.base_mailbox.create_mailbox", return_value=mailbox):
            provider, _ = resolve_otp_mail_provider("legacy+tag9@outlook.com", config={})

        self.assertEqual(provider._account.email, "legacy@outlook.com")

    def test_exact_match_still_wins_over_the_alias_fallback(self):
        self._add_outlook("legacy+tag1@outlook.com")
        self._add_outlook("legacy@outlook.com")
        mailbox = _StubMailbox(ids=set())

        with mock.patch("core.base_mailbox.create_mailbox", return_value=mailbox):
            provider, _ = resolve_otp_mail_provider("legacy@outlook.com", config={})

        self.assertEqual(provider._account.email, "legacy@outlook.com")

    def test_unrelated_address_does_not_borrow_pool_credentials(self):
        self._add_outlook("legacy+tag1@outlook.com")

        with mock.patch("core.base_mailbox.create_mailbox") as factory:
            provider, reason = resolve_otp_mail_provider("someone-else@outlook.com", config={})

        factory.assert_not_called()
        self.assertIsNone(provider)

    def test_configured_provider_is_not_consulted(self):
        """解析只认两个本地号池，不再按全局 `mail_provider` 猜渠道。

        回归：这里曾走 `_build_configured_provider` —— 拿全局配置里的 provider
        （cfworker / luckmail / 各种临时邮箱）去建邮箱。临时邮箱渠道整体删除后
        这条路径没有意义了：一个不在号池里的地址，配了 provider 也不该被认领。
        """
        mailbox = _StubMailbox(ids=set())

        with mock.patch("core.base_mailbox.create_mailbox", return_value=mailbox) as factory:
            provider, _ = resolve_otp_mail_provider(
                "someone@mail.example.com",
                account_extra={"mail_provider": "cfworker", "mailbox_token": "jwt"},
                config={"cfworker_domains": "example.com,other.com"},
            )

        factory.assert_not_called()
        self.assertIsNone(provider)

    def test_address_outside_both_pools_reports_no_channel(self):
        """两个号池都匹配不上时，理由里要说清「没有匹配的收件通道」。

        以前这条断言写的是「不属于当前邮箱服务」—— 那是 `_build_configured_provider`
        在拿全局 provider 的域名清单做匹配时的话术，随该函数一起删除了。
        """
        with mock.patch("core.base_mailbox.create_mailbox") as factory:
            provider, reason = resolve_otp_mail_provider("someone@example.com", config={})

        factory.assert_not_called()
        self.assertIsNone(provider)
        self.assertIn("没有匹配的收件通道", reason)

    def test_no_provider_configured(self):
        provider, reason = resolve_otp_mail_provider("someone@example.com", config={})

        self.assertIsNone(provider)
        self.assertIn("没有匹配的收件通道", reason)

    def test_blank_address_is_rejected(self):
        provider, reason = resolve_otp_mail_provider("  ")

        self.assertIsNone(provider)
        self.assertIn("没有邮箱地址", reason)


if __name__ == "__main__":
    unittest.main()
