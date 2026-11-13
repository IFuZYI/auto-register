"""Fail when a `test_*` function can never be collected.

A test defined **inside** the module-level `if __name__ == "__main__":` guard
(or at module level *after* it) is never seen by pytest/unittest — it sits
there looking like coverage.  This session found one: a `test_export_only_
keys_are_exactly_cookies` after `unittest.main()` that asserted an outdated
tuple and had never run.

Usage:
    python scripts/check_dead_tests.py [tests_dir]
Exit code 1 when a dead definition is found.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path


def _is_main_guard(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare):
        return False
    left, ops, comparators = test.left, test.ops, test.comparators
    return (
        isinstance(left, ast.Name)
        and left.id == "__name__"
        and len(ops) == 1
        and isinstance(ops[0], ast.Eq)
        and len(comparators) == 1
        and isinstance(comparators[0], ast.Constant)
        and comparators[0].value == "__main__"
    )


def _test_defs(nodes: list[ast.stmt]) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [
        node
        for node in nodes
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    ]


def find_dead_tests(source: str) -> list[tuple[str, int]]:
    """Return `(name, lineno)` for every test definition that is never collected."""
    tree = ast.parse(source)
    dead: list[tuple[str, int]] = []

    guards = [node for node in tree.body if _is_main_guard(node)]
    for guard in guards:
        # 1) A def *inside* the guard body (indented under `if __name__ ...`).
        dead.extend((node.name, node.lineno) for node in _test_defs(guard.body))
        # 2) A module-level def *after* the guard (syntactically valid, never run).
        guard_end = guard.end_lineno or guard.lineno
        dead.extend(
            (node.name, node.lineno)
            for node in _test_defs(tree.body)
            if node.lineno > guard_end
        )
    return dead


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path("tests")
    failures: list[str] = []
    for path in sorted(root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            dead = find_dead_tests(source)
        except SyntaxError:
            continue
        for name, lineno in dead:
            failures.append(
                f"{path}:{lineno}: {name} 定义在 __main__ 守卫内/之后，永远不会被收集"
            )
    for line in failures:
        print(line)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
