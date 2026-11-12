"""冻结工作树再跑验证：运行期文件变化必须判「结果无效」。

背景（2026-11-13）：后台跑全量测试（约 6 分钟）期间脱敏 fixture，
`test_kiro_platform.py` 出现 2 条**假失败**；冻结工作树后重跑才通过。
「树动了」和「测试挂了」是两件事，退出码也要分开：

* 0/其它 —— 命令自己的退出码原样传播；
* 2 —— 工作树在运行期间变化，本次结果无效。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "frozen_verify.py"


def _run(root: Path, *command: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--", *command],
        capture_output=True,
        text=True,
        timeout=60,
    )


class FrozenVerifyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        subprocess.run(["git", "-C", str(self.repo), "init", "-q"], check=True)
        (self.repo / "a.txt").write_text("original\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.repo), "add", "a.txt"], check=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_read_only_command_passes(self):
        result = _run(self.repo, sys.executable, "-c", "print('ok')")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_command_failure_propagates(self):
        result = _run(self.repo, sys.executable, "-c", "raise SystemExit(3)")
        self.assertEqual(result.returncode, 3, "命令失败不能被改写成「树动了」")

    def test_moving_tree_is_invalidated(self):
        result = _run(
            self.repo,
            sys.executable,
            "-c",
            "import pathlib, time; pathlib.Path('a.txt').write_text(str(time.time()))",
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("结果无效", result.stderr)
        self.assertIn("退出码 0", result.stderr, "要同时报出命令自身的退出码")

    def test_non_git_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as plain:
            result = _run(Path(plain), sys.executable, "-c", "pass")
        self.assertEqual(result.returncode, 2)
        self.assertIn("不是 git 仓库", result.stderr)


if __name__ == "__main__":
    unittest.main()
