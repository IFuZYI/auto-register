"""账号导入：文本格式（email 密码）与 JSON 全字段格式。

JSON 导入是导出（`services/account_export._render_json`）的**逆向**：
那份导出写 platform / email / password / totp_secret / access_token /
refresh_token / id_token / session_token / status / created_at 十个字段，
导入必须能原样收回来 —— 否则「导出备份 → 换台机器导入」会丢 token。
"""

from __future__ import annotations

import json
import unittest

from fastapi.testclient import TestClient

from core.db import account_repository
from core.db.models_account import AccountModel


def _client():
    """必须 `with TestClient(app)` —— 否则 lifespan 不跑、平台不加载。"""
    import main as main_mod

    return TestClient(main_mod.app)


class JsonImportTests(unittest.TestCase):
    """JSON 全字段导入。"""

    @classmethod
    def setUpClass(cls):
        cls._ctx = _client()
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def _import(self, payload, platform="chatgpt"):
        body = {
            "platform": platform,
            "lines": [json.dumps(payload, ensure_ascii=False)],
            "format": "json",
        }
        r = self.client.post("/api/accounts/import", json=body)
        return r

    def _cleanup(self, platform, email):
        """按邮箱找到再按 id 删（仓储没有按邮箱删的接口）。"""
        row = account_repository.find_by_email(platform, email)
        if row is not None:
            account_repository.delete(row.id, platform)

    def test_full_fields_round_trip(self):
        """导出格式的十个字段都要能导进来（凭证不丢）。"""
        email = "json-full@example.com"
        record = {
            "platform": "chatgpt",
            "email": email,
            "password": "pw-123",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "access_token": "at-abc",
            "refresh_token": "rt-abc",
            "id_token": "id-abc",
            "session_token": "st-abc",
            # 用保留的状态值：已删除的 trial/subscribed 会被写侧归一（有专项测试覆盖）
            "status": "banned",
            "created_at": "2026-03-31 04:10:00",
        }
        try:
            r = self._import([record])
            self.assertEqual(r.status_code, 200, r.text[:300])
            self.assertEqual(r.json()["created"], 1)

            saved = account_repository.find_by_email("chatgpt", email)
            self.assertIsNotNone(saved)
            self.assertEqual(saved.password, "pw-123")
            self.assertEqual(saved.status, "banned")
            extra = saved.get_extra()
            self.assertEqual(extra["totp_secret"], "JBSWY3DPEHPK3PXP")
            self.assertEqual(extra["access_token"], "at-abc")
            self.assertEqual(extra["refresh_token"], "rt-abc")
            self.assertEqual(extra["id_token"], "id-abc")
            self.assertEqual(extra["session_token"], "st-abc")
        finally:
            self._cleanup("chatgpt", email)

    def test_created_at_is_preserved(self):
        """created_at 要保留原值 —— 列表排序与筛选依赖它。"""
        email = "json-created@example.com"
        try:
            self._import([{
                "platform": "chatgpt", "email": email, "password": "p",
                "created_at": "2026-01-15 08:30:00",
            }])
            saved = account_repository.find_by_email("chatgpt", email)
            self.assertEqual(saved.created_at.year, 2026)
            self.assertEqual(saved.created_at.month, 1)
            self.assertEqual(saved.created_at.day, 15)
        finally:
            self._cleanup("chatgpt", email)

    def test_existing_account_is_updated_not_duplicated(self):
        """邮箱是唯一键：重复导入同一邮箱是更新，不是插两行。"""
        email = "json-upsert@example.com"
        try:
            self._import([{"platform": "chatgpt", "email": email, "password": "old"}])
            r = self._import([{"platform": "chatgpt", "email": email, "password": "new"}])
            self.assertEqual(r.json()["updated"], 1)
            self.assertEqual(r.json()["created"], 0)

            saved = account_repository.find_by_email("chatgpt", email)
            self.assertEqual(saved.password, "new")
        finally:
            self._cleanup("chatgpt", email)

    def test_empty_fields_do_not_wipe_existing_credentials(self):
        """空字段不能覆盖已有值 —— 否则导入成了数据损失。"""
        email = "json-nocover@example.com"
        try:
            self._import([{
                "platform": "chatgpt", "email": email, "password": "p",
                "access_token": "at-keep", "refresh_token": "rt-keep",
            }])
            # 第二次导入同邮箱，凭证字段为空
            self._import([{
                "platform": "chatgpt", "email": email, "password": "",
                "access_token": "", "refresh_token": "",
            }])
            saved = account_repository.find_by_email("chatgpt", email)
            extra = saved.get_extra()
            self.assertEqual(extra["access_token"], "at-keep", "空 AT 把已有值抹掉了")
            self.assertEqual(extra["refresh_token"], "rt-keep", "空 RT 把已有值抹掉了")
            self.assertEqual(saved.password, "p", "空密码把已有值抹掉了")
        finally:
            self._cleanup("chatgpt", email)

    def test_platform_can_come_from_each_entry(self):
        """每条记录可以带自己的 platform，不必等于请求里的那个。"""
        email = "json-perplatform@example.com"
        try:
            r = self._import(
                [{"platform": "grok", "email": email, "password": "p"}],
                platform="chatgpt",  # 请求里写 chatgpt，记录里写 grok
            )
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertIsNone(account_repository.find_by_email("chatgpt", email))
            self.assertIsNotNone(account_repository.find_by_email("grok", email))
        finally:
            self._cleanup("grok", email)

    def test_wrapped_payload_is_accepted(self):
        """容忍 `{"accounts": [...]}` 这类包装。"""
        email = "json-wrapped@example.com"
        try:
            r = self._import({"accounts": [
                {"platform": "chatgpt", "email": email, "password": "p"},
            ]})
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertEqual(r.json()["created"], 1)
        finally:
            self._cleanup("chatgpt", email)

    def test_invalid_json_is_a_clear_400(self):
        """坏 JSON 要给人话错误，不是 500。"""
        r = self.client.post("/api/accounts/import", json={
            "platform": "chatgpt",
            "lines": ["{not json"],
            "format": "json",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("JSON", r.json()["detail"])

    def test_non_array_json_is_rejected(self):
        r = self.client.post("/api/accounts/import", json={
            "platform": "chatgpt",
            "lines": ['"just a string"'],
            "format": "json",
        })
        self.assertEqual(r.status_code, 400)

    def test_entries_without_email_are_skipped_and_counted(self):
        email = "json-skip@example.com"
        try:
            r = self._import([
                {"platform": "chatgpt", "email": "", "password": "x"},
                {"platform": "chatgpt", "email": email, "password": "p"},
            ])
            body = r.json()
            self.assertEqual(body["created"], 1)
            self.assertEqual(body["skipped"], 1)
        finally:
            self._cleanup("chatgpt", email)

    def test_unknown_extra_fields_are_kept(self):
        """导出没有但导入方带的平台字段也要收下（前向兼容）。"""
        email = "json-extra@example.com"
        try:
            self._import([{
                "platform": "chatgpt", "email": email, "password": "p",
                "workspace_id": "ws-1", "some_custom_key": "v",
            }])
            extra = account_repository.find_by_email("chatgpt", email).get_extra()
            self.assertEqual(extra["workspace_id"], "ws-1")
            self.assertEqual(extra["some_custom_key"], "v")
        finally:
            self._cleanup("chatgpt", email)


class TextImportStillWorksTests(unittest.TestCase):
    """文本格式不能被 JSON 改动破坏（默认路径）。"""

    @classmethod
    def setUpClass(cls):
        cls._ctx = _client()
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def test_text_format_is_still_the_default(self):
        email = "text-import@example.com"
        try:
            r = self.client.post("/api/accounts/import", json={
                "platform": "chatgpt",
                "lines": [f"{email} pw-1"],
            })
            self.assertEqual(r.status_code, 200, r.text[:200])
            self.assertEqual(r.json()["created"], 1)
            saved = account_repository.find_by_email("chatgpt", email)
            self.assertEqual(saved.password, "pw-1")
        finally:
            row = account_repository.find_by_email("chatgpt", email)
            if row is not None:
                account_repository.delete(row.id, "chatgpt")

    def test_three_column_text_form(self):
        email = "text-3col@example.com"
        try:
            r = self.client.post("/api/accounts/import", json={
                "platform": "chatgpt",
                "lines": [f"{email} pw-2 https://example.com/cashier"],
            })
            self.assertEqual(r.status_code, 200, r.text[:200])
            saved = account_repository.find_by_email("chatgpt", email)
            self.assertEqual(saved.password, "pw-2")
        finally:
            row = account_repository.find_by_email("chatgpt", email)
            if row is not None:
                account_repository.delete(row.id, "chatgpt")

    def test_skipped_lines_are_counted(self):
        """不成形的行要计入 skipped —— 与 JSON 模式口径一致。

        前端（`Accounts.tsx`）会读 `skipped` 显示「跳过 N 条」；文本模式不返回它
        的话，用户贴进去 10 行、只有 7 行有效时看不出少了 3 行。
        """
        email = "text-skip@example.com"
        try:
            r = self.client.post("/api/accounts/import", json={
                "platform": "chatgpt",
                "lines": [
                    f"{email} pw-ok",   # 有效
                    "only-one-column",  # 少于两列
                    "",                 # 空行
                ],
            })
            self.assertEqual(r.status_code, 200, r.text[:200])
            body = r.json()
            self.assertEqual(body["created"], 1)
            self.assertEqual(body["skipped"], 2)
        finally:
            row = account_repository.find_by_email("chatgpt", email)
            if row is not None:
                account_repository.delete(row.id, "chatgpt")


class ImportExportRoundTripTests(unittest.TestCase):
    """导出 → 导入 往返不丢字段（用户要求的核心价值）。"""

    @classmethod
    def setUpClass(cls):
        cls._ctx = _client()
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def test_exported_json_can_be_imported_back(self):
        from services.account_export import _render_json

        source_email = "roundtrip-src@example.com"
        target_email = "roundtrip-dst@example.com"
        try:
            # 造一个字段齐全的账号
            account = AccountModel(
                platform="chatgpt",
                email=source_email,
                password="pw-rt",
                # 用保留的状态值（trial/subscribed 已删除）
                status="banned",
                user_id="uid-1",
            )
            account.set_extra({
                "totp_secret": "TOTP-RT",
                "access_token": "at-rt",
                "refresh_token": "rt-rt",
                "id_token": "id-rt",
                "session_token": "st-rt",
            })
            account_repository.upsert(account)

            # 导出
            exported = _render_json([account_repository.find_by_email("chatgpt", source_email)])
            rows = json.loads(exported)
            self.assertEqual(len(rows), 1)

            # 换个邮箱导回去（模拟换机器导入，不覆盖源账号）
            rows[0]["email"] = target_email
            r = self.client.post("/api/accounts/import", json={
                "platform": "chatgpt",
                "lines": [json.dumps(rows, ensure_ascii=False)],
                "format": "json",
            })
            self.assertEqual(r.status_code, 200, r.text[:300])

            saved = account_repository.find_by_email("chatgpt", target_email)
            self.assertIsNotNone(saved, "导出的 JSON 没能导回来")
            self.assertEqual(saved.password, "pw-rt")
            extra = saved.get_extra()
            expected = {
                "totp_secret": "TOTP-RT",
                "access_token": "at-rt",
                "refresh_token": "rt-rt",
                "id_token": "id-rt",
                "session_token": "st-rt",
            }
            for key, want in expected.items():
                self.assertEqual(extra.get(key), want, f"往返后 {key} 丢了或变了")
        finally:
            for addr in (source_email, target_email):
                row = account_repository.find_by_email("chatgpt", addr)
                if row is not None:
                    account_repository.delete(row.id, "chatgpt")


class CashierUrlRoundTripTests(unittest.TestCase):
    """`cashier_url` 走的是**账号表的列**（不是 extra）—— 往返必须保住它。

    回归（评审发现，已复现）：JSON 导入把该字段写在列上、又把它排除在 extra
    之外，而仓储 `upsert` 的更新路径**无条件**用 `extra.get("cashier_url")`
    重算该列 —— 于是刚导入的值被就地抹掉。两端都中：
    - 导入带值时丢失（写进去 → 立刻被空 extra 覆盖）
    - 导入不带值时清空已有值（沉默被当成清空）
    """

    @classmethod
    def setUpClass(cls):
        cls._ctx = _client()
        cls.client = cls._ctx.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._ctx.__exit__(None, None, None)

    def _create(self, email, cashier_url=""):
        return self.client.post("/api/accounts", json={
            "platform": "chatgpt", "email": email,
            "password": "pw", "cashier_url": cashier_url,
        })

    def _import(self, entries):
        return self.client.post("/api/accounts/import", json={
            "platform": "chatgpt",
            "lines": [json.dumps(entries, ensure_ascii=False)],
            "format": "json",
        })

    def _row(self, email):
        r = self.client.get(f"/api/accounts?platform=chatgpt&email={email}")
        return (r.json().get("items") or [None])[0]

    def _cleanup(self, email):
        row = account_repository.find_by_email("chatgpt", email)
        if row is not None:
            account_repository.delete(row.id, "chatgpt")

    def test_import_with_cashier_url_stores_it(self):
        email = "cashier-keep@example.com"
        try:
            self._create(email)
            self._import([{
                "platform": "chatgpt", "email": email, "password": "pw",
                "cashier_url": "https://pay.example.com/IMPORTED",
            }])
            self.assertEqual(
                self._row(email).get("cashier_url"), "https://pay.example.com/IMPORTED"
            )
        finally:
            self._cleanup(email)

    def test_import_without_cashier_url_preserves_existing(self):
        """导入方没提这个字段时保持原值 —— 沉默不等于清空。"""
        email = "cashier-preserve@example.com"
        try:
            self._create(email, cashier_url="https://pay.example.com/KEEP")
            self._import([{"platform": "chatgpt", "email": email, "password": "pw2"}])
            self.assertEqual(
                self._row(email).get("cashier_url"), "https://pay.example.com/KEEP"
            )
        finally:
            self._cleanup(email)

    def test_empty_string_does_not_wipe(self):
        """显式空串同样不该抹掉 —— 与 extra 凭证字段的口径一致。"""
        email = "cashier-empty@example.com"
        try:
            self._create(email, cashier_url="https://pay.example.com/KEEP2")
            self._import([{
                "platform": "chatgpt", "email": email, "password": "pw3",
                "cashier_url": "",
            }])
            self.assertEqual(
                self._row(email).get("cashier_url"), "https://pay.example.com/KEEP2"
            )
        finally:
            self._cleanup(email)

    def test_plugin_style_extra_still_writes_the_column(self):
        """插件形态的账号（值在 extra 里）仍要能写进列 —— 不能只顾导入那条路。"""
        from core.base_platform import Account

        email = "cashier-plugin@example.com"
        try:
            account_repository.upsert(Account(
                platform="chatgpt", email=email, password="p",
                extra={"cashier_url": "https://pay.example.com/FROM-EXTRA"},
            ))
            self.assertEqual(
                self._row(email).get("cashier_url"), "https://pay.example.com/FROM-EXTRA"
            )
        finally:
            self._cleanup(email)


if __name__ == "__main__":
    unittest.main()
