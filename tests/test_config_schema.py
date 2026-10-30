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


def _literal_config_reads() -> set[str]:
    """扫出代码里所有 `config_store.get("字面量")` 的键名。"""
    pattern = re.compile(r'config_store\.get\(\s*["\']([A-Za-z0-9_]+)["\']')
    found: set[str] = set()
    for path in REPO.rglob("*.py"):
        text = str(path)
        if "__pycache__" in text or "/tests/" in text or text.startswith(str(REPO / "tests")):
            continue
        found.update(pattern.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return found


class SchemaShapeTests(unittest.TestCase):
    def test_no_duplicate_keys(self):
        self.assertEqual(len(CONFIG_KEYS), len(set(CONFIG_KEYS)), "登记表里有重复键")

    def test_key_count(self):
        self.assertEqual(len(CONFIG_KEYS), 69, "键数变了要同步改文档")

    def test_every_key_has_a_group(self):
        for item in CONFIG_SCHEMA:
            self.assertTrue(item.group, f"{item.key} 没有分组")

    def test_secret_set_is_derived(self):
        expected = {item.key for item in CONFIG_SCHEMA if item.secret}
        self.assertEqual(set(SECRET_CONFIG_KEYS), expected)
        self.assertEqual(len(SECRET_CONFIG_KEYS), 10)

    def test_keys_in_group_covers_everything(self):
        groups = {item.group for item in CONFIG_SCHEMA}
        collected = {key for group in groups for key in keys_in_group(group)}
        self.assertEqual(collected, set(CONFIG_KEYS))


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
        self.assertEqual(len(payload["keys"]), 69)
        self.assertEqual(sorted(payload["secrets"]), sorted(SECRET_CONFIG_KEYS))
        # 只允许这三个键：多一个就说明有人把值塞进来了
        self.assertEqual(set(payload), {"keys", "secrets", "groups"})
        for entries in payload["groups"].values():
            for entry in entries:
                self.assertEqual(set(entry), {"key", "secret"})


if __name__ == "__main__":
    unittest.main()
