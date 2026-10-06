"""环境预检与浏览器运行时鲁棒性（用户要求 2026-10-06：「提高项目兼容性，增加鲁棒性」）。

事故背景：camoufox Python 包升级到 0.5.7（配对浏览器 156.0.1-beta.34），
但本机缓存里只有旧浏览器 152.0.4-beta.31 —— 注册任务启动后才在浏览器
启动处炸 `CamoufoxNotInstalled`，而且被当成**可重试失败**，两轮重试全烧
在同一个环境错误上（实测 task_1791300512055：两轮、每轮 3 秒、全失败）。

修法两层：
1. 任务启动前的**环境预检**（fail-fast）：注册平台要用的浏览器/运行时
   不满足时，立刻以可操作的中文原因失败，不浪费代理/邮箱/重试轮；
2. 环境类错误（CamoufoxNotInstalled 等）判为**不可重试**（重开同样结局），
   由注册 runner 的既有 dead-end 逻辑提前收手。
"""

from __future__ import annotations

import unittest
from unittest import mock


class _FakeNotInstalled(FileNotFoundError):
    """模拟 camoufox.exceptions.CamoufoxNotInstalled（它继承 FileNotFoundError）。"""


class EnvironmentErrorClassificationTests(unittest.TestCase):
    """环境类错误必须被判成「重开也没用」，不烧重试轮。"""

    def test_camoufox_not_installed_is_non_retryable(self):
        from core.environment import classify_environment_error

        exc = _FakeNotInstalled(
            "official 156.0.1-beta.34, the browser this camoufox release pairs "
            "with, is not installed. Please run `camoufox fetch` to install."
        )
        verdict = classify_environment_error(exc)
        self.assertTrue(verdict.is_environment, "CamoufoxNotInstalled 应判为环境错误")
        self.assertIn("camoufox fetch", verdict.hint, "提示里要给出修复命令")
        self.assertTrue(verdict.non_retryable, "环境错误重开同样结局，必须不可重试")

    def test_missing_node_runtime_is_environment_error(self):
        from core.environment import classify_environment_error

        exc = RuntimeError(
            "找不到 Node 运行时 (node)：Sentinel PoW 必须在 Node 沙箱里跑 "
            "OpenAI 的 sdk.js，缺少它会导致验证码邮件被服务端静默丢弃。"
        )
        verdict = classify_environment_error(exc)
        self.assertTrue(verdict.is_environment)
        self.assertTrue(verdict.non_retryable)

    def test_ordinary_business_failure_is_not_environment(self):
        from core.environment import classify_environment_error

        for exc in (
            RuntimeError("邮箱未收到验证码"),
            RuntimeError("HTTP 403 域名被拒"),
            RuntimeError("账号没有可用的刷新方式"),
            _FakeNotInstalled("unrelated file missing"),  # 无 camoufox 关键词的 FileNotFoundError
        ):
            verdict = classify_environment_error(exc)
            self.assertFalse(
                verdict.is_environment,
                f"{exc!r} 不该被判成环境错误 —— 误判会让业务失败不重试",
            )

    def test_geoip_extra_missing_is_environment_error(self):
        """NotInstalledGeoIPExtra（代理路径 geoip=True 的依赖）也是环境错误。

        实测事故：带代理注册时浏览器启动炸
        `NotInstalledGeoIPExtra: Please install the geoip extra ...`，
        被当普通失败烧了两轮重试（与 CamoufoxNotInstalled 同一类）。
        """
        from core.environment import classify_environment_error

        verdict = classify_environment_error(
            RuntimeError(
                "NotInstalledGeoIPExtra: Please install the geoip extra to use "
                "this feature: pip install camoufox[geoip]"
            )
        )
        self.assertTrue(verdict.is_environment, "GeoIP extra 缺失应判为环境错误")
        self.assertTrue(verdict.non_retryable)
        self.assertIn("geoip", verdict.hint.lower())

    def test_wrapped_environment_error_still_recognized(self):
        """异常被包成 f"{type(exc).__name__}: {str(exc)}" 的字符串形态也要认。

        register_browser 的兜底 except 把异常文本化成
        `CamoufoxNotInstalled: ...`，之后一路以字符串形式传播。
        """
        from core.environment import classify_environment_error

        verdict = classify_environment_error(
            RuntimeError(
                "CamoufoxNotInstalled: official 156.0.1-beta.34, the browser "
                "this camoufox release pairs with, is not installed."
            )
        )
        self.assertTrue(verdict.is_environment)
        self.assertTrue(verdict.non_retryable)


class CamoufoxPreflightTests(unittest.TestCase):
    """注册任务启动前的浏览器环境预检。"""

    def test_preflight_passes_when_browser_pairs(self):
        from core.environment import check_camoufox_ready

        with mock.patch(
            "core.environment._camoufox_installed_verstr", return_value="156.0.1-beta.34"
        ), mock.patch(
            "core.environment._camoufox_pinned_spec", return_value="156.0.1-beta.34"
        ):
            verdict = check_camoufox_ready()
        self.assertTrue(verdict.ok)

    def test_preflight_fails_with_actionable_hint_when_browser_missing(self):
        from core.environment import check_camoufox_ready

        def _raise():
            raise _FakeNotInstalled(
                "official 156.0.1-beta.34, the browser this camoufox release "
                "pairs with, is not installed. Please run `camoufox fetch` to install."
            )

        with mock.patch("core.environment._camoufox_installed_verstr", side_effect=_raise):
            verdict = check_camoufox_ready()
        self.assertFalse(verdict.ok)
        self.assertIn("camoufox fetch", verdict.message, "要给出修复命令")
        self.assertIn("156.0.1-beta.34", verdict.message, "要说清缺的是哪个版本")

    def test_preflight_fails_when_package_missing_entirely(self):
        from core.environment import check_camoufox_ready

        with mock.patch(
            "core.environment._camoufox_installed_verstr",
            side_effect=ImportError("No module named 'camoufox'"),
        ):
            verdict = check_camoufox_ready()
        self.assertFalse(verdict.ok)
        self.assertIn("pip install camoufox", verdict.message)


class GrokPlatformPreflightTests(unittest.TestCase):
    """Grok 注册入口（唯一浏览器路径）在启动浏览器前做预检。"""

    def _platform(self):
        from core.base_platform import RegisterConfig
        from platforms.grok.plugin import GrokPlatform

        return GrokPlatform(config=RegisterConfig(extra={}))

    def test_register_fails_fast_with_environment_hint(self):
        """浏览器没装好时，报错要带修复指引，且是 NonRetryable。"""
        from core.environment import EnvironmentNotReadyError
        from core.task_runtime import NonRetryableRegisterError

        platform = self._platform()
        platform._log_fn = lambda msg: None

        bad = mock.Mock(ok=False, message="camoufox 浏览器未安装：请运行 `camoufox fetch`")
        with mock.patch("core.environment.check_camoufox_ready", return_value=bad):
            with self.assertRaises(NonRetryableRegisterError) as ctx:
                platform.register(email="a@b.com", password="pw")

        self.assertIn("camoufox fetch", str(ctx.exception))

    def test_register_proceeds_when_preflight_passes(self):
        """预检通过时正常进入浏览器注册（用 mock 拦住真实浏览器）。"""
        platform = self._platform()
        platform._log_fn = lambda msg: None

        ok = mock.Mock(ok=True, message="")
        browser_result = {"ok": True, "email": "a@b.com", "password": "pw",
                          "sso": "sso-token", "error": ""}
        with mock.patch("core.environment.check_camoufox_ready", return_value=ok), \
             mock.patch(
                 "platforms.grok.register_browser.register_grok_via_browser",
                 return_value=browser_result,
             ), \
             mock.patch.object(platform, "_record_mailbox_status", lambda *a, **k: None), \
             mock.patch.object(platform, "is_email_registered", lambda *a, **k: False):
            account = platform.register(email="a@b.com", password="pw")

        self.assertEqual(account.email, "a@b.com")

    def test_class_method_check_environment_delegates_to_camoufox_probe(self):
        """任务级钩子 `GrokPlatform.check_environment` 必须真实接上配对探测。

        runner 的预检走的是这个类方法（不是实例 register 里的内联调用）——
        两条路各自独立，都要有测试钉住，否则一条断了另一条察觉不到。
        """
        from core.environment import EnvironmentVerdict
        from platforms.grok.plugin import GrokPlatform

        good = EnvironmentVerdict(ok=True)
        with mock.patch("core.environment.check_camoufox_ready", return_value=good) as probe:
            verdict = GrokPlatform.check_environment()

        probe.assert_called_once()
        self.assertTrue(verdict.ok)

    def test_class_method_check_environment_reports_missing_browser(self):
        from core.environment import EnvironmentVerdict
        from platforms.grok.plugin import GrokPlatform

        bad = EnvironmentVerdict(
            ok=False, is_environment=True, non_retryable=True,
            message="camoufox 浏览器未安装：请运行 `camoufox fetch`",
        )
        with mock.patch("core.environment.check_camoufox_ready", return_value=bad):
            verdict = GrokPlatform.check_environment()

        self.assertFalse(verdict.ok)
        self.assertIn("camoufox fetch", verdict.message)


class NodeRuntimePreflightTests(unittest.TestCase):
    """Sentinel PoW 依赖的 Node 运行时预检（ChatGPT 注册链的同类环境缺口）。"""

    def test_node_ready_passes_when_resolvable(self):
        from core.environment import check_node_ready

        with mock.patch.dict(
            "os.environ", {"OPENAI_SENTINEL_NODE_PATH": ""}, clear=False
        ), mock.patch("shutil.which", return_value="/usr/bin/node"):
            verdict = check_node_ready()
        self.assertTrue(verdict.ok)

    def test_node_ready_fails_with_actionable_hint(self):
        from core.environment import check_node_ready

        with mock.patch.dict("os.environ", {"OPENAI_SENTINEL_NODE_PATH": ""}, clear=False), \
             mock.patch("shutil.which", return_value=None):
            verdict = check_node_ready()
        self.assertFalse(verdict.ok)
        self.assertIn("Node", verdict.message)
        self.assertIn("OPENAI_SENTINEL_NODE_PATH", verdict.message)

    def test_node_ready_respects_env_override_absolute_path(self):
        from core.environment import check_node_ready

        # 绝对路径存在 → 直接通过（不走 PATH 查找）
        with mock.patch.dict(
            "os.environ", {"OPENAI_SENTINEL_NODE_PATH": "/opt/node/bin/node"}, clear=False
        ), mock.patch("os.path.exists", return_value=True), mock.patch(
            "shutil.which"
        ) as which:
            verdict = check_node_ready()
        self.assertTrue(verdict.ok)
        which.assert_not_called()

    def test_node_ready_fails_when_env_override_path_missing(self):
        from core.environment import check_node_ready

        with mock.patch.dict(
            "os.environ", {"OPENAI_SENTINEL_NODE_PATH": "/opt/missing/node"}, clear=False
        ), mock.patch("os.path.exists", return_value=False):
            verdict = check_node_ready()
        self.assertFalse(verdict.ok)


class ChatGptPlatformPreflightTests(unittest.TestCase):
    """ChatGPT 的任务级预检钩子（Node 运行时）。"""

    def test_class_method_delegates_to_node_probe(self):
        from core.environment import EnvironmentVerdict
        from platforms.chatgpt.plugin import ChatGPTPlatform

        good = EnvironmentVerdict(ok=True)
        with mock.patch("core.environment.check_node_ready", return_value=good) as probe:
            verdict = ChatGPTPlatform.check_environment()

        probe.assert_called_once()
        self.assertTrue(verdict.ok)


class RegisterRunnerEnvironmentTests(unittest.TestCase):
    """注册 runner：环境错误的字符串形态也要能判成不可重试。"""

    def test_environment_error_marks_result_non_retryable(self):
        from core.environment import is_environment_error_message

        self.assertTrue(
            is_environment_error_message(
                "CamoufoxNotInstalled: official 156.0.1-beta.34, the browser "
                "this camoufox release pairs with, is not installed."
            )
        )
        self.assertFalse(is_environment_error_message("邮箱未收到验证码"))


class _EnvBrokenPlatform:
    """任务级预检失败的平台：register 不该被调用。"""

    name = "chatgpt"
    display_name = "EnvBroken"

    @classmethod
    def check_environment(cls):
        from core.environment import EnvironmentVerdict

        return EnvironmentVerdict(
            ok=False,
            is_environment=True,
            non_retryable=True,
            message="camoufox 浏览器未安装：请运行 `camoufox fetch` 安装配对浏览器",
        )

    def __init__(self, config=None, mailbox=None):
        self.attempts = 0
        self.config = config

    def bind_task_control(self, task_control):
        self._task_control = task_control

    def register(self, email=None, password=None):
        raise AssertionError("任务级预检失败后不应调用 register")


class _TextualEnvErrorPlatform:
    """以文本化形态抛环境错误的平台（模拟 register_browser 的兜底 except）。"""

    name = "chatgpt"
    display_name = "TextualEnv"
    attempts = 0

    @classmethod
    def check_environment(cls):
        from core.environment import EnvironmentVerdict

        return EnvironmentVerdict(ok=True)  # 预检通过（模拟旧代码路径没预检）

    def __init__(self, config=None, mailbox=None):
        self.mailbox = mailbox
        self.config = config

    def bind_task_control(self, task_control):
        self._task_control = task_control

    def register(self, email=None, password=None):
        type(self).attempts += 1
        raise RuntimeError(
            "CamoufoxNotInstalled: official 156.0.1-beta.34, the browser this "
            "camoufox release pairs with, is not installed. Please run "
            "`camoufox fetch` to install."
        )


class TaskLevelPreflightTests(unittest.TestCase):
    """任务级环境预检：环境不就绪时整个任务直接失败，不启动任何账号。"""

    def _run(self, task_id, platform_cls, *, retry_times=1, count=3):
        from api.tasks import RegisterTaskRequest, _create_task_record, _run_register, _task_store

        req = RegisterTaskRequest(
            platform="chatgpt",
            count=count,
            concurrency=1,
            register_retry_times=retry_times,
            extra={"mail_provider": "fake"},
        )
        _create_task_record(task_id, req, "manual", None)

        with mock.patch("core.registry.get", return_value=platform_cls), mock.patch(
            "core.base_mailbox.create_mailbox", return_value=mock.Mock()
        ), mock.patch("core.db.save_account", side_effect=lambda account: account), mock.patch(
            "api.tasks._save_task_log", side_effect=lambda *a, **k: None
        ):
            _run_register(task_id, req)

        return _task_store.snapshot(task_id)

    def test_task_fails_fast_when_environment_not_ready(self):
        snapshot = self._run("task-env-preflight", _EnvBrokenPlatform)

        self.assertEqual(snapshot["status"], "failed", "环境预检失败应让任务直接失败")
        joined = "\n".join(snapshot["logs"])
        self.assertIn("camoufox fetch", joined, "日志里要带修复指引")
        self.assertEqual(snapshot["success"], 0)

    def test_textual_environment_error_stops_retrying_early(self):
        """文本化的环境错误（"CamoufoxNotInstalled: ..."）→ dead-end 提前收手。"""
        _TextualEnvErrorPlatform.attempts = 0
        snapshot = self._run(
            "task-env-textual", _TextualEnvErrorPlatform, retry_times=4, count=1
        )

        # dead-end 语义：1 轮 + 1 轮确认后收手（不会烧满 5 轮）
        self.assertEqual(
            _TextualEnvErrorPlatform.attempts,
            2,
            "环境错误被当普通失败烧满了重试轮",
        )
        self.assertEqual(len(snapshot["errors"]), 1)


if __name__ == "__main__":
    unittest.main()
