"""日志里不许出现凭据（哪怕是前缀）。

安全审查（2026-10-27）发现 `services/external_sync.py` 有 4 处调试 `print`
把 `refresh_token` 的前 20 个字符打进 stdout —— 容器里 stdout 进日志文件，
一次贡献上传就会把半个 RT 落到磁盘上。RT 是能换 AT 的长期凭证，20 个字符
足以缩小暴力空间，也足以在日志被分享/上报时泄漏。

这条测试是**结构性**的：扫描生产代码里的日志/打印调用，禁止出现
「凭据变量的切片」这种模式。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("api", "services", "core", "platforms", "modules")

#: 凭据类变量名（出现这些名字的切片就要警觉）
CREDENTIAL_HINTS = (
    "access_token", "refresh_token", "session_token", "id_token",
    "sso", "totp_secret", "password", "api_key", "secret",
)
#: 允许出现的地方：只打印「有没有」「长度」不算泄漏。
#: **不要**把 `if ... else` 当安全模式 —— 三元表达式里照样可以切片
#: （`{token[:20] if token else "EMPTY"}`），第一版就是这样漏掉真问题的。
SAFE_PATTERNS = (
    r"bool\(", r"len\(", r"_set\b", r"has_",
)


def _iter_py():
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


class NoCredentialSlicesInLogsTests(unittest.TestCase):
    def test_no_credential_prefix_printed(self):
        offenders: list[str] = []
        # 形如 `{refresh_token[:20]}` 或 `token[:12]`
        slice_re = re.compile(r"\b([a-z_]*(?:" + "|".join(CREDENTIAL_HINTS) + r")[a-z_]*)\s*\[\s*:\s*\d+")

        for path in _iter_py():
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                stripped = line.strip()
                # 只看日志/打印行
                if not re.search(r"\b(print|logger\.\w+|log\()", stripped):
                    continue
                if any(re.search(p, stripped) for p in SAFE_PATTERNS):
                    continue
                m = slice_re.search(stripped)
                if m:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno} → {m.group(0)}")

        self.assertEqual(
            offenders, [],
            "日志/打印里出现了凭据切片（会把半个 token 写进磁盘）:\n  "
            + "\n  ".join(offenders),
        )

    def test_no_debug_prints_left_in_sync_path(self):
        """`[DEBUG]` 开头的 print 是排查遗留，不该留在生产路径。"""
        offenders: list[str] = []
        for path in _iter_py():
            text = path.read_text(encoding="utf-8", errors="replace")
            for lineno, line in enumerate(text.splitlines(), 1):
                if "[DEBUG]" in line and "print(" in line:
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
        self.assertEqual(offenders, [], f"遗留调试打印: {offenders}")


if __name__ == "__main__":
    unittest.main()
