"""Grok 只保留 SSO + grok2api，删掉 CPA / Sub2API 双产物结构。

用户要求（2026-10-27）：「Grok 数据结构优化调整，删除 AT/RT 这种（不再支持
CPA，只支持 grok2api）」。

## 为什么可以删

Grok 的 `cpa_record`（含 AT/RT/expired）只服务两个上传动作 —— `upload_cpa` 与
`upload_sub2api`。而 grok2api 的主凭证是 **Web SSO**（`_TOKEN_COLUMN_FIELDS`
里 grok 的 token 列就是 `sso`）。

## 续期不会断

`platforms/grok/token_refresh.py` 有**两条**刷新路径：
- `refresh_via_grant`：RT → 新 token（要 RT）
- `refresh_via_device_flow`：**SSO → 登录协议**重新拿 token（不需要 RT）

删掉 RT 后走第二条，SSO 过期仍有续期路径。这是删除安全的前提。

## 存量清理

写一次性迁移：删 `extra.cpa_record` / `access_token` / `refresh_token` /
`id_token`，保留 `sso`。不清理的话导出/备份里会一直带着无用凭据。
"""

from __future__ import annotations

import unittest


class GrokDoesNotExposeCpaActionsTests(unittest.TestCase):
    def _ids(self):
        """走生产访问器（`get_platform_actions` 是实例方法）。"""
        from platforms.grok.plugin import GrokPlatform

        return [a["id"] for a in GrokPlatform(config=None).get_platform_actions()]

    def test_upload_cpa_is_gone(self):
        ids = self._ids()
        self.assertNotIn("upload_cpa", ids, "Grok 仍暴露 upload_cpa")
        self.assertNotIn("upload_sub2api", ids, "Grok 仍暴露 upload_sub2api")

    def test_grok2api_actions_survive(self):
        ids = self._ids()
        self.assertIn("upload_grok2api", ids, "grok2api 上传被误删")
        self.assertIn("sync_grok2api_status", ids, "grok2api 状态同步被误删")


class CpaRegistryNoLongerClaimsGrokTests(unittest.TestCase):
    def test_cpa_panel_platforms_exclude_grok(self):
        from services.panel_registry import PANELS

        cpa = next(p for p in PANELS if p["key"] == "cpa")
        self.assertNotIn("grok", cpa.get("platforms") or [],
                         "CPA 面板仍声称托管 grok —— 对比页会列出无法上传的 Grok 行")
        self.assertEqual(list((cpa.get("upload_actions") or {}).keys()), ["chatgpt"])
        self.assertEqual(list((cpa.get("sync_actions") or {}).keys()), ["chatgpt"])
        self.assertEqual(list((cpa.get("providers") or {}).keys()), ["chatgpt"])


class CpaRecordIsNoLongerProducedTests(unittest.TestCase):
    """刷新 Token 不再往 extra 写 `cpa_record`。"""

    def test_no_cpa_record_writes_remain(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "platforms" / "grok"
        offenders = []
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                # 找「写」而不是「读」：`patch["cpa_record"] = ...`
                if re.search(r'\[\s*"cpa_record"\s*\]\s*=', line):
                    offenders.append(f"{path.name}:{lineno}")
        self.assertEqual(offenders, [], f"仍在写 cpa_record: {offenders}")

    def test_token_to_cpa_record_is_gone(self):
        from platforms.grok import oauth_device

        self.assertFalse(
            hasattr(oauth_device, "token_to_cpa_record"),
            "`token_to_cpa_record` 仍在 —— 它是 CPA 扁平记录的构造器，应已删除",
        )


class SsoRefreshPathSurvivesTests(unittest.TestCase):
    """删 RT 之后 SSO 续期路径必须还在（否则过期就废号）。"""

    def test_device_flow_refresh_exists(self):
        from platforms.grok import token_refresh

        self.assertTrue(
            hasattr(token_refresh, "refresh_via_device_flow"),
            "SSO 续期路径不见了 —— 删掉 RT 后就没有任何续期手段",
        )

    def test_requires_sso_not_rt(self):
        from platforms.grok.token_refresh import refresh_via_device_flow

        result = refresh_via_device_flow("")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "missing_sso")


class MigrationRemovesDeadFieldsTests(unittest.TestCase):
    def test_cleanup_drops_cpa_fields_keeps_sso(self):
        from platforms.grok.migrate_sso_only import clean_grok_extra

        extra = {
            "sso": "sso-value",
            "cpa_record": {"access_token": "AT", "refresh_token": "RT"},
            "access_token": "AT",
            "refresh_token": "RT",
            "id_token": "IDT",
            "totp_secret": "TOTP",
            "sync_statuses": {"grok2api": {"uploaded": True}},
        }
        cleaned, changed = clean_grok_extra(extra)

        self.assertTrue(changed)
        self.assertEqual(cleaned.get("sso"), "sso-value", "SSO 被误删")
        for dead in ("cpa_record", "access_token", "refresh_token", "id_token"):
            self.assertNotIn(dead, cleaned, f"{dead} 没被清理")
        # 无关字段必须保留
        self.assertEqual(cleaned.get("totp_secret"), "TOTP")
        self.assertIn("sync_statuses", cleaned)

    def test_idempotent(self):
        from platforms.grok.migrate_sso_only import clean_grok_extra

        cleaned = {"sso": "x", "totp_secret": "T"}
        again, changed = clean_grok_extra(dict(cleaned))
        self.assertFalse(changed, "第二次运行还报告变更 —— 迁移不幂等")
        self.assertEqual(again, cleaned)


class RegistrationStillProducesSsoOnlyTests(unittest.TestCase):
    """注册只产 Web SSO（grok2api 要的那一段）。

    这几条从已删的 `test_grok_register_web_sso_only.py` 移植 —— 那个文件
    整体在测 CPA 落盘/上传（功能已删），但「注册默认不换 OAuth、账号上没有
    build 凭据、grok2api 接入照旧」这三条是**仍然有效**的契约，必须留住。
    """

    def _platform(self, extra_overrides: dict):
        from unittest.mock import patch

        from core.base_platform import RegisterConfig
        from platforms.grok.plugin import GrokPlatform

        plat = GrokPlatform(config=RegisterConfig(extra=dict(extra_overrides)))
        plat._log_fn = lambda _m: None
        return plat

    def test_registration_default_does_not_exchange_oauth(self):
        """默认不换 OAuth（`grok_oauth_exchange` 缺省关闭）。

        这是**声明层**的契约：设置里那个开关默认关。实际换不换由
        `_register_via_browser` 读它决定，这里只钉住默认值 ——
        完整链路测试在 `test_grok_send_code_and_grok2api.py`。
        """
        from platforms.grok.plugin import GrokPlatform

        modes = {m["key"]: m for m in GrokPlatform.registration_modes}
        self.assertIn("grok_oauth_exchange", modes, "开关没声明")
        # 默认值必须是「关」（falsy 的字符串/布尔都算关）
        default = str(modes["grok_oauth_exchange"].get("default", "")).strip().lower()
        self.assertIn(default, {"", "0", "false", "no", "off"},
                      f"grok_oauth_exchange 默认值是 {default!r} —— 应当默认关闭")

    def test_cpa_record_is_not_part_of_the_account_shape(self):
        """账号 extra 里不该再有 build 凭据的容器字段。"""
        from platforms.grok.migrate_sso_only import DEAD_EXTRA_KEYS

        self.assertIn("cpa_record", DEAD_EXTRA_KEYS)
        self.assertIn("access_token", DEAD_EXTRA_KEYS)


if __name__ == "__main__":
    unittest.main()
