"""`accounts.id` 是 UUID 字符串（ADR-0008）的行为契约。

为什么单独一条文件：主键形态是**跨模块契约** —— API 路径参数、批量任务请求体、
面板同步的 `local_id`、JSON 后端的排序、前端 `openDetail(record.id)` 全都依赖它。
它变了却只有一处测试盯着，等于没盯。
"""

from __future__ import annotations

import unittest
import uuid

from sqlmodel import Session, select

from core.base_platform import Account
from core.db import AccountModel, account_repository, current_engine


def _row(account_id: str) -> AccountModel | None:
    with Session(current_engine()) as session:
        return session.exec(
            select(AccountModel).where(AccountModel.id == account_id)
        ).first()


class UuidPrimaryKeyTests(unittest.TestCase):
    def test_upsert_assigns_a_uuid_string(self):
        acc = account_repository.upsert(
            Account(platform="chatgpt", email="uuid-a@example.com", password="p")
        )
        self.assertIsInstance(acc.id, str)
        # 能解析才算合法 UUID，光看长度会把 'x'*36 也放过
        uuid.UUID(str(acc.id))
        self.assertEqual(len(str(acc.id)), 36)

    def test_new_rows_have_no_legacy_id(self):
        acc = account_repository.upsert(
            Account(platform="chatgpt", email="uuid-b@example.com", password="p")
        )
        self.assertIsNone(acc.legacy_id, "新账号不该带旧的自增主键")

    def test_batch_delete_accepts_uuid_strings(self):
        """批量删除的请求体必须收字符串 id。

        原来是 `ids: list[int]` —— UUID 化之后前端传 `Array.from(selectedRowKeys)`
        全被 422 挡掉，也就是「批量删除」按钮直接失效。单测没有一条走这个端点，
        抓到它的是黑盒冒烟（`scripts/e2e_smoke.py`）。
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.accounts import router

        first = account_repository.upsert(
            Account(platform="chatgpt", email="uuid-batch-a@example.com", password="p")
        )
        second = account_repository.upsert(
            Account(platform="chatgpt", email="uuid-batch-b@example.com", password="p")
        )

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/accounts/batch-delete",
            json={"ids": [str(first.id), str(second.id)], "platform": "chatgpt"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json().get("deleted"), 2)
        self.assertIsNone(_row(str(first.id)))
        self.assertIsNone(_row(str(second.id)))

    def test_two_accounts_get_distinct_ids(self):
        first = account_repository.upsert(
            Account(platform="chatgpt", email="uuid-c@example.com", password="p")
        )
        second = account_repository.upsert(
            Account(platform="chatgpt", email="uuid-d@example.com", password="p")
        )
        self.assertNotEqual(first.id, second.id)

    def test_get_and_delete_round_trip_by_uuid(self):
        acc = account_repository.upsert(
            Account(platform="chatgpt", email="uuid-e@example.com", password="p")
        )
        self.assertIsNotNone(account_repository.get(str(acc.id)))
        self.assertTrue(account_repository.delete(str(acc.id)))
        self.assertIsNone(_row(str(acc.id)))

    def test_batch_helpers_accept_uuid_strings(self):
        first = account_repository.upsert(
            Account(platform="chatgpt", email="uuid-f@example.com", password="p")
        )
        second = account_repository.upsert(
            Account(platform="chatgpt", email="uuid-g@example.com", password="p")
        )
        rows = account_repository.get_many([str(second.id), str(first.id)])
        self.assertEqual([r.id for r in rows], [str(second.id), str(first.id)],
                         "批量取必须保持调用方给的顺序")

        deleted, not_found = account_repository.delete_many([str(first.id), "999999"])
        self.assertEqual(deleted, [str(first.id)])
        self.assertEqual(not_found, ["999999"])


if __name__ == "__main__":
    unittest.main()
