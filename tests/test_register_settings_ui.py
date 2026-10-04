"""注册设置渐进式 UI + 默认注册参数的回归。

背景：原先把「默认注册方式」「发码节流」平铺成两张卡片，所有字段一眼全露 ——
为了改一个并发数要在一屏平台下拉里找。现在改成：
  默认注册参数（数字，每次开任务都可能动）
  → 各平台设置（每平台一张卡：执行器 + 该平台声明的注册方式/专属参数，
     折叠，收起时显示已选摘要）

发码节流（Grok 专属的两个数值旋钮）原先是一张独立的卡片，现在由 Grok 插件
自己声明（`registration_modes` 的数值形状），跟着 Grok 卡片走 —— 它只对
Grok 有意义，摆在全局区域里就成了「Grok 的设置长在别处」。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from api.config import CONFIG_KEYS

ROOT = Path(__file__).resolve().parents[1]


class DefaultRegisterParamKeysTests(unittest.TestCase):
    """默认注册参数必须是可写配置键，否则界面保存会被静默忽略。"""

    def test_default_param_keys_are_writable(self):
        for key in (
            "register_count",
            "register_concurrency",
            "register_delay_seconds",
            "register_retry_times",
        ):
            self.assertIn(key, CONFIG_KEYS, f"{key} 不在 CONFIG_KEYS 里")

    def test_per_platform_executor_keys_are_writable(self):
        """执行器按平台设置（`<platform>_executor`），都要能保存。"""
        for key in ("chatgpt_executor", "grok_executor"):
            self.assertIn(key, CONFIG_KEYS, f"{key} 不在 CONFIG_KEYS 里")

    def test_config_endpoint_returns_them(self):
        """GET /api/config 要回这些键（前端靠它预填任务页）。"""
        from fastapi.testclient import TestClient

        from main import app

        with TestClient(app) as client:
            response = client.get("/api/config")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        for key in ("register_count", "register_concurrency", "register_delay_seconds"):
            self.assertIn(key, body, f"/api/config 没返回 {key}")

    def test_they_are_writable_through_the_api(self):
        """写入后能读回 —— 验证白名单真的放行（不只是列表里有）。"""
        from fastapi.testclient import TestClient

        from core.config_store import config_store
        from main import app

        with TestClient(app) as client:
            response = client.put(
                "/api/config",
                json={"data": {"register_count": "7", "register_concurrency": "2"}},
            )
            self.assertEqual(response.status_code, 200, response.text[:200])
            ignored = response.json().get("ignored") or []
            self.assertNotIn("register_count", ignored)
            self.assertNotIn("register_concurrency", ignored)

            back = client.get("/api/config").json()
            self.assertEqual(back["register_count"], "7")
            self.assertEqual(back["register_concurrency"], "2")

        # 收尾：别把测试值留在库里
        config_store.set("register_count", "")
        config_store.set("register_concurrency", "")


class RegisterSettingsPanelTests(unittest.TestCase):
    """面板结构：渐进式布局的几条硬约束。"""

    def setUp(self):
        self.src = (
            ROOT / "frontend/src/components/settings/RegisterSettingsPanel.tsx"
        ).read_text(encoding="utf-8")

    def test_declares_the_four_default_params(self):
        for key in (
            "register_count",
            "register_concurrency",
            "register_delay_seconds",
            "register_retry_times",
        ):
            self.assertIn(key, self.src, f"默认参数面板缺字段 {key}")

    def test_uses_progressive_disclosure(self):
        """平台方式与节流都收进 Collapse（渐进式：默认不展开）。"""
        self.assertIn("Collapse", self.src, "渐进式布局要用 Collapse")
        self.assertIn("ghost", self.src)

    def test_throttle_fields_live_in_the_platform_card_not_a_standalone_card(self):
        """发码节流（Grok 专属）由插件声明，跟着 Grok 卡片渲染。

        原先界面里手写了一张独立的「发码节流（Grok）」卡片 —— 它只对 Grok
        有意义，放在全局区域就成了「Grok 的设置长在别处」。现在数值形状
        （type: 'number'）由插件声明，界面通用渲染。
        """
        # 界面不再硬编码这两个键（它们是 Grok 的实现细节）
        self.assertNotIn("grok_send_code_timeout", self.src,
                         "节流字段应由 Grok 插件声明，界面不硬编码")
        self.assertNotIn("grok_send_code_min_interval", self.src)
        self.assertNotIn("发码节流", self.src)
        # 但要能渲染数值形状的声明
        self.assertIn("mode.type === 'number'", self.src,
                      "界面要支持数值形状的可配置项")

    def test_executor_is_per_platform_not_global(self):
        """执行器按平台设置，不再有全局默认。

        各平台支持的执行器集合不同（插件的 `supported_executors`），
        一个全局值必然对某些平台无效 —— Grok 的默认是 browser（协议路径在
        x.ai 上会 CF 403），ChatGPT 只有 protocol。
        """
        # 每个平台卡里渲染自己的执行器下拉（键名按平台生成）
        self.assertIn("executorKey", self.src)
        self.assertIn("${platform.name}_executor", self.src)
        # 不再有全局的 default_executor 字段
        self.assertNotIn("name=\"default_executor\"", self.src,
                         "全局默认执行器已取消，应改为按平台设置")
        self.assertNotIn("默认执行器", self.src)

    def test_executor_labels_come_from_platform_declaration(self):
        """执行器显示名由平台声明（`executor_labels`），不靠前端猜。

        Grok 的执行器是 `browser`/`protocol`，不在通用标签表里 ——
        界面若只查通用表会显示原始值 `browser`。
        """
        self.assertIn("executor_labels", self.src)
        self.assertIn("executorLabelOf", self.src)

    def test_platform_modes_come_from_api_not_hardcoded(self):
        """注册方式取值由插件声明，界面不硬编码。"""
        self.assertIn("apiFetch('/platforms')", self.src)
        self.assertIn("registration_modes", self.src)
        # 不该出现具体的取值字面量
        for hardcoded in ("'browser'", "'protocol'", "'refresh_token'"):
            self.assertNotIn(hardcoded, self.src,
                             f"界面不该硬编码注册方式取值 {hardcoded}")

    def test_shows_summary_when_collapsed(self):
        """收起时要有摘要，且摘要必须**订阅**表单值。

        只断言「文件里出现 useWatch」是假绿 —— 注释里提一句也能满足
        （反向对照时实测如此）。这里断言真正的调用形态。
        """
        self.assertIn("const value = Form.useWatch(", self.src,
                      "折叠摘要要真的订阅表单值（不是注释里提一句）")
        self.assertIn("modeSummary(", self.src)

    def test_collapsed_summary_reads_the_whole_store(self):
        """摘要必须读**整个 store**（`preserve: true`），不能只看已注册字段。

        实测（真实浏览器）：默认的 useWatch 只读已注册字段，而 Collapse 首次
        渲染前 Form.Item 未注册 —— 摘要显示的是**声明默认值**（90）而不是实际
        值（120），展开一次才对上。preserve 让它读 allValues，收起态也是实际值。

        断言真正的调用形态：只查字符串 `preserve: true` 是**假绿** —— 上面
        的说明注释里就有这几个字，把调用改回去测试照样通过（反向对照实测如此）。
        """
        self.assertIn("{ form, preserve: true }", self.src,
                      "收起态的摘要要读整个 store，否则显示的是默认值不是实际值")


class SettingsPageWiringTests(unittest.TestCase):
    def setUp(self):
        self.src = (ROOT / "frontend/src/pages/Settings.tsx").read_text(encoding="utf-8")

    def test_register_tab_uses_the_custom_panel(self):
        self.assertIn("RegisterSettingsPanel", self.src)
        self.assertIn("custom: 'register'", self.src)

    def test_old_flat_sections_are_gone(self):
        """旧的平铺 section 必须删掉，不能与面板并存（会重复渲染同一批字段）。"""
        self.assertNotIn("title: '默认注册方式'", self.src)
        self.assertNotIn("title: '发码节流（Grok）'", self.src)


class TaskPagePrefillTests(unittest.TestCase):
    def test_task_page_prefills_from_defaults(self):
        src = (ROOT / "frontend/src/pages/RegisterTaskPage.tsx").read_text(encoding="utf-8")
        for key in ("register_count", "register_concurrency", "register_delay_seconds"):
            self.assertIn(key, src, f"任务页没有从 {key} 预填")

    def test_prefill_does_not_clobber_with_zero(self):
        """配置为空时不能把任务页的兜底值覆盖成 0。"""
        src = (ROOT / "frontend/src/pages/RegisterTaskPage.tsx").read_text(encoding="utf-8")
        # 只在该键有值时覆盖 —— 用展开运算符条件合并
        self.assertIn("Number(cfg.register_count) > 0", src)
        self.assertIn("String(cfg.register_delay_seconds ?? '').trim()", src)


if __name__ == "__main__":
    unittest.main()
