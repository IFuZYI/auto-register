"""对比时间判定的**单侧回落**契约（用户实测报的 bug）。

用户实测（2026-10-05）：「2026-10-02 21:57 | 2026-10-04 19:04 | 状态 active
| 1 项相同 | 本地较新 —— 不对吧，这个时间全是由 AT 判断的，而且为什么还是
本地新，对比有问题吧」

复现（grok2api 的 web 线账号，22 个同病）：
- 本地 AT 签发时间 10-02 13:57Z（列显示 10-02 21:57）；
- 远端**只有 SSO、没有 AT** → 远端列回落显示记录时间 10-04 11:04Z
  （列显示 10-04 19:04）；
- 判定却回落到**两侧的记录时间**：本地记录 10-05 10:04Z（被「同步远端
  状态」批量 touch 成噪声，32 行同一毫秒）vs 远端 10-04 11:04Z
  → 判出「本地较新」—— 与两列显示的值（10-02 vs 10-04）自相矛盾。

根因：`compare_credential_time` 要求**两侧都能解出 iat** 才出结论，
否则**整体**回落记录时间。一侧有、一侧没有（grok2api web 线常态）时，
有 iat 的那侧的信息被丢弃，拿噪声记录时间去比。

修法：**单侧回落** —— 每侧独立取「iat 优先、无则回落该侧记录时间」，
与「本地/远端更新时间」两列的显示口径完全一致（显示什么就比什么）。
两侧都取不到任何时间才返回空串。

本文件按 TDD 先写（RED），修 `compare_credential_time` 后转绿。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.panel_comparison import (  # noqa: E402
    RemoteAccount,
    build_comparison,
    compare_credential_time,
)


def _jwt_with_iat(iat: int) -> str:
    """造一个带 `iat` 的 JWT 形状 token（对比只解 payload，不验签）。"""
    import base64
    import json

    payload = (
        base64.urlsafe_b64encode(json.dumps({"iat": iat, "sub": "u-1"}).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return f"h.{payload}.s"


def _epoch(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


class SingleSideIssuanceTests(unittest.TestCase):
    """一侧有 iat、另一侧没有 → 有的一侧仍参与比较（不回落到全噪声）。"""

    def test_local_iat_vs_remote_record_time(self):
        """本地有 iat（旧）、远端无 iat 但有记录时间（新）→ 远端较新。

        用户实测行：本地 AT 10-02 13:57Z；远端只有 SSO，记录时间
        10-04 11:04Z（且本地记录时间 10-05 10:04Z 是状态同步噪声）。
        正确结论：远端较新（10-04 > 10-02）。
        """
        local = {"access_token": _jwt_with_iat(_epoch(2026, 10, 2, 13, 57))}
        remote = {"sso": "sso-only"}  # 远端解不出 iat
        relation = compare_credential_time(
            local, remote,
            local_record="2026-10-05T10:04:08+00:00",   # 噪声（状态同步 touch）
            remote_record="2026-10-04T11:04:27+00:00",  # 远端记录时间（真实）
        )
        self.assertEqual(
            relation, "remote_newer",
            "本地 iat（10-02）比远端记录时间（10-04）旧 → 应判远端较新；"
            "整体回落记录时间会拿噪声（10-05）判反",
        )

    def test_remote_iat_vs_local_record_time(self):
        """远端有 iat（新）、本地无 iat → 远端较新（远端侧信息不被丢）。"""
        remote = {"access_token": _jwt_with_iat(_epoch(2026, 10, 5, 1, 18))}
        relation = compare_credential_time(
            {"sso": "sso-only"},
            remote,
            local_record="2026-10-04T21:26:00+00:00",
            remote_record="2026-10-04T15:30:00+00:00",
        )
        self.assertEqual(relation, "remote_newer")

    def test_same_hour_across_sources_is_time_synced(self):
        """本地 iat 与远端记录时间同小时 → 分秒是噪声，不算谁新。"""
        local = {"access_token": _jwt_with_iat(_epoch(2026, 10, 4, 11, 5))}
        relation = compare_credential_time(
            local, {"sso": "sso-only"},
            local_record="2026-10-04T11:30:00+00:00",
            remote_record="2026-10-04T11:50:00+00:00",
        )
        self.assertEqual(relation, "time_synced")

    def test_no_time_at_all_has_no_verdict(self):
        """两侧都取不到任何时间（iat 与记录时间都没有）→ 空串（无从判定）。"""
        self.assertEqual(compare_credential_time({"sso": "x"}, {"sso": "y"}), "")

    def test_both_sides_iat_still_wins_over_record_time(self):
        """两侧都有 iat 时仍以 iat 为准（记录时间不参与）—— 保持既有口径。"""
        local = {"access_token": _jwt_with_iat(_epoch(2026, 10, 5, 3, 0))}
        remote = {"access_token": _jwt_with_iat(_epoch(2026, 10, 5, 1, 0))}
        relation = compare_credential_time(
            local, remote,
            local_record="2026-10-04T01:00:00+00:00",
            remote_record="2026-10-06T01:00:00+00:00",
        )
        self.assertEqual(relation, "local_newer", "iat 优先于记录时间")


class BuildComparisonSingleSideTests(unittest.TestCase):
    """行级：用户实测的那一行 —— 显示与判定必须同一口径。"""

    def test_reported_row_is_remote_newer(self):
        """用户报的行（indrawn.prows_5v 形状）→ 应判 remote_newer。

        显示：本地列 10-02（iat）、远端列 10-04（记录时间，远端无 iat）。
        判定必须按同样的两个值：10-04 > 10-02 → remote_newer。
        修复前判 local_newer（拿本地 10-05 噪声记录时间去比）。
        """
        local = [{
            "id": 30, "email": "indrawn.prows_5v@icloud.com", "platform": "grok",
            "status": "registered",
            "updated_at": "2026-10-05T10:04:08+00:00",  # 状态同步噪声
            "extra": {
                "sso": "sso-local",
                "access_token": _jwt_with_iat(_epoch(2026, 10, 2, 13, 57)),
            },
        }]
        remote = [RemoteAccount(
            email="indrawn.prows_5v@icloud.com", platform="grok", remote_id="164",
            updated_at=datetime(2026, 10, 4, 11, 4, 27, tzinfo=timezone.utc),
            credentials={"sso": "sso-remote"},  # web 线只有 SSO
        )]
        row = build_comparison(local, remote)[0]
        self.assertEqual(row.time_relation, "remote_newer")
        self.assertEqual(
            row.time_basis, "mixed",
            "本地用 iat、远端无 iat 回落记录时间 —— basis 应是 mixed（单侧）",
        )

    def test_local_record_time_not_used_when_local_iat_exists(self):
        """本地有 iat 时不许拿本地记录时间（噪声）参与判定。"""
        local = [{
            "id": 1, "email": "a@example.com", "platform": "grok",
            "status": "registered",
            "updated_at": "2026-10-05T10:04:08+00:00",
            "extra": {"access_token": _jwt_with_iat(_epoch(2026, 10, 4, 21, 26))},
        }]
        remote = [RemoteAccount(
            email="a@example.com", platform="grok",
            updated_at=datetime(2026, 10, 4, 15, 30, tzinfo=timezone.utc),
            credentials={"sso": "sso-only"},
        )]
        row = build_comparison(local, remote)[0]
        # 本地 iat 21:26 比远端记录 15:30 新 → local_newer（不是噪声 10-05 判的）
        self.assertEqual(row.time_relation, "local_newer")
        self.assertEqual(
            row.time_basis, "mixed",
            "本地用 iat、远端无 iat 回落记录时间 —— basis 应是 mixed（单侧）",
        )

    def test_both_record_times_only_still_falls_back(self):
        """两侧都无 iat → 仍回落记录时间（time_basis=record）—— 保留兜底。"""
        local = [{
            "id": 1, "email": "a@example.com", "platform": "grok",
            "status": "registered",
            "updated_at": "2026-10-04T04:10:00+00:00",
            "extra": {"access_token": "opaque-local"},
        }]
        remote = [RemoteAccount(
            email="a@example.com", platform="grok",
            updated_at=datetime(2026, 10, 4, 9, 50, tzinfo=timezone.utc),
            credentials={"access_token": "opaque-remote"},
        )]
        row = build_comparison(local, remote)[0]
        self.assertEqual(row.time_relation, "remote_newer")
        self.assertEqual(row.time_basis, "record")


class PlanSyncSingleSideTests(unittest.TestCase):
    """拉回方向：单侧 iat 时不再被噪声记录时间带反。"""

    def test_remote_record_newer_than_local_iat_is_pulled(self):
        """本地 iat 10-02（旧）、远端无 iat、远端记录 10-04（新）→ 拉回。

        用户实测行方向：远端较新 → 「更新本地凭证」应拉回这一行。
        """
        from services.panel_sync import plan_sync

        local = {
            "sso": "sso-local",
            "access_token": _jwt_with_iat(_epoch(2026, 10, 2, 13, 57)),
        }
        remote = RemoteAccount(
            email="indrawn.prows_5v@icloud.com", platform="grok", remote_id="164",
            updated_at=datetime(2026, 10, 4, 11, 4, 27, tzinfo=timezone.utc),
            credentials={"sso": "sso-remote"},
        )
        outcome = plan_sync(
            local, remote,
            local_updated="2026-10-05T10:04:08+00:00",  # 状态同步噪声
        )
        self.assertTrue(
            outcome.pulled,
            f"应拉回远端较新的 SSO（实得 reason={outcome.reason}）—— "
            "整体回落记录时间会把方向判反",
        )
        self.assertEqual(outcome.reason, "remote_newer")

    def test_local_iat_newer_than_remote_record_is_not_pulled(self):
        """本地 iat 比远端记录时间新 → 不拉（本地较新，等推送）。"""
        from services.panel_sync import plan_sync

        local = {
            "sso": "sso-local",
            "access_token": _jwt_with_iat(_epoch(2026, 10, 5, 5, 0)),
        }
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            updated_at=datetime(2026, 10, 4, 11, 4, tzinfo=timezone.utc),
            credentials={"sso": "sso-remote"},
        )
        outcome = plan_sync(
            local, remote,
            local_updated="2026-10-05T10:04:08+00:00",
        )
        self.assertFalse(outcome.pulled)
        self.assertEqual(outcome.reason, "local_newer")


class PlanPushSingleSideTests(unittest.TestCase):
    """推送方向：单侧 iat 时同样按同一口径判。"""

    def test_remote_record_newer_than_local_iat_is_not_pushed(self):
        """本地 iat 10-02（旧）、远端记录 10-04（新）→ 不推（远端较新）。"""
        from services.panel_push import plan_push

        local = {
            "sso": "sso-local",
            "access_token": _jwt_with_iat(_epoch(2026, 10, 2, 13, 57)),
        }
        remote = RemoteAccount(
            email="indrawn.prows_5v@icloud.com", platform="grok", remote_id="164",
            updated_at=datetime(2026, 10, 4, 11, 4, 27, tzinfo=timezone.utc),
            credentials={"sso": "sso-remote"},
        )
        outcome = plan_push(
            local, remote,
            local_updated="2026-10-05T10:04:08+00:00",  # 噪声
        )
        self.assertFalse(
            outcome.push,
            f"远端较新不该推（实得 reason={outcome.reason}）",
        )
        self.assertEqual(outcome.reason, "remote_newer")

    def test_local_iat_newer_than_remote_record_is_pushed(self):
        """本地 iat 比远端记录时间新 → 推（本地较新）。"""
        from services.panel_push import plan_push

        local = {
            "sso": "sso-local",
            "access_token": _jwt_with_iat(_epoch(2026, 10, 5, 5, 0)),
        }
        remote = RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 4, 11, 4, tzinfo=timezone.utc),
            credentials={"sso": "sso-remote"},
        )
        outcome = plan_push(
            local, remote,
            local_updated="2026-10-05T10:04:08+00:00",
        )
        self.assertTrue(outcome.push)
        self.assertEqual(outcome.reason, "local_newer")


if __name__ == "__main__":
    unittest.main()
