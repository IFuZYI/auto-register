import json
import unittest

from core.db import AccountModel
from services.account_export import (
    EXPORT_FORMATS,
    SEPARATOR,
    export_filename,
    list_export_formats,
    render_accounts,
)

SECRET = "JBSWY3DPEHPK3PXP"


def _account(**overrides) -> AccountModel:
    extra = {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "id_token": "id-1",
        "session_token": "sess-1",
        "totp_secret": SECRET,
        "phone_number": "+66123456789",
    }
    extra.update(overrides.pop("extra", {}))
    defaults = dict(
        platform="chatgpt",
        email="demo@example.com",
        password="pw-1",
        user_id="user-1",
        status="registered",
        token="at-1",
        extra_json=json.dumps(extra, ensure_ascii=False),
    )
    defaults.update(overrides)
    return AccountModel(**defaults)


class ExportFormatTests(unittest.TestCase):
    def test_email_password_pair(self):
        self.assertEqual(
            render_accounts([_account()], "email_pw"),
            f"demo@example.com{SEPARATOR}pw-1",
        )

    def test_email_password_two_factor(self):
        self.assertEqual(
            render_accounts([_account()], "email_pw_2fa"),
            f"demo@example.com{SEPARATOR}pw-1{SEPARATOR}{SECRET}",
        )

    def test_two_factor_column_comes_from_extra_json(self):
        line = render_accounts([_account()], "email_pw_2fa_rt")
        self.assertEqual(line.split(SEPARATOR)[2], SECRET)
        self.assertEqual(line.split(SEPARATOR)[3], "rt-1")

    def test_missing_fields_keep_their_separator(self):
        account = _account(password="", extra={"totp_secret": "", "refresh_token": ""})
        self.assertEqual(
            render_accounts([account], "email_pw_2fa_rt"),
            f"demo@example.com{SEPARATOR}{SEPARATOR}{SEPARATOR}",
        )

    def test_access_token_falls_back_to_the_account_column(self):
        account = _account(token="at-column", extra={"access_token": ""})
        self.assertEqual(render_accounts([account], "at"), "at-column")

    def test_single_column_formats_drop_rows_without_a_value(self):
        with_secret = _account()
        without_secret = _account(email="other@example.com", extra={"totp_secret": ""})
        self.assertEqual(render_accounts([with_secret, without_secret], "totp"), SECRET)

    def test_one_line_per_account(self):
        rows = render_accounts([_account(), _account(email="b@example.com")], "email_pw")
        self.assertEqual(len(rows.split("\n")), 2)

    def test_csv_carries_a_header_and_the_secret(self):
        content = render_accounts([_account()], "csv")
        header, row = content.split("\n")
        self.assertIn("totp_secret", header.split(","))
        self.assertEqual(row.split(",")[header.split(",").index("totp_secret")], SECRET)

    def test_json_export_is_parsable(self):
        rows = json.loads(render_accounts([_account()], "json"))
        self.assertEqual(rows[0]["totp_secret"], SECRET)
        self.assertEqual(rows[0]["refresh_token"], "rt-1")

    def test_broken_extra_json_does_not_break_the_export(self):
        account = _account(extra_json="{not json")
        self.assertEqual(
            render_accounts([account], "email_pw_2fa"),
            f"demo@example.com{SEPARATOR}pw-1{SEPARATOR}",
        )

    def test_unknown_format_is_rejected(self):
        with self.assertRaises(KeyError):
            render_accounts([_account()], "nope")

    def test_empty_selection_renders_nothing(self):
        self.assertEqual(render_accounts([], "email_pw_2fa"), "")

    def test_every_registered_format_renders(self):
        account = _account()
        for format_id in EXPORT_FORMATS:
            with self.subTest(format_id=format_id):
                self.assertTrue(render_accounts([account], format_id))

    def test_format_catalog_is_shaped_for_the_dropdown(self):
        formats = {item["id"]: item for item in list_export_formats()}
        self.assertIn("email_pw_2fa", formats)
        self.assertEqual(formats["csv"]["extension"], "csv")
        self.assertEqual(
            formats["email_pw_2fa"]["sample"],
            f"邮箱{SEPARATOR}密码{SEPARATOR}2FA 密钥",
        )

    def test_filename_carries_the_format_and_extension(self):
        name = export_filename("chatgpt", "csv")
        self.assertTrue(name.startswith("chatgpt_csv_"))
        self.assertTrue(name.endswith(".csv"))


if __name__ == "__main__":
    unittest.main()
