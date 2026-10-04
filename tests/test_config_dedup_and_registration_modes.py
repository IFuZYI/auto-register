"""两个界面问题的回归：

① CLIProxyAPI 与 CPA 面板是同一个服务（同一端点、同一套配置键），
   不能在两处各配一份。
② 各平台的「注册方式」由插件声明、随 /api/platforms 下发，
   界面不硬编码 —— 否则界面显示「支持」而运行时静默忽略。
"""
from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class LegacyValueMigrationTests(unittest.TestCase):
    """注册方式（`grok_register_mode`）并入执行器后的老数据迁移。

    实测场景（本机库里就是这样）：用户存了 `grok_register_mode='browser'`，
    新界面只读 `grok_executor`（空）→ 回落到已取消的全局 `default_executor`
    （'protocol'）→ 把默认任务打进协议路径，而那条路在 x.ai 上会被 CF 403。
    迁移必须发生在 `get_all()`（所有读取方的必经之路）。
    """

    def test_legacy_register_mode_migrates_to_executor(self):
        from core.config_store import _migrate_legacy_values

        values = {"grok_register_mode": "browser", "grok_executor": ""}
        _migrate_legacy_values(values)
        self.assertEqual(values["grok_executor"], "browser",
                         "老数据只填过注册方式时应迁移到执行器")
        # 旧键保留 —— Grok 的兜底链仍会读它（执行器未显式指定时）
        self.assertEqual(values["grok_register_mode"], "browser")

    def test_new_key_wins_over_legacy(self):
        """用户在执行器上的新选择优先，不能被旧值覆盖。"""
        from core.config_store import _migrate_legacy_values

        values = {"grok_register_mode": "protocol", "grok_executor": "browser"}
        _migrate_legacy_values(values)
        self.assertEqual(values["grok_executor"], "browser")

    def test_empty_legacy_is_noop(self):
        from core.config_store import _migrate_legacy_values

        values = {"grok_register_mode": "", "grok_executor": ""}
        _migrate_legacy_values(values)
        self.assertEqual(values["grok_executor"], "")

    def test_get_all_applies_the_migration(self):
        """迁移必须挂在 get_all() 上 —— 它是所有读取方的必经之路。"""
        from unittest.mock import patch

        from core.config_store import ConfigStore

        with patch.object(ConfigStore, "_ConfigStore__dict__", create=True), \
             patch("core.config_store._merge_env_fallback",
                   return_value={"grok_register_mode": "browser", "grok_executor": ""}):
            values = ConfigStore.get_all(ConfigStore.__new__(ConfigStore))
        self.assertEqual(values["grok_executor"], "browser")


class DuplicateServiceKeysTests(unittest.TestCase):
    """① `cpa_*` 与 `cliproxyapi_*` 是同一服务，读取时必须收敛。"""

    def test_alias_collapses_to_canonical(self):
        from core.config_store import _collapse_duplicate_service_keys as collapse

        values = {"cliproxyapi_base_url": "http://b", "cpa_api_url": ""}
        collapse(values)
        self.assertEqual(values["cpa_api_url"], "http://b",
                         "老数据只填过别名键时应迁移到规范键")
        self.assertEqual(values["cliproxyapi_base_url"], "http://b")

    def test_canonical_wins_on_conflict(self):
        from core.config_store import _collapse_duplicate_service_keys as collapse

        values = {"cpa_api_url": "http://new", "cliproxyapi_base_url": "http://old"}
        collapse(values)
        self.assertEqual(values["cpa_api_url"], "http://new")
        self.assertEqual(values["cliproxyapi_base_url"], "http://new",
                         "别名键要跟着规范键，否则直读别名的代码拿不到新值")

    def test_secret_key_also_collapses(self):
        from core.config_store import _collapse_duplicate_service_keys as collapse

        values = {"cliproxyapi_management_key": "secret1"}
        collapse(values)
        self.assertEqual(values["cpa_api_key"], "secret1")

    def test_both_empty_is_noop(self):
        from core.config_store import _collapse_duplicate_service_keys as collapse

        values = {"cpa_api_url": "", "cliproxyapi_base_url": ""}
        collapse(values)
        self.assertEqual(values["cpa_api_url"], "")

    def test_get_all_applies_the_collapse(self):
        """迁移必须挂在 get_all() 上 —— 它是所有读取方的必经之路。"""
        from core.config_store import ConfigStore

        with patch.object(ConfigStore, "_ConfigStore__dict__", create=True), \
             patch("core.config_store._merge_env_fallback",
                   return_value={"cliproxyapi_base_url": "http://migrated",
                                 "cpa_api_url": ""}):
            values = ConfigStore.get_all(ConfigStore.__new__(ConfigStore))
        self.assertEqual(values["cpa_api_url"], "http://migrated")


class PanelRegistryContractTests(unittest.TestCase):
    """① 面板注册表里 CLIProxyAPI 与 CPA 面板是**一项**，不是两项。"""

    def test_cliproxyapi_is_not_a_separate_panel(self):
        """`cliproxyapi` 不再单开一项 —— 它与 CPA 面板是同一个服务。

        两个卡片并排摆着只会让人以为是两个服务、各配一次（历史上就是两套
        配置键，改了一个另一个不生效）。合并成 `cpa` 一项后，
        `resolve_panel_key` 仍认旧 key，老链接不会 404。
        """
        from services.panel_registry import PANELS_BY_KEY, resolve_panel_key

        self.assertNotIn("cliproxyapi", PANELS_BY_KEY, "不该再单开一项")
        self.assertEqual(resolve_panel_key("cliproxyapi"), "cpa",
                         "旧 key 要归一到 cpa（老链接/老脚本）")

    def test_cpa_panel_carries_the_shared_keys(self):
        """合并后：地址与口令键不变（老数据仍读得到），口令字段在 CPA 项上。"""
        from services.panel_registry import PANELS_BY_KEY

        panel = PANELS_BY_KEY["cpa"]
        self.assertEqual(panel["url_key"], "cpa_api_url")
        self.assertEqual(panel["secret_key"], "cpa_api_key")

    def test_cpa_opts_out_of_panel_launcher_form(self):
        """面板管理页不渲染配置表单（配置统一在「全局配置 → 面板配置」）。"""
        from services.panel_registry import PANELS_BY_KEY

        self.assertIs(PANELS_BY_KEY["cpa"].get("config_form"), False)

    def test_panel_keys_are_all_resolvable(self):
        """每个现存 key 都能被 `resolve_panel_key` 原样解析。"""
        from services.panel_registry import PANELS, resolve_panel_key

        for panel in PANELS:
            self.assertEqual(resolve_panel_key(panel["key"]), panel["key"])

    def test_no_undeclared_url_key_sharing_after_merge(self):
        """合并后：url_key 必须唯一 —— 共享靠「并成一项」实现，不再靠声明。

        历史：`cliproxyapi` 曾是与 `cpa` 并排的第二项，靠 `same_service_as`
        声明共享同一个 url_key。现在两项已并成一项，声明机制随之退场；
        留下的这条断言守住「不许再有意外重复」。
        """
        from services.panel_registry import PANELS

        self.assertFalse(
            [p for p in PANELS if p.get("same_service_as")],
            "同服务的面板应当并成一项，而不是靠 same_service_as 声明共享",
        )
        keys = [p["url_key"] for p in PANELS]
        dupes = {k for k in keys if keys.count(k) > 1}
        self.assertFalse(dupes, f"面板 url_key 重复: {sorted(dupes)}")


class RegistrationModesDeclarationTests(unittest.TestCase):
    """② 各平台自己声明注册方式，后端随 /api/platforms 下发。"""

    @classmethod
    def setUpClass(cls):
        from core.registry import load_all

        load_all()

    def test_platforms_endpoint_exposes_registration_modes(self):
        from core.registry import list_platforms

        platforms = {p["name"]: p for p in list_platforms()}
        for name in ("grok", "chatgpt"):
            self.assertIn(name, platforms)
            modes = platforms[name].get("registration_modes")
            self.assertIsInstance(modes, list)
            self.assertTrue(modes, f"{name} 应声明至少一个注册方式")

    def test_each_mode_has_required_shape(self):
        from core.registry import list_platforms

        for p in list_platforms():
            for mode in p.get("registration_modes") or []:
                self.assertTrue(mode.get("key"), f"{p['name']} 的注册方式缺 key")
                self.assertTrue(mode.get("label"), f"{p['name']} 的注册方式缺 label")
                # 两种形状：下拉（有 options）或数值（type: 'number'）。
                # 数值项没有选项表 —— 断言它必须有 options 会把节流字段判死。
                if mode.get("type") == "number":
                    self.assertIsInstance(mode.get("min"), int,
                                          f"{p['name']}.{mode['key']} 是数值字段，应有 min")
                    self.assertIsNotNone(mode.get("default"),
                                         f"{p['name']}.{mode['key']} 是数值字段，应有 default")
                    continue
                options = mode.get("options")
                self.assertIsInstance(options, list)
                self.assertTrue(options, f"{p['name']}.{mode['key']} 没有选项")
                for opt in options:
                    self.assertIn("value", opt)
                    self.assertIn("label", opt)
                # default 必须是自己声明的合法取值之一，否则界面会显示一个
                # 选不中的默认值
                if mode.get("default") is not None:
                    values = [o["value"] for o in options]
                    self.assertIn(mode["default"], values,
                                  f"{p['name']}.{mode['key']} 的 default "
                                  f"{mode['default']!r} 不在选项里: {values}")

    def test_grok_declares_its_modes(self):
        from core.registry import list_platforms

        grok = next(p for p in list_platforms() if p["name"] == "grok")
        keys = {m["key"] for m in grok["registration_modes"]}
        # 只剩发码节流一个数值旋钮：
        # - 注册方式（browser/protocol）已并入「执行器」—— 那是「怎么访问目标站」，
        #   与执行器是同一个问题；
        # - `grok_send_code_mode` / `grok_send_code_timeout` 随协议路径删除 ——
        #   它们只被那条路读取（浏览器路径自己在页面发码），留着就是死旋钮。
        self.assertEqual(keys, {"grok_send_code_min_interval"})

    def test_grok_executor_carries_the_register_mode(self):
        """注册方式并入执行器；协议路径删除后只剩 browser 一个取值。"""
        from core.registry import list_platforms

        grok = next(p for p in list_platforms() if p["name"] == "grok")
        self.assertEqual(
            grok["supported_executors"], ["browser"],
            "协议路径已删除（x.ai 发码假接受 + 验码 grpc=3），只剩浏览器一条路",
        )
        labels = grok["executor_labels"]
        self.assertTrue(labels.get("browser"), "执行器 browser 缺显示名")

    def test_grok_throttle_fields_are_numeric(self):
        """节流是数值旋钮，不能按下拉渲染（否则界面上是空下拉，选了不生效）。"""
        from core.registry import list_platforms

        grok = next(p for p in list_platforms() if p["name"] == "grok")
        by_key = {m["key"]: m for m in grok["registration_modes"]}
        for key in ("grok_send_code_min_interval",):
            field = by_key[key]
            self.assertEqual(field.get("type"), "number", f"{key} 应是数值字段")
            self.assertIsInstance(field.get("min"), int)
            # 数值字段没有 options —— 契约测试不能要求它有
            self.assertNotIn("options", field)

    def test_declared_defaults_match_runtime_fallbacks(self):
        """声明的默认值必须与运行时兜底一致。

        这是本机制最容易出的错：声明 `default: "ui"` 而运行时兜底是
        `protocol`，界面摘要就会显示「真实页面」而实际跑 gRPC ——
        正是「界面说 X、运行时做 Y」的分叉，也正是声明机制要防的东西。
        兜底值从插件源码里抓（它是唯一的权威来源）。
        """
        import re

        from core.registry import list_platforms

        src = (ROOT / "platforms/grok/plugin.py").read_text(encoding="utf-8")
        runtime_env = dict(re.findall(r'_env_default\("([A-Z_]+)", "([^"]*)"\)', src))

        grok = next(p for p in list_platforms() if p["name"] == "grok")
        by_key = {m["key"]: m for m in grok["registration_modes"]}

        # 注册方式（browser/protocol）现在由执行器承载：默认值必须与运行时一致。
        self.assertEqual(
            grok["supported_executors"][0], "browser",
            "Grok 的执行器默认必须是 browser（协议路径已删除）",
        )

        # 唯一的数值旋钮：运行时兜底写在表达式里（`_to_float(..., 0.0)`）
        m = re.search(
            r'_to_float\(\s*extra\.get\("grok_send_code_min_interval"\),\s*([\d.]+)',
            src,
        )
        if m is None:
            self.fail("找不到 grok_send_code_min_interval 的运行时兜底")
        self.assertEqual(
            float(m.group(1)), float(by_key["grok_send_code_min_interval"]["default"])
        )

    def test_chatgpt_declared_defaults_match_runtime(self):
        """ChatGPT 的两个声明默认也要与运行时常量一致。"""
        from core.registry import list_platforms
        from platforms.chatgpt.chatgpt_registration_mode_adapter import (
            DEFAULT_CHATGPT_REGISTER_FLOW,
            DEFAULT_CHATGPT_REGISTRATION_MODE,
        )

        chatgpt = next(p for p in list_platforms() if p["name"] == "chatgpt")
        by_key = {m["key"]: m for m in chatgpt["registration_modes"]}
        self.assertEqual(
            by_key["chatgpt_registration_mode"]["default"], DEFAULT_CHATGPT_REGISTRATION_MODE
        )
        self.assertEqual(
            by_key["chatgpt_register_flow"]["default"], DEFAULT_CHATGPT_REGISTER_FLOW
        )

    def test_mode_keys_are_writable_config(self):
        """声明了配置键就必须在白名单里，否则界面选了保存不生效。"""
        from api.config import CONFIG_KEYS
        from core.registry import list_platforms

        for p in list_platforms():
            for mode in p.get("registration_modes") or []:
                self.assertIn(mode["key"], CONFIG_KEYS,
                              f"{p['name']} 的注册方式 {mode['key']} 不在 "
                              f"CONFIG_KEYS 白名单里 —— 界面保存会被静默忽略")

    def test_chatgpt_modes_are_writable(self):
        """ChatGPT 的两个注册方式键也要能保存（本轮新加）。"""
        from api.config import CONFIG_KEYS

        for key in ("chatgpt_registration_mode", "chatgpt_register_flow"):
            self.assertIn(key, CONFIG_KEYS)


class FrontendWiringTests(unittest.TestCase):
    """界面接线：不在前端再硬编码一份注册方式选项。"""

    def test_config_fields_no_longer_hardcodes_grok_modes(self):
        src = (ROOT / "frontend/src/lib/configFields.ts").read_text(encoding="utf-8")
        self.assertNotIn("grok_register_mode:", src,
                         "grok 注册方式的选项应由插件声明，前端不再硬编码")
        self.assertNotIn("grok_send_code_mode:", src)

    def test_panel_config_drops_the_duplicate_grok_sections(self):
        src = (
            ROOT / "frontend/src/components/settings/PanelConfigPanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertNotIn("title: 'Grok 注册方式'", src,
                         "已移到「默认注册方式」，不能在两处各配一份")
        self.assertNotIn("title: 'Grok 发码方式'", src)

    def test_settings_renders_modes_from_platforms_api(self):
        src = (
            ROOT / "frontend/src/components/settings/RegisterSettingsPanel.tsx"
        ).read_text(encoding="utf-8")
        self.assertIn("registration_modes", src,
                      "各平台卡片要渲染插件声明的可配置项")
        self.assertIn("apiFetch('/platforms')", src,
                      "可配置项由 /api/platforms 下发，界面不硬编码")

    def test_panel_management_is_a_pure_launcher(self):
        """面板管理只跳转，不再渲染连接配置（配置统一在「全局配置 → 面板配置」）。"""
        src = (ROOT / "frontend/src/pages/PanelManagement.tsx").read_text(encoding="utf-8")
        self.assertNotIn("ConfigField", src,
                         "面板管理页不该再渲染配置表单 —— 同一个键出现两次会让人以为要分别填")
        self.assertIn("/settings?tab=panel", src,
                      "「去配置」要跳到全局配置的面板配置栏")

    def test_panel_config_lives_under_global_settings(self):
        """「面板配置」是全局配置下的一个 tab，不再是一级菜单页面。"""
        root = ROOT / "frontend/src"
        settings = (root / "pages/Settings.tsx").read_text(encoding="utf-8")
        app = (root / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("label: '面板配置'", settings)
        self.assertIn("PanelConfigPanel", settings)
        self.assertNotIn("'平台配置'", app, "一级菜单里不该再有「平台配置」")
        self.assertNotIn("'/platform-config',", app.split("MENU_KEYS")[1].split("])")[0],
                         "MENU_KEYS 里不该再有 /platform-config")
        # 旧路径重定向，老书签不 404
        self.assertIn('to="/settings?tab=panel"', app)

    def test_panel_config_page_is_gone(self):
        """旧的一级页面文件已删除（内容搬进 PanelConfigPanel）。"""
        root = ROOT / "frontend/src/pages"
        self.assertFalse((root / "PlatformConfig.tsx").exists())
        self.assertTrue((root.parent / "components/settings/PanelConfigPanel.tsx").exists())

    def test_panel_config_never_leaks_or_clobbers_secrets(self):
        """口令字段既不回填明文、也不被空值覆盖。

        服务端 `GET /api/config` 已对口令键打码（见 `tests/test_config_secret_masking.py`），
        前端仍要守住两件事，否则用户口令会被静默清空：
        1. 灌表单前把这些键清空（双保险，且页面状态明确）；
        2. 保存时把留空的键**删掉**，不能拿空串覆盖已存的口令
           —— 这一条由共享的 `dropEmptySecrets` 实现，三个写 `/api/config`
           的页面共用（见 `tests/test_secret_config_contract.py`）。

        两个来源的口令都要覆盖：注册表声明的（`secret_key`）与本节声明的
        （`secret: true`）—— 只收一个来源就会漏。
        """
        src = (
            ROOT / "frontend/src/components/settings/PanelConfigPanel.tsx"
        ).read_text(encoding="utf-8")

        self.assertIn("function secretFieldKeys", src,
                      "口令键必须由两个来源汇总，不能只扫注册表")
        self.assertIn("for (const { section } of PANEL_SECTIONS)", src,
                      "本节声明的 secret 字段（grok2api 密码等）也要收进来")
        self.assertIn("if (panel.secret_key) keys.add(panel.secret_key)", src)

        # 读：清空全部口令键（双保险）
        self.assertIn("for (const key of secretFieldKeys(items)) config[key] = ''", src,
                      "口令键必须清空后才灌表单")
        # 写：留空则删除，不能用空串覆盖 —— 走共享 helper
        self.assertIn("dropEmptySecrets(payload, secretFieldKeys(panels))", src,
                      "留空的口令必须从 payload 里删掉，否则空串会覆盖已存的口令")
        # 只读标记不能提交回去
        self.assertIn("stripSecretSetFlags", src,
                      "`<key>_set` 是只读标记，提交前要摘掉")


class SidebarOrderTests(unittest.TestCase):
    """侧边栏顺序：面板管理、代理管理排在任务历史**上面**。

    用户要求：「将面板管理、代理管理移到任务历史上面。」这两个是「配置类」
    入口（外部面板、代理池），任务历史是「回看类」—— 按操作频次把配置放前。
    """

    #: 期望的一级菜单顺序（`/mail` 的二级项会被正则一并抓到，见下）
    EXPECTED = [
        "/",
        "/running-tasks",
        "/accounts",
        "/mail",
        "/mail/icloud",
        "/mail/outlook",
        "/panel-management",
        "/proxies",
        "/history",
        "/settings",
    ]

    def _menu_keys_in_order(self) -> list[str]:
        import re

        src = (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")
        # menuItems 数组里的 `key: '/xxx',` 按出现顺序（含 /mail 的二级项）
        block = src.split("const menuItems = [")[1].split("\n  ]")[0]
        return re.findall(r"key: '(/[^']*)'", block)

    def test_panel_and_proxy_management_come_before_task_history(self):
        keys = self._menu_keys_in_order()
        for path in self.EXPECTED:
            self.assertIn(path, keys, f"一级菜单里找不到 {path}")

        self.assertLess(
            keys.index("/panel-management"),
            keys.index("/history"),
            "「面板管理」应排在「任务历史」上面",
        )
        self.assertLess(
            keys.index("/proxies"),
            keys.index("/history"),
            "「代理管理」应排在「任务历史」上面",
        )

    def test_overall_order_is_stable(self):
        """整条菜单的顺序钉住 —— 顺手挪动别的项也会被这条发现。"""
        self.assertEqual(self._menu_keys_in_order(), self.EXPECTED)


class PanelSubmenuTests(unittest.TestCase):
    """「面板管理」是二级菜单，子项是各面板 —— 与「平台管理」同一种结构。

    用户要求：「面板管理也像平台管理那样是二级菜单。」

    两个要点：
    ① 子项由 `/api/integrations/panels` 下发，不硬编码面板名（注册表加面板，
       菜单自动多一项）；
    ② 选中态由 **URL** 决定（`/panel-management/:panelKey`），不是组件内部
       state —— 侧栏高亮、浏览器前进后退、直接粘链接都要能对上同一个面板。
    """

    def _app_src(self) -> str:
        return (ROOT / "frontend/src/App.tsx").read_text(encoding="utf-8")

    def _page_src(self) -> str:
        return (ROOT / "frontend/src/pages/PanelManagement.tsx").read_text(encoding="utf-8")

    def test_sidebar_children_come_from_the_api_not_hardcoded(self):
        src = self._app_src()
        block = src.split("key: '/panel-management'")[1].split("},\n    {")[0]
        self.assertIn("children:", block, "面板管理应是二级菜单（要有 children）")
        self.assertIn("/panel-management/${p.key}", block,
                      "子项 key 应是 /panel-management/<key>")
        self.assertIn("panels.map", block,
                      "子项应由面板列表生成，不能硬编码 —— 注册表加面板要自动多一项")
        # 硬编码的面板名一旦写死，注册表改了菜单就与后端脱节
        for hard in ("'cpa'", "'sub2api'", "'grok2api'"):
            self.assertNotIn(hard, block, f"子项里不该硬编码面板 key {hard}")

    def test_panel_list_is_fetched_in_the_layout(self):
        src = self._app_src()
        self.assertIn("apiFetch('/integrations/panels')", src,
                      "侧栏要取面板列表来渲染二级项")
        self.assertIn("d?.items", src, "面板列表在响应的 items 字段里")

    def test_selected_panel_comes_from_the_url(self):
        """选中态必须由 URL 参数决定 —— 否则深链与后退会对不上。"""
        page = self._page_src()
        self.assertIn("useParams", page, "面板页要读路由参数")
        self.assertIn("panelKey", page)
        self.assertNotIn("const [selectedKey, setSelectedKey]", page,
                         "选中态不该再是组件内部 state（深链/后退对不上）")

        app = self._app_src()
        self.assertIn('<Route path="/panel-management/:panelKey"', app,
                      "要有带面板参数的子路由")

    def test_child_path_highlights_the_child_item(self):
        """`/panel-management/cpa` 要选中二级项，而不是回落到一级。"""
        src = self._app_src()
        block = src.split("function getSelectedKey")[1].split("return ['/']")[0]
        self.assertIn("'/panel-management/'", block,
                      "子路径要选中自己（与 /accounts/、/mail/ 同一套逻辑）")

    def test_child_path_also_selects_the_parent(self):
        """子路径要**连父项一起**选中 —— 只给子 key 的话父项在硬刷新后是灰的。

        机制：父项高亮由 rc-menu 内部「子项注册路径 → 重算」得出，而平台/面板
        子项是异步加载的（首帧还不存在）→ 那次重算落空，父项一直不亮。
        `/mail` 的子项是写死的，所以只有异步的那两组会犯。实测三个页面：
        `/accounts/chatgpt`、`/panel-management/cpa` 硬刷新后父项灰，`/mail/icloud` 正常。
        """
        src = self._app_src()
        block = src.split("function getSelectedKey")[1].split("return ['/']")[0]
        self.assertIn(
            "return [key.slice(0, key.indexOf('/', 1)), key]",
            block,
            "子路径要返回 [父 key, 子 key] 两项，否则异步子项的父级高亮会丢",
        )

    def test_submenu_starts_expanded(self):
        """二级项默认展开 —— 与「平台管理」「邮箱服务」一致。"""
        src = self._app_src()
        self.assertIn("defaultOpenKeys={['/accounts', '/mail', '/panel-management']}", src)


if __name__ == "__main__":
    unittest.main()
