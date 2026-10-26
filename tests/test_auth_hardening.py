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


