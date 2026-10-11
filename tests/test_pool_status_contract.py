"""`poolStatus.ts` 三态展示映射与筛选折叠的行为契约。

用户要求（2026-10-11）：号池标签三态化 —— 未入池 / 已入池 / 使用中。
存储层 `available` / `used` 在展示层合并为「已入池」，「是否使用」由
「已注册平台」列负责；旧标签「未使用 / 已使用」不再出现。

做法与 `test_account_format_contract.py` 相同：前端没有测试运行器，用 vite
自带的 rolldown 把 TS 打成 ESM 再真实执行 —— `frontend/scripts/
run_pool_status_checks.mjs` 负责打包 + 执行 + 断言，本文件只驱动它并把
结果接进 pytest。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
HARNESS = FRONTEND / "scripts" / "run_pool_status_checks.mjs"


class PoolStatusBehaviorTests(unittest.TestCase):
    def test_three_state_display_behaves_as_documented(self):
        """真实执行断言：三态标签、available+used 合并、筛选按三态折叠。"""
        if shutil.which("node") is None:
            self.skipTest("未安装 node —— 跳过前端纯函数行为检查")
        if not (FRONTEND / "node_modules" / "rolldown").exists():
            self.skipTest("frontend/node_modules/rolldown 不存在（未 npm install）")

        result = subprocess.run(
            ["node", str(HARNESS)],
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertTrue(result.stdout.strip(), f"harness 没有输出：{result.stderr[-500:]}")
        report = json.loads(result.stdout)
        self.assertTrue(
            report["passed"],
            "poolStatus 三态展示行为不符：\n" + "\n".join(report["failures"]),
        )
        self.assertGreaterEqual(report["checked"], 25, "断言条数太少，网太稀")


if __name__ == "__main__":
    unittest.main()
