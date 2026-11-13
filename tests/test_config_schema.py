"""配置键只有一处定义（`core/config_schema.py`）。

改造前三份名单互不校验：`api/config.py` 的 69 键白名单、同文件的 10 个口令键、
前端设置页的字段表。加一个键忘了加进白名单 → 前端能填、后端静默丢弃；把口令
键漏出 `SECRET_CONFIG_KEYS` → 明文从 `GET /api/config` 下发。

这里钉住三件事：派生关系、口令集合、以及「代码里读的键必须登记过」。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from core.config_schema import (
    BY_KEY,
    CONFIG_KEYS,
    CONFIG_SCHEMA,
    INTERNAL_KEYS,
    SECRET_CONFIG_KEYS,
    is_known,
    keys_in_group,
    schema_payload,
)

REPO = Path(__file__).resolve().parent.parent

#: 允许直接读、但不对外暴露的内部键（见 `core/config_schema.py`）。
_ALLOWED_UNDECLARED = set(INTERNAL_KEYS)


#: 不扫的目录：第三方参考实现、嵌套 worktree、虚拟环境、前端依赖。
#: `reference/` 里是别人的实现（`any-auto-register*`），它们读自己的配置键；
#: `.worktrees/` 是本仓库的其它 checkout —— 扫进来等于把同一份代码数两遍，
#: 还会因为 checkout 里多出来的实验文件误报。只查**本仓库第一方代码**。
_SKIP_DIRS = frozenset({"reference", ".worktrees", ".venv", "node_modules", "__pycache__", "tests"})


def _literal_config_reads() -> set[str]:
    """扫出代码里所有 `config_store.get("字面量")` 的键名。"""
    pattern = re.compile(r'config_store\.get\(\s*["\']([A-Za-z0-9_]+)["\']')
    found: set[str] = set()
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO)
        if set(rel.parts) & _SKIP_DIRS:
            continue
        found.update(pattern.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return found


class SchemaShapeTests(unittest.TestCase):
    def test_no_duplicate_keys(self):
        self.assertEqual(len(CONFIG_KEYS), len(set(CONFIG_KEYS)), "登记表里有重复键")

    def test_key_count(self):
        # 71 → 77（2026-10-30）：面板改版新增 6 个键 ——
        #   chatgpt2api_import_mode       导入内容（仅 AT/RT / 完全账户）
        #   sub2api_upload_proxy_enabled  上传代理
        #   grok2api_upload_proxy_enabled 上传代理
        #   cpa_auto_sync_enabled         面板与本地同步
        #   sub2api_auto_sync_enabled     面板与本地同步
        #   grok2api_auto_sync_enabled    面板与本地同步
        # 101 → 105（2026-11-13）：新增 aws / kiro 两个注册平台 ——
        #   aws_executor / kiro_executor      执行器（两平台都只有 protocol）
        #   aws_full_name / kiro_plan         插件 `registration_modes` 声明的旋钮
        # 105 → 110（同日）：AWS WAF 打码 ——
        #   capmonster_key / ai_captcha_url / ai_captcha_key /
        #   ai_captcha_model / captcha_provider_order
        # 110 → 130（同日）：AWS / Kiro 接入自动维护 —— 每平台 10 个维护键
        #   （auto_refresh_* / refresh_* / auto_probe_* / probe_*），
        #   **不含** sync_strategy：这两个平台没有可同步的面板。
        self.assertEqual(len(CONFIG_KEYS), 130, "键数变了要同步改文档")

    def test_every_key_has_a_group(self):
        for item in CONFIG_SCHEMA:
            self.assertTrue(item.group, f"{item.key} 没有分组")

    def test_secret_set_is_derived(self):
        expected = {item.key for item in CONFIG_SCHEMA if item.secret}
        self.assertEqual(set(SECRET_CONFIG_KEYS), expected)
        # 10 → 12：CapMonster key 与 AI 视觉 key 都是口令（GET 只回 `<key>_set`）
        self.assertEqual(len(SECRET_CONFIG_KEYS), 12)

    def test_keys_in_group_covers_everything(self):
        groups = {item.group for item in CONFIG_SCHEMA}
        collected = {key for group in groups for key in keys_in_group(group)}
        self.assertEqual(collected, set(CONFIG_KEYS))


class MaintenanceKeyTests(unittest.TestCase):
    """AWS / Kiro 的维护键：每平台 10 个，且**没有** sync_strategy。

    两个平台没有可同步的面板（`services.local_maintenance.PLATFORM_ACTIONS`
    只登记 probe / refresh）。配一个 `*_sync_strategy` 只会在设置页长出一个
    永远无效的下拉 —— 用户配了它也不会生效，比缺一个键更难发现。
    """

    #: 与 chatgpt / grok 对齐的十个键，顺序即界面顺序。
    PER_PLATFORM = (
        "auto_refresh_enabled",
        "refresh_scope",
        "refresh_random_count",
        "refresh_interval",
        "refresh_disable_banned",
        "auto_probe_enabled",
        "probe_mode",
        "probe_sample_percent",
        "probe_strategy",
        "probe_interval",
    )

    def test_aws_and_kiro_maintenance_keys_are_registered(self):
        for platform in ("aws", "kiro"):
            for suffix in self.PER_PLATFORM:
                key = f"{platform}_{suffix}"
                self.assertIn(key, CONFIG_KEYS, f"{key} 没登记 → PUT /api/config 会静默丢弃")
                self.assertEqual(BY_KEY[key].group, "maintenance", f"{key} 的分组不对")

    def test_aws_and_kiro_have_no_sync_strategy(self):
        """没有面板就没有「刷新策略」——多一个键就是多一个无效控件。"""
        for platform in ("aws", "kiro"):
            self.assertNotIn(f"{platform}_sync_strategy", CONFIG_KEYS)


class ApiDerivesFromSchemaTests(unittest.TestCase):
    def test_api_config_keys_match_schema_exactly(self):
        from api.config import CONFIG_KEYS as API_KEYS

        self.assertEqual(list(API_KEYS), list(CONFIG_KEYS), "顺序即界面顺序，不能变")

    def test_api_secret_keys_match_schema(self):
        from api.config import SECRET_CONFIG_KEYS as API_SECRETS

        self.assertEqual(API_SECRETS, SECRET_CONFIG_KEYS)


class ReadsAreRegisteredTests(unittest.TestCase):
    def test_every_literal_read_is_registered(self):
        """`config_store.get("typo_key")` 会静默返回默认值 —— 拼错永远不报错。"""
        unknown = {
            key
            for key in _literal_config_reads()
            if not is_known(key) and key not in _ALLOWED_UNDECLARED
        }
        self.assertEqual(
            unknown, set(), f"这些键被读取但没在 core/config_schema.py 登记：{sorted(unknown)}"
        )

    def test_schema_lookup_helpers(self):
        self.assertTrue(is_known("yescaptcha_key"))
        self.assertFalse(is_known("not_a_key"))
        self.assertEqual(BY_KEY["cpa_api_key"].group, "cpa")
        self.assertTrue(BY_KEY["cpa_api_key"].secret)


class SchemaPayloadTests(unittest.TestCase):
    def test_payload_has_no_values(self):
        payload = schema_payload()
        self.assertEqual(len(payload["keys"]), 130)
        self.assertEqual(sorted(payload["secrets"]), sorted(SECRET_CONFIG_KEYS))
        # 只允许这三个键：多一个就说明有人把值塞进来了
        self.assertEqual(set(payload), {"keys", "secrets", "groups"})
        for entries in payload["groups"].values():
            for entry in entries:
                self.assertEqual(set(entry), {"key", "secret"})


if __name__ == "__main__":
    unittest.main()
