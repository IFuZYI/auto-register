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


class AtLifecycleDisplayContractTests(unittest.TestCase):
    """ChatGPT 的 AT 生成/到期时间必须在**列表**与详情两处都可见。

    用户反馈（2026-10-05）：「AT有效判断和AT日期呢？我怎么在GPT界面没见到，
    这个在chatgpt2api应该有啊。」——参考实现（chatgpt2api 的
    AccountCredentialStatus.vue）在列表行内直接显示凭据状态 chip + hover
    详情卡；我们此前只在详情弹窗里有 AT 生成/到期，列表行看不到。

    另：取 AT 必须**列（token）与 extra.access_token 都看** —— 实测存在
    token 列为空而 extra.access_token 有值的账号（较新的落库路径写 extra），
    只读 token 会让这类账号误显示「无 AT」。
    """

    def _src(self) -> str:
        return (FRONTEND / "src" / "pages" / "Accounts.tsx").read_text(encoding="utf-8")

    def test_list_has_at_expiry_column(self):
        """列表要有「AT 有效期」列（用 atListSummary 渲染，两平台通用）。"""
        src = self._src()
        self.assertIn("atListSummary", src, "列表没有用 atListSummary —— AT 日期在列表不可见")
        self.assertIn("title: 'AT 有效期'", src, "列表没有「AT 有效期」列标题")
        # 列必须在**公共区**（两平台都显示）——ChatGPT 与 Grok 的 AT 都是
        # JWT，详情弹窗本来就是共享的，列表不该只给一个平台。
        # 锚点：列定义出现在 `if (isChatgptPlatform)` 之前。
        at_idx = src.find("title: 'AT 有效期'")
        branch_idx = src.find("if (isChatgptPlatform) {")
        self.assertLess(
            at_idx,
            branch_idx,
            "「AT 有效期」列只在 ChatGPT 分支里 —— Grok 列表看不到（同类 UI 不一致）",
        )

    def test_scroll_x_covers_the_new_column(self):
        """scroll.x 要盖住加了 AT 列之后的列宽和（否则 antd 等比压缩）。"""
        import re

        src = self._src()
        m = re.search(r"scroll=\{\{ x: isChatgptPlatform \? (\d+) : (\d+) \}\}", src)
        if m is None:
            self.fail("找不到 scroll.x 配置")
        chatgpt_x, grok_x = int(m.group(1)), int(m.group(2))
        # ChatGPT: 260+120+120+110+130+260+140+132+150 = 1422
        # Grok:    260+120+120+110+130+100+120+132+150 = 1242
        self.assertGreaterEqual(chatgpt_x, 1422, f"ChatGPT scroll.x={chatgpt_x} 小于列宽和 1422")
        self.assertGreaterEqual(grok_x, 1242, f"Grok scroll.x={grok_x} 小于列宽和 1242")

    def test_detail_and_list_read_at_from_both_sources(self):
        """取 AT 要兼容 token 列与 extra.access_token 两个来源。"""
        src = self._src()
        self.assertIn(
            "access_token",
            src,
            "取 AT 没有读 extra.access_token —— token 列为空的账号会误显示「无 AT」",
        )
        # 至少一处 helper 把两者合并（锚点：访问 record.token 且提到 extra）
        helper_idx = src.find("const getAccessToken")
        self.assertGreater(helper_idx, -1, "缺少统一的 getAccessToken helper（两处取值口径会漂）")
        block = src[helper_idx : helper_idx + 500]
        self.assertIn("token", block)
        self.assertIn("access_token", block)

    def test_detail_modal_uses_the_shared_helper(self):
        """详情弹窗也要走同一个 helper（不能只读 token 列）。"""
        src = self._src()
        idx = src.find("AT 有效期")
        self.assertGreater(idx, -1)
        # 弹窗里 atLifecycleMeta 的参数必须是 helper 调用，不是裸 currentAccount.token
        self.assertNotIn(
            "atLifecycleMeta(currentAccount.token)",
            src,
            "详情弹窗还在只读 token 列 —— 应走 getAccessToken(currentAccount)",
        )

    def test_grok_token_column_is_not_read_as_at(self):
        """grok 的 token 列镜像是 SSO（不是 AT）—— helper 不许把它当 AT 读。

        整理前 helper 的兜底是无条件 `record.token`：grok 账号在列表里
        会把 SSO（session_id 的 JWT）显示成「无法解析」。整理后按平台镜像
        规则短路（`currentPlatform === 'grok'` 时不读列）。
        """
        src = self._src()
        helper_idx = src.find("const getAccessToken")
        self.assertGreater(helper_idx, -1)
        block = src[helper_idx : helper_idx + 700]
        self.assertIn(
            "currentPlatform === 'grok'",
            block,
            "getAccessToken 没有按平台镜像规则短路 —— grok 的 SSO 会被当 AT 显示",
        )

    def test_at_column_splits_date_and_remaining_into_separate_lines(self):
        """AT 列的「到期日期」与「剩余时间」要分行渲染。

        dogfood 实测（2026-10-05，90% 缩放 + 100% 均复现）：两者拼成一行
        需要 146px，而该列可用内容宽只有 ~92px（130px 列宽 - cell padding -
        panel padding）—— 日期被截断成「到期 2026-10-…」。拆行后
        「到期 2026-10-12」78px、「6 天 20 小时」59px，都放得下。
        """
        src = self._src()
        block = src.split("title: 'AT 有效期'", 1)[1].split("if (isChatgptPlatform)", 1)[0]
        self.assertNotIn(
            "at.expiresShort ? ' · ' : ''",
            block,
            "AT 列又把「到期日期 · 剩余」拼回一行 —— 列宽放不下会截断（实测 146px > 92px）",
        )
        self.assertIn(
            "at.remainingText",
            block,
            "AT 列丢了剩余时间渲染",
        )

    def test_plus_trial_time_uses_two_line_date_format(self):
        """Plus 试用列的探测时间要用两行日期格式（同「注册时间」列）。

        dogfood 实测（2026-10-05）：单行 `toLocaleString()`（`10/1/2026,
        6:43:48 PM`）需要 118px，该列可用内容宽只有 ~102px —— 时间戳被截断
        成「10/1/2026, 6:43…」。同页「注册时间」列早已用 formatCreatedAt
        的两行格式（日期一行、时间一行），两列显示的是同类信息，格式应当一致。
        """
        src = self._src()
        block = src.split("title: 'Plus 试用'", 1)[1].split("} else {", 1)[0]
        self.assertNotIn(
            "formatSyncTime(check.checked_at)",
            block,
            "Plus 列又用单行 formatSyncTime —— 时间戳会被截断（实测 118px > 102px）",
        )
        self.assertIn(
            "formatCreatedAt(check.checked_at)",
            block,
            "Plus 列没有用两行日期格式（与「注册时间」列不一致）",
        )


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


class PanelTimeColumnsUseCredentialIssuedAtTests(unittest.TestCase):
    """对比表的「本地/远端更新时间」两列显示 **AT 生成时间**。

    用户要求：「本地更新时间 远端更新时间 用的是 AT 生成的时间」。
    记录更新时间会被状态回写 touch 成噪声（实测把 10 个远端较新的账号顶成
    本地较新）—— AT 生成时间（JWT iat）才是「凭证什么时候生成的」真实口径，
    与方向判定（time_relation / time_basis）同一来源。

    行为：优先显示 `*_credential_issued_at`，为空（解不出 iat）回落记录时间。
    """

    def _src(self) -> str:
        return (
            FRONTEND / "src" / "components" / "settings" / "PanelComparisonPanel.tsx"
        ).read_text(encoding="utf-8")

    def test_row_interface_has_credential_issued_fields(self):
        src = self._src()
        self.assertIn("local_credential_issued_at", src, "行接口缺本地 AT 生成时间字段")
        self.assertIn("remote_credential_issued_at", src, "行接口缺远端 AT 生成时间字段")

    def test_local_column_prefers_credential_issued_at(self):
        src = self._src()
        block = src.split("title: '本地更新时间'", 1)[1].split("title: '远端更新时间'", 1)[0]
        self.assertIn(
            "local_credential_issued_at",
            block,
            "「本地更新时间」列没有用 AT 生成时间",
        )
        self.assertIn(
            "local_updated_at",
            block,
            "「本地更新时间」列丢了记录时间回落 —— 解不出 iat 的账号会显示空白",
        )

    def test_remote_column_prefers_credential_issued_at(self):
        src = self._src()
        block = src.split("title: '远端更新时间'", 1)[1].split("title: '远端信息'", 1)[0]
        self.assertIn(
            "remote_credential_issued_at",
            block,
            "「远端更新时间」列没有用 AT 生成时间",
        )
        self.assertIn(
            "remote_updated_at",
            block,
            "「远端更新时间」列丢了记录时间回落",
        )

    def test_column_titles_say_credential_generated(self):
        """两列的 tooltip 都要说明口径是 AT 生成时间（含回落分支）。

        用**精确串**（主分支 / 回落分支各一条）而不是「块里出现过」——
        注释里也会出现同样的词，计数式断言会被注释蒙混过关（实测变异验证
        抓到过：删掉主分支的说明后计数仍然 ≥2，因为注释与回落分支还在）。
        """
        src = self._src()
        local_block = src.split("title: '本地更新时间'", 1)[1].split("title: '远端更新时间'", 1)[0]
        remote_block = src.split("title: '远端更新时间'", 1)[1].split("title: '远端信息'", 1)[0]
        # 主分支（解出 iat）
        self.assertIn("本地 AT 生成时间（JWT iat）", local_block, "本地列 tooltip 没说口径是 AT 生成时间")
        self.assertIn("远端 AT 生成时间（JWT iat）", remote_block, "远端列 tooltip 没说口径是 AT 生成时间")
        # 回落分支（解不出 iat）
        self.assertIn("本地没有可解析的 AT 生成时间", local_block, "本地列回落分支没说明显示的是记录时间")
        self.assertIn("远端没有可解析的 AT 生成时间", remote_block, "远端列回落分支没说明显示的是记录时间")

    def test_time_tooltip_explains_mixed_basis(self):
        """「时间」列 tooltip 要说明单侧回落（mixed）—— 一侧有 iat、一侧没有。

        用户实测报的 bug（2026-10-05）：grok2api web 线账号远端只有 SSO，
        判定曾整体回落记录时间、拿状态回写的噪声把方向判反。修复后每侧独立
        「iat 优先、无则回落该侧记录时间」，basis=mixed 表示这种单侧情况 ——
        tooltip 必须能向用户解释清楚，不能只留 credential/record 两档。
        """
        src = self._src()
        block = src.split("title: '时间'", 1)[1].split("title: '差异'", 1)[0]
        self.assertIn(
            "time_basis === 'mixed'",
            block,
            "「时间」列没有处理 mixed 档 —— 单侧回落的行 tooltip 会误导",
        )


class UIScaleContractTests(unittest.TestCase):
    """全站 90% 缩放（用户要求「UI按90%进行缩小一点」）。

    实现走 CSS `zoom`（html 级）：布局级缩放、文字不模糊（transform: scale
    会光栅化重采样），弹层/真实点击/媒体查询不受影响（Chrome 145 实测）。

    实测坑（Chrome 145）：
    * zoom 下 `100vh` 先按**物理视口**解析再被缩放 → 整屏高度比视口矮 10%
      （577px 视口下渲染 519px，底部露出背景）。所有整屏高度必须走
      `--app-vh: calc(100vh / var(--ui-scale))`（实测恰好填满视口）。
    * `getBoundingClientRect` 在 zoom 下返回缩放后坐标，但 rc-trigger 的
      弹层定位（Select/Popconfirm/Tooltip）与 CDP 真实点击均正确（实测）。

    form-grid 宽度类的修复：`flex: 0 0 auto` 下 max-width 不决定宽度，
    控件按内容 intrinsic 排 —— 实测同档控件 98/82/286/187 各不相同。
    """

    def _css(self) -> str:
        return (FRONTEND / "src" / "index.css").read_text(encoding="utf-8")

    def test_ui_scale_token_defined_and_applied(self):
        css = self._css()
        self.assertIn("--ui-scale: 0.9", css, "index.css 缺 --ui-scale: 0.9")
        self.assertIn("zoom: var(--ui-scale)", css, "没有把 zoom 应用到 html 上")

    def test_app_vh_compensates_zoom(self):
        """整屏高度要用 --app-vh 补偿，否则底部露出 10% 背景（实测）。"""
        css = self._css()
        self.assertIn(
            "--app-vh: calc(100vh / var(--ui-scale))",
            css,
            "缺 --app-vh 补偿定义",
        )
        self.assertIn("min-height: var(--app-vh)", css, "#root 没有用 --app-vh")
        self.assertIn("height: var(--app-vh)", css, "侧栏没有用 --app-vh")

    def test_tsx_full_height_uses_app_vh(self):
        """App.tsx / Login.tsx 的整屏高度不许再用裸 100vh。"""
        app = (FRONTEND / "src" / "App.tsx").read_text(encoding="utf-8")
        self.assertNotIn("'100vh'", app, "App.tsx 还有裸 100vh —— zoom 下会矮 10%")
        self.assertIn("'var(--app-vh)'", app, "App.tsx 没接 --app-vh")
        login = (FRONTEND / "src" / "pages" / "Login.tsx").read_text(encoding="utf-8")
        self.assertNotIn("'100vh'", login, "Login.tsx 还有裸 100vh")
        self.assertIn("'var(--app-vh)'", login, "Login.tsx 没接 --app-vh")

    def test_index_css_100vh_only_in_app_vh_token(self):
        """index.css 里 100vh 只允许出现在 --app-vh 定义行。

        只查「声明行」（以 ; 结尾）—— 注释里提到 100vh 是解释性文字，
        不是会被浏览器解析的规则。
        """
        for i, line in enumerate(self._css().splitlines(), 1):
            if "100vh" not in line or not line.strip().endswith(";"):
                continue
            self.assertIn(
                "--app-vh",
                line,
                f"index.css:{i} 有裸 100vh 声明：{line.strip()}",
            )

    def test_form_grid_tier_fields_share_width_evenly(self):
        """form-grid 同档字段等宽平分（封顶=档位宽度）。

        dogfood 实测（2026-10-05）：旧规则 `flex: 0 0 auto` 让子项按内容
        intrinsic 排 —— 「平台 99 / 执行器 286 / 验证码 187」「批量数量 98 /
        并发数 82」同档控件各不相同。修复：`flex: 1 1 0` 平分 + `max-width`
        封顶到档位 token。窄屏（<992px）由媒体查询放开封顶并堆叠。
        """
        css = self._css()
        for cls, token in (
            ("form-field--sm", "--w-field"),
            ("form-field--md", "--w-field-md"),
            ("form-field--lg", "--w-field-lg"),
        ):
            marker = f".form-grid > .{cls}"
            idx = css.find(marker)
            self.assertGreater(idx, -1, f"缺 {marker} 规则")
            block = css[idx : idx + 200]
            self.assertIn(
                f"max-width: var({token})",
                block,
                f"{marker} 没有档位封顶",
            )
            self.assertIn(
                "flex: 1 1 0",
                block,
                f"{marker} 没有等分 —— 同档字段会按内容宽度散乱",
            )
        # 窄屏要放开档位封顶（否则媒体查询里的 max-width:100% 被更高
        # 特异性的 `.form-grid > .form-field--*` 规则压掉，实测窄屏仍限宽）
        media_idx = css.find("@media (max-width: 991px)")
        self.assertGreater(media_idx, -1, "找不到 <992px 媒体查询")
        media = css[media_idx:]
        for cls in ("form-field--sm", "form-field--md", "form-field--lg"):
            self.assertIn(
                f".form-grid > .{cls}",
                media,
                f"窄屏没有放开 {cls} 的档位封顶",
            )


if __name__ == "__main__":
    unittest.main()
