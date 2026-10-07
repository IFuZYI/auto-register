"""维护手册（`docs/MAINTENANCE.md`）的接线契约。

手册的价值全在「里面写的文件与命令仍然对得上当前代码」。它天然会腐烂：
有人重命名了 `panel_registry.py` 的 `FETCHERS`、删了某个契约测试、改了
eslint 基线 —— 手册里那句话就变成了误导，而且**没有任何东西会报错**。

这里只钉两类东西，都是「错了就一定误导人」的：

1. 手册引用的文件/路径**必须存在**（含 README 的入口链接）；
2. 手册里写死的**数字**（eslint 基线、测试收集数下限）与仓库现状一致。

刻意**不**断言手册的措辞 —— 那是自由文本，钉住它会让每次润色都变红。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "MAINTENANCE.md"
README = ROOT / "README.md"
FRONTEND = ROOT / "frontend"


class MaintenanceDocStructureTests(unittest.TestCase):
    def test_doc_exists_and_is_linked_from_readme(self):
        """手册必须存在，且 README 里有入口 —— 否则没人会读到它。"""
        self.assertTrue(DOC.exists(), "docs/MAINTENANCE.md 不存在")
        self.assertIn(
            "docs/MAINTENANCE.md",
            README.read_text(encoding="utf-8"),
            "README 没有链接到维护手册",
        )

    def test_code_fences_are_balanced(self):
        """围栏数量必须是偶数 —— 少一个后面整篇都渲染成代码块。"""
        text = DOC.read_text(encoding="utf-8")
        fences = [line for line in text.splitlines() if line.startswith("```")]
        self.assertEqual(
            len(fences) % 2, 0, f"代码围栏 {len(fences)} 个（应为偶数）"
        )


class MaintenanceDocReferenceTests(unittest.TestCase):
    """手册点名的文件必须真的在仓库里。

    允许写文件名（不带目录）—— 手册讲的是「这个文件里的哪一处」，读者能搜到。
    这里把每个名字在整个仓库里找一遍，找不到才算失效。
    """

    #: 手册里以反引号引用、且**必须存在**的路径（含文件名形式）。
    REQUIRED = (
        "services/panel_registry.py",
        "services/panel_comparison.py",
        "services/panel_comparison_cache.py",
        "services/external_sync.py",
        "services/data_bundle.py",
        "api/config.py",
        "api/actions.py",
        "core/registry.py",
        "core/paths.py",
        "core/db/migrations.py",
        "core/mailboxes/channels/__init__.py",
        "core/mailboxes/registry.py",
        "frontend/src/lib/platforms.ts",
        "frontend/src/lib/mailboxSections.ts",
        "frontend/src/components/settings/PanelConfigPanel.tsx",
        "frontend/scripts/run_account_format_checks.mjs",
        "frontend/scripts/run_panel_filter_checks.mjs",
        "docs/EXTENDING.md",
        "docs/API_REFERENCE.md",
        "docs/DATABASE_MODULARITY.md",
        "docs/DATA_DIRECTORY.md",
        "docs/FEATURES.md",
        "docs/DEPLOYMENT.md",
    )

    def test_referenced_files_exist(self):
        missing = [rel for rel in self.REQUIRED if not (ROOT / rel).exists()]
        self.assertEqual(missing, [], f"手册引用的文件已不存在: {missing}")

    def test_named_tests_exist(self):
        """手册点名的契约测试必须都在 —— 删了测试却留着说明书是误导。"""
        text = DOC.read_text(encoding="utf-8")
        named = set(re.findall(r"`?(test_[a-z0-9_]+\.py)`?", text))
        self.assertTrue(named, "手册没有点名任何测试文件（结构变了？）")
        missing = sorted(n for n in named if not (ROOT / "tests" / n).exists())
        self.assertEqual(missing, [], f"手册点名的测试文件不存在: {missing}")


class MaintenanceDocNumberTests(unittest.TestCase):
    """手册里写死的数字必须与仓库现状一致。

    实测踩过的坑：文档里的「eslint 基线 55」是这一版的事实，但没人守着它 ——
    改动让基线变化时，手册会继续拿旧数字当验收标准。
    """

    def test_eslint_baseline_matches_the_doc(self):
        if shutil.which("npx") is None or not (FRONTEND / "node_modules").exists():
            self.skipTest("未安装 node/npx 或前端依赖 —— 跳过 eslint 基线核对")
        result = subprocess.run(
            ["npm", "run", "lint"],
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            timeout=300,
        )
        match = re.search(r"(\d+) problems? \((\d+) errors?, (\d+) warnings?\)",
                          result.stdout + result.stderr)
        if match is None:
            self.fail(f"解析 eslint 输出失败:\n{(result.stdout + result.stderr)[-500:]}")
        total = int(match.group(1))
        doc = DOC.read_text(encoding="utf-8")
        m = re.search(r"基线是 (\d+) problems", doc)
        if m is None:
            self.fail("手册里找不到 eslint 基线那句（措辞改了？）")
        self.assertEqual(
            int(m.group(1)),
            total,
            f"手册写的 eslint 基线 {m.group(1)}，实际 {total} —— 手册要跟着更新",
        )

    def test_collected_test_count_is_in_the_documented_ballpark(self):
        """手册说「2300+ 用例」—— 收集数掉下去说明删多了。"""
        result = subprocess.run(
            ["python3", "-m", "pytest", "tests/", "--collect-only", "-q"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        match = re.search(r"(\d+) tests collected", result.stdout)
        if match is None:
            self.fail(f"解析 pytest 收集数失败:\n{result.stdout[-500:]}")
        collected = int(match.group(1))
        doc = DOC.read_text(encoding="utf-8")
        self.assertIn("2300+", doc, "手册里的用例量级描述改了？")
        self.assertGreaterEqual(
            collected, 2300, f"实际只收集到 {collected} 个用例 —— 删多了？"
        )


if __name__ == "__main__":
    unittest.main()
