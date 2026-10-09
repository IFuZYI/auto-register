"""远程 iCloud 渠道的前端接线契约。

前端没有单元测试运行器，跨文件一致性靠这类契约测试守住（见
docs/MAINTENANCE.md §3）。本文件钉住「远程仓库」功能的前端接线：
- 注册页邮箱服务下拉里有 `icloud_remote`（否则渠道建不出来）；
- 注册页与账号页都提交 `icloud_remote_*` 三个配置键（否则选了也没用）；
- iCloud 页面有来源标签列、来源筛选、添加远程服务入口；
- 别名字段 `source` 进了前端类型与筛选规则（本地优先去重后仍可筛）。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend" / "src"


def _read(rel: str) -> str:
    return (FRONTEND / rel).read_text(encoding="utf-8")


class TestRegisterPage:
    def test_provider_option_present(self):
        src = _read("pages/RegisterTaskPage.tsx")
        assert "'icloud_remote'" in src, "注册页邮箱服务下拉缺少 icloud_remote"
        assert "iCloud 隐私邮箱（远程）" in src, "下拉项要有中文显示名"

    def test_register_extra_carries_remote_keys(self):
        src = _read("pages/RegisterTaskPage.tsx")
        for key in ("icloud_remote_account_id", "icloud_remote_label", "icloud_remote_note"):
            assert key in src, f"注册页没有提交 {key}"

    def test_remote_account_selector_wired(self):
        """选了远程渠道要能挑远端账号（与本地渠道同款下拉）。"""
        src = _read("pages/RegisterTaskPage.tsx")
        assert "mailProvider === 'icloud_remote'" in src, "没有为远程渠道渲染专属字段"
        # 远程账号下拉的数据来源
        assert "listICloudAccounts" in src

    def test_accounts_page_quick_register_carries_remote_keys(self):
        src = _read("pages/Accounts.tsx")
        for key in ("icloud_remote_account_id", "icloud_remote_label", "icloud_remote_note"):
            assert key in src, f"账号页快速注册没有提交 {key}"


class TestICloudPage:
    def test_source_column_present(self):
        src = _read("pages/ICloud.tsx")
        assert "title: '来源'" in src, "主号表/别名表缺少「来源」列（用户要求标注本地/远程）"

    def test_source_filter_present(self):
        src = _read("pages/ICloud.tsx")
        assert 'placeholder="全部来源"' in src, "缺少来源筛选下拉"
        assert "setFilterSource" in src and "filterSource" in src

    def test_add_remote_service_entry(self):
        src = _read("pages/ICloud.tsx")
        assert "添加远程服务" in src, "缺少添加远程服务入口"
        assert "AddRemoteServiceModal" in src, "弹窗没有接线"

    def test_remote_accounts_use_dedicated_labels(self):
        """远程行不能显示成「Web Session 缺失」这类误导性标签。"""
        src = _read("pages/ICloud.tsx")
        assert "远端凭据" in src, "远程账号的会话状态列应显示「远端凭据」"
        assert "远端管理" in src, "远程账号的额度列应显示「远端管理」"


class TestAliasType:
    def test_source_field_in_type(self):
        src = _read("api/icloud.ts")
        assert re.search(r"source:\s*'local'\s*\|\s*'remote'", src), "ICloudAlias 缺少 source 字段"

    def test_source_filter_rule_in_lib(self):
        src = _read("lib/icloud.ts")
        assert "source" in src, "筛选规则没有来源维度"
        assert "filter.source" in src, "filterAliases 没有应用来源筛选"
