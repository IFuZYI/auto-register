"""iCloud 别名失败计数必须「会清零」且「不把上游故障算在别名头上」。

两个真实缺陷（审计 2026-11-14）：

1. **只增不减**：`mark_failed(reset=True)` 的 docstring 写着「领取成功时清零」，
   但全仓没有任何生产调用点。于是一个别名只要累计失败 3 次，
   `find_candidates` 就永久跳过它 —— 哪怕之后每次都能正常领到。

2. **上游故障也算失败**：`release_alias_claim` 对任何 `failed` 都累加计数，
   而「iCloud 网关超时 / 会话过期」与「这个地址被 OpenAI 标记」是两件
   完全不同的事。前者是基础设施抖动，把它记到别名头上会让整池好号
   在几分钟内被集体判死 —— 而 Apple 每小时只给 5 个新地址，池子是稀缺资源。

判据用 `platforms.icloud.errors.is_upstream_outage`（已有函数，不是新造的语义）。
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from platforms.icloud.errors import upstream_unavailable


def _nullcontext():
    from contextlib import nullcontext

    return nullcontext()


class AliasesResetOnSuccessTests(unittest.TestCase):
    """领取成功要把失败计数清零 —— 否则好号会被永久跳过。"""

    def test_claiming_an_alias_resets_its_failure_count(self):
        from types import SimpleNamespace

        from services.icloud import aliases

        writes: list[dict] = []
        candidate = {
            "id": 1, "address": "a@icloud.com", "pool_status": "available",
            "used_platforms": "", "consecutive_failures": 2,
        }

        class _Repo:
            @staticmethod
            def find_candidates(**kwargs):
                return [dict(candidate)]

            @staticmethod
            def get(alias_id):
                return dict(candidate)

            @staticmethod
            def set_fields(alias_id, **fields):
                writes.append(fields)

            @staticmethod
            def claim_available(alias_id):
                # 抢号现在是条件更新（跨进程唯一），fake 必须实现这条契约。
                # 返回 True = 本次抢到；返回 False 时 claim_alias 会试下一个候选。
                return True

            @staticmethod
            def mark_failed(alias_id, *, reset=False):
                writes.append({"consecutive_failures": 0 if reset else 3})
                return 0 if reset else 3

        # `claim_alias` 还依赖主号记录、跨库已注册平台查询与账号锁。
        fake_facade = SimpleNamespace(
            _account_lock=lambda account_id: _nullcontext(),
            _registered_platforms_for=lambda addresses: {},
        )

        with patch("core.db.icloud_alias_repository.icloud_alias_repository", _Repo), \
             patch.object(aliases, "get_account",
                          lambda account_id: SimpleNamespace(email="main@icloud.com", enabled=True)), \
             patch.object(aliases, "_facade", fake_facade):
            aliases.claim_alias(account_id=1, platform="chatgpt")

        self.assertTrue(
            any(fields.get("consecutive_failures") == 0 for fields in writes),
            "领取成功没有把 consecutive_failures 清零 —— "
            "累计 3 次后这个号会被 find_candidates 永久跳过",
        )


class UpstreamOutageNotCountedAsAliasFailureTests(unittest.TestCase):
    """上游故障是基础设施问题，不该把别名计入失败。"""

    def test_release_after_upstream_outage_does_not_increment_failures(self):
        from services.icloud import aliases

        calls = {"mark_failed": 0}

        class _Repo:
            @staticmethod
            def get(alias_id):
                return {"id": 2, "address": "b@icloud.com", "pool_status": "in_use",
                        "consecutive_failures": 0}

            @staticmethod
            def set_fields(alias_id, **fields):
                return None

            @staticmethod
            def mark_failed(alias_id, *, reset=False):
                calls["mark_failed"] += 1
                return 1

        with patch("core.db.icloud_alias_repository.icloud_alias_repository", _Repo):
            aliases.release_alias_claim(2, error=upstream_unavailable("网关 503"))

        self.assertEqual(
            calls["mark_failed"], 0,
            "上游故障被记成了别名失败 —— 一次抖动就会批量判死好号",
        )

    def test_release_after_a_real_failure_still_counts(self):
        from services.icloud import aliases

        calls = {"mark_failed": 0}

        class _Repo:
            @staticmethod
            def get(alias_id):
                return {"id": 3, "address": "c@icloud.com", "pool_status": "in_use",
                        "consecutive_failures": 0}

            @staticmethod
            def set_fields(alias_id, **fields):
                return None

            @staticmethod
            def mark_failed(alias_id, *, reset=False):
                calls["mark_failed"] += 1
                return 1

        with patch("core.db.icloud_alias_repository.icloud_alias_repository", _Repo):
            aliases.release_alias_claim(3, error=RuntimeError("地址已被使用"))

        self.assertEqual(
            calls["mark_failed"], 1,
            "真实失败必须照旧记账，否则号池会把同一个撞墙地址反复排队",
        )


if __name__ == "__main__":
    unittest.main()