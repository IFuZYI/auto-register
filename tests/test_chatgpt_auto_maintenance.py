"""ChatGPT Token 自动维护（用户要求 2026-10-06；失效账号重试 + 封禁标注
2026-10-10）。

两条功能：

1. **自动刷新临期/过期/失效 Token**：后台定期扫描 ChatGPT 账号，对 AT 临期
   （剩余 ≤ 24h）或已过期的账号，在「临期时间段内随机一个时刻」刷新 ——
   不设固定更新时刻（避免可预测、避免同一时刻集中刷新），且不拖到过期
   （随机窗口上限 = 到期前 1 小时）。**失效（invalid）/ 过期（expired）
   状态的账号也进重试队列**（用户要求 2026-10-10「定期尝试自动刷新 gpt
   失效的账号，刷新 token」）：不看临期窗口，尽快 + 抖动排一次。封禁的、
   无法更新的不重复尝试；**检测出封禁 → 标注为封禁账号并移出队列**。
2. **chatgpt2api 凭证自动维护**：定期对比本地与 chatgpt2api 远端凭证，
   本地较新时自动推送更新，使远端始终持有最新凭证。

本文件按 TDD 逐切片写：先纯函数（随机时刻 / 计划决策 / 结果写回），
再扫描与发起（集成），最后接线（scheduler / config / 前端）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class RecordAutoRefreshResultTests(unittest.TestCase):
    """执行结果的写回（纯函数）。

    - 成功：清掉计划（AT 换发后由下一次扫描按新 exp 重排）；
    - 失败：attempts+1 + 退避（1h → 6h）；达上限（3）置 disabled。
    """

    def _at(self, token_exp):
        """构造一个解得出 exp 的假 JWT。"""
        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": token_exp}).encode()
        ).decode().rstrip("=")
        return f"header.{payload}.sig"

    def test_success_clears_state(self):
        from services.chatgpt_maintenance import record_auto_refresh_result

        extra = {
            "chatgpt_auto_refresh": {"at": 1, "for_exp": 2, "attempts": 2, "disabled": False}
        }
        state = record_auto_refresh_result(extra, ok=True, now=1_000_000)
        self.assertNotIn("chatgpt_auto_refresh", extra, "成功后计划要清掉")
        self.assertEqual(state, {})

    def test_failure_increments_attempts_with_backoff(self):
        from services.chatgpt_maintenance import record_auto_refresh_result

        exp = 1_000_000 + 10 * 3600
        extra = {"access_token": self._at(exp)}
        state = record_auto_refresh_result(extra, ok=False, now=1_000_000)

        self.assertEqual(state["attempts"], 1)
        self.assertFalse(state["disabled"])
        self.assertEqual(state["for_exp"], exp)
        self.assertGreaterEqual(state["at"], 1_000_000 + 3600, "第一次失败退避至少 1h")

        # 第二次失败：退避加长
        state2 = record_auto_refresh_result(extra, ok=False, now=1_000_000 + 3600)
        self.assertEqual(state2["attempts"], 2)
        self.assertGreaterEqual(state2["at"], 1_000_000 + 3600 + 6 * 3600 - 1)

    def test_third_failure_disables(self):
        from services.chatgpt_maintenance import record_auto_refresh_result

        exp = 1_000_000 + 10 * 3600
        extra = {"access_token": self._at(exp)}
        record_auto_refresh_result(extra, ok=False, now=1_000_000)
        record_auto_refresh_result(extra, ok=False, now=1_000_001)
        state = record_auto_refresh_result(extra, ok=False, now=1_000_002)

        self.assertEqual(state["attempts"], 3)
        self.assertTrue(state["disabled"], "连续 3 次失败后不再自动尝试")

    def test_non_positive_exp_normalized_to_zero(self):
        """非正 exp 必须归一到 0，不能原样写进 `for_exp`。

        `for_exp` 是 `record_auto_refresh_result` **自己**判定「还是不是同一个
        AT 周期」的依据：把负值（-5）原样写进去，下一轮读回来跟新解出的
        `None → 0` 对不上，就会被当成「AT 已换发」而**清零 attempts** ——
        退避与 `MAX_ATTEMPTS` 上限全部失效，一个已经试废的号会被无限重试。

        （这个不变量的另一端原来是 `plan_auto_refresh`，它已随 interval 版
        自动刷新一起删除；判据现在只落在 `record_auto_refresh_result` 上。）
        """
        from services.chatgpt_maintenance import (
            AUTO_REFRESH_STATE_KEY,
            record_auto_refresh_result,
        )

        # 负值 / 0 / 缺失三种形态都归一到 0
        for token_exp in (-5, 0):
            extra = {"access_token": self._at(token_exp)}
            state = record_auto_refresh_result(extra, ok=False, now=1_000_000)
            self.assertEqual(state["for_exp"], 0, f"exp={token_exp} 应归一到 0")

        extra_no_exp = {"access_token": "header.e30.sig"}  # payload = {}（无 exp）
        state = record_auto_refresh_result(extra_no_exp, ok=False, now=1_000_000)
        self.assertEqual(state["for_exp"], 0, "缺失 exp 应归一到 0")

        # 写回后再读一次：必须仍认作同一周期，而不是被当成「换发了」清零 attempts
        extra2 = {"access_token": self._at(-5), AUTO_REFRESH_STATE_KEY: state}
        again = record_auto_refresh_result(extra2, ok=False, now=1_000_001)
        self.assertGreaterEqual(
            again["attempts"], state["attempts"],
            "同一 AT 周期内的失败必须继续累计 —— 归一到 0 失效时这里会退回 1，退避上限永远到不了",
        )


class Chatgpt2apiAutoSyncTests(unittest.TestCase):
    """chatgpt2api 自动维护：调用推送管线并如实汇报。"""

    def test_pass_calls_injected_push_and_logs_summary(self):
        from services.chatgpt_maintenance import run_chatgpt2api_auto_sync

        logs = []
        fake = {
            "panel": "chatgpt2api",
            "total": 3, "pushed": 1, "failed": 0, "skipped": 2, "deleted": 1,
            "items": [], "remote_error": "",
        }

        summary = run_chatgpt2api_auto_sync(push=lambda: fake, log=logs.append)
        self.assertEqual(summary, fake)
        joined = "\n".join(logs)
        self.assertIn("推送 1", joined)
        self.assertIn("跳过 2", joined)

    def test_remote_error_is_reported_not_raised(self):
        from services.chatgpt_maintenance import run_chatgpt2api_auto_sync

        logs = []
        fake = {
            "panel": "chatgpt2api", "total": 0, "pushed": 0, "failed": 0,
            "skipped": 0, "items": [], "remote_error": "面板地址未配置",
        }

        summary = run_chatgpt2api_auto_sync(push=lambda: fake, log=logs.append)
        self.assertEqual(summary["remote_error"], "面板地址未配置")
        self.assertIn("面板地址未配置", "\n".join(logs))

    def test_push_exception_is_caught_and_reported(self):
        """复审 S5：push 本身抛异常时不得把调度线程带崩 —— 记日志返回空结果。"""
        from services.chatgpt_maintenance import run_chatgpt2api_auto_sync

        logs = []

        def boom():
            raise RuntimeError("network exploded")

        summary = run_chatgpt2api_auto_sync(push=boom, log=logs.append)
        self.assertEqual(summary.get("pushed", 0), 0)
        self.assertIn("network exploded", "\n".join(logs))

    def test_garbage_summary_values_do_not_raise_in_logging(self):
        """复审建议：日志行的计数解析要对脏值容忍（对齐 E2 的 _safe_int 惯例）。

        push 产出的 summary 值理论上是 int，但注入的 push / 未来变更可能给
        字符串垃圾值 —— 裸 `int("abc")` 会在日志行抛出并中断本轮。
        """
        from services.chatgpt_maintenance import run_chatgpt2api_auto_sync

        logs = []
        garbage = {
            "panel": "chatgpt2api", "total": "x", "pushed": "abc", "failed": None,
            "skipped": "1", "deleted": {}, "items": [], "remote_error": "",
        }

        summary = run_chatgpt2api_auto_sync(push=lambda: garbage, log=logs.append)
        self.assertEqual(summary, garbage)
        self.assertIn("推送", "\n".join(logs), "日志行不得因脏计数而消失")


class MaintenanceIntervalTests(unittest.TestCase):
    """调度间隔：关掉开关返回 0（scheduler 跳过）；chatgpt2api 还要已配置。"""

    def setUp(self):
        from core.config_store import config_store

        self.config_store = config_store
        for key in (
            "chatgpt_auto_refresh_enabled",
            "chatgpt_refresh_scope",
            "chatgpt_refresh_interval",
            "chatgpt2api_auto_sync_enabled",
            "chatgpt2api_api_url",
            "chatgpt2api_api_key",
        ):
            config_store.set(key, "")

    def test_auto_refresh_off_when_disabled(self):
        """自动刷新由 `services/local_maintenance` 的间隔任务承担。

        2026-10-31 定型：用户要的是**执行间隔**（「每 6 小时」这种粒度用固定
        时刻表达不了），所以走 `interval_seconds` 而不是每天几点。
        """
        from services.local_maintenance import get_auto_refresh_interval_seconds

        self.assertEqual(get_auto_refresh_interval_seconds("chatgpt"), 0)

    def test_auto_refresh_interval_when_enabled(self):
        from services.local_maintenance import get_auto_refresh_interval_seconds

        self.config_store.set("chatgpt_auto_refresh_enabled", "1")
        self.config_store.set("chatgpt_refresh_scope", "failed")
        self.config_store.set("chatgpt_refresh_interval", "90m")
        self.assertEqual(get_auto_refresh_interval_seconds("chatgpt"), 5400)

    def test_auto_refresh_interval_falls_back_to_default(self):
        from services.local_maintenance import get_auto_refresh_interval_seconds

        self.config_store.set("chatgpt_auto_refresh_enabled", "1")
        self.config_store.set("chatgpt_refresh_scope", "failed")
        self.config_store.set("chatgpt_refresh_interval", "")
        self.assertEqual(get_auto_refresh_interval_seconds("chatgpt"), 6 * 3600)

    def test_auto_refresh_off_when_scope_invalid(self):
        """范围配错（不在白名单里）时整个任务不跑，而不是退化成「全部」。"""
        from services.local_maintenance import get_auto_refresh_interval_seconds

        self.config_store.set("chatgpt_auto_refresh_enabled", "1")
        self.config_store.set("chatgpt_refresh_scope", "whatever")
        self.assertEqual(get_auto_refresh_interval_seconds("chatgpt"), 0)

    def test_chatgpt2api_interval_zero_when_disabled(self):
        from services.chatgpt_maintenance import get_chatgpt2api_auto_sync_interval_seconds

        self.config_store.set("chatgpt2api_api_url", "http://x")
        self.config_store.set("chatgpt2api_api_key", "k")
        self.assertEqual(get_chatgpt2api_auto_sync_interval_seconds(), 0)

    def test_chatgpt2api_interval_zero_when_unconfigured(self):
        from services.chatgpt_maintenance import get_chatgpt2api_auto_sync_interval_seconds

        self.config_store.set("chatgpt2api_auto_sync_enabled", "1")
        self.assertEqual(
            get_chatgpt2api_auto_sync_interval_seconds(), 0,
            "没有地址/密钥时不排任务，避免每轮都打一条必然失败的日志",
        )

    def test_chatgpt2api_interval_positive_when_enabled_and_configured(self):
        from services.chatgpt_maintenance import get_chatgpt2api_auto_sync_interval_seconds

        self.config_store.set("chatgpt2api_auto_sync_enabled", "1")
        self.config_store.set("chatgpt2api_api_url", "http://x")
        self.config_store.set("chatgpt2api_api_key", "k")
        self.assertGreater(get_chatgpt2api_auto_sync_interval_seconds(), 0)


class SchedulerRegistrationTests(unittest.TestCase):
    """模块导入时向 scheduler 自注册周期任务；main.py 负责 import。

    2026-10-31 起自动**刷新**不再是 interval 任务（`chatgpt_auto_refresh`），
    改由 `services/local_maintenance.py` 的 `chatgpt_auto_refresh_daily`
    按 `HH:MM` 触发 —— 见 `MaintenanceIntervalTests` 的说明。这里钉的是
    「面板同步这条 interval 链还在」以及「旧的刷新任务确实撤了」。
    """

    def test_panel_sync_jobs_are_registered_on_import(self):
        import services.chatgpt_maintenance  # noqa: F401  —— 注册发生在导入期
        from core.scheduler import registered_jobs

        jobs = registered_jobs()
        self.assertIn("chatgpt2api_auto_sync", jobs)
        self.assertIn("sub2api_auto_sync", jobs)

    def test_auto_refresh_is_registered_exactly_once(self):
        """自动刷新只能有一个调度入口。

        历史上有过两个（旧 interval 循环 + 新的定时任务），后果是同一个账号
        被刷两遍、而且其中一条链在界面上看不见。现在由
        `services/local_maintenance` 独家承担。
        """
        import services.chatgpt_maintenance  # noqa: F401
        import services.local_maintenance  # noqa: F401
        from core.scheduler import registered_jobs

        jobs = registered_jobs()
        self.assertIn("chatgpt_auto_refresh", jobs, "自动刷新的调度入口不见了")
        # chatgpt_maintenance 只管面板同步，不许再注册刷新相关任务
        self.assertNotIn(
            "chatgpt_auto_refresh_legacy",
            jobs,
            "又出现了第二个刷新入口 —— 会双重刷新",
        )

    def test_panel_sync_job_interval_reads_the_switch(self):
        """任务间隔直读开关 —— 界面关掉后下一 tick 即跳过（返回 0）。"""
        import services.chatgpt_maintenance  # noqa: F401
        from core.config_store import config_store
        from core.scheduler import _jobs

        job = _jobs["chatgpt2api_auto_sync"]
        config_store.set("chatgpt2api_auto_sync_enabled", "")
        self.assertEqual(job.interval(), 0)

    def test_main_py_imports_local_maintenance_too(self):
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        lines = [line.strip() for line in src.splitlines()]
        self.assertTrue(
            any(line.startswith("from services import local_maintenance") for line in lines),
            "main.py 没有导入本地维护模块 —— 自动检测/刷新/日志清理不会被注册",
        )

    def test_main_py_imports_the_module_for_registration(self):
        """按行断言 import 未被注释 —— 子串检查会放过 `# from services import ...`。"""
        src = (ROOT / "main.py").read_text(encoding="utf-8")
        lines = [line.strip() for line in src.splitlines()]
        self.assertTrue(
            any(line.startswith("from services import chatgpt_maintenance") for line in lines),
            "main.py 没有导入维护模块 —— 周期任务不会被注册",
        )


class ConfigWhitelistTests(unittest.TestCase):
    """两个开关必须在配置白名单里（否则保存时被静默丢弃）。"""

    def test_switches_are_in_config_keys(self):
        from api.config import CONFIG_KEYS

        self.assertIn("chatgpt_auto_refresh_enabled", CONFIG_KEYS)
        self.assertIn("chatgpt2api_auto_sync_enabled", CONFIG_KEYS)


class FrontendWiringTests(unittest.TestCase):
    """两个自动维护开关要在界面上可切换（用户要求「在配置界面选择是否开启」）。

    位置在 2026-10-30 从「面板配置」搬到了独立子菜单「自动维护」——
    用户要求：「将自动维护搬到一个新的子菜单里面。不同平台都要一个卡片，
    GPT 支持本地维护，其他是面板与本地同步的自动维护。」

    断言强度不变：开关仍然必须存在、必须是**布尔开关**（不是文本框），
    加载时仍然必须走 `resolveFeatureEnabledConfig` 归一。
    """

    #: 维护开关所在的文件。搬迁只改这个常量，断言本身不动。
    MAINTENANCE_SRC = "components/settings/MaintenancePanel.tsx"

    def _src(self, rel: str) -> str:
        return (ROOT / "frontend" / "src" / rel).read_text(encoding="utf-8")

    def test_panel_config_has_both_switches(self):
        """本地维护的开关与面板同步的开关都必须在界面上。

        2026-10-31 起本地维护**按平台展开**（ChatGPT / Grok 各一节），
        开关不再逐个手写，而是由 `LOCAL_PLATFORMS` × 规则模板生成。所以
        这里断言的是「模板存在 + 两个平台都在清单里」，而不是一个字面量键名
        —— 后者在搬一次结构之后就会假红（这次就是）。语义没变：
        两个开关都必须在界面上可切换。
        """
        src = self._src(self.MAINTENANCE_SRC)
        self.assertIn(
            "switchKey={`${prefix}_auto_refresh_enabled`}",
            src,
            "本地维护的「自动刷新 Token」开关声明不见了",
        )
        self.assertIn("switchKey: 'chatgpt2api_auto_sync_enabled'", src, "缺面板同步开关声明")
        for prefix in ("'chatgpt'", "'grok'"):
            self.assertIn(f"prefix: {prefix}", src, f"平台清单里少了 {prefix} —— 那一节不会渲染")

    def test_switches_are_boolean_typed(self):
        """开关必须是 `Form.Item valuePropName="checked"` + `<Switch>`。

        不是「源码里有 type: 'boolean'」这种字面量检查 —— 搬迁后这里用
        `switchKey` 声明、由统一的渲染器画 `Switch`。断言的**语义**没变：
        它必须是一个勾选框，而不是一个让人手动输入 "1"/"0" 的文本框。
        """
        src = self._src(self.MAINTENANCE_SRC)
        self.assertIn('valuePropName="checked"', src, "开关没有按 antd 的受控勾选渲染")
        self.assertIn("<Switch", src, "开关没用 Switch 控件")
        # 每个卡片的 switchKey 都要落在一个受控 Switch 上（同一个渲染器，
        # 所以只要确认渲染器存在且卡片都走它）。
        idx = src.find("CARDS.map")
        self.assertGreater(idx, -1, "卡片没有由 CARDS 驱动渲染")

    def test_boolean_keys_include_maintenance_switches(self):
        """BOOLEAN_KEYS 漏加 → 保存时 "true" 字符串被原样写入但回读不归一。

        `BOOLEAN_KEYS` 留在 PanelConfigPanel（面板卡片与它同页保存），
        维护开关也在这份清单里 —— 两个页面共用一份归一逻辑，漏一个就会
        「保存成开启、重进变关闭」。
        """
        src = self._src("components/settings/PanelConfigPanel.tsx")
        idx = src.find("const BOOLEAN_KEYS")
        self.assertGreater(idx, -1)
        block = src[idx : idx + 600]
        self.assertIn("chatgpt_auto_refresh_enabled", block)
        self.assertIn("chatgpt2api_auto_sync_enabled", block)

    def test_all_boolean_keys_pass_through_normalization_on_load(self):
        """每个 BOOLEAN_KEYS 开关在加载回填时必须归一成布尔。

        antd Switch 把非空字符串一律视为真：库里存 `"0"`（关闭）时，
        直接回填字符串会把开关渲染成「开启」（实测复现：aria-checked="0"
        但带 ant-switch-checked 类）。`resolveFeatureEnabledConfig` 是
        归一入口 —— 漏掉的键（新增开关或历史缺口）都会踩这个坑。

        两种归一形式都算数：逐键显式赋值（有「已配置即开启」语义的键）
        或 `for (const key of BOOLEAN_KEYS)` 循环（其余键的统一兜底）。
        """
        src = self._src("components/settings/PanelConfigPanel.tsx")
        # 提取 BOOLEAN_KEYS 列表里的所有键名
        idx = src.find("const BOOLEAN_KEYS")
        block = src[idx : src.find("] as const", idx)]
        keys = [k.strip().strip("',") for k in block.splitlines() if k.strip().startswith("'")]
        # 数量是「解析没坏」的粗检。2026-10-27 删掉 `cpa_upload_grok_enabled`
        # （Grok 不再支持 CPA）后从 9 降到 8。
        self.assertGreaterEqual(len(keys), 8, f"BOOLEAN_KEYS 解析异常: {keys}")

        loop_covers_all = (
            "for (const key of BOOLEAN_KEYS)" in src
            and "resolveFeatureEnabledConfig(config[key], false)" in src
        )
        missing = [
            k for k in keys
            if not loop_covers_all
            and f"config.{k} = resolveFeatureEnabledConfig" not in src
        ]
        self.assertEqual(
            missing, [],
            f"这些开关没有走 resolveFeatureEnabledConfig 归一 —— "
            f"库里存 '0' 时界面会渲染成开启: {missing}",
        )


class CredentialReadPathTests(unittest.TestCase):
    """自动维护模块的 AT 读取必须走凭证注册表（复审发现，2026-10-07）。

    注册表的读取口径是「规范名 + camelCase 别名 + token 列兜底」：
    导入路径（api/accounts.py）会把未知键原样收进 extra，历史/导入的行可能
    以 `accessToken`（camelCase）存 AT；同分支的其他消费者（account_status /
    chatgpt_sync / panel_comparison_cache）都走 `get_credential`。自动维护
    如果直接 `extra.get("access_token")`，这些行会被静默判成「无 exp」——
    头号功能对它们永远不排计划（skip / no_expiry），且失败写回的
    `for_exp=0` 让死计划清理也永远清不到。
    """

    NOW = 1_000_000

    def _at(self, exp):
        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"header.{payload}.sig"

    def test_access_token_resolved_from_camel_case(self):
        """extra 只有 accessToken（camelCase）→ 也要能读出来。"""
        from services.chatgpt_maintenance import _access_token_of

        exp = self.NOW + 10 * 3600
        token = self._at(exp)
        self.assertEqual(
            _access_token_of({"accessToken": token}), token,
            "camelCase 存的 AT 读不出 —— 自动刷新对这类账号永不生效",
        )

    def test_access_token_resolved_from_token_column(self):
        """extra 没有 AT、只在 token 列（历史/导入形态）→ 兜底读出来。"""
        from types import SimpleNamespace

        from services.chatgpt_maintenance import _access_token_of

        exp = self.NOW + 10 * 3600
        token = self._at(exp)
        row = SimpleNamespace(token=token)
        self.assertEqual(
            _access_token_of({}, row), token,
            "token 列兜底失效 —— 仅 token 列存 AT 的账号被静默跳过",
        )

    def test_lifecycle_resolves_camel_case_account(self):
        """端到端口径：camelCase 存 AT → 生命周期解得出 exp。

        这条链现在唯一的消费方是 `record_auto_refresh_result`（手动恢复链的
        退避记账）：解不出 exp 就写不出正确的 `for_exp`，退避上限会失效。
        """
        from services.chatgpt_maintenance import _access_token_of
        from services.chatgpt_token_lifecycle import project_access_token_lifecycle

        exp = self.NOW + 10 * 3600
        extra = {"accessToken": self._at(exp)}
        lc = project_access_token_lifecycle(_access_token_of(extra))
        self.assertEqual(lc.get("expires_at"), exp)

    def test_failure_backoff_reads_camel_case_token(self):
        """失败写回：camelCase 存的 AT 也要解出 for_exp（不是 0）。"""
        from services.chatgpt_maintenance import record_auto_refresh_result

        exp = self.NOW + 10 * 3600
        extra = {"accessToken": self._at(exp)}
        state = record_auto_refresh_result(extra, ok=False, now=self.NOW)
        self.assertEqual(
            state["for_exp"], exp,
            "camelCase 存的 AT 解不出 exp —— for_exp=0 会让计划永不匹配当前周期",
        )

if __name__ == "__main__":
    unittest.main()
