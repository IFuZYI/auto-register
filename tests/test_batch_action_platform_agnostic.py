"""批量动作必须对**任意平台**生效，不许硬编码 chatgpt。

## 实测的缺陷（2026-10-28）

用户要求「所有作业都是任务」，但 Grok 的批量动作跑不起来 —— 三层硬编码：

1. `services/chatgpt_account_selection.py`：`.where(AccountModel.platform == "chatgpt")`
   → 建任务时 grok 账号被判「所选账号不存在」；
2. `services/task_runners._load_account_fields`：`platform_session("chatgpt")`
   + `account.platform != "chatgpt"` → 任务建起来了，执行时每个号都「不存在」；
3. `services/chatgpt_rt_backfill.select_backfill_targets` 不透传 platform。

Grok 平台库里 id=1（`dear-shrouds-1w@icloud.com`）确实存在，端点返回
`{"detail":"所选账号不存在"}`。

## 为什么这类 bug 特别隐蔽

单测都绿：既有测试只喂 chatgpt 账号。而真实使用里 grok 是**另一个平台**，
它走的正是同一条代码。所以这条测试直接断言「选择函数接受 platform」，并用
**非 chatgpt** 的数据证明它能查到。
"""

from __future__ import annotations

import unittest


class SelectionAcceptsPlatformTests(unittest.TestCase):
    def test_select_chatgpt_accounts_has_a_platform_parameter(self):
        import inspect

        from services.chatgpt_account_selection import select_chatgpt_accounts

        params = inspect.signature(select_chatgpt_accounts).parameters
        self.assertIn(
            "platform", params,
            "选择函数没有 platform 参数 —— 它只能查 chatgpt，其它平台的批量动作必挂",
        )

    def test_select_backfill_targets_passes_platform_through(self):
        import inspect

        from services.chatgpt_rt_backfill import select_backfill_targets

        params = inspect.signature(select_backfill_targets).parameters
        self.assertIn("platform", params, "select_backfill_targets 不透传 platform")

    def test_load_account_fields_has_a_platform_parameter(self):
        import inspect

        from services import task_runners

        params = inspect.signature(task_runners._load_account_fields).parameters
        self.assertIn(
            "platform", params,
            "_load_account_fields 没有 platform 参数 —— 它只会从 chatgpt 库读账号",
        )

    def test_run_account_batch_task_has_a_platform_parameter(self):
        import inspect

        from services import task_runners

        params = inspect.signature(task_runners._run_account_batch_task).parameters
        self.assertIn("platform", params, "批量任务骨架不透传 platform")


class NoHardcodedChatgptInTheSelectionPathTests(unittest.TestCase):
    """结构性：选择路径里不许再出现字面量 `"chatgpt"` 作为 platform 过滤。

    这比单测更能防回归 —— 三条硬编码当时都躲过了既有测试。
    """

    def test_no_hardcoded_platform_filter(self):
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        targets = [
            root / "services" / "chatgpt_account_selection.py",
        ]
        offenders: list[str] = []
        pattern = re.compile(r'AccountModel\.platform\s*==\s*"chatgpt"')
        for path in targets:
            text = path.read_text(encoding="utf-8")
            for m in pattern.finditer(text):
                line = text[: m.start()].count("\n") + 1
                offenders.append(f"{path.name}:{line}")
        self.assertEqual(
            offenders, [],
            "选择路径里仍有硬编码 platform 过滤 —— 其它平台必挂：\n  "
            + "\n  ".join(offenders),
        )


class GrokAccountIsSelectableTests(unittest.TestCase):
    """端到端：用 grok 账号调选择函数，必须能选中（不是 missing）。"""

    def test_grok_account_is_found(self):
        from sqlmodel import select

        from core.db import AccountModel, account_repository, platform_session
        from services.chatgpt_account_selection import select_chatgpt_accounts

        email = "route-grok-select@x.c"
        try:
            # 直接在 grok 库里建行：`account_repository.upsert` 在测试的
            # 多库环境下会走 `_session_for`，拿到的行是 detached，`id` 读不到
            # （实测）；这里用显式会话建，语义相同但可控。
            with platform_session("grok") as s:
                row = AccountModel(
                    platform="grok", email=email, password="pw",
                    status="registered", extra_json="{}",
                )
                s.add(row)
                s.commit()
                s.refresh(row)
                real_id = row.id
            self.assertIsNotNone(real_id, "测试账号没建上")

            with platform_session("grok") as s:
                accs, missing = select_chatgpt_accounts(
                    s, platform="grok", account_ids=[real_id]
                )
            self.assertEqual(missing, [], f"grok 账号被判成不存在: {missing}")
            self.assertEqual([a.id for a in accs], [real_id])
        finally:
            with platform_session("grok") as s:
                row = s.exec(
                    select(AccountModel).where(
                        AccountModel.platform == "grok",
                        AccountModel.email == email,
                    )
                ).first()
                if row is not None:
                    s.delete(row)
                    s.commit()


if __name__ == "__main__":
    unittest.main()
