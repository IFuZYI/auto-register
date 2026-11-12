#!/usr/bin/env python3
"""扫描受版本控制的文件，拦截密钥字面量（提交前守卫）。

用法::

    python scripts/check_no_secrets.py [--root .] [--quiet]

- 有 git 仓库时只扫 `git ls-files`（= 会被提交的文件）；
- 没有 git 时退化为遍历目录（跳过 .git/.venv/node_modules 等）。
- 命中即退出码 1，并打印 `文件:行号: 规则名`（**不回显密钥正文**）。

为什么只收「高置信度」形状：通用长随机串会误伤测试里的 JWT/base64 夹具，
误报几次之后守卫就会被绕过 —— 那比没有守卫更糟。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

#: 高置信度密钥形状（宁可漏掉「看起来像哈希」的 32 位十六进制）。
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai-key", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}")),
    ("aws-access-key-id", re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}")),
    ("github-token", re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{36}")),
    ("slack-token", re.compile(r"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("google-api-key", re.compile(r"(?<![A-Za-z0-9])AIza[0-9A-Za-z_-]{35}")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    # AWS Builder ID / Kiro 的 bearer token（aoa… / aor…，参考 kiro.json）
    ("aws-builder-id-token", re.compile(r"\b(?:aoa|aor)[A-Za-z0-9+/=_-]{40,}")),
)

#: 显式假值标记：命中里带这些词就跳过（测试夹具必须用它们）。
FAKE_MARKERS = ("FAKE", "EXAMPLE", "SAMPLE", "PLACEHOLDER", "DUMMY", "REDACTED", "TEST")

SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", "static", "data", ".worktrees", "reference"}
MAX_BYTES = 2 * 1024 * 1024


def _tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            check=True, capture_output=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return _walk_files(root)
    files = []
    for raw in out.split(b"\0"):
        if not raw:
            continue
        path = root / raw.decode("utf-8", "surrogateescape")
        if path.is_file():
            files.append(path)
    return files


def _walk_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def scan(root: Path) -> list[tuple[Path, int, str]]:
    findings: list[tuple[Path, int, str]] = []
    for path in _tracked_files(root):
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            for name, pattern in PATTERNS:
                match = pattern.search(line)
                if not match:
                    continue
                value = match.group(0)
                if any(marker in value.upper() for marker in FAKE_MARKERS):
                    continue
                findings.append((path, line_no, name))
                break
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="拦截受版本控制文件里的密钥字面量")
    parser.add_argument("--root", default=".", help="仓库根目录")
    parser.add_argument("--quiet", action="store_true", help="只返回退出码")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    findings = scan(root)
    if not args.quiet:
        for path, line_no, name in findings:
            try:
                rel = path.relative_to(root)
            except ValueError:
                rel = path
            print(f"{rel}:{line_no}: {name}")
        if findings:
            print(f"\n发现 {len(findings)} 处疑似密钥字面量 —— 先脱敏再提交。", file=sys.stderr)
        else:
            print("未发现密钥字面量。")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
