"""注册任务的数量/并发上界。

背景（实测，High）：`count` 只有下界没有上界，`count=1000000`
会建出一个**停不掉**的任务 —— 线程池按 count 提交一百万个 future，`stop` 只置
标志位而已提交的任务还在排队；实测内存 863MB、CPU 37.6%，`DELETE` 因「运行中」
被 409 拒绝，用户在界面上没有任何手段清掉它。

上界的意义不是限制业务，而是保证误填一个大数不会产生无法回收的任务。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from pydantic import ValidationError

from api.tasks import (
    MAX_REGISTER_CONCURRENCY,
    MAX_REGISTER_COUNT,
    RegisterTaskRequest,
)

ROOT = Path(__file__).resolve().parents[1]


class CountUpperBoundTests(unittest.TestCase):
    def test_rejects_absurdly_large_count(self):
        for bad in (1_000_000, MAX_REGISTER_COUNT + 1, 10**9):
            with self.subTest(count=bad):
                with self.assertRaises(ValidationError):
                    RegisterTaskRequest(platform="grok", count=bad)

    def test_accepts_the_limit_itself(self):
        """边界值本身要能过 —— 否则上限就成了「上限减一」。"""
        req = RegisterTaskRequest(platform="grok", count=MAX_REGISTER_COUNT)
        self.assertEqual(req.count, MAX_REGISTER_COUNT)

    def test_still_rejects_zero_and_negative(self):
        """下界是既有行为，别被上界的修改带丢。"""
        for bad in (0, -1, -100):
            with self.subTest(count=bad):
                with self.assertRaises(ValidationError):
                    RegisterTaskRequest(platform="grok", count=bad)

    def test_rejects_absurd_concurrency(self):
        with self.assertRaises(ValidationError):
            RegisterTaskRequest(platform="grok", concurrency=MAX_REGISTER_CONCURRENCY + 1)
        req = RegisterTaskRequest(platform="grok", concurrency=MAX_REGISTER_CONCURRENCY)
        self.assertEqual(req.concurrency, MAX_REGISTER_CONCURRENCY)


class FrontendLimitMirrorTests(unittest.TestCase):
    """前端的 max 要与后端一致。

    两边不一致的后果：前端放行、后端 422 —— 用户填完一长串数字、提交后才看到
    错误。前端加 `max` 的意义就是别让人白填一遍。
    """

    def _frontend_value(self, name: str) -> int:
        src = (ROOT / "frontend/src/lib/registerLimits.ts").read_text(encoding="utf-8")
        match = re.search(rf"export const {name} = (\d+)", src)
        self.assertIsNotNone(match, f"registerLimits.ts 里没有 {name}")
        return int(match.group(1))

    def test_count_matches_backend(self):
        self.assertEqual(self._frontend_value("MAX_REGISTER_COUNT"), MAX_REGISTER_COUNT)

    def test_concurrency_matches_backend(self):
        self.assertEqual(
            self._frontend_value("MAX_REGISTER_CONCURRENCY"), MAX_REGISTER_CONCURRENCY
        )

    def test_both_input_forms_apply_the_max(self):
        """两个入口（账号页的注册弹窗、注册任务页）都要带上 max。"""
        for rel in ("frontend/src/pages/Accounts.tsx",
                    "frontend/src/pages/RegisterTaskPage.tsx"):
            with self.subTest(file=rel):
                src = (ROOT / rel).read_text(encoding="utf-8")
                self.assertIn("max={MAX_REGISTER_COUNT}", src)
                self.assertIn("max={MAX_REGISTER_CONCURRENCY}", src)


if __name__ == "__main__":
    unittest.main()
