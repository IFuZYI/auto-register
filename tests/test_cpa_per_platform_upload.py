"""CPA 自动上传按平台分开开关 + 自动维护已删除。

用户要求：
1. 「CPA 面板里的自动上传不同平台要分开开启」—— ChatGPT 与 Grok 各有独立开关；
2. 「CPA 自动维护」整块删除。

本文件钉住这两件事，防止回退。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CpaPerPlatformUploadToggleTests(unittest.TestCase):
    def test_platform_specific_keys_exist_in_config_whitelist(self):
        from api.config import CONFIG_KEYS

        self.assertIn("cpa_upload_chatgpt_enabled", CONFIG_KEYS)
        self.assertIn("cpa_upload_grok_enabled", CONFIG_KEYS)

    def test_resolver_prefers_the_platform_key(self):
        """平台键优先于历史单开关 —— 否则分不开。"""
        from services.external_sync import cpa_upload_enabled_for

        class _Cfg:
            def __init__(self, values):
                self._v = values

            def get(self, key, default=""):
                return self._v.get(key, default)

        # ChatGPT 开、Grok 关：这正是「分开」的核心场景
        cfg = _Cfg({
            "cpa_upload_chatgpt_enabled": "1",
            "cpa_upload_grok_enabled": "0",
            "cpa_enabled": "1",          # 老开关开着也不该把 Grok 带开
            "cpa_api_url": "http://x",
        })
        self.assertTrue(cpa_upload_enabled_for("chatgpt", cfg))
        self.assertFalse(cpa_upload_enabled_for("grok", cfg))

        # 反过来
        cfg2 = _Cfg({
            "cpa_upload_chatgpt_enabled": "0",
            "cpa_upload_grok_enabled": "1",
            "cpa_enabled": "1",
            "cpa_api_url": "http://x",
        })
        self.assertFalse(cpa_upload_enabled_for("chatgpt", cfg2))
        self.assertTrue(cpa_upload_enabled_for("grok", cfg2))

    def test_legacy_single_switch_still_applies_when_platform_keys_absent(self):
        """老配置只有 `cpa_enabled=true` 时，两边都要传 —— 不能静默失效。"""
        from services.external_sync import cpa_upload_enabled_for

        class _Cfg:
            def get(self, key, default=""):
                return {"cpa_enabled": "true", "cpa_api_url": "http://x"}.get(key, default)

        self.assertTrue(cpa_upload_enabled_for("chatgpt", _Cfg()))
        self.assertTrue(cpa_upload_enabled_for("grok", _Cfg()))

    def test_legacy_switch_off_keeps_both_off(self):
        from services.external_sync import cpa_upload_enabled_for

        class _Cfg:
            def get(self, key, default=""):
                return {"cpa_enabled": "false", "cpa_api_url": "http://x"}.get(key, default)

        self.assertFalse(cpa_upload_enabled_for("chatgpt", _Cfg()))
        self.assertFalse(cpa_upload_enabled_for("grok", _Cfg()))

    def test_url_alone_is_the_last_resort(self):
        """平台键与老开关都没设时，填了地址就传（与其它外部同步口径一致）。"""
        from services.external_sync import cpa_upload_enabled_for

        class _Cfg:
            def __init__(self, url):
                self._url = url

            def get(self, key, default=""):
                return self._url if key == "cpa_api_url" else default

        self.assertTrue(cpa_upload_enabled_for("grok", _Cfg("http://x")))
        self.assertFalse(cpa_upload_enabled_for("grok", _Cfg("")))

    def test_chatgpt_upload_path_uses_the_resolver(self):
        """自动上传入口必须走解析器，不能退回读单一 `cpa_enabled`。"""
        src = (ROOT / "services/external_sync.py").read_text(encoding="utf-8")
        self.assertIn('cpa_upload_enabled_for("chatgpt")', src)

    def test_grok_registration_auto_uploads_to_cpa(self):
        """Grok 注册链路要能自动上传到远端 CPA（此前只有写本地目录）。"""
        src = (ROOT / "platforms/grok/plugin.py").read_text(encoding="utf-8")
        self.assertIn('cpa_upload_enabled_for("grok")', src)
        self.assertIn("upload_to_cpa", src)


class CpaAutoMaintenanceRemovedTests(unittest.TestCase):
    def test_module_is_gone(self):
        self.assertFalse(
            (ROOT / "services/cpa_manager.py").exists(),
            "CPA 自动维护模块应已删除",
        )

    def test_config_keys_are_gone(self):
        from api.config import CONFIG_KEYS

        for key in (
            "cpa_cleanup_enabled",
            "cpa_cleanup_interval_minutes",
            "cpa_cleanup_threshold",
            "cpa_cleanup_concurrency",
            "cpa_cleanup_register_delay_seconds",
        ):
            self.assertNotIn(key, CONFIG_KEYS, f"{key} 应随自动维护一起删除")

    def test_nothing_imports_it(self):
        """全仓扫描：没有任何**源码**文件再引用已删除的 cpa_manager。

        评审指出旧版只检查 main.py（`for rel in ("main.py",)`），其它模块
        残留 import 不会变红，与测试名声称的范围不符。现在扫全仓 .py
        （排除 tests/ 自身、reference/、.worktrees/（本地工作树检出）、
        __pycache__、以及本文件）。
        """
        offenders = []
        for path in ROOT.rglob("*.py"):
            rel = path.relative_to(ROOT)
            parts = rel.parts
            if parts[0] in {"reference", "tests", ".worktrees"} or "__pycache__" in parts:
                continue
            try:
                src = path.read_text(encoding="utf-8")
            except Exception:
                continue
            if "cpa_manager" in src:
                offenders.append(str(rel))
        self.assertEqual(offenders, [], f"仍引用已删除的 cpa_manager: {offenders}")

    def test_ui_section_is_gone(self):
        src = (
            ROOT / "frontend/src/components/settings/PanelConfigPanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertNotIn("CPA 自动维护", src)
        self.assertNotIn("cpa_cleanup_enabled", src)

    def test_scheduler_extension_point_survives(self):
        """删的是那个任务，不是调度器的扩展点（新增业务任务还要靠它）。"""
        from core.scheduler import register_job, registered_jobs

        self.assertTrue(callable(register_job))
        self.assertIsInstance(registered_jobs(), list)


if __name__ == "__main__":
    unittest.main()
