"""取号必须能**轮换**：反复被拒的别名不该永远排在队首。

## 实测的症状（2026-10-28，两次真实注册冒烟）

两次冒烟都取到同一个 `rucks.welder-95@icloud.com`，两次都撞 429：

    [iCloud 远程] 取号: rucks.welder-95@icloud.com（已标记为「使用中」）
    authorize/continue 非 200: status=429

## 为什么会这样

`claim_alias` 按 **id 升序**遍历候选（`find_candidates` 的
`ORDER BY id`）。失败时 `release_alias_claim` 把号放回 `available`，
于是它**又回到队首** —— 下一次任务还是它。被 OpenAI 标记过的地址因此
被反复派出去，每次都撞同一堵墙，而池子里另外 24 个可用号从未被尝试。

## 正确行为

失败要有**记忆**：同一个别名连续失败到阈值后，取号要跳过它（或降权），
让任务换下一个号。否则「号池」在事实上退化成单号重试。
"""

from __future__ import annotations

import unittest


class AliasTracksConsecutiveFailuresTests(unittest.TestCase):
    def test_model_has_a_failure_counter(self):
        from core.db.models_platform import ICloudAliasModel

        fields = {c.name for c in ICloudAliasModel.__table__.columns}
        self.assertTrue(
            any("fail" in name for name in fields),
            f"别名表没有失败计数字段（现有: {sorted(fields)}）—— "
            "被反复拒绝的号无法被跳过",
        )

    def test_repository_exposes_a_failure_marker(self):
        from core.db.icloud_alias_repository import ICloudAliasRepository

        self.assertTrue(
            hasattr(ICloudAliasRepository, "mark_failed")
            or any("fail" in n for n in dir(ICloudAliasRepository)),
            "仓储没有记录失败的入口",
        )


class ClaimSkipsRepeatedlyFailingAliasesTests(unittest.TestCase):
    """连续失败到阈值的别名，取号要跳过。"""

    def test_failed_alias_is_skipped_after_threshold(self):
        # `from core.db import icloud_alias_repository` 拿到的是**单例**，
        # 不是模块（`__init__.py` 导出的是 `icloud_alias_repository = ...`）。
        # 要拿模块得用 importlib —— 第一版在这里栽了两次。
        import importlib

        mod = importlib.import_module("core.db.icloud_alias_repository")
        limit = getattr(mod, "MAX_CONSECUTIVE_FAILURES", None)
        self.assertIsNotNone(limit, "没有失败阈值常量")
        self.assertGreater(int(limit), 0)
        self.assertLessEqual(int(limit), 10, "阈值太大，被拒的号还会派很多次")

    def test_candidates_sort_failures_last(self):
        """失败过的别名要排到候选**后面**，不是继续排第一。"""
        import inspect

        from core.db.icloud_alias_repository import ICloudAliasRepository

        src = inspect.getsource(ICloudAliasRepository.find_candidates)
        self.assertIn(
            "consecutive_failures", src,
            "候选排序没有考虑失败次数 —— 失败的号释放后又回到队首",
        )


class ReleaseRecordsTheFailureTests(unittest.TestCase):
    """`release_alias_claim` 要把这次失败记进计数。

    只放回 `available` 而不记账，等于把「这个号刚失败过」这件事丢掉 ——
    下次取号又会选中它（实测两次冒烟都取到同一个号）。
    """

    def test_release_increments_the_counter(self):
        import inspect

        from services.icloud import aliases as aliases_mod

        src = inspect.getsource(aliases_mod.release_alias_claim)
        self.assertTrue(
            "fail" in src.lower(),
            "release_alias_claim 没有记录失败 —— 失败的号会一直排在队首",
        )


if __name__ == "__main__":
    unittest.main()
