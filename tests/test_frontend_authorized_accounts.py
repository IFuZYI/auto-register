"""前端接线契约：AWS 从「平台管理」移到「授权账号」页。

这是纯源码契约（不跑浏览器）：改菜单、路由或平台下拉时，这里会指出漏改的一侧。

* `App.tsx`：菜单里有 `/authorized-accounts`，且「平台管理」的二级项过滤掉 aws；
  路由把 `/accounts/aws` 重定向过去。
* `platforms.ts`：注册页的 `PLATFORM_OPTIONS` 不再包含 aws，但筛选用的
  `PLATFORM_FILTER_OPTIONS` 仍然包含（历史任务/邮箱池里 AWS 仍是合法来源）。
* `AuthorizedAccounts.tsx`：确实用 `platform: 'aws'` 起注册任务，并给出
  Kiro 派生链（`source_email`）。
"""
from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


class AuthorizedAccountsWiringTests(unittest.TestCase):
    def _read(self, relative: str) -> str:
        return (FRONTEND / relative).read_text(encoding="utf-8")

    def test_menu_and_route_are_wired(self):
        source = self._read("App.tsx")
        # 一级「授权账号」是父菜单，AWS 是它的子项
        self.assertIn("key: '/authorized-accounts'", source)
        self.assertIn("label: '授权账号'", source)
        self.assertIn("key: '/authorized-accounts/aws'", source)
        self.assertIn("label: 'AWS'", source)
        self.assertIn('path="/authorized-accounts/aws"', source)
        # 两条旧入口各自重定向；分开断言，删掉任一条都会红
        self.assertIn(
            '<Route path="/authorized-accounts" element={<Navigate to="/authorized-accounts/aws"',
            source,
            "/authorized-accounts 应重定向到子项",
        )
        self.assertIn(
            '<Route path="/accounts/aws" element={<Navigate to="/authorized-accounts/aws"',
            source,
            "/accounts/aws 应重定向到子项",
        )

    def test_platform_menu_excludes_aws(self):
        source = self._read("App.tsx")
        self.assertIn("p.key !== 'aws'", source, "平台管理的二级菜单仍在列 AWS")

    def test_platform_options_exclude_aws_but_filters_keep_it(self):
        source = self._read("lib/platforms.ts")
        self.assertIn(".filter(({ name }) => name !== 'aws')", source)
        # 筛选清单必须从**未过滤**的 PLATFORMS 生成，不能复用 PLATFORM_OPTIONS
        # （曾经因为复用，AWS 从任务历史/邮箱筛选里消失，旧断言是恒真的）。
        block = source.split("export const PLATFORM_FILTER_OPTIONS")[1].split("]")[0]
        self.assertIn("Object.values(PLATFORMS)", block)
        self.assertNotIn("PLATFORM_OPTIONS", block)

    def test_aws_list_shows_registered_platforms(self):
        source = self._read("pages/AuthorizedAccounts.tsx")
        self.assertIn("已注册平台", source)
        self.assertIn("Kiro", source)

    def test_kiro_registration_offers_aws_source_choices(self):
        source = self._read("pages/RegisterTaskPage.tsx")
        self.assertIn("kiro_source", source)
        self.assertIn("kiro_aws_email", source)
        self.assertIn("kiro_auto_register", source)
        self.assertIn("复用未注册 Kiro 的 AWS", source)
        self.assertIn("指定 AWS 账号", source)
        self.assertIn("新建 AWS + Kiro", source)
        self.assertIn("没有可用 AWS 时自动注册", source)

    def test_selected_key_handles_authorized_subpaths(self):
        """侧栏选中态：/authorized-accounts/* 要同时选父项与子项。"""
        source = self._read("App.tsx")
        block = source.split("function getSelectedKey")[1].split("return ['/']")[0]
        self.assertIn("key.startsWith('/authorized-accounts/')", block)

    def test_kiro_export_credentials_ui(self):
        """Kiro 的「导出凭据」：格式、下载名、仅 kiro 可见。"""
        source = self._read("pages/Accounts.tsx")
        self.assertIn("kiro_export", source)
        self.assertIn("format: 'kiro_json'", source)
        self.assertIn("link.download = 'kiro.json'", source)
        self.assertIn("currentPlatform === 'kiro'", source)

    def test_page_registers_aws_and_shows_the_derivation_chain(self):
        source = self._read("pages/AuthorizedAccounts.tsx")
        self.assertIn("platform: 'aws'", source)
        self.assertIn("/tasks/register", source)
        self.assertIn("source_email", source, "授权链要按 Kiro 的 source_email 归组")
        self.assertIn("authorized", source.lower())


class TaskPagePlatformWiringTests(unittest.TestCase):
    """任务页（运行中 / 历史）必须能显示并筛选 AWS / Kiro。

    平台筛选与标签都来自 `lib/platforms.ts`：筛选下拉用 `PLATFORM_FILTER_OPTIONS`
    （从**未过滤**的 `PLATFORMS` 生成），平台列用 `getPlatformLabel`。任何一侧
    缺 aws / kiro，任务页上这两个平台就是原始值 `aws` 或干脆选不到 —— 这条契约
    把「页面接了共用表」和「共用表里有这两个平台」一起钉住。
    """

    def _read(self, relative: str) -> str:
        return (FRONTEND / relative).read_text(encoding="utf-8")

    def test_task_history_filters_and_labels_by_shared_platform_table(self):
        source = self._read("pages/TaskHistory.tsx")
        self.assertIn(
            "PLATFORM_FILTER_OPTIONS",
            source,
            "任务历史没有用共用的平台筛选表 —— AWS / Kiro 可能选不到",
        )
        self.assertIn(
            "options={PLATFORM_FILTER_OPTIONS}",
            source,
            "筛选下拉没把 PLATFORM_FILTER_OPTIONS 接到 Select 上",
        )
        self.assertIn("getPlatformLabel", source, "任务历史的平台列没用共用标签")

    def test_running_tasks_labels_by_shared_platform_table(self):
        source = self._read("pages/RunningTasks.tsx")
        self.assertIn("getPlatformLabel", source, "运行中任务的平台标签没用共用标签")

    def test_platform_table_contains_aws_and_kiro(self):
        """筛选选项与标签都从 PLATFORMS 展开 —— 少了平台，标签回落成原始值。"""
        source = self._read("lib/platforms.ts")
        block = source.split("export const PLATFORMS", 1)[1].split(
            "export const PLATFORM_OPTIONS", 1
        )[0]
        for name, label in (("aws", "AWS"), ("kiro", "Kiro")):
            self.assertIn(f"{name}: {{", block, f"PLATFORMS 里没有 {name}")
            self.assertIn(
                f"label: '{label}'", block, f"PLATFORMS 里 {name} 的标签不对"
            )


if __name__ == "__main__":
    unittest.main()
