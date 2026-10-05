"""PATCH /accounts/{id}：账号详情弹窗的「保存」。

背景（dogfood 契约检测实测）：`update_account` 函数体引用了一个不存在的
`session` 变量（签名里没有、模块级也没有绑定）—— 实测 PATCH 直接 500
`NameError: name 'session' is not defined`，账号详情弹窗的保存按钮
100% 失败。本文件钉住「能保存 + 真落库 + 只改提交的字段」。
"""

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, delete, select

from core.db import AccountModel, engine


def _account(email, *, extra=None, platform="chatgpt", status="registered"):
    model = AccountModel(platform=platform, email=email, password="pw", status=status)
    model.set_extra(extra or {})
    return model


class AccountUpdateEndpointTests(unittest.TestCase):
    def setUp(self):
        from api.accounts import router

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.add_all([_account("patch-me@example.com")])
            session.commit()

        app = FastAPI()
        app.include_router(router)
        self.client = TestClient(app, raise_server_exceptions=False)

    def _id_of(self, email: str) -> int:
        with Session(engine) as session:
            return session.exec(select(AccountModel).where(AccountModel.email == email)).first().id

    def test_patch_saves_status_and_token(self):
        account_id = self._id_of("patch-me@example.com")

        response = self.client.patch(
            f"/accounts/{account_id}?platform=chatgpt",
            json={"status": "invalid", "token": "at-new"},
        )

        self.assertEqual(response.status_code, 200, response.text)

        # 真落库（新会话读回，不是响应回显）
        with Session(engine) as session:
            row = session.get(AccountModel, account_id)
            self.assertEqual(row.status, "invalid")
            self.assertEqual(row.token, "at-new")

    def test_patch_keeps_unsubmitted_fields(self):
        """没提交的字段不能被清空（沉默不等于清空）。"""
        account_id = self._id_of("patch-me@example.com")

        response = self.client.patch(
            f"/accounts/{account_id}?platform=chatgpt",
            json={"status": "banned"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        with Session(engine) as session:
            row = session.get(AccountModel, account_id)
            self.assertEqual(row.password, "pw", "没提交的密码被清空了")
            self.assertEqual(row.token, "", "token 本来为空，应保持为空")

    def test_patch_unknown_account_is_404(self):
        response = self.client.patch("/accounts/999999?platform=chatgpt", json={"status": "invalid"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
