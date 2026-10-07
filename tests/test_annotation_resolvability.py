"""类型注解可解析性：`typing.get_type_hints` 不得因未定义名而炸。

回归背景（全量代码审查发现）：两处函数注解引用了模块里不存在的名字 ——
注解在 `from __future__ import annotations` 下是惰性字符串，**调用**不炸，
但任何做反射的工具链（`typing.get_type_hints`、FastAPI 依赖注入、pydantic
模型生成、Sphinx autodoc、mypy）都会 `NameError`。

实测（修复前）：
- `platforms/chatgpt/token_refresh.py:266` — `account: Account`，`Account`
  从未定义（旧 import 注释掉后忘了删注解）；
- `services/chatgpt_sync.py` — 7 处 `session: Session | None`，`Session`
  从未 import。

这两处都是「运行时不炸、工具链炸」的隐性缺陷 —— 静态扫描（pyflakes）
看得见，但此前没人扫过。
"""

from __future__ import annotations

import typing
import unittest


class TokenRefreshAnnotationsTests(unittest.TestCase):
    """token_refresh 的注解必须可解析。"""

    def test_refresh_account_annotations_resolve(self):
        from platforms.chatgpt.token_refresh import TokenRefreshManager

        hints = typing.get_type_hints(TokenRefreshManager.refresh_account)
        self.assertIn("account", hints, "account 参数应有可解析的类型")
        self.assertIn("return", hints)


class ChatgptSyncAnnotationsTests(unittest.TestCase):
    """chatgpt_sync 的注解必须可解析。"""

    def test_update_account_model_annotations_resolve(self):
        from services.chatgpt_sync import update_account_model_cpa_sync

        hints = typing.get_type_hints(update_account_model_cpa_sync)
        self.assertIn("session", hints)

    def test_all_public_functions_annotations_resolve(self):
        """模块内所有带注解的函数都不得 NameError。"""
        import services.chatgpt_sync as mod

        failures = []
        for name in dir(mod):
            fn = getattr(mod, name)
            if not callable(fn) or not getattr(fn, "__annotations__", None):
                continue
            try:
                typing.get_type_hints(fn)
            except NameError as exc:
                failures.append(f"{name}: {exc}")
            except Exception:
                pass  # 其它异常（如自定义类型）不属本测试范围
        self.assertEqual(failures, [], f"注解不可解析: {failures}")


class OutlookBackendAnnotationsTests(unittest.TestCase):
    """Outlook 后端基类的注解必须可解析（循环引用的 `OutlookMailbox` 曾未定义）。"""

    def test_backend_init_annotations_resolve(self):
        from core.mailboxes.channels.outlook.backends import OutlookMailboxBackend

        hints = typing.get_type_hints(OutlookMailboxBackend.__init__)
        self.assertIn("mailbox", hints)


if __name__ == "__main__":
    unittest.main()
