"""提交前守卫：受版本控制的文件里不能出现密钥字面量。

背景（2026-11-13）：把用户提供的 `kiro.json`（含真实 `aoa…`/`aor…` token 与
邮箱）原样复制成 `tests/fixtures/` 夹具，直到提交前扫描才发现。夹具已经脱敏，
但「先脱敏、再提交」当时只靠人记得 —— 这个守卫把它变成可执行检查：

* `scripts/check_no_secrets.py` 扫 `git ls-files`（= 会被提交的文件）；
* 命中即退出码 1，且**不回显密钥正文**（日志里不能再泄一次）。

为什么只收高置信度形状：通用长随机串会误伤测试里的 JWT/base64 夹具，误报几次
之后守卫就会被绕过 —— 那比没有守卫更糟。显式假值（FAKE / EXAMPLE / SAMPLE …）
一律放行，测试夹具必须用这些标记。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_no_secrets.py"


class RepositorySecretGuardTests(unittest.TestCase):
    def test_repository_has_no_secret_literals(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--root", str(ROOT)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"受版本控制的文件里发现疑似密钥字面量（先脱敏再提交）:\n"
            f"{result.stdout}\n{result.stderr}",
        )

    def test_planted_secret_is_blocked_without_echoing_it(self):
        """负向用例：守卫必须真的拦住，而且不能把密钥打进日志。"""
        secret = "sk-" + "a" * 40
        with tempfile.TemporaryDirectory() as tmp:
            leak = Path(tmp) / "leak.txt"
            leak.write_text(f"OPENAI_API_KEY={secret}\n", encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--root", tmp],
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("leak.txt:1: openai-key", result.stdout)
        self.assertNotIn(
            secret, result.stdout + result.stderr, "守卫不能把密钥正文写进日志"
        )


if __name__ == "__main__":
    unittest.main()
