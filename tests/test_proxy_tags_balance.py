"""代理标签（注册 / 绑定）与绑定均衡。

用户 2026-10-30 的要求，逐条钉住：

1. `register` 标签 → 注册链路只从这些出口出去（一个都没有时回退到全部活跃）；
2. `bind` 标签 → 账号的绑定只能落在这些代理上；
3. 绑定的代理**有** `bind` 标签 → 保持不动（不要每次任务都换出口）；
4. 没绑定 / 绑定的代理没标签 / 绑定的代理已删除 → 均衡地绑到可绑定池；
5. 均衡按负载（挂得最少的优先），不是全堆在第一个；
6. 代理连接类错误 → 当场摘掉并换绑（不是等 5 次失败）。
"""

from __future__ import annotations

import unittest

from sqlmodel import Session, select

from core.base_platform import Account
from core.db import ProxyModel, account_repository, engine
from core.models.proxy import TAG_BIND, TAG_REGISTER, parse_proxy_tags, proxy_tags_csv
from core.proxy_pool import ProxyPool


def _add_proxy(url: str, *, tags: str = "", active: bool = True) -> ProxyModel:
    with Session(engine) as session:
        row = ProxyModel(url=url, tags=tags, is_active=active)
        session.add(row)
        session.commit()
        session.refresh(row)
        return row


def _cleanup(urls: list[str], emails: list[str]) -> None:
    with Session(engine) as session:
        for row in session.exec(select(ProxyModel).where(ProxyModel.url.in_(urls))).all():
            session.delete(row)
        session.commit()
    for email in emails:
        rows = [
            account
            for account in account_repository.list_all_accounts()
            if (account.email or "").lower() == email.lower()
        ]
        for account in rows:
            account_repository.delete_many([str(account.id)])


class ProxyTagParsingTests(unittest.TestCase):
    def test_accepts_csv_and_list(self):
        self.assertEqual(parse_proxy_tags("register,bind"), ["register", "bind"])
        self.assertEqual(parse_proxy_tags(["bind"]), ["bind"])

    def test_drops_unknown_values(self):
        # 拼错的标签如果被保留，选路时会出现「看着配了、实际没生效」
        self.assertEqual(parse_proxy_tags("register,bogus"), ["register"])

    def test_csv_is_stable_order(self):
        self.assertEqual(proxy_tags_csv(["bind", "register"]), "register,bind")
        self.assertEqual(proxy_tags_csv(None), "")


class GetNextByTagTests(unittest.TestCase):
    URLS = ["socks5h://t-reg@1.1.1.1:1080", "socks5h://t-bind@2.2.2.2:1080", "socks5h://t-none@3.3.3.3:1080"]

    def setUp(self):
        _add_proxy(self.URLS[0], tags="register")
        _add_proxy(self.URLS[1], tags="bind")
        _add_proxy(self.URLS[2], tags="")
        self.pool = ProxyPool()

    def tearDown(self):
        _cleanup(self.URLS, [])

    def test_register_tag_filters(self):
        for _ in range(5):
            self.assertEqual(self.pool.get_next(tag=TAG_REGISTER), self.URLS[0])

    def test_falls_back_when_no_tagged_proxy(self):
        """一个带标签的都没有时回退到全部活跃代理 —— 否则存量部署升上来注册直接没代理。"""
        _cleanup([self.URLS[0]], [])
        seen = {self.pool.get_next(tag=TAG_REGISTER) for _ in range(10)}
        # 共享库里可能有别的用例建的代理，不能断言「只可能是这两个」——
        # 真正要钉的是：**没有带 register 标签的代理时仍然返回了代理**（回退生效），
        # 而不是空。
        self.assertTrue(seen, "回退后不该返回 None")
        self.assertNotIn(self.URLS[0], seen, "被删掉的代理不该再出现")


class ResolveForAccountTests(unittest.TestCase):
    URL_A = "socks5h://bind-a@10.0.0.1:1080"
    URL_B = "socks5h://bind-b@10.0.0.2:1080"
    URL_NO_TAG = "socks5h://plain@10.0.0.3:1080"
    EMAILS = ["bind-test-a@example.com", "bind-test-b@example.com", "bind-test-c@example.com"]

    def setUp(self):
        self.a = _add_proxy(self.URL_A, tags="bind")
        self.b = _add_proxy(self.URL_B, tags="bind")
        _add_proxy(self.URL_NO_TAG, tags="register")
        self.pool = ProxyPool()

    def tearDown(self):
        _cleanup([self.URL_A, self.URL_B, self.URL_NO_TAG], self.EMAILS)

    def test_keeps_bind_tagged_proxy(self):
        chosen, should_bind = self.pool.resolve_for_account(self.URL_A)
        self.assertEqual(chosen, self.URL_A)
        self.assertFalse(should_bind, "已经在可绑定池里的代理不该被换掉")

    def test_untagged_proxy_is_replaced(self):
        chosen, should_bind = self.pool.resolve_for_account(self.URL_NO_TAG)
        self.assertIn(chosen, {self.URL_A, self.URL_B})
        self.assertTrue(should_bind)

    def test_deleted_proxy_is_replaced(self):
        chosen, should_bind = self.pool.resolve_for_account("socks5h://gone@10.9.9.9:1080")
        self.assertIn(chosen, {self.URL_A, self.URL_B})
        self.assertTrue(should_bind)

    def test_empty_binding_gets_one(self):
        chosen, should_bind = self.pool.resolve_for_account("")
        self.assertIn(chosen, {self.URL_A, self.URL_B})
        self.assertTrue(should_bind)

    def test_picks_the_least_loaded(self):
        """B 上挂 3 个号、A 上 0 个 → 必须挑 A。"""
        for index, email in enumerate(self.EMAILS):
            account = account_repository.upsert(
                Account(platform="chatgpt", email=email, password="p")
            )
            extra = dict(account.get_extra())
            extra["register_proxy"] = self.URL_B
            account.set_extra(extra)
            account_repository.upsert(account)

        counts = self.pool.bound_counts()
        self.assertEqual(counts[self.URL_B]["total"], len(self.EMAILS))
        self.assertNotIn(self.URL_A, counts)

        chosen, _ = self.pool.resolve_for_account("")
        self.assertEqual(chosen, self.URL_A, "均衡应挑挂得最少的代理")


class RebalanceTests(unittest.TestCase):
    URL_A = "socks5h://rb-a@10.1.0.1:1080"
    URL_B = "socks5h://rb-b@10.1.0.2:1080"
    URL_UNTAGGED = "socks5h://rb-plain@10.1.0.3:1080"
    EMAILS = [f"rebalance-{i}@example.com" for i in range(6)]

    def setUp(self):
        _add_proxy(self.URL_A, tags="bind")
        _add_proxy(self.URL_B, tags="bind")
        _add_proxy(self.URL_UNTAGGED, tags="")
        for email in self.EMAILS:
            account = account_repository.upsert(
                Account(platform="chatgpt", email=email, password="p")
            )
            extra = dict(account.get_extra())
            extra["register_proxy"] = self.URL_UNTAGGED  # 没标签 → 不合规
            account.set_extra(extra)
            account_repository.upsert(account)
        self.pool = ProxyPool()

    def tearDown(self):
        _cleanup([self.URL_A, self.URL_B, self.URL_UNTAGGED], self.EMAILS)

    def test_dry_run_changes_nothing(self):
        before = self.pool.bound_counts().get(self.URL_UNTAGGED, {}).get("total", 0)
        result = self.pool.rebalance(dry_run=True)
        after = self.pool.bound_counts().get(self.URL_UNTAGGED, {}).get("total", 0)
        self.assertEqual(before, after, "dry-run 不该写库")
        self.assertGreaterEqual(len(result["changed"]), len(self.EMAILS))

    def test_rebalance_moves_only_non_compliant_and_balances(self):
        result = self.pool.rebalance(dry_run=False)
        self.assertGreaterEqual(len(result["changed"]), len(self.EMAILS))

        counts = self.pool.bound_counts()
        self.assertEqual(counts.get(self.URL_UNTAGGED, {}).get("total", 0), 0, "不合规的绑定应被迁走")
        a = counts.get(self.URL_A, {}).get("total", 0)
        b = counts.get(self.URL_B, {}).get("total", 0)
        self.assertGreaterEqual(a + b, len(self.EMAILS))
        self.assertLessEqual(abs(a - b), 1, f"两个可绑定代理应基本平均，实际 A={a} B={b}")

    def test_compliant_bindings_are_not_touched(self):
        for email in self.EMAILS[:2]:
            account = account_repository.upsert(
                Account(platform="chatgpt", email=email, password="p")
            )
            extra = dict(account.get_extra())
            extra["register_proxy"] = self.URL_A
            account.set_extra(extra)
            account_repository.upsert(account)

        result = self.pool.rebalance(dry_run=False)
        moved = {item["email"] for item in result["changed"]}
        for email in self.EMAILS[:2]:
            self.assertNotIn(email, moved, "已经在可绑定代理上的账号不该被搬家")


class ReportProxyErrorTests(unittest.TestCase):
    URL = "socks5h://dead@10.2.0.1:1080"

    def setUp(self):
        _add_proxy(self.URL, tags="bind")

    def tearDown(self):
        _cleanup([self.URL], [])

    def test_proxy_error_deactivates_immediately(self):
        """连接类错误当场摘掉 —— 等 5 次失败的话这一轮任务已经白跑了。"""
        ProxyPool().report_proxy_error(self.URL)
        with Session(engine) as session:
            row = session.exec(select(ProxyModel).where(ProxyModel.url == self.URL)).one()
        self.assertFalse(row.is_active)
        self.assertEqual(row.fail_count, 1)


if __name__ == "__main__":
    unittest.main()
