"""密码加密（T1.4b）之后，**对外契约必须和以前一模一样**。

这条文件是一次真实回归的守卫：`password` 从字段改成 property 之后，
`jsonable_encoder` 不再把它序列化 —— 建账号返回体里没有 `password`、
详情弹窗的密码栏是空的（前端「复制密码」直接失效），而**全量测试全绿**。
抓到这个的是黑盒冒烟（`scripts/e2e_smoke.py`），不是单测。

根因：pydantic 只序列化**字段**，property 不算。修法是给 property 加
`@computed_field`，并让 `password_cipher` `exclude=True`（密文不该出现在响应里）。
"""

from __future__ import annotations

import json
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from core.db import AccountModel, engine

PASSWORD = "Pw-123"


class AccountPasswordContractTests(unittest.TestCase):
    def setUp(self):
        from api.accounts import router

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.commit()

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _create(self) -> dict:
        response = self.client.post(
            "/accounts",
            json={
                "platform": "chatgpt",
                "email": "pw-contract@example.com",
                "password": PASSWORD,
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_create_response_carries_plaintext_password(self):
        body = self._create()
        self.assertEqual(
            body.get("password"), PASSWORD,
            "建账号的响应里没有明文密码 —— 前端拿不到（property 不会被 pydantic 序列化）",
        )
        self.assertNotIn(
            "password_cipher", body, "响应里泄露了密文列"
        )

    def test_detail_response_carries_plaintext_password(self):
        created = self._create()
        response = self.client.get(f"/accounts/{created['id']}")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body.get("password"), PASSWORD, "详情弹窗拿不到密码")
        self.assertNotIn("password_cipher", body)

    def test_list_hides_password_but_keeps_the_flag(self):
        self._create()
        response = self.client.get("/accounts?page_size=10")
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()["items"]
        self.assertTrue(items)
        row = items[0]
        self.assertNotIn("password", row, "列表里出现了明文密码")
        self.assertNotIn("password_cipher", row, "列表里出现了密文")
        self.assertTrue(row.get("has_password"), "「有没有密码」的标记丢了")

    def test_database_stores_ciphertext(self):
        self._create()
        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == "pw-contract@example.com")
            ).first()
        self.assertTrue(row.password_cipher.startswith("v1:"), "库里不是密文")
        self.assertNotIn(PASSWORD, row.password_cipher, "库里出现了明文")
        self.assertEqual(row.password, PASSWORD, "读回来必须是明文")

    def test_dump_matches_the_pre_encryption_shape(self):
        """`model_dump()` 的键集合 = 加密之前的形状（有 password、无 cipher）。"""
        self._create()
        with Session(engine) as session:
            row = session.exec(select(AccountModel)).first()
        keys = set(row.model_dump().keys())
        self.assertIn("password", keys)
        self.assertNotIn("password_cipher", keys)


if __name__ == "__main__":
    unittest.main()
