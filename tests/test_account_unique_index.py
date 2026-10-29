"""唯一索引 `uq_accounts_platform_email` 的行为契约。

背景：账号的唯一业务键 `(platform, email)` 一直只靠 `repository.upsert` 的
select-then-write 保证 —— 并发注册同邮箱会插重复行，而重复行会让「已注册不再
注册」的判重失效（同平台同邮箱出现两行，谁是真身说不清）。

这条索引是**表达式索引** `(platform, lower(email))`：裸列索引挡不住大小写
不同的同一地址（实测 `USER@X.AI` 能插到 `user@x.ai` 旁边）。
"""

from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from core.base_platform import Account
from core.db import AccountModel, account_repository, current_engine

INDEX_NAME = "uq_accounts_platform_email"


def _select(email: str) -> list[AccountModel]:
    with Session(current_engine()) as session:
        return list(
            session.exec(
                select(AccountModel)
                .where(AccountModel.platform == "chatgpt")
                .where(AccountModel.email == email.strip().lower())
            ).all()
        )


class UniqueIndexTests(unittest.TestCase):
    def test_index_exists_on_the_test_database(self):
        """测试库必须带生产那条约束 —— 约束不一致等于没测。"""
        with Session(current_engine()) as session:
            names = {
                row[0]
                for row in session.exec(
                    text("select name from sqlite_master where type='index'")
                )
            }
        self.assertIn(INDEX_NAME, names)

    def test_upsert_normalizes_case_into_one_row(self):
        email = "UniqueIndex.Case@Example.com"
        account_repository.upsert(
            Account(platform="chatgpt", email=email, password="first")
        )
        account_repository.upsert(
            Account(platform="chatgpt", email=email.upper(), password="second")
        )
        rows = _select(email)
        self.assertEqual(len(rows), 1, "大小写不同的同一地址必须合成一行")
        self.assertEqual(rows[0].password, "second", "后写的那份字段胜出")

    def test_raw_insert_of_case_variant_is_rejected(self):
        """绕过仓储直接插：必须被数据库拦住，而不是靠应用层自觉。"""
        email = "UniqueIndex.Raw@Example.com"
        account_repository.upsert(
            Account(platform="chatgpt", email=email, password="p")
        )
        with Session(current_engine()) as session:
            session.add(
                AccountModel(
                    platform="chatgpt",
                    email=email.upper(),
                    password="dup",
                    user_id="",
                    region="",
                    token="",
                    status="registered",
                    cashier_url="",
                    extra_json="{}",
                )
            )
            with self.assertRaises(IntegrityError):
                session.commit()
            session.rollback()

    def test_concurrent_upsert_yields_exactly_one_row(self):
        """20 个线程同时 upsert 同一邮箱：只允许一行，且不许抛异常。

        加索引之前这里是「静默插 20 行」；加之后是「一个赢、其余撞
        IntegrityError」—— 仓储必须把后者转成一次更新，否则并发注册会直接失败。
        """
        email = "UniqueIndex.Race@Example.com"

        def worker(index: int) -> None:
            account_repository.upsert(
                Account(
                    platform="chatgpt",
                    email=email,
                    password=f"pw-{index}",
                )
            )

        with ThreadPoolExecutor(max_workers=20) as pool:
            list(pool.map(worker, range(20)))

        rows = _select(email)
        self.assertEqual(len(rows), 1, f"并发 upsert 产生了 {len(rows)} 行")


if __name__ == "__main__":
    unittest.main()
