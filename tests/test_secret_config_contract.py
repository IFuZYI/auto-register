"""口令「输入后不可查看」的前后端契约。

服务端在 `GET /api/config` 打码（见 `tests/test_config_secret_masking.py`），
前端必须配套做两件事，否则用户口令会被静默清空：

1. 提交前摘掉 `<key>_set` 只读标记 —— 它不是配置项；
2. 口令留空 = 不修改，空值不能提交。

任一处漏掉，页面保存一次就把口令抹掉，而用户要等下次注册失败才发现。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SecretConfigContractTests(unittest.TestCase):
    def test_shared_helpers_exist(self):
        src = (ROOT / "frontend/src/lib/secretConfig.ts").read_text(encoding="utf-8")
        self.assertIn("export function stripSecretSetFlags", src)
        self.assertIn("export function dropEmptySecrets", src)
        self.assertIn("export function secretSetKeysFromConfig", src)

    def test_drop_empty_secrets_mutates_in_place(self):
        """`dropEmptySecrets` 必须原地生效。

        踩过：先写成「返回新对象」，但三个调用点都是 `dropEmptySecrets(x, keys)`
        然后继续用 `x` —— 那行等于没生效，空口令照样被提交（当时靠服务端兜底
        才没丢数据）。契约测试钉住实现方式，避免有人再「顺手改成不可变」。
        """
        src = (ROOT / "frontend/src/lib/secretConfig.ts").read_text(encoding="utf-8")
        # 函数体里不能出现「拷一层再返回」的写法
        self.assertNotIn("{ ...payload }", src,
                         "dropEmptySecrets 又改回不可变了 —— 调用点会静默失效")
        self.assertIn("delete payload[key]", src)

    def test_set_flags_reach_the_secret_fields(self):
        """三个页面都要把 `<key>_set` 传给 ConfigSection，提示才显示得出来。"""
        for rel in (
            "frontend/src/pages/Settings.tsx",
            "frontend/src/components/settings/PanelConfigPanel.tsx",
            "frontend/src/components/mail/MailServicePage.tsx",
        ):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("secretSetKeysFromConfig", src, f"{rel} 没取 _set 标记")
            self.assertIn("secretSetKeys={secretSetKeys}", src, f"{rel} 没把标记传给 ConfigSection")

    def test_every_config_writer_strips_set_flags(self):
        """三个写 `/api/config` 的页面都要摘掉只读标记。"""
        for rel in (
            "frontend/src/pages/Settings.tsx",
            "frontend/src/components/settings/PanelConfigPanel.tsx",
            "frontend/src/components/mail/MailServicePage.tsx",
        ):
            src = (ROOT / rel).read_text(encoding="utf-8")
            self.assertIn("stripSecretSetFlags", src, f"{rel} 没摘 _set 标记")
            self.assertIn("dropEmptySecrets", src, f"{rel} 没拦空口令")

    def test_panel_config_uses_set_flags_not_plaintext(self):
        """面板「已配置」判据必须用 `_set`，不能读口令明文（那永远是空串）。"""
        src = (ROOT / "frontend/src/components/settings/PanelConfigPanel.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("cpa_api_key_set", src)
        self.assertIn("sub2api_api_key_set", src)
        self.assertIn("grok2api_password_set", src)
        # 旧的明文判据不该再出现
        self.assertNotIn("String(config.cpa_api_key ?? '')", src)
        self.assertNotIn("String(config.grok2api_password ?? '')", src)

    def test_secret_field_shows_configured_hint(self):
        """口令输入框要提示「已配置」，否则用户无法判断库里有没有。"""
        src = (ROOT / "frontend/src/components/settings/ConfigPanels.tsx").read_text(
            encoding="utf-8"
        )
        self.assertIn("_set", src)
        self.assertIn("已配置", src)

    def test_backend_secret_registry_covers_the_panel_secrets(self):
        """服务端口令清单必须覆盖注册表声明的 secret_key（漏一个就明文外泄）。"""
        from api.config import CONFIG_KEYS, SECRET_CONFIG_KEYS
        from services.panel_registry import PANELS

        for panel in PANELS:
            secret_key = str(panel.get("secret_key") or "").strip()
            if not secret_key:
                continue
            self.assertIn(
                secret_key,
                SECRET_CONFIG_KEYS,
                f"面板 {panel['key']} 的口令 {secret_key} 不在打码清单里",
            )
            self.assertIn(secret_key, CONFIG_KEYS, f"{secret_key} 不在配置白名单里")


if __name__ == "__main__":
    unittest.main()
