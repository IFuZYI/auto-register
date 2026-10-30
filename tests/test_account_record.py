"""`AccountRecord` 是账号的唯一定义处（T1.1）。

验收标准是**三向往返等价**：领域对象 ↔ ORM 行 ↔ JSON 字典，转一圈回来每个字段
都要一样。以前 `Account` 与 `AccountModel` 各有一套字段，四份手写搬运代码
（`api/actions.py` / `api/accounts.py` / `core/scheduler.py` / `api/integrations.py`）
里 `region` 和 `cashier_url` 各漏一处 —— 这个文件就是防它们再漏。
"""

from __future__ import annotations

import unittest

from core.base_platform import Account, AccountStatus
from core.db.models_account import AccountModel
from core.models.account import AccountRecord, Credentials

#: 往返比较时忽略的字段：时间在 ORM 侧是 datetime、领域侧是 epoch 秒，
#: 精度只能到秒（`fromtimestamp` 会丢微秒），单独断言。
_TIME_FIELDS = {"created_at", "updated_at"}


class SingleDefinitionTests(unittest.TestCase):
    def test_account_is_an_alias_not_a_subclass(self):
        self.assertIs(Account, AccountRecord, "`Account` 必须是别名，不能是第二个类")

    def test_status_string_and_enum_compare_equal(self):
        record = AccountRecord(platform="chatgpt", email="a@x.ai")
        self.assertEqual(record.status, AccountStatus.REGISTERED)
        self.assertEqual(record.status, "registered")


class CredentialsViewTests(unittest.TestCase):
    def test_reads_and_writes_land_on_extra(self):
        record = AccountRecord(platform="chatgpt", email="a@x.ai")
        record.credentials.refresh_token = "RT-1"
        self.assertEqual(record.extra["refresh_token"], "RT-1")
        self.assertEqual(record.credentials.refresh_token, "RT-1")

    def test_old_code_reading_extra_sees_the_same_value(self):
        record = AccountRecord(platform="chatgpt", email="a@x.ai")
        record.extra["access_token"] = "AT-legacy"
        self.assertEqual(record.credentials.access_token, "AT-legacy")

    def test_clearing_a_credential_removes_the_key(self):
        record = AccountRecord(
            platform="chatgpt", email="a@x.ai", extra={"sso": "SSO-1"}
        )
        record.credentials.sso = ""
        self.assertNotIn("sso", record.extra, "空串会让「有没有 SSO」的判断失真")

    def test_password_proxies_the_record_field(self):
        record = AccountRecord(platform="chatgpt", email="a@x.ai", password="pw")
        self.assertEqual(record.credentials.password, "pw")
        record.credentials.password = "pw2"
        self.assertEqual(record.password, "pw2")


class RoundTripTests(unittest.TestCase):
    def _record(self) -> AccountRecord:
        return AccountRecord(
            platform="chatgpt",
            email="round@trip.ai",
            password="pw",
            user_id="user-1",
            region="US",
            token="TOKEN-1",
            status=AccountStatus.BANNED,
            cashier_url="https://pay.example/x",
            extra={"access_token": "AT-1", "refresh_token": "RT-1", "note": "n"},
            id="11111111-1111-4111-8111-111111111111",
            legacy_id=42,
        )

    def test_record_to_model_to_record(self):
        source = self._record()
        model = AccountModel(**source.to_model_fields())
        restored = model.to_record()

        for name in (
            "platform", "email", "password", "user_id", "region", "token",
            "status", "cashier_url", "extra", "id", "legacy_id",
        ):
            self.assertEqual(
                getattr(restored, name), getattr(source, name), f"字段 {name} 往返不一致"
            )
        self.assertEqual(restored.created_at, source.created_at, "created_at 秒级应相等")

    def test_record_to_dict_to_record(self):
        source = self._record()
        restored = AccountRecord.from_dict(source.to_dict())
        for name in (
            "platform", "email", "password", "user_id", "region", "token",
            "status", "cashier_url", "extra", "id", "legacy_id", "created_at",
        ):
            self.assertEqual(
                getattr(restored, name), getattr(source, name), f"字段 {name} 往返不一致"
            )

    def test_model_keeps_every_field_the_old_copies_dropped(self):
        """四份手写搬运各漏过 `region` / `cashier_url` —— 这里逐个钉住。"""
        model = AccountModel(
            platform="chatgpt",
            email="keep@all.ai",
            password="pw",
            user_id="u",
            region="JP",
            token="T",
            status="registered",
            cashier_url="https://pay.example/keep",
            extra_json='{"sso": "SSO"}',
        )
        record = model.to_record()
        self.assertEqual(record.region, "JP")
        self.assertEqual(record.cashier_url, "https://pay.example/keep")
        self.assertEqual(record.credentials.sso, "SSO")
        self.assertEqual(record.id, model.id, "领域对象必须带着主键")


if __name__ == "__main__":
    unittest.main()
