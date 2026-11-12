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
        self.assertIn('to="/authorized-accounts/aws"', source, "/authorized-accounts 应重定向到子项")
        self.assertIn('to="/authorized-accounts/aws"', source, "/accounts/aws 应重定向")

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

    def test_page_registers_aws_and_shows_the_derivation_chain(self):
        source = self._read("pages/AuthorizedAccounts.tsx")
        self.assertIn("platform: 'aws'", source)
        self.assertIn("/tasks/register", source)
        self.assertIn("source_email", source, "授权链要按 Kiro 的 source_email 归组")
        self.assertIn("authorized", source.lower())


if __name__ == "__main__":
    unittest.main()
