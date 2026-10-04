"""导出接口：格式清单、勾选导出、按筛选导出。"""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from core.db import AccountModel, engine
from services.account_export import SEPARATOR

SECRET = "JBSWY3DPEHPK3PXP"


def _account(email, *, extra=None, platform="chatgpt", status="registered"):
    model = AccountModel(platform=platform, email=email, password="pw", status=status)
    model.set_extra(extra or {})
    return model


class ExportEndpointTests(unittest.TestCase):
    def setUp(self):
        from api.accounts import router

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all(
                [
                    _account("bound@example.com", extra={"totp_secret": SECRET, "refresh_token": "rt-1"}),
                    _account("plain@example.com"),
                    _account("other@example.com", platform="cursor"),
                ]
            )
            session.commit()

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app)

    def _id_of(self, email: str) -> int:
        with Session(engine) as session:
            return session.exec(select(AccountModel).where(AccountModel.email == email)).first().id

    def test_format_catalog_is_served(self):
        body = self.client.get("/accounts/export-formats").json()

        ids = [item["id"] for item in body["formats"]]
        self.assertIn("email_pw_2fa", ids)
        self.assertIn(body["default"], ids)

    def test_selection_export_keeps_the_requested_order(self):
        ids = [self._id_of("plain@example.com"), self._id_of("bound@example.com")]

        body = self.client.post(
            "/accounts/export-text",
            json={"format": "email_pw_2fa", "platform": "chatgpt", "account_ids": ids},
        ).json()

        self.assertEqual(body["total"], 2)
        self.assertEqual(
            body["content"].split("\n"),
            [
                f"plain@example.com{SEPARATOR}pw{SEPARATOR}",
                f"bound@example.com{SEPARATOR}pw{SEPARATOR}{SECRET}",
            ],
        )

    def test_platform_filter_is_applied_when_nothing_is_selected(self):
        body = self.client.post(
            "/accounts/export-text", json={"format": "email_pw", "platform": "chatgpt"}
        ).json()

        self.assertEqual(body["total"], 2)
        self.assertNotIn("other@example.com", body["content"])

    def test_email_filter_narrows_the_export(self):
        body = self.client.post(
            "/accounts/export-text",
            json={"format": "totp", "platform": "chatgpt", "email": "bound"},
        ).json()

        self.assertEqual(body["content"], SECRET)
        self.assertEqual(body["lines"], 1)

    def test_filename_matches_the_format(self):
        body = self.client.post(
            "/accounts/export-text", json={"format": "csv", "platform": "chatgpt"}
        ).json()

        self.assertTrue(body["filename"].endswith(".csv"))

    def test_unknown_format_is_a_400(self):
        response = self.client.post(
            "/accounts/export-text", json={"format": "nope", "platform": "chatgpt"}
        )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
