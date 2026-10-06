"""ChatGPT Token 自动维护（用户要求 2026-10-06）。

两条功能：

1. **自动刷新临期/过期 Token**：后台定期扫描 ChatGPT 账号，对 AT 临期
   （剩余 ≤ 24h）或已过期的账号，在「临期时间段内随机一个时刻」刷新 ——
   不设固定更新时刻（避免可预测、避免同一时刻集中刷新），且不拖到过期
   （随机窗口上限 = 到期前 1 小时）。封禁的、无法更新的不重复尝试。
2. **chatgpt2api 凭证自动维护**：定期对比本地与 chatgpt2api 远端凭证，
   本地较新时自动推送更新，使远端始终持有最新凭证。

本文件按 TDD 逐切片写：先纯函数（随机时刻 / 计划决策 / 结果写回），
再扫描与发起（集成），最后接线（scheduler / config / 前端）。
"""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ComputeNextAttemptAtTests(unittest.TestCase):
    """随机刷新时刻的计算（纯函数）。

    窗口规则：
    - 剩余 > 24h：未临期，None（不排计划）；
    - 1h < 剩余 ≤ 24h：在 [now, exp - 1h] 内均匀随机 —— 保证至少提前
      1 小时刷新；
    - 剩余 ≤ 1h 或已过期：尽快 + 小抖动（[now, now+300s]），避免同秒并发。
    """

    def test_far_from_expiry_returns_none(self):
        from services.chatgpt_maintenance import compute_next_attempt_at

        now = 1_000_000
        self.assertIsNone(
            compute_next_attempt_at(now + 48 * 3600, now, rand=lambda: 0.5),
            "剩余 48h 不该排计划",
        )
        self.assertIsNone(
            compute_next_attempt_at(now + 24 * 3600 + 1, now, rand=lambda: 0.5),
            "刚好超过 24h 不该排计划",
        )

    def test_expiring_window_is_uniform_between_now_and_one_hour_before(self):
        from services.chatgpt_maintenance import compute_next_attempt_at

        now = 1_000_000
        expires_at = now + 10 * 3600  # 剩余 10h
        window = expires_at - 3600 - now  # 可随机跨度

        lo = compute_next_attempt_at(expires_at, now, rand=lambda: 0.0)
        hi = compute_next_attempt_at(expires_at, now, rand=lambda: 0.999999)
        mid = compute_next_attempt_at(expires_at, now, rand=lambda: 0.5)

        self.assertEqual(lo, now, "rand=0 → 立即（窗口起点）")
        self.assertLessEqual(hi, expires_at - 3600, "窗口上限 = 到期前 1 小时")
        self.assertAlmostEqual(mid, now + window * 0.5, delta=2)
        # 至少提前 1 小时：任何采样都不晚于 exp - 3600
        for r in (0.1, 0.3, 0.7, 0.9):
            at = compute_next_attempt_at(expires_at, now, rand=lambda: r)
            self.assertLessEqual(at, expires_at - 3600)

    def test_near_expiry_uses_small_jitter(self):
        from services.chatgpt_maintenance import compute_next_attempt_at

        now = 1_000_000
        expires_at = now + 1800  # 只剩 30 分钟

        lo = compute_next_attempt_at(expires_at, now, rand=lambda: 0.0)
        hi = compute_next_attempt_at(expires_at, now, rand=lambda: 0.999999)
        self.assertEqual(lo, now)
        self.assertGreater(hi, now)
        self.assertLessEqual(hi, now + 300, "紧急窗口的抖动上限 5 分钟")

    def test_expired_uses_small_jitter(self):
        from services.chatgpt_maintenance import compute_next_attempt_at

        now = 1_000_000
        expires_at = now - 9999  # 已过期

        lo = compute_next_attempt_at(expires_at, now, rand=lambda: 0.0)
        hi = compute_next_attempt_at(expires_at, now, rand=lambda: 0.999999)
        self.assertEqual(lo, now)
        self.assertLessEqual(hi, now + 300)


class PlanAutoRefreshTests(unittest.TestCase):
    """每账号的计划决策（纯函数）。

    状态存在 extra 的 `chatgpt_auto_refresh`：
    `{at, for_exp, attempts, disabled}`。规则：
    - banned / 无 exp / 未临期 → skip；
    - 有计划（for_exp == 当前 exp）：未到点 wait、到点 due；disabled → skip；
    - 无计划或 for_exp 变了（AT 已换发）→ 重排（清 attempts/disabled）。
    """

    def _plan(self, extra, *, expires_at, status="registered", now=1_000_000, rand=None):
        from services.chatgpt_maintenance import plan_auto_refresh

        return plan_auto_refresh(
            extra,
            expires_at=expires_at,
            status=status,
            now=now,
            rand=rand or (lambda: 0.5),
        )

    def test_banned_is_skipped(self):
        plan = self._plan({}, expires_at=1_000_000 + 3600, status="banned")
        self.assertEqual(plan.action, "skip")
        self.assertEqual(plan.reason, "banned")

    def test_no_expiry_is_skipped(self):
        plan = self._plan({}, expires_at=None)
        self.assertEqual(plan.action, "skip")
        self.assertEqual(plan.reason, "no_expiry")

    def test_far_from_expiry_is_skipped(self):
        plan = self._plan({}, expires_at=1_000_000 + 48 * 3600)
        self.assertEqual(plan.action, "skip")
        self.assertEqual(plan.reason, "not_expiring")

    def test_expiring_without_state_gets_schedule(self):
        expires_at = 1_000_000 + 10 * 3600
        plan = self._plan({}, expires_at=expires_at, rand=lambda: 0.5)
        self.assertEqual(plan.action, "schedule")
        state = plan.state
        self.assertIsNotNone(state)
        self.assertEqual(state["for_exp"], expires_at)
        self.assertEqual(state["attempts"], 0)
        self.assertFalse(state["disabled"])
        self.assertGreater(state["at"], 1_000_000)
        self.assertLessEqual(state["at"], expires_at - 3600)

    def test_scheduled_future_is_wait(self):
        expires_at = 1_000_000 + 10 * 3600
        extra = {
            "chatgpt_auto_refresh": {
                "at": 1_000_000 + 3600,
                "for_exp": expires_at,
                "attempts": 0,
                "disabled": False,
            }
        }
        plan = self._plan(extra, expires_at=expires_at)
        self.assertEqual(plan.action, "wait")
        self.assertEqual(plan.at, 1_000_000 + 3600)

    def test_scheduled_due_is_due(self):
        expires_at = 1_000_000 + 10 * 3600
        extra = {
            "chatgpt_auto_refresh": {
                "at": 1_000_000 - 1,
                "for_exp": expires_at,
                "attempts": 1,
                "disabled": False,
            }
        }
        plan = self._plan(extra, expires_at=expires_at)
        self.assertEqual(plan.action, "due")

    def test_disabled_state_for_same_exp_is_skipped(self):
        """连续失败达上限后：同一 AT 不再尝试（用户要求不重复尝试）。"""
        expires_at = 1_000_000 + 10 * 3600
        extra = {
            "chatgpt_auto_refresh": {
                "at": 1_000_000 + 24 * 3600,
                "for_exp": expires_at,
                "attempts": 3,
                "disabled": True,
            }
        }
        plan = self._plan(extra, expires_at=expires_at)
        self.assertEqual(plan.action, "skip")
        self.assertEqual(plan.reason, "disabled")

    def test_state_for_other_exp_is_replanned(self):
        """AT 已换发（exp 变了）→ 旧计划作废重排，disabled 一并复位。"""
        expires_at = 1_000_000 + 10 * 3600
        extra = {
            "chatgpt_auto_refresh": {
                "at": 1_000_000 - 5,
                "for_exp": 999_999_999,  # 旧的 exp（换发前）
                "attempts": 3,
                "disabled": True,
            }
        }
        plan = self._plan(extra, expires_at=expires_at, rand=lambda: 0.5)
        self.assertEqual(plan.action, "schedule")
        self.assertEqual(plan.state["for_exp"], expires_at)
        self.assertFalse(plan.state["disabled"])
        self.assertEqual(plan.state["attempts"], 0)


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


class RunAutoRefreshPassTests(unittest.TestCase):
    """扫描执行：due 的账号逐个串行执行、结果落库（真实 DB + 注入 execute）。"""

    def setUp(self):
        from sqlmodel import Session, delete

        from core.db import AccountModel, engine

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.commit()
        self._now = int(__import__("time").time())

    def _at(self, exp):
        import base64
        import json

        payload = base64.urlsafe_b64encode(
            json.dumps({"exp": exp}).encode()
        ).decode().rstrip("=")
        return f"header.{payload}.sig"

    def _insert(self, email, *, extra, status="registered"):
        from sqlmodel import Session

        from core.db import AccountModel, engine

        model = AccountModel(platform="chatgpt", email=email, password="pw", status=status)
        model.set_extra(extra)
        with Session(engine) as session:
            session.add(model)
            session.commit()
            session.refresh(model)
        return int(model.id)

    def _extra_of(self, account_id):
        from sqlmodel import Session

        from core.db import AccountModel, engine

        with Session(engine) as session:
            row = session.get(AccountModel, account_id)
            return row.get_extra()

    def _run(self, **kwargs):
        from services.chatgpt_maintenance import run_auto_refresh_pass

        kwargs.setdefault("now", self._now)
        kwargs.setdefault("delay_seconds", 0)
        return run_auto_refresh_pass(**kwargs)

    def _due_state(self, exp):
        return {"at": self._now - 5, "for_exp": exp, "attempts": 0, "disabled": False}

    def test_due_account_is_executed_and_state_cleared_on_success(self):
        exp = self._now + 10 * 3600
        aid = self._insert(
            "due@example.com",
            extra={"access_token": self._at(exp), "chatgpt_auto_refresh": self._due_state(exp)},
        )
        calls = []

        def fake_execute(account_id):
            calls.append(account_id)
            return {"ok": True, "data": {"refreshed": True}}

        summary = self._run(execute=fake_execute)
        self.assertEqual(calls, [aid])
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["ok"], 1)
        self.assertNotIn(
            "chatgpt_auto_refresh", self._extra_of(aid), "成功后计划要清掉"
        )

    def test_failure_records_backoff(self):
        exp = self._now + 10 * 3600
        aid = self._insert(
            "fail@example.com",
            extra={"access_token": self._at(exp), "chatgpt_auto_refresh": self._due_state(exp)},
        )

        summary = self._run(execute=lambda _aid: {"ok": False, "error": "网络错误"})
        self.assertEqual(summary["failed"], 1)
        state = self._extra_of(aid)["chatgpt_auto_refresh"]
        self.assertEqual(state["attempts"], 1)
        self.assertFalse(state["disabled"])
        self.assertGreaterEqual(state["at"], self._now + 3600 - 1)

    def test_success_without_reissue_counts_as_failure(self):
        """ok 但未换发（服务端原样返还）→ 视为一次失败 —— 不重复尝试
        「无法更新」的账号（用户要求）。"""
        exp = self._now + 10 * 3600
        aid = self._insert(
            "noop@example.com",
            extra={"access_token": self._at(exp), "chatgpt_auto_refresh": self._due_state(exp)},
        )

        summary = self._run(
            execute=lambda _aid: {"ok": True, "data": {"refreshed": False}}
        )
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(self._extra_of(aid)["chatgpt_auto_refresh"]["attempts"], 1)

    def test_execute_exception_recorded_as_failure(self):
        exp = self._now + 10 * 3600
        aid = self._insert(
            "boom@example.com",
            extra={"access_token": self._at(exp), "chatgpt_auto_refresh": self._due_state(exp)},
        )

        def boom(_aid):
            raise RuntimeError("explode")

        summary = self._run(execute=boom)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(self._extra_of(aid)["chatgpt_auto_refresh"]["attempts"], 1)

    def test_banned_account_is_not_executed(self):
        exp = self._now + 10 * 3600
        self._insert(
            "banned@example.com",
            extra={"access_token": self._at(exp), "chatgpt_auto_refresh": self._due_state(exp)},
            status="banned",
        )
        calls = []

        summary = self._run(execute=lambda aid: calls.append(aid) or {"ok": True})
        self.assertEqual(calls, [], "封禁账号不得执行（用户要求不重复尝试）")
        self.assertGreaterEqual(summary["skipped"], 1)

    def test_disabled_state_is_not_executed(self):
        exp = self._now + 10 * 3600
        state = self._due_state(exp)
        state["disabled"] = True
        state["attempts"] = 3
        self._insert("disabled@example.com", extra={"access_token": self._at(exp), "chatgpt_auto_refresh": state})
        calls = []

        self._run(execute=lambda aid: calls.append(aid) or {"ok": True})
        self.assertEqual(calls, [])

    def test_expiring_account_gets_schedule_written_without_execution(self):
        exp = self._now + 10 * 3600
        aid = self._insert("plan@example.com", extra={"access_token": self._at(exp)})
        calls = []

        summary = self._run(execute=lambda aid: calls.append(aid) or {"ok": True})
        self.assertEqual(calls, [], "首次扫描只排计划，不到点不执行")
        self.assertEqual(summary["scheduled"], 1)
        state = self._extra_of(aid)["chatgpt_auto_refresh"]
        self.assertEqual(state["for_exp"], exp)
        self.assertGreater(state["at"], self._now)
        self.assertLessEqual(state["at"], exp - 3600, "至少提前 1 小时")

    def test_far_from_expiry_is_skipped_without_state(self):
        exp = self._now + 48 * 3600
        aid = self._insert("far@example.com", extra={"access_token": self._at(exp)})

        summary = self._run(execute=lambda _aid: {"ok": True})
        self.assertEqual(summary["executed"], 0)
        self.assertNotIn("chatgpt_auto_refresh", self._extra_of(aid))

    def test_max_accounts_limits_execution_per_pass(self):
        """避免高并发：每轮限量，其余下轮再跑。"""
        ids = []
        for i in range(3):
            exp = self._now + 10 * 3600
            ids.append(
                self._insert(
                    f"many{i}@example.com",
                    extra={"access_token": self._at(exp), "chatgpt_auto_refresh": self._due_state(exp)},
                )
            )
        calls = []

        summary = self._run(execute=lambda aid: calls.append(aid) or {"ok": True, "data": {"refreshed": True}}, max_accounts=1)
        self.assertEqual(len(calls), 1, "每轮最多执行 max_accounts 个")
        self.assertEqual(summary["due"], 3, "其余 due 的留在下一轮")
        self.assertIn(calls[0], ids)

    def test_stale_plan_is_replanned_not_executed(self):
        """扫描与执行之间 AT 被手动刷新（exp 变了）→ 不执行，按新 exp 重排。"""
        old_exp = self._now + 10 * 3600
        new_exp = self._now + 7 * 24 * 3600
        # 状态针对旧 exp 且已 due；但当前 AT 已换发（新 exp 很远 → 不再临期）
        aid = self._insert(
            "stale@example.com",
            extra={
                "access_token": self._at(new_exp),
                "chatgpt_auto_refresh": self._due_state(old_exp),
            },
        )
        calls = []

        self._run(execute=lambda aid: calls.append(aid) or {"ok": True})
        self.assertEqual(calls, [], "AT 已换发后旧计划作废，不得执行")
        self.assertNotIn("chatgpt_auto_refresh", self._extra_of(aid), "未临期不重排")

    def test_account_deleted_mid_pass_does_not_kill_the_round(self):
        """复审 E1（已复现）：执行时账号被删 + 刷新失败 → 不得崩整轮。

        旧实现里 `state` 只在 `row is not None` 分支里绑定；账号在扫描与
        写回之间被删（用户手动删除 / 另一进程清库）且结果失败时，
        失败日志引用未绑定的 `state` → UnboundLocalError 中断整轮 ——
        违反「单账号失败不该毁整轮」的不变量。
        """
        exp = self._now + 10 * 3600
        self._insert(
            "vanish@example.com",
            extra={"access_token": self._at(exp), "chatgpt_auto_refresh": self._due_state(exp)},
        )

        def delete_then_fail(account_id):
            from sqlmodel import Session

            from core.db import AccountModel, engine

            with Session(engine) as s:
                row = s.get(AccountModel, account_id)
                if row is not None:
                    s.delete(row)
                    s.commit()
            return {"ok": False, "error": "模拟：账号已消失"}

        summary = self._run(execute=delete_then_fail)
        self.assertEqual(summary["executed"], 1)
        self.assertEqual(summary["failed"], 1, "账号消失按一次失败记，整轮不崩")

    def test_corrupted_state_values_do_not_kill_the_pass(self):
        """复审 E2（已复现）：extra 状态值损坏时整轮不得崩。

        仓库惯例是脏数据容忍（见 `AccountModel.get_extra` 与
        `chatgpt_token_lifecycle._positive_int`）。旧实现对持久化的
        `at` / `for_exp` / `attempts` 裸 `int()` —— 一个 `at="xyz"`
        的脏行会 ValueError 中断整轮扫描。
        """
        exp = self._now + 10 * 3600
        # 三种脏字段各一行：at / for_exp / attempts
        aid_at = self._insert(
            "corrupt-at@example.com",
            extra={
                "access_token": self._at(exp),
                "chatgpt_auto_refresh": {"at": "xyz", "for_exp": exp, "attempts": 0, "disabled": False},
            },
        )
        aid_exp = self._insert(
            "corrupt-exp@example.com",
            extra={
                "access_token": self._at(exp),
                "chatgpt_auto_refresh": {"at": self._now - 5, "for_exp": "garbage", "attempts": 0, "disabled": False},
            },
        )
        aid_attempts = self._insert(
            "corrupt-attempts@example.com",
            extra={
                "access_token": self._at(exp),
                "chatgpt_auto_refresh": {
                    "at": self._now - 5, "for_exp": exp, "attempts": "nope", "disabled": False,
                },
            },
        )

        summary = self._run(execute=lambda _aid: {"ok": False, "error": "x"})
        # 整轮不崩：at/attempts 损坏的两行进执行（脏值安全回退 0），
        # for_exp 损坏的行按「无有效计划」重排（safe recovery，不执行）。
        self.assertGreaterEqual(summary["executed"], 2, "脏行不得让扫描中断")
        self.assertGreaterEqual(summary["scheduled"], 1, "for_exp 损坏应按重排恢复")
        for aid in (aid_at, aid_exp, aid_attempts):
            self.assertIsInstance(self._extra_of(aid), dict)


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
            "chatgpt2api_auto_sync_enabled",
            "chatgpt2api_api_url",
            "chatgpt2api_api_key",
        ):
            config_store.set(key, "")

    def test_auto_refresh_interval_zero_when_disabled(self):
        from services.chatgpt_maintenance import get_auto_refresh_interval_seconds

        self.assertEqual(get_auto_refresh_interval_seconds(), 0)

    def test_auto_refresh_interval_positive_when_enabled(self):
        from services.chatgpt_maintenance import get_auto_refresh_interval_seconds

        self.config_store.set("chatgpt_auto_refresh_enabled", "1")
        self.assertGreater(get_auto_refresh_interval_seconds(), 0)

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
    """模块导入时向 scheduler 自注册两个周期任务；main.py 负责 import。"""

    def test_jobs_are_registered_on_import(self):
        import services.chatgpt_maintenance  # noqa: F401  —— 注册发生在导入期
        from core.scheduler import registered_jobs

        jobs = registered_jobs()
        self.assertIn("chatgpt_auto_refresh", jobs)
        self.assertIn("chatgpt2api_auto_sync", jobs)

    def test_auto_refresh_job_interval_reads_the_switch(self):
        """任务间隔直读开关 —— 界面关掉后下一 tick 即跳过（返回 0）。"""
        import services.chatgpt_maintenance  # noqa: F401
        from core.config_store import config_store
        from core.scheduler import _jobs

        job = _jobs["chatgpt_auto_refresh"]
        config_store.set("chatgpt_auto_refresh_enabled", "")
        self.assertEqual(job.interval(), 0)
        config_store.set("chatgpt_auto_refresh_enabled", "1")
        self.assertGreater(job.interval(), 0)
        config_store.set("chatgpt_auto_refresh_enabled", "")

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
    """面板配置页要有两个开关（用户要求「在配置界面选择是否开启」）。"""

    def _src(self, rel: str) -> str:
        return (ROOT / "frontend" / "src" / rel).read_text(encoding="utf-8")

    def test_panel_config_has_both_switches(self):
        src = self._src("components/settings/PanelConfigPanel.tsx")
        self.assertIn("chatgpt_auto_refresh_enabled", src)
        self.assertIn("chatgpt2api_auto_sync_enabled", src)

    def test_switches_are_boolean_typed(self):
        src = self._src("components/settings/PanelConfigPanel.tsx")
        for key in ("chatgpt_auto_refresh_enabled", "chatgpt2api_auto_sync_enabled"):
            idx = src.find(f"key: '{key}'")
            self.assertGreater(idx, -1, f"缺 {key} 字段声明")
            block = src[idx : idx + 200]
            self.assertIn("type: 'boolean'", block, f"{key} 不是开关")

    def test_boolean_keys_include_maintenance_switches(self):
        """BOOLEAN_KEYS 漏加 → 保存时 "true" 字符串被原样写入但回读不归一。"""
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
        self.assertGreaterEqual(len(keys), 9, f"BOOLEAN_KEYS 解析异常: {keys}")

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


if __name__ == "__main__":
    unittest.main()
