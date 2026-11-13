"""导出接口：格式清单、勾选导出、按筛选导出。"""

import json
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

        # **自己保证表存在**：JSON 后端下 `init_db` 不再建库（数据全在
        # JSON），而这个用例直接用 `core.db.engine` 造数据 —— 不建表就会
        # `no such table: accounts`（实测）。
        from sqlmodel import SQLModel

        SQLModel.metadata.create_all(engine, tables=[AccountModel.__table__])

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

    def test_kiro_auth_token_export_is_the_flat_agent2api_file(self):
        from sqlmodel import Session, delete

        from core.db import AccountModel, engine

        with Session(engine) as session:
            session.exec(delete(AccountModel).where(AccountModel.email == "kiro-export@x.ai"))
            model = AccountModel(platform="kiro", email="kiro-export@x.ai", password="pw")
            model.set_extra(
                {
                    "access_token": "at-kiro",
                    "refresh_token": "rt-kiro",
                    "client_id": "cid-kiro",
                    "client_secret": "cs-kiro",
                    "auth_method": "idc",
                    "login_provider": "BuilderId",
                    "account_id": "kiro-export@x.ai",
                    "kiro_aws_client_id": "aws-cid",
                    "kiro_aws_client_secret": "aws-cs",
                    "kiro_aws_refresh_token": "aws-rt",
                    "kiro_aws_access_token": "aws-at",
                    "kiro_aws_region": "us-east-1",
                }
            )
            session.add(model)
            session.commit()
            model_id = model.id
        try:
            response = self.client.post(
                "/accounts/export-text",
                json={"format": "kiro_auth_token", "account_ids": [model_id]},
            )
            self.assertEqual(response.status_code, 200, response.text[:300])
            body = response.json()
            self.assertEqual(body["total"], 1)
            doc = json.loads(body["content"])
            self.assertEqual(doc["accessToken"], "aws-at")
            self.assertEqual(doc["clientId"], "aws-cid")
            self.assertEqual(doc["email"], "kiro-export@x.ai")
        finally:
            with Session(engine) as session:
                session.exec(delete(AccountModel).where(AccountModel.email == "kiro-export@x.ai"))
                session.commit()

    def test_kiro_auth_token_without_kiro_accounts_is_400(self):
        response = self.client.post(
            "/accounts/export-text",
            json={"format": "kiro_auth_token", "platform": "chatgpt"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("没有可导出的 Kiro 账号", response.json()["detail"])

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

    def test_kiro_json_export_is_a_manager_document(self):
        with Session(engine) as session:
            model = _account(
                "kiro-export@example.com",
                platform="kiro",
                extra={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "client_id": "cid-1",
                    "client_secret": "cs-1",
                    "profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/X",
                    "region": "us-east-1",
                    "auth_method": "idc",
                    "login_provider": "BuilderId",
                    "plan": "Kiro FREE",
                    "account_id": "kiro-export@example.com",
                    "uid": "builderid",
                    "source": "aws",
                    "source_email": "kiro-export@example.com",
                    "expires_at": 1794478346773,
                },
            )
            session.add(model)
            session.commit()

        response = self.client.post(
            "/accounts/export-text",
            json={"format": "kiro_json", "platform": "kiro"},
        )
        self.assertEqual(response.status_code, 200)
        document = json.loads(response.json()["content"])
        self.assertEqual(document["version"], 2)
        self.assertEqual(document["customProviders"], [])
        entry = document["accounts"][0]
        self.assertEqual(entry["source"], "plugin", "管理器只认 plugin")
        self.assertEqual(entry["pluginSecret"]["clientId"], "cid-1")
        self.assertEqual(entry["pluginSecret"]["refresh"], "rt-1")

    def test_unknown_format_is_a_400(self):
        response = self.client.post(
            "/accounts/export-text", json={"format": "nope", "platform": "chatgpt"}
        )

        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
