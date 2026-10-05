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
from unittest import mock

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


class PushBatchTests(unittest.TestCase):
    """`push_local_to_remote` 的行为细节。"""

    def test_not_uploaded_outcome_carries_local_email(self):
        """未上传的行也要带上邮箱（remote 是 None，不能从它取）。"""
        from services.panel_push import push_local_to_remote

        rows = [{
            "id": 1, "email": "a@x.com", "platform": "grok",
            "updated_at": datetime(2026, 10, 4, tzinfo=timezone.utc).isoformat(),
            "extra": {"sso": "sso-1"},
        }]
        summary = push_local_to_remote(
            rows, [], upload=lambda row: (True, "ok")
        )
        self.assertEqual(summary["items"][0]["email"], "a@x.com")
        self.assertEqual(summary["items"][0]["platform"], "grok")

    def test_failed_push_is_counted_as_failed_not_skipped(self):
        """推了但失败 ≠ 无需更新 —— 混在一起前端会显示「没有需要推送的账号」。"""
        from services.panel_push import push_local_to_remote

        rows = [{
            "id": 1, "email": "a@x.com", "platform": "grok",
            "updated_at": datetime(2026, 10, 4, tzinfo=timezone.utc).isoformat(),
            "extra": {"sso": "sso-1"},
        }]
        summary = push_local_to_remote(
            rows, [], upload=lambda row: (False, "未配置 CPA API URL")
        )
        self.assertEqual(summary["pushed"], 0)
        self.assertEqual(summary["failed"], 1, "失败要有独立计数")
        self.assertEqual(summary["skipped"], 0, "失败不该计入 skipped")
        self.assertIn("未配置", summary["items"][0]["message"])

    def test_skipped_means_no_action_needed(self):
        """真正无需更新的（synced 等）才算 skipped。"""
        from services.panel_comparison import RemoteAccount
        from services.panel_push import push_local_to_remote

        creds = {"access_token": "same-at"}
        rows = [{
            "id": 1, "email": "a@x.com", "platform": "grok",
            "updated_at": datetime(2026, 10, 4, tzinfo=timezone.utc).isoformat(),
            "extra": dict(creds),
        }]
        remote = [RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 4, tzinfo=timezone.utc),
            credentials=dict(creds),
        )]
        summary = push_local_to_remote(rows, remote, upload=lambda row: (True, "ok"))
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["failed"], 0)


class GrokUploadConfigFallbackTests(unittest.TestCase):
    """Grok 的 CPA 上传要回落全局配置（与 ChatGPT 侧同口径）。

    面板 push 端点 / 批量动作不带 params 时，`upload_to_cpa` 必须自己读
    `cpa_api_url` / `cpa_api_key` —— 否则每个账号都报「未配置 CPA API URL」，
    而配置明明填好了（实测踩过同类问题）。
    """

    def test_reads_global_config_when_not_given(self):
        import json as _json

        from platforms.grok.upload import upload_to_cpa

        captured = {}

        class _FakeMime:
            def addpart(self, name, data, filename, content_type):
                captured["data"] = data

            def close(self):
                pass

        def _cfg_get(key, default=""):
            return {
                "cpa_api_url": "https://cpa.example",
                "cpa_api_key": "k-1",
            }.get(key, default)

        with mock.patch("core.config_store.config_store.get", side_effect=_cfg_get), \
             mock.patch("curl_cffi.CurlMime", _FakeMime), \
             mock.patch("curl_cffi.requests.post") as post:
            post.return_value = mock.MagicMock(status_code=200, text="{}")
            ok, _ = upload_to_cpa({"email": "g@x.com", "type": "xai", "access_token": "at"})

        self.assertTrue(ok)
        self.assertEqual(post.call_args[0][0], "https://cpa.example/v0/management/auth-files")
        self.assertEqual(
            post.call_args.kwargs["headers"]["Authorization"], "Bearer k-1"
        )


def _jwt_with_iat(iat: int) -> str:
    import base64
    import json

    payload = (
        base64.urlsafe_b64encode(json.dumps({"iat": iat}).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return f"h.{payload}.s"


def _epoch(*args) -> int:
    return int(datetime(*args, tzinfo=timezone.utc).timestamp())


class PlanPushCredentialTimeTests(unittest.TestCase):
    """方向判定优先按凭证签发时间（iat）—— 记录时间会被状态同步顶掉。"""

    def test_credential_time_beats_record_time(self):
        """记录时间说远端新（噪声），凭证说本地新 → 仍应推。"""
        local = {"access_token": _jwt_with_iat(_epoch(2026, 10, 5, 1, 18))}
        remote = RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc),
            credentials={"access_token": _jwt_with_iat(_epoch(2026, 10, 4, 21, 26))},
        )
        outcome = plan_push(local, remote, local_updated=_utc(2026, 10, 5, 0, 30))
        self.assertTrue(outcome.push)
        self.assertEqual(outcome.reason, "local_newer")

    def test_same_hour_issuance_is_not_pushed(self):
        """凭证同小时签发 → 分秒是噪声，不动（即使记录时间说本地新）。"""
        local = {"access_token": _jwt_with_iat(_epoch(2026, 10, 5, 1, 50))}
        remote = RemoteAccount(
            email="a@x.com", platform="grok", remote_id="r-1",
            updated_at=datetime(2026, 10, 5, 1, 0, tzinfo=timezone.utc),
            credentials={"access_token": _jwt_with_iat(_epoch(2026, 10, 5, 1, 5))},
        )
        outcome = plan_push(local, remote, local_updated=_utc(2026, 10, 5, 3, 37))
        self.assertFalse(outcome.push)
        self.assertEqual(outcome.reason, "time_synced")


if __name__ == "__main__":
    unittest.main()
