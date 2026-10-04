"""代理传递的回归测试。

背景（2026-09-30 实测，全部在本机验证过）：
  - Chromium **不支持带认证的 SOCKS5**：
      {"server": "socks5://user:pass@host:port"}  → net::ERR_NO_SUPPORTED_PROXIES
      {"server": "socks5://host:port", "username": ...} → 启动即失败
  - 同一端点改成 http:// + 认证 → 通（住宅代理实测能过 x.ai 的 CF）
  - solver 的兜底端口曾写死参考项目的 5072（本机没人监听）→ 每次 Connection refused
  - 代理池会在「0 成功 + 5 失败」后把代理停用，且没有复活入口

这些用例把上述行为钉死。
"""

import unittest
from unittest.mock import patch

from core.proxy_utils import (
    build_playwright_proxy_config,
    is_authenticated_socks5_proxy,
)


class PlaywrightProxyConfigTests(unittest.TestCase):
    def test_authenticated_socks5_downgrades_to_http(self):
        """带认证的 socks5 必须降级成 http —— 否则 Chromium 直接拒连。"""
        cfg = build_playwright_proxy_config(
            "socks5://user:pass@proxy.example:6740"
        )
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["server"], "http://proxy.example:6740")
        self.assertEqual(cfg["username"], "user")
        self.assertEqual(cfg["password"], "pass")
        self.assertNotIn("socks", cfg["server"], "server 里不能残留 socks 方案")

    def test_authenticated_socks5h_also_downgrades(self):
        """socks5h 是 socks5 的别名（远程 DNS），同样要降级。"""
        cfg = build_playwright_proxy_config("socks5h://u:p@host.example:1080")
        self.assertEqual(cfg["server"], "http://host.example:1080")
        self.assertEqual(cfg["username"], "u")
        self.assertEqual(cfg["password"], "p")

    def test_unauthenticated_socks5_kept_as_is(self):
        """不带认证的 socks5 Chromium 支持，不该无谓降级。"""
        cfg = build_playwright_proxy_config("socks5://host.example:1080")
        self.assertEqual(cfg["server"], "socks5://host.example:1080")
        self.assertNotIn("username", cfg)

    def test_plain_http_proxy_unchanged(self):
        cfg = build_playwright_proxy_config("http://user:pass@host.example:8080")
        self.assertEqual(cfg["server"], "http://host.example:8080")
        self.assertEqual(cfg["username"], "user")

    def test_legacy_colon_format_still_works(self):
        """`host:port:user:pass` 这种老格式（UI 里手填的）要照旧解析。"""
        cfg = build_playwright_proxy_config("proxy.example:2000:user-name:secret-pass")
        self.assertEqual(cfg["server"], "http://proxy.example:2000")
        self.assertEqual(cfg["username"], "user-name")
        self.assertEqual(cfg["password"], "secret-pass")

    def test_credentials_are_url_decoded(self):
        """口令里的特殊字符在 URL 里是百分号编码的，取出时要还原。"""
        cfg = build_playwright_proxy_config("socks5://u%40ser:p%3Aass@host.example:1080")
        self.assertEqual(cfg["username"], "u@ser")
        self.assertEqual(cfg["password"], "p:ass")

    def test_empty_returns_none(self):
        for bad in ("", None, "   "):
            self.assertIsNone(build_playwright_proxy_config(bad), repr(bad))


class AuthenticatedSocksDetectionTests(unittest.TestCase):
    """`is_authenticated_socks5_proxy` 是给调用方做前置判断用的。"""

    def test_detects_authenticated_socks5(self):
        self.assertTrue(is_authenticated_socks5_proxy("socks5://u:p@host:1080"))
        self.assertTrue(is_authenticated_socks5_proxy("socks5h://u:p@host:1080"))

    def test_plain_and_http_are_not_flagged(self):
        self.assertFalse(is_authenticated_socks5_proxy("socks5://host:1080"))
        self.assertFalse(is_authenticated_socks5_proxy("http://u:p@host:8080"))
        self.assertFalse(is_authenticated_socks5_proxy(""))


class SolverProxyTests(unittest.TestCase):
    """solver 侧的代理选择与启动开关。

    solver 的模块级 import 依赖它自己的目录（`from db_results import ...`），
    所以先把该目录放进 sys.path —— 与 solver 真实启动方式（cwd 即该目录）一致。
    """

    @classmethod
    def setUpClass(cls):
        import sys
        from pathlib import Path

        solver_dir = str(Path(__file__).resolve().parents[1] / "services" / "turnstile_solver")
        if solver_dir not in sys.path:
            sys.path.insert(0, solver_dir)

    def test_solver_proxy_env_wins(self):
        from services.turnstile_solver.api_solver import TurnstileAPIServer

        srv = TurnstileAPIServer.__new__(TurnstileAPIServer)
        with patch.dict("os.environ", {"SOLVER_PROXY": "http://env-proxy:8080"}, clear=False):
            self.assertEqual(srv._pick_proxy(), "http://env-proxy:8080")

    def test_solver_redacts_credentials(self):
        from services.turnstile_solver.api_solver import TurnstileAPIServer

        out = TurnstileAPIServer._redact("socks5://user:secret@1.2.3.4:6740")
        self.assertNotIn("secret", out)
        self.assertIn("1.2.3.4:6740", out)

    def test_manager_enables_proxy_by_default(self):
        from services import solver_manager

        with patch.dict("os.environ", {}, clear=True):
            self.assertTrue(solver_manager._solver_proxy_enabled())

    def test_manager_proxy_can_be_disabled(self):
        from services import solver_manager

        with patch.dict("os.environ", {"SOLVER_PROXY_ENABLED": "0"}, clear=False):
            self.assertFalse(solver_manager._solver_proxy_enabled())


class ProxyPoolReactivateTests(unittest.TestCase):
    """被自动停用的代理要能一键复活。"""

    def test_reactivate_all_turns_inactive_back_on(self):
        from core.db import ProxyModel
        from core.proxy_pool import ProxyPool

        pool = ProxyPool()
        # 造两个代理：一个被停用、一个本来就好
        with patch.object(pool, "_find_by_url", return_value=None):
            pass

        calls = {"saved": []}

        class _FakeSession:
            def exec(self, *_a, **_k):
                class _R:
                    def all(self_inner):
                        dead = ProxyModel(url="socks5://dead:1", is_active=False, fail_count=6)
                        alive = ProxyModel(url="http://alive:2", is_active=True, fail_count=0)
                        return [dead, alive]

                return _R()

            def add(self, obj):
                calls["saved"].append(obj)

            def commit(self):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with patch("core.proxy_pool.Session", return_value=_FakeSession()):
            n = pool.reactivate_all()

        self.assertEqual(n, 1, "只该复活被停用的那一条")
        revived = [o for o in calls["saved"] if o.url == "socks5://dead:1"]
        self.assertTrue(revived, "被停用的代理应被写回")
        self.assertTrue(revived[0].is_active)
        self.assertEqual(revived[0].fail_count, 0, "复活时失败计数要清零")


if __name__ == "__main__":
    unittest.main()
