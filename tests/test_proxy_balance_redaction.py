"""`/api/proxies/balance` 不能回吐明文代理凭据。

代理 URL 的形状是 `socks5://user:pass@host:port`，口令就是凭据。列表接口
（`_public_proxy`）已经统一打码，均衡接口却把 `rebalance()` 的 `from/to`
原样返回 —— 一次 `dry_run=true` 就能把全部代理口令送进浏览器、截图和日志。

这里只钉住「返回体里不出现明文口令」这个可观测事实，不关心打码后长什么样。
"""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.proxies import router as proxies_router
from core.proxy_utils import redact_proxy_url


class BalanceRedactionTests(unittest.TestCase):
    SECRET = "s3cr3t-passw0rd"

    def _client(self) -> TestClient:
        import api.proxies as proxies_module

        app = FastAPI()
        app.include_router(proxies_router, prefix="/api")

        # 不碰真实代理池：直接替换 rebalance 的返回值，模拟「会发生变更」。
        fake = {
            "changed": [
                {
                    "account_id": "acc-1",
                    "email": "a@example.com",
                    "from": f"socks5://user:{self.SECRET}@10.0.0.1:1080",
                    "to": f"socks5://user:{self.SECRET}@10.0.0.2:1080",
                }
            ],
            "skipped": 0,
            "reason": "",
        }
        original = proxies_module.proxy_pool.rebalance
        proxies_module.proxy_pool.rebalance = lambda dry_run=True: fake
        self.addCleanup(lambda: setattr(proxies_module.proxy_pool, "rebalance", original))
        return TestClient(app)

    def test_balance_response_does_not_leak_the_password(self):
        response = self._client().post("/api/proxies/balance", json={"dry_run": True})
        self.assertEqual(response.status_code, 200, response.text[:200])
        body = response.text
        self.assertNotIn(
            self.SECRET, body,
            f"均衡接口把代理口令原样返回了：{body[:300]}",
        )
        # 仍然要能认出是哪台代理（主机端口保留），否则界面无法映射到行。
        items = response.json().get("items") or []
        self.assertEqual(len(items), 1, "变更条目不该被吞掉")
        self.assertIn("10.0.0.2", str(items[0]["to"]), "打码不能把主机也抹掉")

    def test_redaction_shape_matches_the_list_endpoint(self):
        """与 `_public_proxy` 用同一个函数，前端按 url 做的 key 匹配才不会错位。"""
        masked = redact_proxy_url(f"socks5://user:{self.SECRET}@10.0.0.2:1080")
        response = self._client().post("/api/proxies/balance", json={"dry_run": True})
        items = response.json().get("items") or []
        self.assertEqual(items[0]["to"], masked)


if __name__ == "__main__":
    unittest.main()