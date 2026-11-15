"""代理绑定必须是**双向**的：能绑上，也要能解绑 / 改绑。

真实缺陷（审计 2026-11-14）：`resolve_for_account` 在「没有更好选择时保留
原值」的注释写着「总比清空好」，于是整个系统没有任何路径能把账号从代理上
摘下来。绑错了（或那个代理后来变差）就只能靠 `rebalance` 挪到另一个
**带 bind 标签**的代理；没有可绑定代理时永久卡死，也没法恢复成直连。

这里定义 `PATCH /api/accounts/{id}/proxy` 的行为：
- 传 `proxy=""` → 解绑（清空 `register_proxy`），该账号此后直连
- 传具体 URL → 改绑成它（归一化后写入）
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient
from sqlmodel import Session

from core.db import AccountModel, engine


def _account(email: str = "unbind@example.com", *, proxy: str = "") -> AccountModel:
    model = AccountModel(platform="chatgpt", email=email, password="pw", status="registered")
    model.set_extra({"register_proxy": proxy} if proxy else {})
    return model


class AccountProxyRebindTests(unittest.TestCase):
    def setUp(self):
        from sqlmodel import select

        with Session(engine) as session:
            for stale in session.exec(
                select(AccountModel).where(AccountModel.email == "unbind@example.com")
            ).all():
                session.delete(stale)
            session.commit()
        model = _account(proxy="socks5://user:pass@10.0.0.9:1080")
        with Session(engine) as session:
            session.add(model)
            session.commit()
            self.account_id = model.id

        from main import app

        self.client = TestClient(app)

    def _stored_proxy(self) -> str:
        with Session(engine) as session:
            row = session.get(AccountModel, self.account_id)
        return str(row.get_extra().get("register_proxy") or "")

    def test_unbind_clears_the_bound_proxy(self):
        response = self.client.patch(
            f"/api/accounts/{self.account_id}/proxy",
            json={"proxy": ""},
        )
        self.assertEqual(response.status_code, 200, response.text[:200])
        self.assertEqual(
            self._stored_proxy(), "",
            "解绑没有清空 register_proxy —— 账号仍会走那个代理",
        )

    def test_rebind_sets_the_normalized_url(self):
        response = self.client.patch(
            f"/api/accounts/{self.account_id}/proxy",
            json={"proxy": "socks5h://user:pass@10.0.0.10:1080"},
        )
        self.assertEqual(response.status_code, 200, response.text[:200])
        stored = self._stored_proxy()
        self.assertIn("10.0.0.10", stored, f"改绑没生效: {stored!r}")

    def test_unknown_account_is_404(self):
        response = self.client.patch(
            "/api/accounts/does-not-exist/proxy",
            json={"proxy": ""},
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()