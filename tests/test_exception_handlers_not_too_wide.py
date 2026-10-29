"""过宽的异常处理会吞掉真 bug —— 用基线快照防**新增**。

实测案例（2026-10-27）：我在 `services/external_sync.py` 加了
`logger.debug(...)`，但那个模块**没有 logger**。`NameError` 被
`except Exception as exc` 吞掉、变成「上传失败」的一个普通原因 ——
contribution 上传恒失败，而测试给出的信息只是「ok 是 False」，
看不出根因是代码写错了。

`except Exception` 本身不是错（外部调用失败必须兜住）。存量有 240 多处
「不绑定异常对象」的处理块，逐个改风险远大于收益，所以这里**不要求**清理
存量 —— 只钉住基线：**新增**一个未绑定的宽泛处理就红。

要往下减，改完一个文件就把它的数字调低（或删掉该行）。
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = ("api", "services", "core", "platforms")

#: 基线：文件名 → 允许的「未绑定异常对象的 `except Exception:`」数量。
#: 只减不增。新增文件不在表里 = 允许 0 个。
BASELINE: dict[str, int] = {
    "api/accounts.py": 4,
    "api/auth.py": 1,
    "api/contribution.py": 1,
    "api/integrations.py": 1,
    "api/shared_mail.py": 1,
    "core/base_captcha.py": 1,
    "core/config_store.py": 1,
    "core/credential_fields.py": 1,
    "core/db/__init__.py": 1,
    "core/db/base.py": 1,
    "core/db/migrations.py": 9,
    "core/db/registry.py": 1,
    "core/db/repository.py": 1,
    "core/environment.py": 3,
    "core/mailboxes/base.py": 2,
    "core/mailboxes/channels/outlook/backends.py": 4,
    "core/mailboxes/channels/outlook/mailbox.py": 8,
    "core/panel_tls.py": 1,
    "core/proxy_pool.py": 2,
    "core/proxy_utils.py": 1,
    "core/scheduler.py": 2,
    "platforms/chatgpt/chatgpt2api_upload.py": 4,
    "platforms/chatgpt/cpa_upload.py": 3,
    "platforms/chatgpt/plugin.py": 1,
    "platforms/chatgpt/protocol/auth_flow.py": 7,
    "platforms/chatgpt/protocol/mixins/add_phone.py": 9,
    "platforms/chatgpt/protocol/mixins/codex.py": 6,
    "platforms/chatgpt/protocol/mixins/platform_rt.py": 1,
    "platforms/chatgpt/protocol/mixins/redirect.py": 12,
    "platforms/chatgpt/protocol/mixins/signup.py": 5,
    "platforms/chatgpt/protocol/mixins/trace.py": 8,
    "platforms/chatgpt/protocol/two_factor.py": 3,
    "platforms/chatgpt/protocol_log_relay.py": 1,
    "platforms/chatgpt/registration_engine.py": 1,
    "platforms/chatgpt/status_probe.py": 3,
    "platforms/chatgpt/sub2api_upload.py": 3,
    "platforms/chatgpt/token_refresh.py": 1,
    "platforms/grok/grok2api.py": 3,
    "platforms/grok/oauth_browser.py": 10,
    "platforms/grok/oauth_device.py": 10,
    "platforms/grok/plugin.py": 4,
    "platforms/grok/register_browser.py": 15,
    "platforms/grok/token_refresh.py": 1,
    "platforms/grok/turnstile_mint.py": 3,
    "platforms/grok/upload.py": 3,
    "platforms/icloud/login.py": 1,
    "platforms/icloud/mailbox.py": 5,
    "platforms/icloud/utils.py": 1,
    "services/account_export.py": 1,
    "services/account_status.py": 2,
    "services/chatgpt_maintenance.py": 5,
    "services/chatgpt_otp_mailbox.py": 1,
    "services/chatgpt_sync.py": 4,
    "services/chatgpt_token_lifecycle.py": 1,
    "services/chatgpt_two_factor.py": 2,
    "services/cliproxyapi_sync.py": 4,
    "services/data_bundle.py": 2,
    "services/external_sync.py": 1,
    "services/icloud/accounts.py": 1,
    "services/mail_imports/providers.py": 2,
    "services/panel_comparison.py": 1,
    "services/sms/controller.py": 6,
    "services/sms/providers.py": 11,
    "services/solver_manager.py": 1,
    "services/task_runners.py": 2,
    "services/task_store_io.py": 5,
    "services/turnstile_solver/api_solver.py": 3,
    "services/turnstile_solver/start.py": 1,
}


def _iter_py():
    for d in SCAN_DIRS:
        base = ROOT / d
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            yield path


def _unbound_broad_handlers() -> dict[str, int]:
    """统计每个文件里 `except Exception:`（未绑定名字）的个数。"""
    counts: dict[str, int] = {}
    for path in _iter_py():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        n = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler) or node.name is not None:
                continue
            if node.type is None:
                continue
            type_name = ""
            if isinstance(node.type, ast.Name):
                type_name = node.type.id
            elif isinstance(node.type, ast.Attribute):
                type_name = node.type.attr
            if type_name in {"Exception", "BaseException"}:
                n += 1
        if n:
            counts[str(path.relative_to(ROOT))] = n
    return counts


class NoNewUnboundBroadHandlersTests(unittest.TestCase):
    def test_no_new_unbound_broad_except(self):
        counts = _unbound_broad_handlers()
        regressions: list[str] = []
        for rel, n in sorted(counts.items()):
            allowed = BASELINE.get(rel, 0)
            if n > allowed:
                regressions.append(f"{rel}: {n} 个（基线 {allowed}）")
        self.assertEqual(
            regressions, [],
            "新增了未绑定异常对象的 `except Exception:`（栈信息会全丢，"
            "排查时看不到根因）—— 改成 `except Exception as exc:` 并记日志:\n  "
            + "\n  ".join(regressions),
        )


class NoNameErrorFromUndefinedLoggerTests(unittest.TestCase):
    """用了裸 `logger` 却没定义 —— 这类错误会被 `except Exception` 吞掉。"""

    def test_modules_using_logger_define_it(self):
        offenders: list[str] = []
        for path in _iter_py():
            text = path.read_text(encoding="utf-8", errors="replace")
            # `(?<![.\w])logger\.` 只匹配裸用法；`_facade.logger.` 不算
            if not re.search(r"(?<![.\w])logger\.", text):
                continue
            # 支持 `logger = ...` 与带类型标注的 `logger: X = ...`
            has_logger = re.search(
                r"^logger\s*(?::[^=]+)?=\s*(logging\.)?getLogger", text, re.M
            ) is not None
            if not has_logger:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(
            offenders, [],
            "这些模块用了裸 `logger` 却没定义（NameError 会被 except 吞掉）:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
