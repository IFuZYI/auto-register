#!/usr/bin/env python3
"""在「冻结的工作树」上跑验证命令：运行期文件变化 → 结果无效（exit 2）。

用法::

    python scripts/frozen_verify.py -- pytest -q tests/
    python scripts/frozen_verify.py --root . -- python -m pytest -q

为什么需要：长跑（本仓全量测试约 6 分钟）期间如果还在改文件，**失败与通过都不
代表任何一棵确定的树**。实测（2026-11-13）：后台跑全量测试时脱敏 fixture，
`test_kiro_platform.py` 出现 2 条假失败；冻结工作树后重跑才通过。把「树有没有
动」变成可执行判定，比事后怀疑结果可靠。

判定：命令前后各算一次工作树指纹（HEAD + `git status --porcelain` + 受控文件
内容 sha256）。不一致 → stderr 打印「结果无效」并退出 **2**（命令自身的退出码
写进消息，避免把「树动了」误读成「测试挂了」）。一致 → 原样返回命令退出码。
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

#: 树被移动时的专用退出码（与「命令失败」区分开）。
MOVED_EXIT_CODE = 2


def _git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        timeout=120,
    ).stdout


def snapshot(root: Path) -> str:
    """工作树指纹：HEAD + 状态 + 每个受控文件的内容。"""
    digest = hashlib.sha256()
    try:
        digest.update(_git(root, "rev-parse", "HEAD"))
    except subprocess.CalledProcessError:
        # 还没有第一个提交（刚 init 的仓库）—— 指纹仍然有效，只是没有 HEAD
        digest.update(b"<no-head>")
    digest.update(_git(root, "status", "--porcelain=v1", "-z"))
    for raw in _git(root, "ls-files", "-z").split(b"\0"):
        if not raw:
            continue
        path = root / raw.decode("utf-8", "surrogateescape")
        digest.update(raw)
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在冻结的工作树上跑验证命令")
    parser.add_argument("--root", default=".", help="仓库根目录")
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="-- 之后的命令（参数数组）"
    )
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("缺少要执行的命令（用 `-- <command...>` 分隔）")

    root = Path(args.root).resolve()
    try:
        _git(root, "rev-parse", "--git-dir")
    except subprocess.CalledProcessError:
        print(f"[frozen-verify] {root} 不是 git 仓库 —— 无法判定树有没有动", file=sys.stderr)
        return MOVED_EXIT_CODE
    before = snapshot(root)
    completed = subprocess.run(command, cwd=str(root))
    after = snapshot(root)

    if before != after:
        print(
            f"[frozen-verify] 工作树在运行期间发生变化 —— 本次结果无效"
            f"（命令退出码 {completed.returncode}）。"
            "冻结改动后重跑，别把「树动了」当成测试失败。",
            file=sys.stderr,
        )
        return MOVED_EXIT_CODE
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
