"""批量动作必须用 UUID 主键查结果（回归：`int(uuid)` → 500）。

事故形态：`_execute_batch_cliproxy_sync` 里写的是

    sync_result = sync_results.get(int(acc_model.id or 0), {})

`accounts.id` 在 c3f8a1d64b27 里从 int 改成了 UUID 字符串，于是这一行对**任何**
账号都抛 `ValueError: invalid literal for int()` —— 「批量同步 CLI 代理状态」
按钮点了必 500，而全量测试全绿（没有一条测试真的走到这个函数）。

同一文件里的 `_execute_batch_panel_status` 用的是 `str(acc_model.id)`，两兄弟
写法不一致就是最早的可疑信号。这里钉住：UUID 主键能跑通，且结果按 id 对齐。
"""

from __future__ import annotations

import unittest
from unittest import mock

from core.db.models_account import AccountModel


def _account(email: str = "u1@example.com", platform: str = "chatgpt") -> AccountModel:
    return AccountModel(platform=platform, email=email, status="active")


class BatchCliproxySyncUuidKeyTests(unittest.TestCase):
    def test_uuid_primary_key_does_not_raise(self):
        from api import actions

        account = _account()
        account_id = str(account.id)
        self.assertGreater(len(account_id), 8, "accounts.id 应该是 UUID 字符串")

        session = mock.MagicMock()
        with mock.patch(
            "services.cliproxyapi_sync.sync_chatgpt_cliproxyapi_status_batch",
            return_value={account_id: {"status": "ok", "remote_state": "active"}},
        ) as batch, mock.patch(
            "services.cliproxyapi_sync.is_sync_ok", return_value=True
        ), mock.patch(
            "api.actions.update_account_model_cliproxy_sync"
        ):
            result = actions._execute_batch_cliproxy_sync([account], session)

        batch.assert_called_once()
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["success"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["items"][0]["id"], account_id)
        self.assertTrue(result["items"][0]["ok"])

    def test_missing_result_counts_as_failure_not_crash(self):
        """远端没返回这个 id 时算失败，不能抛。"""
        from api import actions

        account = _account("u2@example.com")
        session = mock.MagicMock()
        with mock.patch(
            "services.cliproxyapi_sync.sync_chatgpt_cliproxyapi_status_batch",
            return_value={},
        ), mock.patch("services.cliproxyapi_sync.is_sync_ok", return_value=False), mock.patch(
            "api.actions.update_account_model_cliproxy_sync"
        ):
            result = actions._execute_batch_cliproxy_sync([account], session)

        self.assertEqual(result["failed"], 1)
        # item["status"] 是账号自身状态（active），同步结论在 message 里
        self.assertIn("not_found", result["items"][0]["message"])

    def test_two_accounts_get_their_own_results(self):
        """结果按 id 对齐 —— 不能把 A 的状态写到 B 上。"""
        from api import actions

        a, b = _account("a@example.com"), _account("b@example.com")
        session = mock.MagicMock()
        with mock.patch(
            "services.cliproxyapi_sync.sync_chatgpt_cliproxyapi_status_batch",
            return_value={
                str(a.id): {"status": "active", "remote_state": "active"},
                str(b.id): {"status": "not_found", "remote_state": "missing"},
            },
        ), mock.patch(
            "services.cliproxyapi_sync.is_sync_ok",
            side_effect=lambda r: r.get("status") == "active",
        ), mock.patch(
            "api.actions.update_account_model_cliproxy_sync"
        ):
            result = actions._execute_batch_cliproxy_sync([a, b], session)

        by_id = {item["id"]: item for item in result["items"]}
        self.assertTrue(by_id[str(a.id)]["ok"])
        self.assertFalse(by_id[str(b.id)]["ok"])
        self.assertIn("active", by_id[str(a.id)]["message"])
        self.assertIn("not_found", by_id[str(b.id)]["message"])


if __name__ == "__main__":
    unittest.main()
