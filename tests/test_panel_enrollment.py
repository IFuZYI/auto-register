"""面板登记：只有登记过的账号参与该面板的同步。

术语见 `GLOSSARY.md`：**登记** = 上传到某面板**并**纳入其同步范围。
ADR：`docs/adr/0002-panel-enrollment-and-bidirectional-sync.md`。

为什么需要它（用户 2026-10-26 报的困惑）：原来看不出「这个号传过没有」——
只能从「远端列表里有没有」推导，而对比表里唯一以凭据字段名出现的标签就是
`无RT`，于是用户以为「只同步了 RT」。
"""

from __future__ import annotations

import unittest


class EnrollmentStateTests(unittest.TestCase):
    """`extra.panel_uploads[<panel>]` 是登记的唯一判据。"""

    def test_upload_success_enrolls_the_panel(self):
        from services.panel_enrollment import enroll, enrolled_panels

        extra = {}
        enroll(extra, "chatgpt2api", remote_id="acc-1", ok=True, message="新增 1")

        self.assertEqual(enrolled_panels(extra), {"chatgpt2api"})
        record = extra["panel_uploads"]["chatgpt2api"]
        self.assertTrue(record.get("at"), "登记要记下时间（用户要看「上次什么时候传的」）")
        self.assertEqual(record.get("remote_id"), "acc-1")

    def test_upload_failure_does_not_enroll(self):
        from services.panel_enrollment import enroll, is_enrolled

        extra = {}
        enroll(extra, "chatgpt2api", remote_id="", ok=False, message="上传失败: 401")

        self.assertFalse(is_enrolled(extra, "chatgpt2api"),
                         "上传失败还被算作已登记 —— 对比表会显示「已登记但远端没有」")

    def test_enrollment_is_per_panel(self):
        from services.panel_enrollment import enroll, is_enrolled

        extra = {}
        enroll(extra, "chatgpt2api", remote_id="a", ok=True, message="ok")

        self.assertTrue(is_enrolled(extra, "chatgpt2api"))
        self.assertFalse(is_enrolled(extra, "cpa"),
                         "登记必须按面板独立 —— CPA 传 codex 凭据、chatgpt2api 传网页号")

    def test_can_be_revoked(self):
        from services.panel_enrollment import enroll, is_enrolled, revoke

        extra = {}
        enroll(extra, "chatgpt2api", remote_id="a", ok=True, message="ok")
        revoke(extra, "chatgpt2api")

        self.assertFalse(is_enrolled(extra, "chatgpt2api"), "登记应当可撤销")

    def test_remote_dropped_account_revokes_enrollment(self):
        """远端账号被删 → 自动撤销该面板登记（Q9）。

        不撤的话，本地一直以为「已登记」，同步会反复尝试拉一个不存在的账号。
        """
        from services.panel_enrollment import enroll, is_enrolled, reconcile_with_remote

        extra = {}
        enroll(extra, "chatgpt2api", remote_id="a", ok=True, message="ok")

        reconcile_with_remote(extra, "chatgpt2api", present=False)
        self.assertFalse(is_enrolled(extra, "chatgpt2api"),
                         "远端已经没有这个号了，本地还标着「已登记」")

    def test_present_remote_does_not_revoke(self):
        from services.panel_enrollment import enroll, is_enrolled, reconcile_with_remote

        extra = {}
        enroll(extra, "chatgpt2api", remote_id="a", ok=True, message="ok")
        reconcile_with_remote(extra, "chatgpt2api", present=True)
        self.assertTrue(is_enrolled(extra, "chatgpt2api"))

    def test_legacy_uploaded_flag_alone_is_not_enrollment(self):
        """旧的粘性格 `sync_statuses[x].uploaded` 不再作为登记判据。

        它 sticky 永久为真（`services/chatgpt_sync.py:88`），无法区分
        「登记过但远端后来没了」与「现在完整」；而且前端从不读它。
        """
        from services.panel_enrollment import is_enrolled

        extra = {"sync_statuses": {"chatgpt2api": {"uploaded": True, "uploaded_at": "x"}}}
        self.assertFalse(is_enrolled(extra, "chatgpt2api"),
                         "旧粘性标记被当成了登记 —— 它会永远为真，判不出撤销")

    def test_garbage_extra_does_not_raise(self):
        """脏数据不能让整个列表 500。"""
        from services.panel_enrollment import enrolled_panels, is_enrolled

        for bad in ({"panel_uploads": "not-a-dict"}, {"panel_uploads": {"chatgpt2api": 5}},
                    {"panel_uploads": None}):
            self.assertFalse(is_enrolled(bad, "chatgpt2api"))
            self.assertEqual(enrolled_panels(bad), set())


class SyncResultWritesEnrollmentTests(unittest.TestCase):
    """上传结果落库时要顺带登记 —— 那是所有上传路径的唯一汇聚点。

    `services/chatgpt_sync.py::_record_sync_result` 被自动上传、面板推送、
    单账号动作三处调用，所以在这里接一次就全覆盖。
    """

    def test_successful_sync_result_enrolls(self):
        from services.chatgpt_sync import _record_sync_result
        from services.panel_enrollment import is_enrolled

        extra = {}
        _record_sync_result(extra, "chatgpt2api", True, "上传成功（新增 1）")
        self.assertTrue(is_enrolled(extra, "chatgpt2api"),
                        "上传成功了却没有登记 —— 对比表看不出这个号传过")

    def test_failed_sync_result_does_not_enroll(self):
        from services.chatgpt_sync import _record_sync_result
        from services.panel_enrollment import is_enrolled

        extra = {}
        _record_sync_result(extra, "chatgpt2api", False, "上传失败: 401")
        self.assertFalse(is_enrolled(extra, "chatgpt2api"),
                         "上传失败还算登记 —— 会出现「已登记但远端没有」")

    def test_each_panel_enrolls_independently(self):
        from services.chatgpt_sync import _record_sync_result
        from services.panel_enrollment import enrolled_panels

        extra = {}
        _record_sync_result(extra, "chatgpt2api", True, "ok")
        _record_sync_result(extra, "cpa", True, "ok")
        self.assertEqual(enrolled_panels(extra), {"chatgpt2api", "cpa"})

    def test_legacy_sync_statuses_still_written(self):
        """旧的 `sync_statuses` 不能丢 —— 已有读方（cliproxy 的 pending_only）。"""
        from services.chatgpt_sync import _record_sync_result

        extra = {}
        state = _record_sync_result(extra, "chatgpt2api", True, "ok")
        self.assertTrue(state.get("uploaded"))
        self.assertTrue(state.get("uploaded_at"))
        self.assertIn("sync_statuses", extra)


if __name__ == "__main__":
    unittest.main()


class SyncScopeHonoursEnrollmentTests(unittest.TestCase):
    """「只有已登记的账号参与该面板的同步」必须真的生效。

    代码审查（2026-10-27 C3）发现：`panel_uploads` 只写不读 —— `plan_sync` /
    `plan_push` / 对比表都不看它，于是「登记」是纯记账，同步范围与改动前完全
    一样。ADR-0002 写的规则没有落地。
    """

    def _remote(self):
        from services.panel_comparison import RemoteAccount

        return RemoteAccount(
            email="a@b.c", platform="chatgpt",
            credentials={"access_token": "AT-remote"},
            updated_at="2026-01-02T00:00:00+00:00",
        )

    def test_unenrolled_account_is_skipped(self):
        from services.panel_sync import plan_sync

        outcome = plan_sync(
            {"access_token": "AT-local"},
            self._remote(),
            panel="chatgpt2api",
            enrolled=set(),          # 一个面板都没登记
        )
        self.assertFalse(outcome.pulled, "未登记的账号仍被拉回")
        self.assertEqual(outcome.reason, "not_enrolled")

    def test_enrolled_account_proceeds_normally(self):
        from services.panel_sync import plan_sync

        outcome = plan_sync(
            {"access_token": "AT-local"},
            self._remote(),
            panel="chatgpt2api",
            enrolled={"chatgpt2api"},
        )
        self.assertNotEqual(outcome.reason, "not_enrolled")

    def test_omitting_enrolled_keeps_old_behaviour(self):
        """不传 `enrolled` 时按原逻辑走（老调用方/测试不受影响）。"""
        from services.panel_sync import plan_sync

        outcome = plan_sync({"access_token": "AT-local"}, self._remote(), panel="x")
        self.assertNotEqual(outcome.reason, "not_enrolled")


class EnrollmentIsReadableFromExtraTests(unittest.TestCase):
    """`plan_sync` 的 `enrolled` 由调用方从账号 extra 取。"""

    def test_helper_reads_the_panel_uploads_table(self):
        from services.panel_enrollment import enroll, enrolled_panels

        extra = {}
        enroll(extra, "chatgpt2api", remote_id="r1", ok=True, message="ok")
        enroll(extra, "cpa", remote_id="r2", ok=True, message="ok")
        self.assertEqual(enrolled_panels(extra), {"chatgpt2api", "cpa"})


class RemoteDeletionRevokesEnrollmentTests(unittest.TestCase):
    """远端删号 → 本地自动撤销登记（ADR-0002）。

    代码审查（2026-10-27 C2）发现 `reconcile_with_remote` **零调用方** ——
    需求「远端删号自动撤销」在生产代码里不存在，只有定义与测试。
    不撤销的后果：本地一直以为「已登记」，同步反复去拉一个不存在的账号。
    """

    def test_reconcile_is_called_from_a_production_path(self):
        """结构性：生产代码里必须有调用方（不能只有定义与测试）。"""
        import re
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        callers = []
        for d in ("api", "services"):
            base = root / d
            if not base.exists():
                continue
            for path in base.rglob("*.py"):
                if "__pycache__" in path.parts:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                # 排除定义文件自身
                if path.name == "panel_enrollment.py":
                    continue
                if re.search(r"reconcile_with_remote\s*\(", text):
                    callers.append(str(path.relative_to(root)))
        self.assertTrue(
            callers,
            "`reconcile_with_remote` 没有任何生产调用方 —— "
            "「远端删号自动撤销」没有落地",
        )

    def test_revokes_when_remote_lacks_the_account(self):
        from services.panel_enrollment import enroll, is_enrolled, reconcile_with_remote

        extra = {}
        enroll(extra, "chatgpt2api", remote_id="r1", ok=True, message="ok")
        changed = reconcile_with_remote(extra, "chatgpt2api", present=False)
        self.assertTrue(changed, "撤销应当报告发生了变更（调用方据此决定要不要落库）")
        self.assertFalse(is_enrolled(extra, "chatgpt2api"))
