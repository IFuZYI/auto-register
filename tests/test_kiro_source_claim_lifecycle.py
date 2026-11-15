"""Kiro 注册任务的 AWS 来源认领必须覆盖并发选择与最终落库。

两个 worker 都停在模拟登录阶段时，旧实现会选中同一个 AWS 并读取同一封
OTP；在任一行保存前释放认领，也会让另一个 worker 再次选中它。
"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from core.base_platform import Account
from platforms.kiro.plugin import KiroPlatform
from platforms.kiro.source_claims import try_acquire


def _aws_row(email: str):
    return SimpleNamespace(
        email=email,
        password="Pw-1!",
        status="registered",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        get_extra=lambda: {},
    )


class _BlockingKiro(KiroPlatform):
    both_logins_started = threading.Barrier(2)
    instances: list["_BlockingKiro"] = []

    def __init__(self, config=None, mailbox=None):
        super().__init__(config=config, mailbox=mailbox)
        type(self).instances.append(self)

    def _reuse_aws_account(self, row, *, extra, proxy, plan):
        # 两个 worker 必须都选好来源，不能由一个先完成并改变数据库快照。
        self.both_logins_started.wait(timeout=10)
        return Account(platform="kiro", email=row.email, password=row.password)


class KiroSourceClaimLifecycleTests(unittest.TestCase):
    def test_parallel_task_workers_use_distinct_sources_and_release_after_save(self):
        from api.tasks import RegisterTaskRequest, _create_task_record, _run_register

        rows = [_aws_row("first@x.ai"), _aws_row("second@x.ai")]
        saved: list[Account] = []
        claims_held_at_save: list[bool] = []
        both_saving = threading.Barrier(2)

        def save(account):
            # 两个 worker 都已完成选择、进入保存阶段，才能检查对方的锁；
            # 否则一个 worker 可能在另一个尚未开始检查时就结束并释放。
            both_saving.wait(timeout=10)
            # **只断言「本条账号的来源此刻仍被某个 worker 持有」**。
            #
            # 原写法遍历 `instances` 断言「每个实例的 _source_claim 都不是
            # None」——那会和「落库后立刻释放」这个**合法**时序赛跑：A 已经
            # 保存完并调了 release_registration_resources（把 _source_claim
            # 置 None），B 才开始遍历，于是报假的「保存前已经释放来源」。
            # 全量并发跑时实测偶发（单跑 10/10 通过）。
            # 独占性仍由下面的 try_acquire 证明：抢不到 = 别人正持有。
            held = [
                w for w in _BlockingKiro.instances
                if w._source_claim is not None and w._source_claim.email == account.email
            ]
            self.assertTrue(held, f"保存前已经释放来源: {account.email}")
            email = held[0]._source_claim.email
            competing = try_acquire(email)
            claims_held_at_save.append(competing is None)
            if competing is not None:
                competing.release()
            saved.append(account)
            return account

        _BlockingKiro.instances = []
        _BlockingKiro.both_logins_started = threading.Barrier(2)
        req = RegisterTaskRequest(
            platform="kiro", count=2, concurrency=2, register_retry_times=0,
            extra={"mail_provider": "fake"},
        )
        task_id = "test-kiro-source-claims"
        _create_task_record(task_id, req, "manual", None)
        with (
            mock.patch("core.registry.get", return_value=_BlockingKiro),
            mock.patch("modules.mail.create_mailbox", return_value=SimpleNamespace()),
            mock.patch(
                "core.db.account_repository.list_all_accounts",
                side_effect=lambda *, platform: (
                    rows if platform == "aws" else list(saved)
                ),
            ),
            mock.patch(
                "core.db.account_repository.find_by_email",
                side_effect=lambda platform, email: next(
                    (row for row in saved if platform == "kiro" and row.email == email),
                    None,
                ),
            ),
            mock.patch("core.db.save_account", side_effect=save),
            mock.patch("services.task_service.auto_upload_integrations"),
            mock.patch("services.task_service.save_task_log"),
        ):
            _run_register(task_id, req)

        self.assertCountEqual([row.email for row in saved], ["first@x.ai", "second@x.ai"])
        self.assertTrue(claims_held_at_save, "没有执行到保存路径")
        self.assertTrue(all(claims_held_at_save), "保存前有来源可被其他任务认领")
        for email in ("first@x.ai", "second@x.ai"):
            claim = try_acquire(email)
            self.assertIsNotNone(claim, f"任务完成后没有释放 {email}")
            claim.release()


if __name__ == "__main__":
    unittest.main()


class RepeatedPickMustNotLeakClaimsTests(unittest.TestCase):
    """同一个 platform 连续挑号时，**不能把前一个认领泄漏掉**。

    2026-11-15 实测：`_pick_unused_aws()` 每次把新认领写进 `self._source_claim`，
    直接覆盖旧值而**不释放**它 —— 旧的 route 永久留在 `_CLAIMED_EMAILS` 里：

        第1次挑 → 37roaring-altar（认领）
        第2次挑 → 65_spade.winces（认领，覆盖 _source_claim）
        release_registration_resources() → 只释放 65_spade
        _CLAIMED_EMAILS 仍留 37roaring-altar  ← 泄漏，永远拿不回来

    Kiro 的候选池本来就浅（55 个 AWS 里只剩 3 个可复用）。一条泄漏路径
    足够让「还能用」的号变成「永远选不到」，这与「成功率低」是同一件事的
    两个方向：不是失败变多，而是**可用来源变少**。
    """

    def setUp(self):
        from platforms.kiro.source_claims import _CLAIMED_EMAILS, clear_source_failures

        clear_source_failures("leak-test")
        _CLAIMED_EMAILS.clear()

    def settle(self):
        pass

    def _platform(self):
        from core.base_platform import RegisterConfig
        from platforms.kiro.plugin import KiroPlatform

        p = KiroPlatform(config=RegisterConfig(extra={"kiro_source": "reuse"}))
        p._task_attempt_token = "leak-test"
        return p

    def test_second_pick_releases_the_first_claim(self):
        from datetime import datetime
        from types import SimpleNamespace
        from unittest import mock

        from platforms.kiro.source_claims import _CLAIMED_EMAILS

        def _row(email, day):
            return SimpleNamespace(
                email=email, password="Pw-1!", status="registered",
                created_at=datetime(2026, 1, day), get_extra=lambda: {},
            )

        rows = [_row("a@x.ai", 1), _row("b@x.ai", 2)]
        p = self._platform()
        # 用注入的假数据隔离真实库（库里候选会被别的测试/真实状态影响）
        with mock.patch(
            "core.db.account_repository.list_all_accounts",
            side_effect=lambda platform: rows if platform == "aws" else [],
        ):
            first = p._pick_unused_aws()
            self.assertIsNotNone(first, "没有可用候选，测试前提不成立")
            second = p._pick_unused_aws()
            self.assertIsNotNone(second, "第二个候选拿不到")
            self.assertNotEqual(first.email, second.email)

        p.release_registration_resources()
        self.assertEqual(
            _CLAIMED_EMAILS, set(),
            f"释放后仍有认领泄漏: {sorted(_CLAIMED_EMAILS)} —— 这些号永远选不回来了",
        )
