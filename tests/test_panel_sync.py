"""本地 ↔ 远程凭证同步（拉回远端较新的凭证）。

背景：x.ai 的 RT 每次刷新都会轮换（谁最后刷新谁持有有效 RT）。本地存的
RT 在远端刷新过之后就是死值（实测 22 个账号全部 `invalid_grant`）。现有
的「上传」只覆盖本地 → 远端方向；本模块补「远端较新 → 拉回本地」。

时间比较必须**先归一**：远端面板服务器时区与本地不同（grok2api 实测
`+08:00`，本地 UTC）—— 字符串比大小会得出错误结论（`2026-10-04T19:00+08:00`
实际是 `11:00Z`，比本地 `12:00Z` 更早，但字符串更大）。
"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.panel_comparison import RemoteAccount  # noqa: E402
from services.panel_sync import (  # noqa: E402
    apply_pull,
    plan_sync,
    sync_local_from_remote,
)


def _utc(*args) -> str:
    return datetime(*args, tzinfo=timezone.utc).isoformat()


class PlanSyncDirectionTests(unittest.TestCase):
    """方向判定：只有「远端较新」才拉。"""

    def test_remote_newer_is_pulled(self):
        local = {"access_token": "old-at", "refresh_token": "old-rt"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            updated_at=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
            credentials={"access_token": "new-at", "refresh_token": "new-rt"},
        )
        outcome = plan_sync(local, remote, local_updated=_utc(2026, 10, 4, 11, 0))
        self.assertTrue(outcome.pulled)
        self.assertEqual(outcome.reason, "remote_newer")
        self.assertEqual(sorted(outcome.fields), ["access_token", "refresh_token"])

    def test_local_newer_is_not_pulled(self):
        local = {"access_token": "newer-at", "refresh_token": "newer-rt"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            updated_at=datetime(2026, 10, 4, 10, 0, tzinfo=timezone.utc),
            credentials={"access_token": "older-at", "refresh_token": "older-rt"},
        )
        outcome = plan_sync(local, remote, local_updated=_utc(2026, 10, 4, 12, 0))
        self.assertFalse(outcome.pulled)
        self.assertEqual(outcome.reason, "local_newer")

    def test_synced_credentials_are_skipped(self):
        creds = {"access_token": "same-at", "refresh_token": "same-rt"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            updated_at=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
            credentials=dict(creds),
        )
        outcome = plan_sync(creds, remote, local_updated=_utc(2026, 10, 4, 11, 0))
        self.assertFalse(outcome.pulled)
        self.assertEqual(outcome.reason, "synced")

    def test_remote_missing_credentials_is_skipped(self):
        local = {"access_token": "at"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            updated_at=datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc),
            credentials={},
        )
        outcome = plan_sync(local, remote, local_updated=_utc(2026, 10, 4, 11, 0))
        self.assertFalse(outcome.pulled)
        self.assertEqual(outcome.reason, "remote_missing_credential")

    def test_remote_missing_time_is_conservative(self):
        """远端没有时间可比 → 不动（不拿不确定的数据覆盖本地）。"""
        local = {"access_token": "local-at", "refresh_token": "local-rt"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            updated_at=None,
            credentials={"access_token": "remote-at", "refresh_token": "remote-rt"},
        )
        outcome = plan_sync(local, remote, local_updated=_utc(2026, 10, 4, 11, 0))
        self.assertFalse(outcome.pulled)
        self.assertEqual(outcome.reason, "unknown_time")

    def test_timezone_difference_does_not_flip_the_verdict(self):
        """时区差异不能把「本地较新」误判成「远端较新」。

        远端 `2026-10-04T19:00+08:00` = `11:00Z`，本地 `12:00Z` 更新。
        字符串比较会得出相反结论 —— 必须按归一后的 epoch 比。
        """
        local = {"access_token": "local-at", "refresh_token": "local-rt"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            # +08:00 的 19:00 == 11:00 UTC（更早）
            updated_at=datetime(2026, 10, 4, 19, 0, tzinfo=timezone(timedelta(hours=8))),
            credentials={"access_token": "remote-at", "refresh_token": "remote-rt"},
        )
        outcome = plan_sync(local, remote, local_updated=_utc(2026, 10, 4, 12, 0))
        self.assertFalse(
            outcome.pulled,
            "远端 11:00Z 比本地 12:00Z 早，不该拉回（时区归一失效？）",
        )
        self.assertEqual(outcome.reason, "local_newer")


class ApplyPullTests(unittest.TestCase):
    """拉回只覆盖凭证字段，不动本地其它键。"""

    def test_only_credential_fields_are_overwritten(self):
        local = {
            "access_token": "old-at",
            "refresh_token": "old-rt",
            "register_proxy": "socks5://p:1",
            "mail_provider": "icloud_local",
            "cpa_record": {"sub": "keep-me"},
        }
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            credentials={
                "access_token": "new-at", "refresh_token": "new-rt",
                "id_token": "new-idt",
            },
        )
        merged = apply_pull(local, remote)
        self.assertEqual(merged["access_token"], "new-at")
        self.assertEqual(merged["refresh_token"], "new-rt")
        self.assertEqual(merged["id_token"], "new-idt")
        # 本地上下文保留
        self.assertEqual(merged["register_proxy"], "socks5://p:1")
        self.assertEqual(merged["mail_provider"], "icloud_local")
        self.assertEqual(merged["cpa_record"], {"sub": "keep-me"})

    def test_original_dict_is_not_mutated(self):
        local = {"access_token": "old-at"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            credentials={"access_token": "new-at"},
        )
        apply_pull(local, remote)
        self.assertEqual(local["access_token"], "old-at", "apply_pull 不该改原 dict")

    def test_camelcase_remote_keys_are_normalized(self):
        """远端落库路径可能写 camelCase —— 写回本地统一用蛇形。"""
        local = {"access_token": "old"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            credentials={"accessToken": "new-at", "refreshToken": "new-rt"},
        )
        merged = apply_pull(local, remote)
        self.assertEqual(merged["access_token"], "new-at")
        self.assertEqual(merged["refresh_token"], "new-rt")

    def test_missing_remote_field_keeps_local_value(self):
        """远端某字段缺失 → 保留本地值（不是清空）。"""
        local = {"access_token": "keep-at", "refresh_token": "old-rt"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            credentials={"refresh_token": "new-rt"},
        )
        merged = apply_pull(local, remote)
        self.assertEqual(merged["access_token"], "keep-at")
        self.assertEqual(merged["refresh_token"], "new-rt")


class SyncBatchTests(unittest.TestCase):
    """批量拉回：按 (平台, 邮箱) 匹配，写库走真实 DB（conftest 隔离）。"""

    def _make_account(self, email: str, platform: str, extra: dict):
        from core.base_platform import Account, AccountStatus
        from core.db import account_repository

        row = account_repository.upsert(Account(
            platform=platform, email=email, password="p",
            status=AccountStatus.REGISTERED, extra=extra,
        ))
        return row

    def test_batch_pulls_remote_newer_accounts(self):
        row = self._make_account(
            "sync-test-1@x.com", "grok",
            {"access_token": "old-at", "refresh_token": "old-rt"},
        )
        local_rows = [{
            "id": row.id, "email": row.email, "platform": "grok",
            # 本地时间：昨天（远端今天 → 远端较新）
            "updated_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            "extra": dict(row.get_extra()),
        }]
        remote = [RemoteAccount(
            email=row.email, platform="grok",
            updated_at=datetime.now(timezone.utc),
            credentials={"access_token": "new-at", "refresh_token": "new-rt", "sso": "new-sso"},
        )]
        summary = sync_local_from_remote(local_rows, remote)
        self.assertEqual(summary["pulled"], 1)
        self.assertEqual(summary["skipped"], 0)

        # 验证真的落库了
        from core.db import account_repository

        fresh = account_repository.find_by_email("grok", row.email)
        extra = fresh.get_extra()
        self.assertEqual(extra["access_token"], "new-at")
        self.assertEqual(extra["refresh_token"], "new-rt")
        # token 列 = 平台主凭证的镜像：grok → sso（**不是 AT**）。
        # 整理前这里写的是 AT，把 SSO 镜像盖坏（线上 32 行里 12 行如此）。
        self.assertEqual(fresh.token, "new-sso", "grok 的 token 列应镜像 SSO")
        self.assertNotEqual(fresh.token, "new-at", "AT 不许写进 grok 的 token 列")

    def test_batch_skips_remote_only_accounts(self):
        """远端有、本地没有 → 不在拉回职责内（那是别的方向）。"""
        summary = sync_local_from_remote([], [RemoteAccount(
            email="remote-only@x.com", platform="grok",
            updated_at=datetime.now(timezone.utc),
            credentials={"access_token": "at"},
        )])
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["pulled"], 0)

    def test_batch_reports_per_account_reason(self):
        row = self._make_account(
            "sync-test-2@x.com", "grok",
            {"access_token": "same-at", "refresh_token": "same-rt"},
        )
        local_rows = [{
            "id": row.id, "email": row.email, "platform": "grok",
            "updated_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            "extra": dict(row.get_extra()),
        }]
        remote = [RemoteAccount(
            email=row.email, platform="grok",
            updated_at=datetime.now(timezone.utc),
            credentials={"access_token": "same-at", "refresh_token": "same-rt"},
        )]
        summary = sync_local_from_remote(local_rows, remote)
        self.assertEqual(summary["pulled"], 0)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["items"][0]["reason"], "synced")


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


class PlanSyncCredentialTimeTests(unittest.TestCase):
    """方向判定优先按凭证签发时间（iat）—— 记录时间会被状态同步顶掉。"""

    def test_credential_time_beats_record_time(self):
        """记录时间说本地新（状态同步噪声），凭证说远端新 → 仍应拉回。"""
        local = {"access_token": _jwt_with_iat(_epoch(2026, 10, 4, 21, 26))}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            updated_at=datetime(2026, 10, 4, 15, 30, tzinfo=timezone.utc),
            credentials={"access_token": _jwt_with_iat(_epoch(2026, 10, 5, 1, 18))},
        )
        outcome = plan_sync(local, remote, local_updated=_utc(2026, 10, 5, 3, 37))
        self.assertTrue(outcome.pulled)
        self.assertEqual(outcome.reason, "remote_newer")

    def test_same_hour_issuance_is_not_pulled(self):
        """凭证同小时签发 → 分秒是噪声，不动（即使记录时间说远端新）。"""
        local = {"access_token": _jwt_with_iat(_epoch(2026, 10, 5, 1, 5))}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            updated_at=datetime(2026, 10, 5, 6, 0, tzinfo=timezone.utc),
            credentials={"access_token": _jwt_with_iat(_epoch(2026, 10, 5, 1, 50))},
        )
        outcome = plan_sync(local, remote, local_updated=_utc(2026, 10, 4, 23, 0))
        self.assertFalse(outcome.pulled)
        self.assertEqual(outcome.reason, "time_synced")

    def test_missing_local_record_time_is_conservative(self):
        """本地记录时间缺失（凭证也解不出 iat）→ 不动（无法判定方向）。

        模块 docstring 的口径是「无法判定 → 不动（不拿不确定的数据覆盖本地）」
        —— 旧实现对 `local_time is None` 放行（当成远端较新直接拉），与文档不符。
        """
        local = {"access_token": "opaque-local"}
        remote = RemoteAccount(
            email="a@x.com", platform="grok",
            updated_at=datetime(2026, 10, 4, 15, 30, tzinfo=timezone.utc),
            credentials={"access_token": "opaque-remote"},
        )
        outcome = plan_sync(local, remote, local_updated=None)
        self.assertFalse(outcome.pulled)
        self.assertEqual(outcome.reason, "unknown_time")


if __name__ == "__main__":
    unittest.main()
