"""换绑 / 均衡的写回必须是「定向更新」，且必须按平台定位（proxy 审计 P0-1/2/3）。

三条都是真库能踩中的：

- **P0-2**：`rebind(email)` 只按邮箱在所有平台里取 `created_at` 最新的一行。
  真库 670 个邮箱里 387 个同时存在于 chatgpt 与 grok、其中 286 个最新行是
  grok —— 于是「ChatGPT 任务代理挂了」会改掉 **Grok 号**的绑定。
- **P0-1**：`_persist_register_proxy` 把 `platform="chatgpt"` 写死，Grok 任务
  永远不落库（每轮重新挑出口），同邮箱时还会改坏 ChatGPT 号。
- **P0-3**：写回用整行快照 `upsert` —— 覆盖并发写入的 password/token/状态，
  账号被删时还会走插入分支**以新 UUID 复活**。

这里用临时库（conftest 已把 DATA_DIR/DATABASE_URL 指到 tmp）钉住行为。
"""

from __future__ import annotations

import unittest

from sqlmodel import Session, select

from core.base_platform import Account
from core.db import ProxyModel, account_repository, engine
from core.models.proxy import TAG_BIND
from core.proxy_pool import ProxyPool
from core.proxy_utils import normalize_proxy_url

URL_A = "socks5://127.0.0.1:2081"
URL_B = "socks5://127.0.0.1:2082"
A = normalize_proxy_url(URL_A)
B = normalize_proxy_url(URL_B)


def _add_proxy(url: str, tags: str = TAG_BIND) -> None:
    with Session(engine) as session:
        session.add(ProxyModel(url=url, tags=tags, is_active=True))
        session.commit()


def _drop_proxies() -> None:
    with Session(engine) as session:
        for row in session.exec(select(ProxyModel).where(ProxyModel.url.in_([URL_A, URL_B]))).all():
            session.delete(row)
        session.commit()


def _make(platform: str, email: str, proxy: str = "") -> object:
    account = account_repository.upsert(Account(platform=platform, email=email, password="p"))
    if proxy:
        extra = dict(account.get_extra())
        extra["register_proxy"] = proxy
        account.set_extra(extra)
        account_repository.upsert(account)
        account = account_repository.get(str(account.id), platform)
    return account


def _proxy_of(account_id: str, platform: str) -> str:
    row = account_repository.get(str(account_id), platform)
    return normalize_proxy_url(str((row.get_extra() or {}).get("register_proxy") or "")) or ""


class CrossPlatformRebindTests(unittest.TestCase):
    EMAIL = "shared-proxy@example.com"

    def setUp(self):
        _drop_proxies()
        _add_proxy(URL_A)
        _add_proxy(URL_B)
        self.pool = ProxyPool()
        self.chatgpt = _make("chatgpt", self.EMAIL, URL_A)
        self.grok = _make("grok", self.EMAIL, URL_A)

    def tearDown(self):
        for row in (self.chatgpt, self.grok):
            try:
                account_repository.delete(str(row.id), row.platform)
            except Exception:
                pass
        _drop_proxies()

    def test_rebind_by_id_only_touches_that_platform(self):
        """按 id + platform 换绑，另一个平台的同邮箱账号必须原样不动。"""
        old, new = self.pool.rebind(
            self.EMAIL, account_id=str(self.grok.id), platform="grok"
        )
        self.assertEqual(old, A)
        self.assertEqual(new, B)
        self.assertEqual(_proxy_of(self.grok.id, "grok"), B)
        self.assertEqual(
            _proxy_of(self.chatgpt.id, "chatgpt"),
            A,
            "ChatGPT 号的绑定被 Grok 的换绑改掉了（跨平台写错号）",
        )

    def test_rebind_by_email_refuses_when_email_exists_on_two_platforms(self):
        """没有 platform 时不要猜 —— 猜错就是改坏另一个平台。"""
        old, new = self.pool.rebind(self.EMAIL)
        self.assertEqual((old, new), ("", ""))
        self.assertEqual(_proxy_of(self.grok.id, "grok"), A)
        self.assertEqual(_proxy_of(self.chatgpt.id, "chatgpt"), A)

    def test_rebind_by_email_with_platform_is_allowed(self):
        old, new = self.pool.rebind(self.EMAIL, platform="chatgpt")
        self.assertEqual(old, A)
        self.assertEqual(new, B)
        self.assertEqual(_proxy_of(self.chatgpt.id, "chatgpt"), B)
        self.assertEqual(_proxy_of(self.grok.id, "grok"), A)


class TargetedWriteTests(unittest.TestCase):
    def setUp(self):
        _drop_proxies()
        _add_proxy(URL_A)
        _add_proxy(URL_B)
        self.account = _make("chatgpt", "writeback@example.com", URL_A)

    def tearDown(self):
        try:
            account_repository.delete(str(self.account.id), "chatgpt")
        except Exception:
            pass
        _drop_proxies()

    def test_update_extra_does_not_insert_missing_rows(self):
        before = len(account_repository.list_all_accounts())
        ok = account_repository.update_extra(
            "00000000-0000-0000-0000-000000000000", "chatgpt", {"register_proxy": URL_B}
        )
        self.assertFalse(ok, "对不存在的账号写回竟然成功")
        self.assertEqual(len(account_repository.list_all_accounts()), before, "凭空多出一行")

    def test_update_extra_keeps_other_fields(self):
        """只改 extra_json：password / token / status 不能被整行快照覆盖。"""
        with Session(engine) as session:
            row = account_repository.get(str(self.account.id), "chatgpt")
            row.token = "token-keep"
            row.status = "active"
            session.add(row)
            session.commit()

        extra = dict(self.account.get_extra())
        extra["register_proxy"] = URL_B
        self.assertTrue(
            account_repository.update_extra(str(self.account.id), "chatgpt", extra)
        )
        row = account_repository.get(str(self.account.id), "chatgpt")
        self.assertEqual(row.token, "token-keep")
        self.assertEqual(row.status, "active")
        self.assertEqual((row.get_extra() or {}).get("register_proxy"), URL_B)

    def test_rebalance_does_not_resurrect_deleted_accounts(self):
        """快照里有、库里已删的账号：跳过，不能以新 UUID 复活。"""
        pool = ProxyPool()
        snapshot = account_repository.list_all_accounts()
        target = next(a for a in snapshot if str(a.id) == str(self.account.id))
        account_repository.delete(str(self.account.id), "chatgpt")

        from unittest import mock

        with mock.patch.object(
            account_repository, "list_all_accounts", return_value=snapshot
        ):
            result = pool.rebalance(dry_run=False)

        self.assertGreaterEqual(result["skipped"], 1)
        remaining = [
            a for a in account_repository.list_all_accounts()
            if (a.email or "").lower() == "writeback@example.com"
        ]
        self.assertEqual(remaining, [], "已删除的账号被均衡写回复活了")


class LoadFieldsCarriesIdentityTests(unittest.TestCase):
    def test_fields_include_account_id_and_platform(self):
        """写回路径要靠这两个字段定位 —— 缺了就退化成按 email 跨平台猜。"""
        from services.task_runners import _load_account_fields

        account = _make("grok", "fields@example.com", URL_A)
        try:
            fields = _load_account_fields(str(account.id), "grok")
            self.assertIsNotNone(fields)
            self.assertEqual(fields["account_id"], str(account.id))
            self.assertEqual(fields["platform"], "grok")
        finally:
            account_repository.delete(str(account.id), "grok")
            _drop_proxies()


if __name__ == "__main__":
    unittest.main()
