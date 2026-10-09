"""iCloud 远程仓库的两个真缺陷（用户报告 2026-10-09）。

缺陷 1：远端凭据同步 500 Internal Server Error
    远端返回的 `createdAt` 是不带时区的 ISO 串时（如 `2026-10-09T12:00:00`），
    `_parse_remote_datetime` 原样解析出 naive datetime 并写进 `created_at` 列 ——
    SQLModel 的 aware 时间列在绑定时抛
    `ValueError: Datetime values must have timezone information`，整个
    `POST /api/icloud/accounts/{id}/sync` 直接 500（实测日志有堆栈）。
    修复：解析出的 naive 值按 UTC 归一（与 `core/time_utils.ensure_utc` 同口径）。

缺陷 2：注册机不应在远端创建隐私邮箱
    远程渠道的 `get_email()` 走 `claim_alias`，池空时会向**远端**新建别名
    （`aliases.py` 第 2 步）。用户明确要求：本注册机只允许用**本地**账号，
    不许在远端创建。修复：远程渠道池空时报错（不再落到远端生成）。

TDD：本文件先于修复写完（RED），修复后必须全绿；随后做变异验证。
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from platforms.icloud.errors import ICloudError

ROOT = Path(__file__).resolve().parents[1]


class RemoteDatetimeNormalizationTests(unittest.TestCase):
    """缺陷 1：远端时间串必须归一成 aware UTC，否则写库 500。"""

    def test_naive_iso_string_is_normalized_to_utc(self):
        from services.icloud.remote import _parse_remote_datetime

        parsed = _parse_remote_datetime("2026-10-09T12:00:00")
        self.assertIsNotNone(parsed)
        self.assertIsNotNone(parsed.tzinfo, "naive 值未归一 —— 写库会抛 StatementError")
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(None))
        self.assertEqual((parsed.year, parsed.month, parsed.day, parsed.hour), (2026, 10, 9, 12))

    def test_aware_iso_string_keeps_its_instant(self):
        from services.icloud.remote import _parse_remote_datetime

        parsed = _parse_remote_datetime("2026-10-09T20:00:00+08:00")
        self.assertEqual(parsed.astimezone(timezone.utc).hour, 12)

    def test_z_suffix_is_accepted(self):
        from services.icloud.remote import _parse_remote_datetime

        parsed = _parse_remote_datetime("2026-10-09T12:00:00Z")
        self.assertEqual(parsed.utcoffset(), timezone.utc.utcoffset(None))

    def test_garbage_still_returns_none(self):
        from services.icloud.remote import _parse_remote_datetime

        self.assertIsNone(_parse_remote_datetime("not-a-date"))
        self.assertIsNone(_parse_remote_datetime(""))
        self.assertIsNone(_parse_remote_datetime(None))


class RemoteAliasCreationTests(unittest.TestCase):
    """缺陷 2：远程账号不得再向远端新建隐私邮箱（只许用本地已导入的号）。

    用户要求（2026-10-09）：「本注册机不应该在远端创建隐私邮箱，只有本地账号才行。」

    真实决策点在 `services/icloud/aliases.generate_alias`：远程账号（source=remote）
    原本会走 `generate_remote_alias` 向 icloud-hme 创建别名。修复后这条路必须堵死，
    而本地账号的路径不受影响。
    """

    def test_generate_alias_refuses_for_remote_account(self):
        """远程账号调 generate_alias → 报错，绝不落到远端创建。"""
        from services.icloud import aliases as aliases_module
        from services import icloud_service

        remote_row = mock.Mock(
            id=3, email="main@icloud.com", enabled=True, source="remote", address="main@icloud.com"
        )
        remote_create_calls: list = []

        def _forbidden(*args, **kwargs):
            remote_create_calls.append((args, kwargs))
            raise AssertionError("远程账号不该触发远端创建")

        with mock.patch.object(
            aliases_module, "get_account", return_value=remote_row
        ), mock.patch.object(
            icloud_service, "generate_remote_alias", side_effect=_forbidden
        ):
            with self.assertRaises(ICloudError) as ctx:
                icloud_service.generate_alias(3, label="x", note="")

        self.assertEqual(
            remote_create_calls, [], "远程账号不该触发远端创建（用户明确要求只允许本地账号）"
        )
        self.assertTrue(ctx.exception.code, "必须是带错误码的 ICloudError")

    def test_remote_account_error_is_actionable(self):
        """报错要能指导用户：说清「只允许本地账号」并给出下一步。"""
        from services.icloud import aliases as aliases_module
        from services import icloud_service

        remote_row = mock.Mock(
            id=3, email="main@icloud.com", enabled=True, source="remote", address="main@icloud.com"
        )
        with mock.patch.object(aliases_module, "get_account", return_value=remote_row):
            with self.assertRaises(ICloudError) as ctx:
                icloud_service.generate_alias(3, label="x", note="")

        message = str(ctx.exception)
        self.assertIn("本地", message, "错误信息要说明只允许本地账号")

    def test_local_account_still_goes_through_local_path(self):
        """本地账号不受影响：仍走本地生成路径（不能被一刀切堵死）。"""
        from services.icloud import aliases as aliases_module
        from services import icloud_service

        local_row = mock.Mock(
            id=1, email="local@icloud.com", enabled=True, source="local", address="local@icloud.com"
        )
        reached_local = []

        with mock.patch.object(
            aliases_module, "get_account", return_value=local_row
        ), mock.patch.object(
            icloud_service, "generate_remote_alias"
        ) as remote_create, mock.patch.object(
            aliases_module, "alias_quota", return_value={"remaining": 5}
        ), mock.patch.object(
            aliases_module, "load_credentials", return_value={}
        ), mock.patch.object(
            icloud_service, "web_client"
        ) as web_client:
            def _local_path(*args, **kwargs):
                reached_local.append(True)
                raise RuntimeError("reached-local-path")

            web_client.return_value.__enter__.return_value.generate_private_email.side_effect = (
                _local_path
            )
            with self.assertRaises(RuntimeError):
                icloud_service.generate_alias(1, label="x", note="")

        self.assertEqual(reached_local, [True], "本地账号必须仍走本地生成路径")
        remote_create.assert_not_called()


class GenerateAliasFrontendContractTests(unittest.TestCase):
    """前端：生成弹窗的主号下拉只列本地账号（远程账号选了必然 400）。"""

    def _src(self, rel: str) -> str:
        return (ROOT / "frontend" / "src" / rel).read_text(encoding="utf-8")

    def test_generate_modal_lists_local_accounts_only(self):
        src = self._src("components/icloud/GenerateAliasModal.tsx")
        self.assertIn(
            "item.source !== 'remote'",
            src,
            "生成弹窗没过滤远程账号 —— 选了必然 400",
        )
        self.assertIn(
            "localAccounts.map",
            src,
            "主号下拉没有改用过滤后的本地账号列表",
        )
        self.assertNotIn(
            "options={accounts.map",
            src,
            "生成弹窗仍把全部账号（含远程）列进主号下拉",
        )

    def test_generate_button_disabled_without_local_account(self):
        src = self._src("pages/ICloud.tsx")
        self.assertIn(
            "disabled={localAccounts.length === 0}",
            src,
            "生成按钮没有只按本地账号判断可用性（远程账号会让按钮假可用）",
        )
        self.assertIn(
            "accounts={localAccounts}",
            src,
            "生成弹窗没有收到过滤后的本地账号列表",
        )


if __name__ == "__main__":
    unittest.main()
