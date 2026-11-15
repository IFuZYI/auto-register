"""上游熔断：同一个远程服务连续失败后，别再让每个 worker 各自白等一轮。

为什么需要它
------------
2026-11-13 实测：`icloud-hme` 读超时，200 个账号的任务里每个 worker 都
各自发一次请求、各自等满 30s、各自判失败 —— 一次服务故障变成一个任务的
142 条失败记录。

熔断把「服务挂了」这个事实记一次，后续 worker 在**发请求前**就拿到可重试
失败：省掉白等、也省掉对已经过载的服务的请求。

设计约束
--------
- **进程内状态**。本项目强制单后端进程（docs/MAINTENANCE.md §1.6），
  与 iCloud 取号锁、Kiro 来源认领同一口径，不引 DB 表。
- **按服务地址分键**。挂在 A 服务不影响 B 服务。
- **冷却可以跨轮次**。失败记账不随某个 worker 结束而清零，否则
  下一轮 worker 又会重新撞一次。
"""
from __future__ import annotations

import threading
import time
import unittest

from services.icloud.upstream_gate import UpstreamGate, UpstreamBusy


class GateClosedTests(unittest.TestCase):
    def test_fresh_gate_lets_everything_through(self):
        gate = UpstreamGate()
        gate.check("svc-a")  # 不抛异常即可

    def test_failures_below_threshold_do_not_open(self):
        gate = UpstreamGate(threshold=3, cooldown_seconds=60)
        for _ in range(2):
            gate.note_failure("svc-a")
        gate.check("svc-a")

    def test_threshold_failures_open_the_gate(self):
        gate = UpstreamGate(threshold=3, cooldown_seconds=60)
        for _ in range(3):
            gate.note_failure("svc-a")
        with self.assertRaises(UpstreamBusy) as ctx:
            gate.check("svc-a")
        self.assertIn("svc-a", str(ctx.exception))

    def test_success_resets_the_counter(self):
        gate = UpstreamGate(threshold=3, cooldown_seconds=60)
        gate.note_failure("svc-a")
        gate.note_failure("svc-a")
        gate.note_success("svc-a")
        gate.note_failure("svc-a")
        gate.note_failure("svc-a")
        gate.check("svc-a")  # 只有 2 次连续失败，不该开

    def test_one_success_after_opening_closes_it(self):
        gate = UpstreamGate(threshold=2, cooldown_seconds=60)
        gate.note_failure("svc-a")
        gate.note_failure("svc-a")
        gate.note_success("svc-a")
        gate.check("svc-a")


class GateKeyingTests(unittest.TestCase):
    def test_services_are_independent(self):
        gate = UpstreamGate(threshold=1, cooldown_seconds=60)
        gate.note_failure("svc-a")
        with self.assertRaises(UpstreamBusy):
            gate.check("svc-a")
        gate.check("svc-b")  # B 不该被 A 拖累

    def test_empty_key_is_shared_fallback(self):
        gate = UpstreamGate(threshold=1, cooldown_seconds=60)
        gate.note_failure("")
        with self.assertRaises(UpstreamBusy):
            gate.check("")


class GateCooldownTests(unittest.TestCase):
    def test_gate_reopens_after_cooldown(self):
        gate = UpstreamGate(threshold=1, cooldown_seconds=0.05)
        gate.note_failure("svc-a")
        with self.assertRaises(UpstreamBusy):
            gate.check("svc-a")
        time.sleep(0.12)
        gate.check("svc-a")  # 冷却过了，放行

    def test_cooldown_backs_off_on_repeated_failures(self):
        """连续失败时冷却翻倍，但不超过上限 —— 服务长挂时别无限等。"""
        gate = UpstreamGate(
            threshold=1, cooldown_seconds=10, max_cooldown_seconds=40
        )
        assert_preview = []
        for _ in range(4):
            gate.note_failure("svc-a")
            assert_preview.append(gate.remaining_cooldown("svc-a"))
        # 10 -> 20 -> 40 -> 40（封顶）
        self.assertLessEqual(assert_preview[0], 10.0)
        self.assertLessEqual(assert_preview[1], 20.0)
        self.assertLessEqual(assert_preview[2], 40.0)
        self.assertLessEqual(assert_preview[3], 40.0)

    def test_remaining_cooldown_is_zero_when_closed(self):
        gate = UpstreamGate()
        self.assertEqual(gate.remaining_cooldown("svc-a"), 0.0)


class GateConcurrencyTests(unittest.TestCase):
    def test_counting_is_thread_safe(self):
        """5 个 worker 同时报错不能只记成 1 次。"""
        gate = UpstreamGate(threshold=10, cooldown_seconds=60)
        barrier = threading.Barrier(5)

        def report():
            barrier.wait(timeout=5)
            gate.note_failure("svc-a")

        threads = [threading.Thread(target=report) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(gate.failure_count("svc-a"), 5)


class GateSnapshotTests(unittest.TestCase):
    def test_snapshot_is_json_friendly(self):
        """熔断状态要能进任务日志 —— 只放基本类型。"""
        gate = UpstreamGate(threshold=2, cooldown_seconds=60)
        gate.note_failure("svc-a")
        snapshot = gate.snapshot()
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot["svc-a"]["failures"], 1)
        import json

        json.dumps(snapshot)  # 不可序列化就会抛


class AccountScopedBreakerTests(unittest.TestCase):
    """熔断的粒度：**一个主号**读不动 ≠ **整个服务**挂了。

    2026-11-14 实测：4 个远程主号共用一个 icloud-hme。`fuzy202609` 的收件
    读超时把 `base_url` 这个键顶开，剩下 3 个健康主号立刻全部被拒
    （376 行「熔断中」日志、212 次直接失败）。故障面从「一个信箱」被放大到
    「整批任务」。

    所以熔断要分两档：
    - 主号级：这个主号的读不动 → 只挡这个主号；
    - 服务级：连不上（连接被拒/断）或**多个主号**都撞成超时 → 整个服务值得
      怀疑，所有主号一起挡。
    """

    def _fail_account(self, gate, service, account, times):
        for _ in range(times):
            gate.note_account_failure(service, account)

    def test_one_account_timing_out_does_not_block_another(self):
        gate = UpstreamGate(threshold=2, cooldown_seconds=60)
        self._fail_account(gate, "svc", "acc_1", 3)

        with self.assertRaises(UpstreamBusy):
            gate.check("svc", account="acc_1")
        gate.check("svc", account="acc_2")  # 另一个主号照常放行

    def test_account_success_clears_only_that_account(self):
        gate = UpstreamGate(threshold=2, cooldown_seconds=60)
        self._fail_account(gate, "svc", "acc_1", 2)
        gate.note_account_success("svc", "acc_2")

        with self.assertRaises(UpstreamBusy):
            gate.check("svc", account="acc_1")

    def test_several_accounts_failing_opens_the_whole_service(self):
        """3 个不同主号都读不动 → 不是单个信箱的问题，整个服务一起挡。"""
        gate = UpstreamGate(
            threshold=2, cooldown_seconds=60, account_threshold_for_service=2
        )
        self._fail_account(gate, "svc", "acc_1", 2)
        self._fail_account(gate, "svc", "acc_2", 2)

        with self.assertRaises(UpstreamBusy):
            gate.check("svc", account="acc_9")

    def test_service_level_failure_blocks_every_account(self):
        """连不上是服务级：不需要等任何主号记账。"""
        gate = UpstreamGate(threshold=1, cooldown_seconds=60)
        gate.note_failure("svc")

        with self.assertRaises(UpstreamBusy):
            gate.check("svc", account="acc_1")
        with self.assertRaises(UpstreamBusy):
            gate.check("svc", account="acc_2")

    def test_check_without_account_keeps_service_only_behaviour(self):
        """不传 account 时只看服务级记账（老调用点不受影响）。"""
        gate = UpstreamGate(threshold=2, cooldown_seconds=60)
        self._fail_account(gate, "svc", "acc_1", 5)

        gate.check("svc")  # 服务级没记账，放行


class GateResetTests(unittest.TestCase):
    def test_reset_clears_one_key_only(self):
        gate = UpstreamGate(threshold=1, cooldown_seconds=60)
        gate.note_failure("svc-a")
        gate.note_failure("svc-b")
        gate.reset("svc-a")
        gate.check("svc-a")
        with self.assertRaises(UpstreamBusy):
            gate.check("svc-b")

    def test_reset_all_clears_everything(self):
        gate = UpstreamGate(threshold=1, cooldown_seconds=60)
        gate.note_failure("svc-a")
        gate.reset()
        gate.check("svc-a")


if __name__ == "__main__":
    unittest.main()