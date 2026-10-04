"""主题色板与对比度门禁的契约。

背景（实测踩过的坑）：

1. **门禁脚本的主题键写错了**：脚本写 `localStorage['themeMode']`，而应用读的是
   `localStorage['theme']` —— reload 后 `applyThemeVars` 仍按 'dark' 重建，
   **light 那一趟实际一直在测暗色**。修正后 light 立刻暴露出 55 处不达标。

2. **探针只读 `backgroundColor`**：主按钮的底是 `backgroundImage` 里的渐变，
   `backgroundColor` 是 transparent —— 白字被算成 1.0:1 的假阳性。渐变必须
   采样后取最差比值。

一次性对比度脚本已随清理移除；这里保留的是「色板本身」的契约 —— 色值可以调，
但两套主题的键必须齐、CSS 变量必须映射，否则主题会悄悄退化。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THEME_TS = ROOT / "frontend/src/theme.ts"
INDEX_CSS = ROOT / "frontend/src/index.css"


class ThemePaletteTests(unittest.TestCase):
    """色板必须两套主题都齐 —— 缺一个键 tsc 会拦，但值退化拦不住。"""

    def setUp(self):
        self.src = THEME_TS.read_text(encoding="utf-8")

    def test_accent_text_exists_in_both_palettes(self):
        """`accentText` 是「强调色当文字用」的取值，两套主题都要有。

        与 `accent` 分开的原因：accent 用于填充（白字压在上面），accentText
        是文字本身，必须压在各种浅色合成底上过 AA。合并成一个值的话，
        满足填充需求的值当文字就不够（实测 #0071e3 在菜单选中底上 4.00:1）。
        """
        self.assertGreaterEqual(
            self.src.count("accentText:"), 2,
            "两套主题都要定义 accentText",
        )

    def test_purple_exists_in_both_palettes(self):
        """紫色（2FA 已绑这类标记）同理：antd preset 的固定色板不过 AA。"""
        self.assertGreaterEqual(self.src.count("purple:"), 2)

    def test_css_vars_cover_the_new_keys(self):
        """新色板键必须映射成 CSS 变量，否则 index.css 里的 var() 取不到值。"""
        for name in ("accentText: '--accent-text'", "purple: '--purple'",
                     "purpleSoft: '--purple-soft'"):
            self.assertIn(name, self.src, f"CSS_VAR_NAMES 缺 {name}")

    def test_css_has_matching_defaults(self):
        """index.css 的 :root 是首屏兜底（JS 注入前的第一帧），也要有这些变量。"""
        css = INDEX_CSS.read_text(encoding="utf-8")
        for var in ("--accent-text", "--purple", "--purple-soft"):
            self.assertIn(var, css, f"index.css 的 :root 缺 {var}")


class PresetTagOverrideTests(unittest.TestCase):
    """antd preset 色标签的钉法。

    `color="green"` / `color="purple"` / `color="processing"` 走的是 antd 的
    **固定**色板，不吃主题的 success/purple —— 亮色下实测 3.37:1 / 4.48:1 /
    4.29:1，都低于 AA。index.css 里逐类钉到主题变量上。
    """

    def setUp(self):
        self.css = INDEX_CSS.read_text(encoding="utf-8")

    def test_green_and_purple_are_pinned(self):
        for cls in (".ant-tag.ant-tag-green", ".ant-tag.ant-tag-purple"):
            self.assertIn(cls, self.css, f"{cls} 没被钉住 —— preset 派生值不过 AA")

    def test_processing_is_pinned(self):
        """对比面板的「远端较新」用 processing —— 固定色板下亮色实测 4.29:1。"""
        self.assertIn(".ant-tag.ant-tag-processing", self.css)

    def test_checkable_tags_are_pinned(self):
        """筛选标签选中态：白字压 colorPrimary（亮色 3.79:1）。"""
        self.assertIn(".ant-tag.ant-tag-checkable-checked", self.css)
        self.assertIn("#005bb5", self.css)

    def test_checkable_tags_meet_minimum_target_size(self):
        """可点标签的点击目标 >= 24px（WCAG 2.5.8）。"""
        block = self.css.split(".ant-tag.ant-tag-checkable {")[1].split("}")[0]
        self.assertIn("min-height: 24px", block)


class SwitchLabelTests(unittest.TestCase):
    """开关标签的可读性。

    antd 一律给标签用 `colorTextLightSolid`（白）：dark 下轨道是深灰没问题，
    light 下轨道是浅灰 —— 白字 1.87:1，等于看不见。
    """

    def test_light_theme_uses_dark_label_text(self):
        css = INDEX_CSS.read_text(encoding="utf-8")
        self.assertIn(".light .ant-switch:not(.ant-switch-checked)", css)
        self.assertIn("ant-switch-inner-unchecked", css)


if __name__ == "__main__":
    unittest.main()
