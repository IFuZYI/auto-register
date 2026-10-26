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
