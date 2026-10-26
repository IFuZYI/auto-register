"""没设密码时不能对公网敞开整个凭据库。

安全审查（2026-10-27）F1：`auth_middleware` 在 `auth_password_hash` 为空时
**直接放行**（fail-open）。而默认部署（compose / Dockerfile）绑 `0.0.0.0`，
于是「装好还没设密码」这个窗口里，任何能访问端口的人都读得到：

- `GET /api/accounts` —— 全量账号，含 password / SSO / AT / RT / id_token /
  totp_secret
- `GET /api/backup/export` —— ZIP，含全部库 **和凭据加密密钥本身**

修法：只允许**本机回环**访问未受保护的 API；非回环来源在设密码前一律 503。
这样「本机调试不受影响」和「公网不裸奔」两件事同时成立。
"""

from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient


def _app():
    """复刻 `main.py` 的中间件行为（只挂必要部分）。"""
    from main import auth_middleware

    app = FastAPI()
    app.middleware("http")(auth_middleware)

    @app.get("/api/accounts")
    def _accounts():
        return {"items": [{"password": "secret"}]}

    @app.get("/api/auth/status")
    def _status():
        return {"has_password": False}

    @app.get("/healthz")
    def _health():
        return {"ok": True}

    return app


class _Cfg:
    def __init__(self, store):
        self._store = store

    def get(self, key, default=None):
        return self._store.get(key, "" if default is None else default)


class NoPasswordBlocksRemoteTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock

        self.store: dict = {}
        self._patch = mock.patch(
            "core.config_store.config_store", _Cfg(self.store)
        )
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _client(self, host: str):
        client = TestClient(_app())
        # TestClient 的 client 元组是 (host, port) —— 改它来模拟远端来源
        client._transport.client = (host, 12345)  # type: ignore[attr-defined]
        return client

    def test_remote_client_is_blocked_without_password(self):
        r = self._client("203.0.113.9").get("/api/accounts")
        self.assertEqual(
            r.status_code, 503,
            f"未设密码时远端仍可读账号: {r.status_code} {r.text[:120]}",
        )
        self.assertNotIn("secret", r.text)

    def test_loopback_client_is_allowed_without_password(self):
        r = self._client("127.0.0.1").get("/api/accounts")
        self.assertEqual(r.status_code, 200, "本机调试被挡住了")

    def test_auth_status_is_always_reachable(self):
        """前端要靠它判断「要不要显示设密码引导」。"""
        r = self._client("203.0.113.9").get("/api/auth/status")
        self.assertEqual(r.status_code, 200)

    def test_non_api_paths_unaffected(self):
        r = self._client("203.0.113.9").get("/healthz")
        self.assertEqual(r.status_code, 200)

    def test_with_password_remote_gets_401_not_503(self):
        self.store["auth_password_hash"] = "x"
        r = self._client("203.0.113.9").get("/api/accounts")
        self.assertEqual(r.status_code, 401, "设了密码后应是 401（未认证）而不是 503")


class DocsAreGuardedTests(unittest.TestCase):
    """F8：`/openapi.json` / `/docs` 绕过了中间件，暴露全部路由清单。"""

    def test_openapi_is_disabled(self):
        import main

        self.assertIsNone(main.app.openapi_url, "openapi.json 仍对外暴露全部路由清单")
        self.assertIsNone(main.app.docs_url, "/docs 仍对外暴露")
        self.assertIsNone(main.app.redoc_url, "/redoc 仍对外暴露")


if __name__ == "__main__":
    unittest.main()
