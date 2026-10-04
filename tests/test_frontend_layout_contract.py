"""前端拆分的契约：页面文件必须仍能通过构建，且关键接线字符串不能丢。

前端没有单元测试，回归全靠 tsc/eslint/构建。这条测试把「构建能过」
与「测试断言依赖的字符串仍在」钉在 pytest 里，避免拆分时静默破坏。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


class FrontendBuildContractTests(unittest.TestCase):
    def test_accounts_page_keeps_the_pinned_strings(self):
        src = (FRONTEND / "src" / "pages" / "Accounts.tsx").read_text(encoding="utf-8")
        self.assertIn("a?.scope !== 'panel'", src)
        self.assertIn("max={MAX_REGISTER_COUNT}", src)
        self.assertIn("max={MAX_REGISTER_CONCURRENCY}", src)

    def test_icloud_page_keeps_the_pinned_strings(self):
        src = (FRONTEND / "src" / "pages" / "ICloud.tsx").read_text(encoding="utf-8")
        for marker in (
            "dataSource={visibleAliases}",
            "return visibleAliases",
            "filterAliases(aliases, {",
            'placeholder="全部平台"',
            "filterPlatform",
            "aliasPlatformStatus",
        ):
            self.assertIn(marker, src, f"ICloud.tsx 丢了 {marker}")

    def test_typescript_still_compiles(self):
        """tsc -b 必须 0 错误（前端没有单测，这是唯一的结构回归网）。

        没装 node/npx 的环境跳过而不是硬失败：CI/开发机上缺 node 时，
        失败信息会指向「测试挂了」而不是「工具链没装」，误导排查方向。
        """
        if shutil.which("npx") is None:
            self.skipTest("未安装 npx —— 跳过 tsc 编译检查")
        if not (FRONTEND / "node_modules").exists():
            self.skipTest("frontend/node_modules 不存在（未 npm install）")
        result = subprocess.run(
            ["npx", "tsc", "-b"],
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ICloudTableLayoutTests(unittest.TestCase):
    """iCloud 页两个表格的列宽契约（实测踩过的 bug 的回归网）。

    症状与面板对比表同根：`scroll.x` 缺失或小于各列宽度之和时，antd 等比压缩
    所有列。本轮实测（2026-10-03，桌面 1280px）：

    * 「主号」列原无 width，被压到 **143px**（设定 260），邮箱地址换行、行高 78px；
      主号表整个没有 `scroll` 配置。
    * 别名表的 `scroll.x` 是 1820，而平台筛选时「号池」列从 130 变 170 ——
      列宽合计 1830 > 1820，最后一列被压缩。

    **列宽会随功能增删变动**，这两条断言把「scroll.x ≥ 当前列宽和」钉住：
    改列宽时忘了同步 scroll.x，测试直接变红，而不是等用户看到竖排文字。
    """

    def _src(self) -> str:
        return (FRONTEND / "src" / "pages" / "ICloud.tsx").read_text(encoding="utf-8")

    def test_account_table_has_scroll_covering_its_columns(self):
        import re

        src = self._src()
        # 主号表列定义：从 `const accountColumns` 到第一个 `]`
        block = src.split("const accountColumns", 1)[1].split("\n  ]", 1)[0]
        widths = [int(m) for m in re.findall(r"width:\s*(\d+)", block)]
        self.assertGreaterEqual(len(widths), 7, f"只解出 {len(widths)} 个列宽，解析可能失效")

        # 两个 scroll={{ x: N }}（主号表 + 别名表），取最小的那个 = 主号表
        scrolls = [int(m) for m in re.findall(r"scroll=\{\{\s*x:\s*(\d+)\s*\}\}", src)]
        self.assertGreaterEqual(len(scrolls), 2, "找不到两个表格的 scroll 配置")

        self.assertGreaterEqual(
            min(scrolls),
            sum(widths),
            f"主号表 scroll.x={min(scrolls)} 小于列宽和 {sum(widths)}（列宽：{widths}）"
            " —— antd 会等比压缩，「主号」列被挤到 143px、行高 78px（实测）",
        )

    def test_alias_table_scroll_covers_widest_pool_column(self):
        """别名表 scroll.x 要按「号池」列的**最大**宽度（筛选时 170）算。

        平台筛选时该列 130 → 170，若按未筛选的合计定 scroll.x，筛选后就不够。
        """
        import re

        src = self._src()
        block = src.split("const aliasColumns", 1)[1].split("\n  ]", 1)[0]
        widths = [int(m) for m in re.findall(r"width:\s*(\d+)", block)]
        # 三元表达式 `filterPlatform ? 170 : 130` 不在 `width: N` 的正常形态里，
        # 单独把两个候选都算进来（取大值）。
        ternaries = re.findall(r"width:\s*\w+\s*\?\s*(\d+)\s*:\s*(\d+)", block)
        for a, b in ternaries:
            widths.append(max(int(a), int(b)))
        self.assertGreaterEqual(len(widths), 9, f"只解出 {len(widths)} 个列宽")

        scrolls = [int(m) for m in re.findall(r"scroll=\{\{\s*x:\s*(\d+)\s*\}\}", src)]
        self.assertGreaterEqual(
            max(scrolls),
            sum(widths),
            f"别名表 scroll.x={max(scrolls)} 小于列宽和 {sum(widths)}（列宽：{widths}）",
        )


class PanelComparisonTableLayoutTests(unittest.TestCase):
    """面板对比表的列宽分配（实测踩过的 bug 的回归网）。

    症状：`scroll.x` 小于各列宽度之和时，antd 等比压缩所有列，**没有显式
    width 的列**被挤到最小值 —— 实测「远端信息」列表头 4 个字竖排成 94px
    高、整行被撑到 62px（桌面与移动端都是）。修法是给该列定宽 + 让
    scroll.x ≥ 列宽和；这条测试从源码里把两个数解出来核对。

    只测源码不测浏览器：前端没有单测基础设施，而这两个数字是纯常量 ——
    数值对不上时，运行时一定出问题（等比压缩是 antd 的确定行为）。
    """

    def test_scroll_x_covers_the_sum_of_column_widths(self):
        import re

        src = (
            FRONTEND / "src" / "components" / "settings" / "PanelComparisonPanel.tsx"
        ).read_text(encoding="utf-8")

        # 各列 width：COLUMNS 数组里的 `width: <n>`（渲染函数内偶有别的
        # width 用法，这里只取「列定义」段落：从 `const COLUMNS` 到 `]`）
        columns_block = src.split("const COLUMNS", 1)[1].split("\n]", 1)[0]
        widths = [int(m) for m in re.findall(r"width:\s*(\d+)", columns_block)]
        self.assertGreaterEqual(len(widths), 8, f"只解出 {len(widths)} 个列宽，解析可能失效")

        # scroll.x
        m = re.search(r"scroll=\{\{\s*x:\s*(\d+)\s*\}\}", src)
        self.assertIsNotNone(m, "找不到 scroll={{ x: ... }} —— 表格结构变了")
        scroll_x = int(m.group(1))

        self.assertGreaterEqual(
            scroll_x,
            sum(widths),
            f"scroll.x={scroll_x} 小于列宽和 {sum(widths)}（列宽：{widths}）"
            " —— antd 会等比压缩，无 width 的列会被挤成竖排文字",
        )

    def test_every_visible_column_has_an_explicit_width(self):
        """除动作列外的所有列都要显式 width。

        实测教训：唯一没有 width 的列成了等比压缩的受害者（30px 宽）。
        动作/图标列是例外（内容固定、不需要宽度约束）。
        """
        import re

        src = (
            FRONTEND / "src" / "components" / "settings" / "PanelComparisonPanel.tsx"
        ).read_text(encoding="utf-8")
        columns_block = src.split("const COLUMNS", 1)[1].split("\n]", 1)[0]

        # 每个 `{ title: '...'` 开始的列对象
        entries = re.split(r"\n  \{\n", columns_block)[1:]
        for entry in entries:
            title_m = re.search(r"title:\s*'([^']+)'", entry)
            if not title_m:
                continue
            title = title_m.group(1)
            self.assertRegex(
                entry,
                r"width:\s*\d+",
                f"列「{title}」没有显式 width —— 会被 antd 的等比压缩挤扁"
                "（实测「远端信息」列 30px 竖排 94px 高）",
            )

    def test_action_button_group_wraps_on_narrow_screens(self):
        """操作按钮组必须允许换行。

        实测：移动端 390px 下按钮一排超宽，`<Space>` 不 wrap 时
        「重新拉取对比」被容器裁掉（right=519 > 视口 390，只有 15px 可见、
        点不到）。Space 组件要带 wrap。
        """
        src = (
            FRONTEND / "src" / "components" / "settings" / "PanelComparisonPanel.tsx"
        ).read_text(encoding="utf-8")
        # 锚点是按钮的 JSX（`data-hermes-action`），不是文案 —— 文案在注释里
        # 也会出现，用文案当锚点会让 `rindex` 找到注释上方的另一个 Space
        # （实测踩过：断言在 `<Space size={4}>` 上失败）。
        push_idx = src.index('data-hermes-action="push-to-remote"')
        # 往上找最近的 <Space
        space_idx = src.rindex("<Space", 0, push_idx)
        space_tag = src[space_idx : src.index(">", space_idx) + 1]
        self.assertIn(
            "wrap",
            space_tag,
            "操作按钮组的 <Space> 没有 wrap —— 窄屏下按钮会被裁掉点不到",
        )


class ICloudActionColumnStickyTests(unittest.TestCase):
    """iCloud 页两个表格的「操作」列必须固定到右侧（sticky）。

    实测（dogfood，桌面 1280px，2026-10-04）：
    * 主号表：操作列 x=1349 / right=1529，初始在视口外 —— 要横向滚动
      306px 才能看到「同步 / 删除」按钮；
    * 别名表：操作列 x=1860 / right=2119 —— 要滚动 906px。

    两表都没有 `fixed: 'right'`，而账号页（Accounts.tsx）从初始版本就有，
    注释写明「不固定的话操作列默认落在视口外」。这是同类表格的一致性问题：
    同一套「列宽超出容器 → 横向滚动」的表格，操作列的处理必须一致。
    """

    def _src(self) -> str:
        return (FRONTEND / "src" / "pages" / "ICloud.tsx").read_text(encoding="utf-8")

    def test_account_table_action_column_is_fixed_right(self):
        src = self._src()
        block = src.split("const accountColumns", 1)[1].split("\n  ]", 1)[0]
        action_idx = block.index("title: '操作'")
        action_block = block[action_idx : action_idx + 200]
        self.assertIn(
            "fixed: 'right'",
            action_block,
            "主号表「操作」列没有 fixed: 'right' —— 列宽超出容器时按钮落在视口外",
        )

    def test_alias_table_action_column_is_fixed_right(self):
        src = self._src()
        block = src.split("const aliasColumns", 1)[1].split("\n  ]", 1)[0]
        action_idx = block.index("title: '操作'")
        action_block = block[action_idx : action_idx + 200]
        self.assertIn(
            "fixed: 'right'",
            action_block,
            "别名表「操作」列没有 fixed: 'right' —— 列宽超出容器时按钮落在视口外",
        )


class MeasureRowKeyboardTrapTests(unittest.TestCase):
    """表格测量行（ant-table-measure-row）里的 checkbox 不能成为键盘陷阱。

    实测（dogfood，2026-10-04）：rc-table 的测量行会克隆各列的 title 用于
    测宽，`rowSelection` 表头的「Select all」checkbox 因此被克隆一份到
    `aria-hidden="true"` 的测量行里。antd 只给它 `pointer-events: none`
    （鼠标点不到），**没有处理键盘焦点** —— 实测 Tab 到第 14 步聚焦到
    这个不可见 checkbox（outline 渲染在视口外），按 Space 直接全选 32 个
    账号。这是键盘用户可复现的意外破坏性操作。

    修复：在全局 CSS 里让测量行内的 checkbox 不可聚焦
    （`visibility: hidden` 或 `display: none`），并保持表头 checkbox 正常。
    """

    def _css(self) -> str:
        return (FRONTEND / "src" / "index.css").read_text(encoding="utf-8")

    def test_measure_row_checkbox_is_not_focusable(self):
        css = self._css()
        # 找针对测量行 checkbox 的规则
        idx = css.find(".ant-table-measure-row")
        self.assertGreater(idx, -1, "index.css 里没有针对 .ant-table-measure-row 的规则")
        # 该规则块内必须包含 checkbox 且让它不可见/不可聚焦
        block = css[idx : idx + 400]
        self.assertIn(".ant-checkbox-input", block, "测量行规则没有覆盖 .ant-checkbox-input")
        self.assertTrue(
            "visibility: hidden" in block or "display: none" in block,
            "测量行的 checkbox 没有隐藏 —— 键盘用户能 Tab 到它并按 Space 全选",
        )


class NotFoundRouteContractTests(unittest.TestCase):
    """未匹配路由必须有兜底页面（实测 404 内容区空白）。

    dogfood 实测（2026-10-04）：访问 /nonexistent-page-xyz 时内容区完全
    空白（innerHTML 30 字节），只有侧栏导航 —— 用户不知道是页面不存在
    还是加载失败。修复：Routes 里加 catch-all 兜底。
    """

    def _src(self) -> str:
        return (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")

    def test_inner_routes_have_a_catch_all(self):
        src = self._src()
        # 内部 Routes（ProtectedLayout 里那个）要有 path="*" 兜底
        self.assertIn(
            'path="*"',
            src,
            "内部 Routes 没有 catch-all —— 未知路径内容区空白（实测 30 字节）",
        )

    def test_catch_all_renders_something_visible(self):
        """兜底页不能是空 div —— 要有可见的提示文案。

        锚点：从 catch-all 的 `element={<X />}` 解出组件名，再检查该组件
        定义体里有「不存在」字样（路由处只有组件引用，文案在定义里）。
        """
        import re

        src = self._src()
        m = re.search(r'path="\*"\s+element=\{<(\w+)\s*/>\}', src)
        if m is None:
            self.fail("catch-all 没有渲染具名组件（element 解不出来）")
        component = m.group(1)

        def_idx = src.find(f"function {component}(")
        self.assertGreater(def_idx, -1, f"{component} 组件没有定义")
        # 组件体（到下一个顶层函数或 800 字符）里要有可见文案
        body = src[def_idx : def_idx + 800]
        self.assertIn(
            "不存在",
            body,
            f"{component} 渲染的是空白 —— 用户看不出发生了什么",
        )


class DataBackupPanelContractTests(unittest.TestCase):
    """「全局配置 → 数据迁移」面板的接线契约。

    前端没有单测，这个面板又全是行为（下载二进制、上传文件）—— 用字符串
    契约钉住关键接线，防止重构时静默拆断：
      * 面板挂在 Settings 的 backup tab 上；
      * 导出走 GET /backup/export 且用**原生 fetch**（apiFetch 会 res.json()
        解析，二进制响应会炸）；
      * 导入 POST /backup/import 且 Content-Type 是 application/zip
        （后端刻意不引 python-multipart，走原始字节体）；
      * 导入是破坏性操作 —— 必须有确认弹窗，且确认文案要提到「自动备份」。
    """

    def _panel_src(self) -> str:
        return (
            FRONTEND / "src" / "components" / "settings" / "DataBackupPanel.tsx"
        ).read_text(encoding="utf-8")

    def test_panel_is_wired_into_settings_tab(self):
        src = (FRONTEND / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
        self.assertIn("DataBackupPanel", src)
        self.assertIn("key: 'backup'", src)
        self.assertIn("activeTab === 'backup'", src)

    def test_export_uses_native_fetch_not_apifetch(self):
        """导出是二进制下载：必须用原生 fetch。

        `apiFetch` 结尾是 `return res.json()` —— 对 ZIP 响应会抛
        "Unexpected token 'P'"，导出功能整个不可用。
        """
        src = self._panel_src()
        self.assertIn("fetch('/api/backup/export'", src)
        self.assertIn("res.blob()", src)

    def test_import_posts_raw_bytes_with_zip_content_type(self):
        src = self._panel_src()
        self.assertIn("fetch('/api/backup/import'", src)
        self.assertIn("'Content-Type': 'application/zip'", src)
        # body 直接给 File（Blob）而不是 arrayBuffer()：后者把整个文件先
        # 拷进内存再发，大备份包时白占一份内存且卡主线程。
        self.assertIn("body: file,", src)
        self.assertNotIn("file.arrayBuffer()", src, "又改回了 arrayBuffer 全量拷贝")

    def test_import_is_guarded_by_a_confirmation(self):
        """破坏性操作必须有确认，且文案要说清会备份。"""
        src = self._panel_src()
        self.assertIn("window.confirm", src)
        self.assertIn("替换当前全部数据", src)
        self.assertIn("自动备份", src)


if __name__ == "__main__":
    unittest.main()
