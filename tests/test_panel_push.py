"""`POST /api/integrations/panels/{key}/push`（「更新远程凭证」）的契约。

用户要求：「同步到最新要变成两个 —— 一个是更新本地、一个是更新远程。
更新本地是将远程新的数据同步到本地，更新远程是将本地新的推送到远程
（如可行删除远端旧的）。」

方向规则（与「更新本地」相反、互斥）：

- 远端没有（未上传）→ 推送；
- 凭证不同且**本地较新** → 推送（推送成功后可选删除旧远端记录）；
- 远端较新 → 不推（那是「更新本地」的职责）；
- 同小时 / 无法判定 / 凭证相同 / 无法比对 → 不动（保守，不拿不确定的数据动远端）。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.panel_comparison import RemoteAccount  # noqa: E402
from services.panel_push import plan_push  # noqa: E402


def _utc(*args) -> str:
    return datetime(*args, tzinfo=timezone.utc).isoformat()


class PlanPushDirectionTests(unittest.TestCase):
    """方向判定：未上传 / 本地较新才推。"""

    def test_not_uploaded_is_pushed(self):
        """远端没有这个账号 → 推送（补传）。"""
        outcome = plan_push(
            {"access_token": "at"}, None, local_updated=_utc(2026, 10, 4, 11, 0)
        )
        self.assertTrue(outcome.push)
        self.assertEqual(outcome.reason, "not_uploaded")
        self.assertEqual(outcome.remote_id, "", "远端没有记录，没有可删的")

    def test_local_newer_is_pushed(self):
        """凭证不同且本地较新 → 推送，并带上要删的旧远端记录 id。"""
        local = {"access_token": "new-at", "refresh_token": "new-rt"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc),
            credentials={"access_token": "old-at", "refresh_token": "old-rt"},
        )
        outcome = plan_push(local, remote, local_updated=_utc(2026, 10, 4, 12, 0))
        self.assertTrue(outcome.push)
        self.assertEqual(outcome.reason, "local_newer")
        self.assertEqual(
            outcome.remote_id, "r-1",
            "推送成功后应删除这条旧远端记录（新建式面板会留重复）",
        )

    def test_remote_newer_is_not_pushed(self):
        """远端较新 → 不推（那是「更新本地」的职责，推上去会覆盖远端好凭证）。"""
        local = {"access_token": "old-at", "refresh_token": "old-rt"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
            credentials={"access_token": "new-at", "refresh_token": "new-rt"},
        )
        outcome = plan_push(local, remote, local_updated=_utc(2026, 10, 4, 11, 0))
        self.assertFalse(outcome.push)
        self.assertEqual(outcome.reason, "remote_newer")
        self.assertEqual(outcome.remote_id, "", "不推就不该删远端记录")

    def test_synced_is_not_pushed(self):
        creds = {"access_token": "same-at", "refresh_token": "same-rt"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
            credentials=dict(creds),
        )
        outcome = plan_push(creds, remote, local_updated=_utc(2026, 10, 4, 11, 0))
        self.assertFalse(outcome.push)
        self.assertEqual(outcome.reason, "synced")

    def test_unknown_time_is_not_pushed(self):
        """时间比不了 → 不动（不拿不确定的数据动远端）。"""
        local = {"access_token": "local-at"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=None,
            credentials={"access_token": "remote-at"},
        )
        outcome = plan_push(local, remote, local_updated=_utc(2026, 10, 4, 11, 0))
        self.assertFalse(outcome.push)
        self.assertEqual(outcome.reason, "unknown_time")

    def test_unknown_credential_is_not_pushed(self):
        """远端不返回凭证（比不了）→ 不动（可能远端其实有同样的凭证）。"""
        local = {"access_token": "local-at"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc),
            credentials={},
        )
        outcome = plan_push(local, remote, local_updated=_utc(2026, 10, 4, 12, 0))
        self.assertFalse(outcome.push)
        self.assertIn(outcome.reason, {"remote_missing_credential", "unknown_credential"})

    def test_local_without_credentials_is_not_pushed(self):
        """本地没有任何可推的凭证 → 不推（推了也是空请求）。"""
        remote = RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc),
            credentials={"access_token": "remote-at"},
        )
        outcome = plan_push({}, remote, local_updated=_utc(2026, 10, 4, 12, 0))
        self.assertFalse(outcome.push)
        self.assertEqual(outcome.reason, "no_pushable_field")

    def test_timezone_difference_does_not_flip_the_verdict(self):
        """时区差异不能把「远端较新」误判成「本地较新」。

        远端 `2026-10-04T19:00+08:00` = `11:00Z`，本地 `12:00Z` 更新
        → 本地较新 → 推。
        """
        local = {"access_token": "local-at", "refresh_token": "local-rt"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            # +08:00 的 19:00 == 11:00 UTC（比本地 12:00Z 早 → 本地较新 → 推）
            updated_at=datetime(2026, 10, 4, 19, 0, tzinfo=timezone(timedelta(hours=8))),
            credentials={"access_token": "remote-at", "refresh_token": "remote-rt"},
        )
        outcome = plan_push(local, remote, local_updated=_utc(2026, 10, 4, 12, 0))
        self.assertTrue(
            outcome.push,
            "远端 11:00Z 比本地 12:00Z 早，应当推（时区归一失效？）",
        )
        self.assertEqual(outcome.reason, "local_newer")


if __name__ == "__main__":
    unittest.main()
