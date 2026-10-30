"""前端两条「静默失效 / 一点就没」的守卫（全量检测 frontend lane F-01~F-05）。

没有 JS 测试框架（仓库现状），但这类问题的形态很稳定，用源码级断言钉住最划算：
它们不是「风格」，而是**功能整块失效**，而且 tsc 不会报错。

- **F-01/F-02**：`selectedRowKeys` 是 UUID 字符串，`Number(uuid)` → NaN，
  再被 `Number.isInteger` 过滤光 → 「同步/补RT/刷新/上传（所选）」永远提示
  「请先选择」，导出退化成「全部筛选结果」（把整个账号池连密码导出去）。
- **F-03**：任务历史固定拉 50 条再按 20/页客户端分页 → 第 51 条以后翻不到。
- **F-04/F-05**：批量删账号、关密码保护、关 2FA 都是一点即生效，无二次确认。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


def _read(*parts: str) -> str:
    return (FRONTEND.joinpath(*parts)).read_text(encoding="utf-8")


class SelectedRowKeyTests(unittest.TestCase):
    def test_accounts_page_never_number_casts_row_keys(self):
        source = _read("pages", "Accounts.tsx")
        self.assertNotIn(
            "Number(value)",
            source,
            "账号主键是 UUID，Number(uuid)=NaN 会让「所选」类操作全部静默失效",
        )
        # 反向确认：确实按字符串传了
        self.assertIn("String(value)", source)

    def test_export_modal_accepts_string_ids(self):
        source = _read("components", "AccountExportModal.tsx")
        self.assertIn("(string | number)[]", source, "导出对话框的 selectedIds 类型要能装 UUID")

    def test_export_modal_does_not_recast_ids_to_number(self):
        """类型改对了不代表实现对了 —— 上一版就是「类型改了、`.map(Number)` 还在」。

        实测形态：`selectionKey.split(',').map(Number)` → `[NaN]` →
        `JSON.stringify` 变 `[null]` → 后端 422。只看类型字面量的守卫会全绿。
        """
        source = _read("components", "AccountExportModal.tsx")
        # 只看代码行：注释里写着「不能 .map(Number)」是说明，不是违规。
        code_lines = [
            line for line in source.splitlines()
            if not line.strip().startswith("//") and not line.strip().startswith("*")
        ]
        code = "\n".join(code_lines)
        self.assertNotIn(".map(Number)", code, "导出请求仍把 UUID 转成 number")
        self.assertNotIn("split(',').map(", code, "导出请求仍在把 selectedIds 逐项转换")


class TaskHistoryPaginationTests(unittest.TestCase):
    def test_page_and_page_size_come_from_state(self):
        source = _read("pages", "TaskHistory.tsx")
        self.assertNotRegex(
            source,
            r"page:\s*'1',\s*page_size:\s*'50'",
            "固定拉 50 条再客户端分页 → 51 条以后永远看不到",
        )
        self.assertIn("page_size: String(pageSize)", source)
        self.assertIn("total,", source)


class DestructiveActionConfirmationTests(unittest.TestCase):
    def test_batch_delete_accounts_asks_first(self):
        source = _read("pages", "Accounts.tsx")
        block = source[source.index("const handleBatchDelete") :]
        block = block[: block.index("const handleAdd")] if "const handleAdd" in block else block[:1200]
        self.assertIn("Modal.confirm", block, "批量删除账号没有二次确认")
        self.assertIn("danger", block)

    def test_disabling_protection_asks_first(self):
        source = _read("pages", "Settings.tsx")
        for handler in ("handleDisableAuth", "handleDisableTotp"):
            with self.subTest(handler=handler):
                start = source.index(f"const {handler}")
                block = source[start : start + 1200]
                self.assertIn("modal.confirm", block, f"{handler} 没有二次确认")


if __name__ == "__main__":
    unittest.main()
