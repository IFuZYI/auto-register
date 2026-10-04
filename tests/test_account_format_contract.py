"""`accountFormat.ts` 纯函数的行为契约。

这些函数从 `Accounts.tsx` 抽出来时的承诺就是「无副作用、可单测」——
这里真的把它们跑起来断言，而不是对着源码做正则（后者改了实现就可能失效）。

做法：前端没有测试运行器，但 vite 自带 rolldown，能把 TS 打成 ESM 再 import。
`frontend/scripts/run_account_format_checks.mjs` 负责打包 + 执行 + 断言，
本文件只驱动它并把结果接进 pytest。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
HARNESS = FRONTEND / "scripts" / "run_account_format_checks.mjs"


class AccountFormatBehaviorTests(unittest.TestCase):
    def test_pure_helpers_behave_as_documented(self):
        """真实执行 24 条断言：时间格式化回退、JSON 美化、状态映射、extra 解析。"""
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
            "accountFormat 纯函数行为不符：\n" + "\n".join(report["failures"]),
        )
        self.assertGreaterEqual(report["checked"], 20, "断言条数太少，网太稀")


class NoBlanketEslintDisableTests(unittest.TestCase):
    def test_account_format_has_no_file_level_disable(self):
        """不允许用文件级 eslint-disable 掩盖类型问题。

        抽包时这里一度挂着 `/* eslint-disable @typescript-eslint/no-explicit-any */`，
        等于把整个文件的类型检查关掉 —— 后来改成显式的 `AccountLike` 接口。
        钉住这条，防止有人为了少写几行类型又把开关打开。
        """
        src = (FRONTEND / "src/lib/accountFormat.ts").read_text(encoding="utf-8")
        self.assertNotIn("eslint-disable", src,
                         "accountFormat.ts 又出现了文件级 eslint-disable")


if __name__ == "__main__":
    unittest.main()
