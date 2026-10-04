"""CPA 面板的平台选择（全部 / ChatGPT / Grok）。

用户要求：「CPA 面板 增加 平台选择，比如选择 全部、gpt、grok。」

为什么必须有：CPA（CLIProxyAPI）同时托管 ChatGPT（`codex`）与 Grok（`xai`）
两类凭据，对比表把两边混在一张表里 —— 实测线上 213 行里 181 行是 ChatGPT、
32 行是 Grok。没有平台选择时：

- 用户想「把 Grok 那 32 个未上传的传上去」，得在 181 行无关行里翻找；
- 筛选条与批量按钮上的数字是**两个平台的和**，点「上传未上传 (32)」时看不出
  这 32 个到底是哪个平台的。

口径：平台选择只对**多平台面板**出现（`platforms.length > 1`）。单平台面板
（Sub2API / grok2api / chatgpt2api）不渲染 —— 给它们摆一个只有「全部」一个
选项的选择器是纯噪声。

筛选要**同时**作用在三处，漏一处就会出现「看到的和传出去的不是同一批」：
1. 表格行；2. 筛选条计数；3. 批量动作的账号 id 集合。

另有**方向**筛选（`selectLocalNewerDiffIds` / `selectRemoteNewerDiffIds`）：
上传/拉回是一对对称动作 —— 上传只推「本地较新」的行，拉回只拉「远端较新」
的行。写错方向等于用旧凭证覆盖新的（x.ai 的 RT 每次刷新都轮换，覆盖后
持有方拿到死值）。

测试分两层（与 `test_account_format_contract.py` 同一套做法）：
- 行为层：node + rolldown 真实执行 `frontend/src/lib/panelComparison.ts` 的纯函数；
- 接线层：静态断言页面确实调了这些函数、并把 `platforms` 透传下去。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
PANEL = FRONTEND / "src" / "components" / "settings" / "PanelComparisonPanel.tsx"
PAGE = FRONTEND / "src" / "pages" / "PanelManagement.tsx"
LIB = FRONTEND / "src" / "lib" / "panelComparison.ts"
HARNESS = FRONTEND / "scripts" / "run_panel_filter_checks.mjs"


class PanelPlatformFilterBehaviorTests(unittest.TestCase):
    """真实执行纯函数（有 node + rolldown 时）。"""

    def test_pure_helpers_behave_as_documented(self):
        if shutil.which("node") is None:
            self.skipTest("未安装 node —— 跳过前端纯函数行为检查")
        if not (FRONTEND / "node_modules" / "rolldown").exists():
            self.skipTest("frontend/node_modules/rolldown 不存在（未 npm install）")

        result = subprocess.run(
            ["node", str(HARNESS)],
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertTrue(
            result.stdout.strip(),
            f"harness 没有输出：{result.stderr[-800:]}",
        )
        report = json.loads(result.stdout)
        self.assertTrue(
            report["passed"],
            "平台筛选纯函数行为不符：\n" + "\n".join(report["failures"]),
        )
        self.assertGreaterEqual(report["checked"], 12, "断言条数太少，网太稀")


class PanelPlatformFilterWiringTests(unittest.TestCase):
    """接线：页面真的用了这些函数，而不是只在 lib 里写了一份。"""

    def _panel(self) -> str:
        return PANEL.read_text(encoding="utf-8")

    def _page(self) -> str:
        return PAGE.read_text(encoding="utf-8")

    def test_rules_live_in_lib(self):
        """规则本体在 lib（可复用、可单测），组件只做接线。"""
        lib = LIB.read_text(encoding="utf-8")
        for fn in ("filterRowsByPlatform", "countByPlatform", "summarizeRows"):
            self.assertIn(f"export function {fn}", lib, f"panelComparison.ts 缺 {fn}")

    def test_panel_renders_the_platform_selector(self):
        """面板要有平台选择器，且「全部」是第一个选项。"""
        src = self._panel()
        self.assertIn("filterRowsByPlatform", src, "对比面板没接平台筛选")
        self.assertIn("platformFilter", src, "没有平台筛选的 state")
        # 钉住选择器本体：`全部` 单独出现不算 —— 状态筛选条上也有「全部 N」。
        self.assertIn(
            'aria-label="按平台筛选"',
            src,
            "平台选择器没渲染（状态筛选条上的「全部」不算选择器）",
        )

    def test_selector_only_shows_for_multi_platform_panels(self):
        """单平台面板不渲染选择器 —— 只有一个「全部」选项是纯噪声。

        钉的是**调用点**（`shouldShowPlatformFilter(platforms)`），不是 import：
        只断言 import 的话，把条件删掉（无条件渲染）测试也照样绿。
        """
        src = self._panel()
        self.assertIn(
            "shouldShowPlatformFilter(platforms)",
            src,
            "要用 shouldShowPlatformFilter 把关，否则单平台面板也会摆一个没用的选择器",
        )

    def test_page_passes_the_platforms_list_down(self):
        """面板管理页要把注册表声明的 `platforms` 透传给对比组件。

        不透传的话组件只知道「一个」平台（`platform` 兼容字段是 chatgpt），
        于是 CPA 的平台选择器永远不会出现。
        """
        src = self._page()
        self.assertIn("platforms={selectedPanel.platforms", src)

    def test_batch_actions_read_the_filtered_rows(self):
        """批量动作的 id 集合必须来自**平台筛选后**的行。

        读原始 `payload.rows` 的话，用户筛了 Grok 却把 ChatGPT 的一起传了。
        """
        src = self._panel()
        for name in ("unuploadedIds", "localNewerIds", "remoteNewerIds"):
            block = src.split(f"const {name} = useMemo(", 1)
            self.assertEqual(len(block), 2, f"找不到 {name} 的定义")
            # 定义体到依赖数组（`\n    [`）为止。切在依赖数组之前很重要 ——
            # 不切的话「platformRows」会从 `[platformRows]` 依赖里匹配到，
            # 断言恒真（变异验证抓到过：把 filter 源换回 payload?.rows 仍全绿）。
            rest = block[1]
            self.assertEqual(
                len(rest.split("\n    [", 1)), 2,
                f"{name} 的依赖数组格式变了（找不到 `\\n    [`），"
                "测试的切分口径失效，请同步更新",
            )
            body = rest.split("\n    [", 1)[0]
            self.assertIn(
                "platformRows",
                body,
                f"{name} 读的不是平台筛选后的行 —— 筛选与批量动作会对不上",
            )
            self.assertNotIn(
                "payload?.rows",
                body,
                f"{name} 直接读了原始行 —— 平台筛选被绕过，会传错平台的账号",
            )

    def test_upload_and_pull_use_the_direction_helpers(self):
        """上传/拉回两个方向必须走 lib 的纯函数（组件里不再自己判方向）。

        方向判定是「谁较新就动谁」的核心：写错方向 = 用旧凭证覆盖新的
        （x.ai 的 RT 每次刷新都轮换，覆盖后持有方拿到死值）。抽到 lib 才能被
        `run_panel_filter_checks.mjs` 真实执行验证。
        """
        src = self._panel()
        self.assertIn(
            "selectLocalNewerDiffIds(platformRows)", src,
            "上传方向没接纯函数（selectLocalNewerDiffIds）",
        )
        self.assertIn(
            "selectRemoteNewerDiffIds(platformRows)", src,
            "拉回方向没接纯函数（selectRemoteNewerDiffIds）",
        )
        # 组件里不该再有「不判方向就上传」的凭证过滤 —— 那正是被修掉的 bug。
        self.assertNotIn(
            "row.state === 'credential_diff' && row.local_id",
            src,
            "出现了不带时间条件的凭证过滤（会把本地旧凭证推上去覆盖远端新的）",
        )

    def test_table_key_includes_platform(self):
        """`rowKey` 必须带平台。

        同一个邮箱在 CPA 上可能同时有 codex 与 xai 两条（邮箱池按平台消耗），
        选「全部」时两行的 `email` 相同 —— 只用 email 当 key 会让 React 认成
        同一个节点（渲染错行 + key 冲突警告）。
        """
        src = self._panel()
        key_block = src.split("rowKey={", 1)[1].split("}", 1)[0]
        self.assertIn("platform", key_block, "rowKey 只用 email 会在双平台同邮箱时撞 key")


if __name__ == "__main__":
    unittest.main()
