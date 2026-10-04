"""outlook 拆包后，渠道注册与三个后端必须仍从原路径可达。"""
import unittest


class ChannelRegistrationTests(unittest.TestCase):
    def test_facade_still_registers_the_provider(self):
        from core.base_mailbox import create_mailbox

        mailbox = create_mailbox("outlook")
        self.assertEqual(type(mailbox).__name__, "OutlookMailbox")

    def test_backends_are_reachable_from_the_old_path(self):
        from core.mailboxes.channels.outlook import (
            MailApiUrlOtpBackend,
            OutlookGraphMailboxBackend,
            OutlookImapMailboxBackend,
            OutlookMailboxBackend,
        )

        self.assertTrue(issubclass(OutlookImapMailboxBackend, OutlookMailboxBackend))
        self.assertTrue(issubclass(OutlookGraphMailboxBackend, OutlookMailboxBackend))
        self.assertTrue(issubclass(MailApiUrlOtpBackend, OutlookMailboxBackend))

    def test_oauth_helpers_stay_on_the_mailbox_class(self):
        """`services/mail_imports/*` 直接调 `mailbox.probe_oauth_availability`。"""
        from core.mailboxes.channels.outlook import OutlookMailbox

        for name in (
            "probe_oauth_availability",
            "_fetch_oauth_token_bundle",
            "_get_oauth_access_token",
            "_imap_auth_oauth",
            "_open_imap",
            "_resolve_backend",
            "pool_status_summary",
            "import_accounts_to_pool",
        ):
            self.assertTrue(hasattr(OutlookMailbox, name), f"OutlookMailbox 缺 {name}")


if __name__ == "__main__":
    unittest.main()
