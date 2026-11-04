"""结构性护栏：**同一文件里不许有同名定义**。

Python 里后定义的同名类/函数会**静默覆盖**前一个，而套件照常全绿 —— 被覆盖的
那些测试/分支根本不存在了，没有任何信号。

这个坑在本仓库已经踩过三次（2026-11-04 全量检测）：

1. `tests/test_grok_exit_probe.py` —— 两个 `PickUsableExitTests`，
   4 条测试从未运行；复活后 3 条真红（它们「死」着的时候生产代码插进了一整层）。
2. `tests/test_storage_backend.py` —— `ConfigStoreWorksOnBothBackendsTests` 与
   `ConfigStoreSwitchesBackendTests` 各两份，**12 条测试从未运行**；复活后
   1 条因夹具腐烂而红（`_json_file` 已从方法变成 property）。
3. `platforms/grok/signup_pages.py` —— `wait_for_code_input` 定义两次，后者覆盖
   前者，而**后者不遍历子 frame**（前者遍历）—— 恰好是「验证码框在 iframe 里」
   那个修过的问题的形状，谁把调用方换过去就静默失效。

三次都是靠偶然发现。这条护栏把它变成必然：**新增同名定义即红**。

设计说明：只扫**模块顶层**（`^class` / `^def`），不扫方法 —— 不同类里有同名方法
是完全正常的（`setUp` / `test_*` 到处都是），扫了只会误报。
"""

from __future__ import annotations

import re
import unittest
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: 生产代码目录（`reference/` 与 `.worktrees/` 是只读参考/并行工作树，不扫）。
PRODUCTION_DIRS = ("api", "core", "services", "platforms", "modules", "tools")

_CLASS_RE = re.compile(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:(]")
_DEF_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _duplicates(path: Path, pattern: re.Pattern[str]) -> list[str]:
    seen: Counter[str] = Counter()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = pattern.match(line)
        if match:
            seen[match.group(1)] += 1
    return sorted(name for name, count in seen.items() if count > 1)


class NoDuplicateTestClassNamesTests(unittest.TestCase):
    def test_no_test_file_defines_the_same_class_twice(self):
        offenders = []
        for path in sorted((REPO / "tests").glob("*.py")):
            dups = _duplicates(path, _CLASS_RE)
            if dups:
                offenders.append(f"{path.name}: {dups}")
        self.assertEqual(
            offenders, [],
            "同一个测试文件里定义了同名类 —— 后一个会覆盖前一个，"
            f"被覆盖的测试**永远不会运行**：{offenders}",
        )


class NoDuplicateTopLevelDefsTests(unittest.TestCase):
    def test_no_production_module_defines_the_same_function_twice(self):
        offenders = []
        for folder in PRODUCTION_DIRS:
            root = REPO / folder
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*.py")):
                dups = _duplicates(path, _DEF_RE)
                if dups:
                    offenders.append(f"{path.relative_to(REPO)}: {dups}")
        self.assertEqual(
            offenders, [],
            "同一个模块里定义了同名顶层函数 —— 后一个静默覆盖前一个，"
            f"前一个的实现等于不存在：{offenders}",
        )


class TheGuardItselfCanFailTests(unittest.TestCase):
    """护栏必须**能红** —— 否则它只是装饰。

    不用变异测试框架，直接喂一个临时文件给同一个扫描函数。
    """

    def test_duplicate_class_detection_is_live(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(
                "class Same(unittest.TestCase):\n    pass\n\n"
                "class Other(unittest.TestCase):\n    pass\n\n"
                "class Same(unittest.TestCase):\n    pass\n",
                encoding="utf-8",
            )
            self.assertEqual(_duplicates(path, _CLASS_RE), ["Same"])

    def test_duplicate_def_detection_is_live(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.py"
            path.write_text(
                "def f():\n    return 1\n\n"
                "def g():\n    return 2\n\n"
                "def f():\n    return 3\n",
                encoding="utf-8",
            )
            self.assertEqual(_duplicates(path, _DEF_RE), ["f"])
