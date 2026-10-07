"""依赖方向守卫：`core/` 不得反向 import `services/`（复审发现，2026-10-07）。

`docs/EXTENDING.md` 明文规定：`platforms → modules → core` 是主骨架，
**core 一律不得反向 import**（仅 `core/registry.py` 与 `core/mailboxes/registry.py`
两个加载器例外）。独立 reviewer 发现 `core/scheduler.py` 的
`check_accounts_valid` 里延迟 import 了 `services.grok_account_state` ——
唯一的违规点，且违反该文件自己的 docstring。

修法（按仓库既有模式）：状态落库的差异逻辑挂到平台插件上
（`BasePlatform.apply_probe_status_policy`，默认 no-op；grok 插件覆写，
在方法体内延迟 import services —— platforms 允许依赖 services），
scheduler 通过注册表拿到的插件对象调用，不再认识具体平台。

本文件用 AST 扫描守卫依赖方向（正则会被字符串/注释骗过）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 允许反向加载的例外（docs/EXTENDING.md 点名的两个加载器）。
ALLOWED_REVERSE_IMPORTERS = {
    "core/registry.py",
    "core/mailboxes/registry.py",
}


def _service_imports_of(path: Path) -> list[tuple[int, str]]:
    """返回文件里 import services 的位置（行号 + 模块名），AST 级判定。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and str(node.module or "").startswith("services"):
            hits.append((node.lineno, str(node.module)))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("services"):
                    hits.append((node.lineno, alias.name))
    return hits


class CoreNeverImportsServicesTests(unittest.TestCase):
    """core/ 下的所有文件（除点名例外）不得 import services。"""

    def test_no_core_file_imports_services(self):
        violations: list[str] = []
        for path in sorted((ROOT / "core").rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in ALLOWED_REVERSE_IMPORTERS:
                continue
            for lineno, module in _service_imports_of(path):
                violations.append(f"{rel}:{lineno} → {module}")
        self.assertEqual(
            violations, [],
            "core 反向依赖了 services（docs/EXTENDING.md 禁止）：\n"
            + "\n".join(violations),
        )

    def test_the_guard_itself_can_detect_a_violation(self):
        """守卫非空转：对一个真含 services import 的文件必须报出命中。"""
        sample = ROOT / "tests" / "_guard_selfcheck_sample.py"
        sample.write_text("from services.grok_account_state import x\n", encoding="utf-8")
        try:
            hits = _service_imports_of(sample)
        finally:
            sample.unlink()
        self.assertTrue(hits, "AST 扫描器失灵 —— 对已知违规文件没有报出命中")


class SchedulerUsesThePluginHookTests(unittest.TestCase):
    """scheduler 通过平台插件的钩子落状态，而不是自己认识具体平台。"""

    def test_base_platform_declares_the_hook(self):
        from core.base_platform import BasePlatform

        self.assertTrue(
            hasattr(BasePlatform, "apply_probe_status_policy"),
            "BasePlatform 缺少 apply_probe_status_policy 钩子",
        )
        # 默认 no-op：不动状态、返回空理由
        import inspect

        sig = inspect.signature(BasePlatform.apply_probe_status_policy)
        self.assertIn("self", sig.parameters)

    def test_grok_plugin_overrides_the_hook(self):
        from platforms.grok.plugin import GrokPlatform
        from core.base_platform import BasePlatform

        self.assertIsNot(
            GrokPlatform.apply_probe_status_policy,
            BasePlatform.apply_probe_status_policy,
            "grok 插件没覆写钩子 —— 调度器无从落 grok 的状态",
        )


if __name__ == "__main__":
    unittest.main()
