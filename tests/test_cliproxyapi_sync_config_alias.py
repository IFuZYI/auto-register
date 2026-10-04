"""CLIProxyAPI 同步读配置时必须有别名兜底。

回归（实测踩到）：`cliproxyapi_sync` 读的是**别名键** `cliproxyapi_base_url`，
而用户在「面板配置」里填的是**规范键** `cpa_api_url`。
`config_store.get_all()` 会把别名折叠进规范键，但**单键 `get()` 不做折叠** ——
于是这个模块读不到用户填的地址，回落到 `http://127.0.0.1:8317` 默认值。

症状：面板对比能连通（它读规范键），「同步 CLIProxyAPI 状态」却报
「CLIProxyAPI 无法连接」—— 同一个服务两个结果，排查方向被带偏。
"""

from __future__ import annotations

import unittest
from unittest import mock

from services import cliproxyapi_sync


class ConfigAliasFallbackTests(unittest.TestCase):
    def _with_config(self, values: dict):
        """把 config_store.get 换成一个只看 values 的替身。"""
        def fake_get(key, default=""):
            return values.get(key, default)

        return mock.patch(
            "core.config_store.config_store.get",
            side_effect=fake_get,
        )

    def test_canonical_key_is_read(self):
        """面板配置里填的规范键要能读到。"""
        with self._with_config({"cpa_api_url": "https://cpa.example.com"}):
            self.assertEqual(
                cliproxyapi_sync._base_url(), "https://cpa.example.com"
            )

    def test_legacy_alias_still_works(self):
        """老数据只填了别名键时也要能读到（读侧迁移兜底）。"""
        with self._with_config({"cliproxyapi_base_url": "https://legacy.example.com"}):
            self.assertEqual(
                cliproxyapi_sync._base_url(), "https://legacy.example.com"
            )

    def test_canonical_wins_over_alias(self):
        """两个键都有值时以规范键为准。"""
        with self._with_config({
            "cpa_api_url": "https://canonical.example.com",
            "cliproxyapi_base_url": "https://legacy.example.com",
        }):
            self.assertEqual(
                cliproxyapi_sync._base_url(), "https://canonical.example.com"
            )

    def test_falls_back_to_the_builtin_default(self):
        """两个键都没有 → 内置默认（保持原行为）。"""
        with self._with_config({}):
            self.assertEqual(
                cliproxyapi_sync._base_url(), cliproxyapi_sync.DEFAULT_CLIPROXYAPI_BASE_URL
            )

    def test_explicit_argument_still_wins(self):
        """显式传的地址优先级最高（动作的 params 走这条）。"""
        with self._with_config({"cpa_api_url": "https://from-config.example.com"}):
            self.assertEqual(
                cliproxyapi_sync._base_url("https://explicit.example.com"),
                "https://explicit.example.com",
            )

    def test_api_key_reads_canonical_key(self):
        with self._with_config({"cpa_api_key": "secret-from-canonical"}):
            self.assertEqual(cliproxyapi_sync._api_key(), "secret-from-canonical")

    def test_api_key_reads_legacy_alias(self):
        with self._with_config({"cliproxyapi_management_key": "secret-from-legacy"}):
            self.assertEqual(cliproxyapi_sync._api_key(), "secret-from-legacy")


class SyncUsesTheConfiguredUrlTests(unittest.TestCase):
    """同步调用要打到配置的地址上（而不是本机默认端口）。"""

    def test_list_auth_files_hits_the_configured_base(self):
        captured: dict = {}

        def fake_request(method, url, **kwargs):
            captured["url"] = url
            raise RuntimeError("stop here")  # 只看地址，不真发请求

        with mock.patch(
            "core.config_store.config_store.get",
            side_effect=lambda key, default="": {
                "cpa_api_url": "https://cpa.example.com",
            }.get(key, default),
        ), mock.patch("requests.request", side_effect=fake_request):
            try:
                cliproxyapi_sync.list_auth_files()
            except Exception:
                pass

        self.assertIn(
            "cpa.example.com", str(captured.get("url")),
            "同步请求打到了默认地址 —— 说明没读到用户配置的 cpa_api_url",
        )


if __name__ == "__main__":
    unittest.main()
