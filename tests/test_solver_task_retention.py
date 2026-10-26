"""Solver 里 `asyncio.create_task` 的返回值必须被持有。

CPython 文档明确：事件循环只对任务持**弱引用**，不保存返回值的任务可能在
运行中被垃圾回收，同时悄悄取消。表现是「求解任务偶尔永远不完成」—— 这类
问题极难复现，所以在源码层面钉住。

实测（2026-10-27 审查）：`api_solver.py` 有三处 `create_task`，其中
`_solve_turnstile`（每次求解一个）与 `_periodic_cleanup`（服务启动一次）
的返回值都直接丢掉了。前者是热路径，量大时泄漏风险最高。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

SOLVER = Path(__file__).resolve().parent.parent / "services" / "turnstile_solver" / "api_solver.py"


class CreateTaskResultsAreRetainedTests(unittest.TestCase):
    def test_no_bare_create_task_call(self):
        """`asyncio.create_task(...)` 作为独立语句出现即为丢弃返回值。"""
        tree = ast.parse(SOLVER.read_text(encoding="utf-8"))
        bare: list[int] = []

        for node in ast.walk(tree):
            # 只关心「表达式语句」形态：`asyncio.create_task(...)` 一行写完
            if not isinstance(node, ast.Expr):
                continue
            call = node.value
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = ""
            if isinstance(func, ast.Attribute):
                name = func.attr
                base = func.value
                if isinstance(base, ast.Name) and base.id != "asyncio":
                    continue
            elif isinstance(func, ast.Name):
                name = func.id
            if name == "create_task":
                bare.append(node.lineno)

        self.assertEqual(
            bare, [],
            f"第 {bare} 行的 create_task 返回值被丢弃 —— 事件循环只持弱引用，"
            "任务可能被 GC 掉并静默取消；请存进实例集合并在完成时移除",
        )


class SolveTaskIsTrackedTests(unittest.TestCase):
    def test_solver_tracks_inflight_solve_tasks(self):
        src = SOLVER.read_text(encoding="utf-8")
        self.assertIn("_solve_tasks", src,
                      "没有用于持有求解任务的集合")


if __name__ == "__main__":
    unittest.main()
