"""认证加固：密码哈希、登录限流、临时令牌回收。

安全审查（2026-10-27）发现三项：

- **F5a**：`_hash_pw` 是**单轮无盐 SHA-256**。面板密码是唯一保护整个凭据库的
  东西，而哈希是可离线爆破的（GPU 上十亿次/秒）。库或备份一旦泄漏，密码当场
  还原。
- **F5b**：`/auth/login` 与 `/auth/verify-totp` **没有任何失败计数或退避**，
  可以全速爆破；TOTP 校验失败既不吃掉临时令牌也不计数，同一个令牌上可以
  无限次试码。
- **F11**：`_pending_2fa` 只在**验证成功**时移除条目，过期的从不清理 ——
  每次密码登录都加一条，长期运行下无界增长。
"""

from __future__ import annotations

import unittest
from unittest import mock

from fastapi.testclient import TestClient


class PasswordHashingTests(unittest.TestCase):
    def test_new_hash_is_slow_and_salted(self):
        """新哈希必须是慢 KDF + 每密码独立盐。"""
        from api.auth import hash_password, verify_password

        first = hash_password("same-password")
        second = hash_password("same-password")
        self.assertNotEqual(first, second, "两次哈希相同 —— 没有盐，彩虹表直接命中")
        self.assertTrue(verify_password("same-password", first))
        self.assertTrue(verify_password("same-password", second))
        self.assertFalse(verify_password("wrong", first))

    def test_hash_is_not_bare_sha256(self):
        """不得再产出裸 SHA-256（审查发现的形态）。"""
        import hashlib

        from api.auth import hash_password

        hashed = hash_password("pw")
        bare = hashlib.sha256(b"pw").hexdigest()
        self.assertNotEqual(hashed, bare, "仍然是裸 SHA-256")

    def test_legacy_sha256_hashes_still_verify(self):
        """老库里的裸 SHA-256 哈希必须继续能登录，否则用户被锁在门外。"""
        import hashlib

        from api.auth import verify_password

        legacy = hashlib.sha256("old-password".encode()).hexdigest()
        self.assertTrue(verify_password("old-password", legacy))
        self.assertFalse(verify_password("wrong", legacy))

    def test_legacy_hash_is_upgraded_on_login(self):
        """老哈希验证通过后应被重写为新的慢哈希（透明迁移）。"""
        import hashlib

        from api.auth import needs_rehash

        legacy = hashlib.sha256(b"pw").hexdigest()
        self.assertTrue(needs_rehash(legacy), "老哈希没被标记为待升级")
        self.assertFalse(needs_rehash("pbkdf2_sha256$600000$c2FsdA==$aGFzaA=="))


class PendingTwoFactorPruningTests(unittest.TestCase):
    def test_expired_entries_are_pruned_on_use(self):
        """过期的临时令牌要被清掉，不能只增不减。"""
        from api import auth

        auth._pending_2fa.clear()
        try:
            now = auth.time.time()
            for i in range(50):
                auth._pending_2fa[f"expired-{i}"] = now - 10
            auth._pending_2fa["live"] = now + 300

            auth._prune_pending_2fa()

            self.assertEqual(list(auth._pending_2fa), ["live"],
                             f"过期条目没被清理: {len(auth._pending_2fa)} 条")
        finally:
            auth._pending_2fa.clear()

    def test_issuing_a_token_prunes_too(self):
        """发新令牌时就顺手清理，不依赖有人去调 prune。"""
        from api import auth

        auth._pending_2fa.clear()
        try:
            now = auth.time.time()
            for i in range(50):
                auth._pending_2fa[f"expired-{i}"] = now - 10
            auth._prune_pending_2fa()
            self.assertLess(len(auth._pending_2fa), 50)
        finally:
            auth._pending_2fa.clear()


class LoginThrottleTests(unittest.TestCase):
    """登录失败要有退避，否则可以全速爆破。"""

    def setUp(self):
        from api import auth

        auth._reset_login_failures()

    def test_repeated_failures_get_throttled(self):
        from api import auth

        for _ in range(5):
            auth._record_login_failure("1.2.3.4")
        self.assertGreater(auth._login_retry_after("1.2.3.4"), 0,
                           "连错 5 次仍不限流 —— 可以全速爆破")

    def test_success_clears_failures(self):
        from api import auth

        for _ in range(5):
            auth._record_login_failure("1.2.3.4")
        auth._clear_login_failures("1.2.3.4")
        self.assertEqual(auth._login_retry_after("1.2.3.4"), 0)

    def test_different_clients_are_independent(self):
        from api import auth

        for _ in range(5):
            auth._record_login_failure("1.2.3.4")
        self.assertEqual(auth._login_retry_after("5.6.7.8"), 0,
                         "一个 IP 的错误不该牵连另一个")


if __name__ == "__main__":
    unittest.main()


class _FakeCfg:
    """内存版 config_store —— 只实现 auth 用到的 get/set。"""

    def __init__(self, store: dict):
        self._store = store

    def get(self, key, default=None):
        return self._store.get(key, "" if default is None else default)

    def set(self, key, value):
        self._store[key] = value


class LoginEndpointThrottleTests(unittest.TestCase):
    """端到端：走真实 `/auth/login` 与 `/auth/verify-totp`。"""

    def setUp(self):
        from api import auth

        auth._reset_login_failures()
        self.store: dict = {}
        self._patch = mock.patch("api.auth._cfg", lambda: _FakeCfg(self.store))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.auth import hash_password, router

        app = FastAPI()
        app.include_router(router, prefix="/api")
        self.store["auth_password_hash"] = hash_password("right-password")
        return TestClient(app)

    def test_wrong_password_eventually_throttles(self):
        client = self._client()
        codes = [
            client.post("/api/auth/login", json={"password": "wrong"}).status_code
            for _ in range(8)
        ]
        self.assertIn(429, codes, f"连错 8 次仍然没有限流: {codes}")

    def test_correct_password_still_works_when_not_throttled(self):
        client = self._client()
        r = client.post("/api/auth/login", json={"password": "right-password"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("access_token", r.json())

    def test_legacy_hash_logs_in_and_gets_upgraded(self):
        """老的无盐哈希能登录，且登录后库里的哈希被换成新格式。"""
        import hashlib

        legacy = hashlib.sha256(b"old-pass").hexdigest()
        self.store["auth_password_hash"] = legacy

        from fastapi import FastAPI

        from api.auth import router

        app = FastAPI()
        app.include_router(router, prefix="/api")
        r = TestClient(app).post("/api/auth/login", json={"password": "old-pass"})

        self.assertEqual(r.status_code, 200, f"老哈希登录失败: {r.text}")
        self.assertTrue(
            self.store["auth_password_hash"].startswith("pbkdf2_sha256$"),
            "登录后哈希没有被升级 —— 弱哈希会一直留着",
        )


class TotpAttemptLimitTests(unittest.TestCase):
    """TOTP 失败要消费尝试次数，否则 6 位码可以无限试。"""

    def setUp(self):
        from api import auth

        auth._reset_login_failures()
        self.store: dict = {"auth_password_hash": auth.hash_password("pw")}
        self._patch = mock.patch("api.auth._cfg", lambda: _FakeCfg(self.store))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        from api import auth

        auth._reset_login_failures()

    def _app(self, secret: str):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from api.auth import router

        self.store["auth_totp_secret"] = secret
        app = FastAPI()
        app.include_router(router, prefix="/api")
        return TestClient(app)

    def test_wrong_codes_invalidate_the_temp_token(self):
        from api.auth import generate_totp_secret

        secret = generate_totp_secret()
        client = self._app(secret)
        temp = client.post("/api/auth/login", json={"password": "pw"}).json()["temp_token"]

        codes = [
            client.post(
                "/api/auth/verify-totp", json={"temp_token": temp, "code": "000000"}
            ).status_code
            for _ in range(7)
        ]
        # 前几次 400（码错），到上限后 401（令牌作废）
        self.assertIn(401, codes, f"错 7 次后令牌仍未作废: {codes}")

        after = client.post(
            "/api/auth/verify-totp", json={"temp_token": temp, "code": "000000"}
        )
        self.assertEqual(after.status_code, 401, "令牌作废后仍可继续试码")


# ── 2026-10-30 全量检测新增：setup / logout / 改密的接管面 ────────────────────


class _DictCfg:
    """最小配置存储替身：只要 get/set 两件事。"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, key, default=None):
        return self.data.get(key, "" if default is None else default)

    def set(self, key, value):
        self.data[key] = value


def _auth_app():
    """只挂 auth 路由的小应用（不动 main.py 的全局中间件）。"""
    from fastapi import FastAPI

    from api import auth

    app = FastAPI()
    app.include_router(auth.router, prefix="/api")
    return app


class SetupRouteTakeoverTests(unittest.TestCase):
    """`/api/auth/setup` 在鉴权中间件之外，必须自己把关（安全审查 F1 / F10）。

    原来的写法：没设密码 → 谁先到谁设，返回 token 直接接管；已设密码 →
    有 token 就能改密，不要旧密码。
    """

    def setUp(self):
        import os

        from api import auth

        self._auth = auth
        self._cfg = _DictCfg()
        self._patches = [
            mock.patch.object(auth, "_cfg", lambda: self._cfg),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patch in self._patches:
            patch.start()
        os.environ.pop("APP_JWT_SECRET", None)
        os.environ.pop("APP_ALLOW_REMOTE_SETUP", None)
        os.environ.pop("APP_TRUST_TEST_CLIENT", None)
        auth._reset_login_failures()

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()

    def _client(self):
        from fastapi.testclient import TestClient

        return TestClient(_auth_app())

    def test_first_setup_from_remote_is_refused(self):
        """没设密码时，非本机来源不能抢先设密码。"""
        r = self._client().post("/api/auth/setup", json={"password": "hunter2"})
        self.assertEqual(r.status_code, 403, f"远程可以抢注密码: {r.text}")
        self.assertEqual(self._cfg.get("auth_password_hash", ""), "")

    def test_first_setup_from_loopback_is_allowed(self):
        """本机（测试客户端 + 显式信任）可以设 —— 正常安装流程不能被挡。"""
        import os

        os.environ["APP_TRUST_TEST_CLIENT"] = "1"
        try:
            r = self._client().post("/api/auth/setup", json={"password": "hunter2"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json().get("access_token"))
            self.assertTrue(self._cfg.get("auth_password_hash", ""))
        finally:
            os.environ.pop("APP_TRUST_TEST_CLIENT", None)

    def test_changing_password_requires_the_old_one(self):
        """已设密码时，只有 token 不够 —— 必须给当前密码。"""
        import os

        os.environ["APP_TRUST_TEST_CLIENT"] = "1"
        try:
            client = self._client()
            first = client.post("/api/auth/setup", json={"password": "hunter2"})
            token = first.json()["access_token"]
            r = client.post(
                "/api/auth/setup",
                json={"password": "newpass1"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(r.status_code, 400, f"无旧密码即可改密: {r.text}")
            self.assertIn("当前密码", r.text)
        finally:
            os.environ.pop("APP_TRUST_TEST_CLIENT", None)

    def test_wrong_old_password_is_refused(self):
        import os

        os.environ["APP_TRUST_TEST_CLIENT"] = "1"
        try:
            client = self._client()
            token = client.post("/api/auth/setup", json={"password": "hunter2"}).json()["access_token"]
            r = client.post(
                "/api/auth/setup",
                json={"password": "newpass1", "old_password": "nope"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(r.status_code, 400)
            self.assertIn("当前密码错误", r.text)
        finally:
            os.environ.pop("APP_TRUST_TEST_CLIENT", None)

    def test_password_change_revokes_existing_tokens(self):
        """改密要真的吊销旧令牌（换密钥），否则旧 token 还能用 7 天。"""
        import os

        os.environ["APP_TRUST_TEST_CLIENT"] = "1"
        try:
            client = self._client()
            old_token = client.post("/api/auth/setup", json={"password": "hunter2"}).json()["access_token"]
            r = client.post(
                "/api/auth/setup",
                json={"password": "newpass1", "old_password": "hunter2"},
                headers={"Authorization": f"Bearer {old_token}"},
            )
            self.assertEqual(r.status_code, 200, r.text)
            with self.assertRaises(Exception):
                self._auth.verify_token(old_token)
            self._auth.verify_token(r.json()["access_token"])  # 新令牌可用
        finally:
            os.environ.pop("APP_TRUST_TEST_CLIENT", None)


class LogoutRevocationTests(unittest.TestCase):
    def setUp(self):
        import os

        from api import auth

        self._auth = auth
        self._cfg = _DictCfg()
        self._patches = [
            mock.patch.object(auth, "_cfg", lambda: self._cfg),
            mock.patch.dict(os.environ, {}, clear=False),
        ]
        for patch in self._patches:
            patch.start()
        os.environ.pop("APP_JWT_SECRET", None)

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()

    def test_logout_invalidates_the_token(self):
        from fastapi.testclient import TestClient

        token = self._auth.create_token()
        client = TestClient(_auth_app())
        r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("revoked"), "logout 没有真正吊销")
        with self.assertRaises(Exception):
            self._auth.verify_token(token)

    def test_env_secret_reports_cannot_revoke(self):
        """密钥由环境变量管时，如实返回 revoked=False，不假装成功。"""
        import os

        from fastapi.testclient import TestClient

        os.environ["APP_JWT_SECRET"] = "fixed-by-deployer"
        try:
            token = self._auth.create_token()
            client = TestClient(_auth_app())
            # logout 现在要求已认证（否则任何人一个 POST 就能把全员踢下线）
            r = client.post("/api/auth/logout", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertFalse(r.json().get("revoked"))
        finally:
            os.environ.pop("APP_JWT_SECRET", None)

    def test_logout_without_token_is_rejected(self):
        """未认证的 logout 不能触发全局密钥轮换（api-security N1）。"""
        from fastapi.testclient import TestClient

        client = TestClient(_auth_app())
        before = self._cfg.get("auth_jwt_secret", "")
        r = client.post("/api/auth/logout")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self._cfg.get("auth_jwt_secret", ""), before)


class ForwardedForSpoofingTests(unittest.TestCase):
    """`X-Forwarded-For` 默认不可信（安全审查 F7）。"""

    def _request(self, forwarded: str):
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/auth/login",
            "headers": [(b"x-forwarded-for", forwarded.encode())],
            "client": ("10.0.0.9", 12345),
            "server": ("127.0.0.1", 8000),
            "scheme": "http",
            "query_string": b"",
        }
        return Request(scope)

    def test_forwarded_for_is_ignored_by_default(self):
        import os

        from api.auth import _client_key

        os.environ.pop("APP_TRUST_PROXY", None)
        self.assertEqual(_client_key(self._request("1.2.3.4")), "10.0.0.9")
        # 伪造不同的头，key 不变 → 退避计数不会被绕过
        self.assertEqual(_client_key(self._request("9.9.9.9")), "10.0.0.9")

    def test_forwarded_for_is_used_when_proxy_is_trusted(self):
        import os

        from api.auth import _client_key

        os.environ["APP_TRUST_PROXY"] = "1"
        try:
            self.assertEqual(_client_key(self._request("1.2.3.4, 10.0.0.1")), "1.2.3.4")
        finally:
            os.environ.pop("APP_TRUST_PROXY", None)
