"""`Grok2ApiClient.from_config` 的实例复用。

回归：登录 token 缓存在实例上，而 `from_config` 原本每次 new 一个 ——
缓存永远命不中，每次调用都重新登录（实测 442ms/477ms，复用后第二次 0.01ms）。
面板对比每次刷新都走这条路径。
"""

from __future__ import annotations

import unittest
from contextlib import contextmanager
from unittest import mock

from platforms.grok import grok2api
from platforms.grok.grok2api import Grok2ApiClient, _CLIENT_CACHE

_CFG = {
    "grok2api_base_url": "http://g2a.test",
    "grok2api_username": "admin",
    "grok2api_password": "pw",
}


@contextmanager
def _config_store(values: dict):
    with mock.patch("core.config_store.config_store.get", side_effect=lambda k, d="": values.get(k, d)):
        yield


class ClientReuseTests(unittest.TestCase):
    def setUp(self):
        _CLIENT_CACHE.clear()

    def tearDown(self):
        _CLIENT_CACHE.clear()

    def test_same_config_returns_the_same_instance(self):
        """同一份配置必须复用实例 —— 否则实例上的 token 缓存永远命中不了。"""
        with _config_store(_CFG):
            a = Grok2ApiClient.from_config()
            b = Grok2ApiClient.from_config()
        self.assertIs(a, b)

    def test_reuse_means_the_second_login_hits_the_cache(self):
        """复用实例时第二次 login 不该再发网络请求。"""
        with _config_store(_CFG):
            client = Grok2ApiClient.from_config()
            with mock.patch.object(client, "_request") as req:
                req.return_value = mock.Mock(
                    status_code=200,
                    json=lambda: {"data": {"tokens": {"accessToken": "tok-1"}}},
                )
                client.login()          # 第一次：真登录
                first_calls = req.call_count
                client.login()          # 第二次：应命中实例缓存
                second_calls = req.call_count
        self.assertEqual(first_calls, 1)
        self.assertEqual(second_calls, 1, "第二次 login 不该再发请求（token 缓存应命中）")

    def test_different_url_gets_a_different_instance(self):
        with _config_store(_CFG):
            a = Grok2ApiClient.from_config(api_url="http://one.test")
            b = Grok2ApiClient.from_config(api_url="http://two.test")
        self.assertIsNot(a, b)

    def test_different_proxy_gets_a_different_instance(self):
        with _config_store(_CFG):
            a = Grok2ApiClient.from_config()
            b = Grok2ApiClient.from_config(proxy="http://proxy.test")
        self.assertIsNot(a, b)

    def test_changed_password_does_not_reuse_the_stale_instance(self):
        """改密码后必须换新实例，不能拿着旧凭据的 token 继续用。"""
        with _config_store(_CFG):
            a = Grok2ApiClient.from_config()
        changed = dict(_CFG, grok2api_password="new-pw")
        with _config_store(changed):
            b = Grok2ApiClient.from_config()
        self.assertIsNot(a, b)
        self.assertEqual(b.password, "new-pw")

    def test_trailing_slash_does_not_split_the_cache(self):
        """`http://x` 与 `http://x/` 是同一个服务，不该各缓存一份。"""
        with _config_store(_CFG):
            a = Grok2ApiClient.from_config(api_url="http://g2a.test")
            b = Grok2ApiClient.from_config(api_url="http://g2a.test/")
        self.assertIs(a, b)

    def test_cache_is_module_level_not_per_call(self):
        with _config_store(_CFG):
            Grok2ApiClient.from_config()
        self.assertEqual(len(_CLIENT_CACHE), 1)


if __name__ == "__main__":
    unittest.main()
