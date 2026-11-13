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
    """`if __name__ == "__main__":` / `if "__main__" == __name__:`."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    return (
        (isinstance(left, ast.Name) and left.id == "__name__" and isinstance(right, ast.Constant) and right.value == "__main__")
        or (isinstance(left, ast.Constant) and left.value == "__main__" and isinstance(right, ast.Name) and right.id == "__name__")
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

    # 只有**守卫内部**的定义是死的：模块级 `def test_*` 在守卫之后仍然会在
    # import 时定义，pytest 照常收集（review 实证：2 collected / 2 passed）。
    # 守卫内部可能嵌在 try/with/for 或 class 里，所以用 walk 而不是只看直接子节点。
    for guard in tree.body:
        if not _is_main_guard(guard):
            continue
        for node in ast.walk(guard):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                dead.append((node.name, node.lineno))
    return sorted(set(dead), key=lambda item: item[1])


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
