import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlmodel import SQLModel, Session, create_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _use_pool_db(engine):
    """把号池会话也指到测试库，并保证用例结束后复位。

    只 patch `core.db.engine` 不够：`accounts` 在默认库、`outlook_accounts` 在
    号池库（`data/platforms/outlook.db`），池会话走 `platform_database_registry`。
    不指过来的话，池查询会落到**真实的生产库**上 —— 测试看着通过，实际读写的是
    本机数据，还会互相污染（文件里的旧行让下一次断言莫名失败）。

    返回一个可 `with` 的对象：`patch` 的多个 contextmanager 需要它。
    """
    from contextlib import contextmanager

    from core.db import platform_database_registry

    @contextmanager
    def _cm():
        platform_database_registry.configure({"outlook": str(engine.url)})
        try:
            yield
        finally:
            # 注册表是进程级单例：留着指向已 dispose 的临时库，会让后续用例
            # 静默查到空表。
            platform_database_registry.dispose_all()
            platform_database_registry.configure({})

    return _cm()


def load_microsoft_import_rules_module():
    module_path = (
        Path(__file__).resolve().parents[1]
        / "services"
        / "mail_imports"
        / "microsoft_import_rules.py"
    )
    spec = importlib.util.spec_from_file_location("test_microsoft_import_rules", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MailImportServiceTests(unittest.TestCase):
    def test_parse_microsoft_import_record_requires_oauth_fields(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_record = rules_module.parse_microsoft_import_record

        with self.assertRaisesRegex(ValueError, "缺少 client_id 或 refresh_token"):
            parse_microsoft_import_record(1, "demo@outlook.com----password")

    def test_parse_microsoft_import_line_supports_mailapi_url(self):
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line

        record = parse_microsoft_import_line(
            1,
            "demo@outlook.com----https://mailapi.icu/key?type=html&orderNo=abc123",
        )

        self.assertEqual(record.email, "demo@outlook.com")
        self.assertEqual(record.account_type, "mailapi_url")
        self.assertEqual(record.mailapi_url, "https://mailapi.icu/key?type=html&orderNo=abc123")

    def test_alias_mail_url_export_line_imports_as_mailapi_url(self):
        """隐私邮箱导出的 `隐私邮箱----邮件 URL` 那一行，得能原样贴回邮箱导入。

        导出串是前端拼的，但格式契约在这一侧：`/m/<share_token>` 这种免登录链接
        必须被认成 mailapi_url，不然导出来的东西没地方用。
        """
        rules_module = load_microsoft_import_rules_module()
        parse_microsoft_import_line = rules_module.parse_microsoft_import_line

        record = parse_microsoft_import_line(
            1,
            "alias.sample@icloud.com----https://reg.example.com/m/y1urEDOgBNVeDE9aK5vUWA",
        )

        self.assertEqual(record.email, "alias.sample@icloud.com")
        self.assertEqual(record.account_type, "mailapi_url")
        self.assertEqual(
            record.mailapi_url,
            "https://reg.example.com/m/y1urEDOgBNVeDE9aK5vUWA",
        )

    def test_mailapi_only_batch_never_runs_the_oauth_probe(self):
        from services.mail_imports.schemas import MailImportExecuteRequest
        from services.mail_imports.providers import MicrosoftMailImportStrategy

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'mailapi-only.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with patch("core.db.engine", test_engine), \
                     _use_pool_db(test_engine), \
                     patch("services.mail_imports.providers.OutlookMailbox") as mailbox_cls:
                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content=(
                                "one@icloud.com----https://reg.example.com/m/tokenone\n"
                                "two@icloud.com----https://reg.example.com/m/tokentwo"
                            ),
                        )
                    )

                    mailbox_cls.assert_not_called()
                    self.assertEqual(response.summary.success, 2)
                    self.assertEqual(response.summary.failed, 0)
                    self.assertEqual(
                        {item.account_type for item in response.snapshot.items},
                        {"mailapi_url"},
                    )
            finally:
                test_engine.dispose()

    def test_rule_engine_returns_first_failure(self):
        rules_module = load_microsoft_import_rules_module()
        MicrosoftMailImportRecord = rules_module.MicrosoftMailImportRecord
        MicrosoftMailImportRuleEngine = rules_module.MicrosoftMailImportRuleEngine

        calls = []

        class FirstRejectRule:
            def evaluate(self, record, context):
                calls.append("first")
                return {"ok": False, "message": f"reject:{record.email}"}

        class SecondRuleMustNotRun:
            def evaluate(self, record, context):
                calls.append("second")
                raise AssertionError("second rule should not be executed after first failure")

        engine = MicrosoftMailImportRuleEngine([FirstRejectRule(), SecondRuleMustNotRun()])
        record = MicrosoftMailImportRecord(
            line_number=1,
            email="demo@outlook.com",
            password="password",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        result = engine.evaluate(record, {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "reject:demo@outlook.com")
        self.assertEqual(calls, ["first"])

    def test_duplicate_email_rule_rejects_existing_account(self):
        rules_module = load_microsoft_import_rules_module()
        DuplicateMicrosoftMailboxRule = rules_module.DuplicateMicrosoftMailboxRule
        MicrosoftMailImportRecord = rules_module.MicrosoftMailImportRecord

        rule = DuplicateMicrosoftMailboxRule()
        record = MicrosoftMailImportRecord(
            line_number=2,
            email="demo@outlook.com",
            password="password",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        result = rule.evaluate(record, {"existing_emails": {"demo@outlook.com"}})
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "行 2: 邮箱已存在: demo@outlook.com")

    def test_registered_email_rule_keeps_used_address_out_of_the_pool(self):
        rules_module = load_microsoft_import_rules_module()
        RegisteredMicrosoftMailboxRule = rules_module.RegisteredMicrosoftMailboxRule
        MicrosoftMailImportRecord = rules_module.MicrosoftMailImportRecord

        rule = RegisteredMicrosoftMailboxRule()
        record = MicrosoftMailImportRecord(
            line_number=3,
            email="Phillip94426+wbuuax@outlook.com",
            password="password",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        result = rule.evaluate(
            record,
            {"registered_emails": {"phillip94426+wbuuax@outlook.com"}},
        )
        self.assertFalse(result["ok"])
        self.assertIn("已注册过账号", result["message"])

    def test_microsoft_mailbox_availability_rule_rejects_service_abuse_mode(self):
        rules_module = load_microsoft_import_rules_module()
        MicrosoftMailImportRecord = rules_module.MicrosoftMailImportRecord
        MicrosoftMailboxAvailabilityRule = rules_module.MicrosoftMailboxAvailabilityRule

        class FakeMailbox:
            def probe_oauth_availability(self, **kwargs):
                return {
                    "ok": False,
                    "reason": "service_abuse_mode",
                    "message": "微软邮箱可用性检测未通过，账号处于 service abuse mode",
                }

        rule = MicrosoftMailboxAvailabilityRule(FakeMailbox())
        record = MicrosoftMailImportRecord(
            line_number=5,
            email="demo@hotmail.com",
            password="password",
            client_id="client-id",
            refresh_token="refresh-token",
        )

        result = rule.evaluate(record, {})
        self.assertFalse(result["ok"])
        self.assertEqual(result["message"], "行 5: 微软邮箱可用性检测未通过，账号处于 service abuse mode")

    def test_microsoft_strategy_rejects_invalid_mailapi_url(self):
        from services.mail_imports.providers import MicrosoftMailImportStrategy
        from services.mail_imports.schemas import MailImportExecuteRequest

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with patch("core.db.engine", test_engine), \
                     _use_pool_db(test_engine):
                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content="demo@outlook.com----not-a-url",
                        )
                    )

                    self.assertEqual(response.summary.total, 1)
                    self.assertEqual(response.summary.success, 0)
                    self.assertEqual(response.summary.failed, 1)
                    self.assertIn("无效的 mailapi_url", response.errors[0])
                    self.assertEqual(response.snapshot.count, 0)
            finally:
                test_engine.dispose()

    def test_microsoft_strategy_imports_only_rows_that_pass_rules(self):
        from services.mail_imports.schemas import MailImportExecuteRequest
        from services.mail_imports.providers import MicrosoftMailImportStrategy

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with patch("core.db.engine", test_engine), \
                     _use_pool_db(test_engine), \
                     patch("services.mail_imports.providers.OutlookMailbox") as mailbox_cls:
                    mailbox = mailbox_cls.return_value
                    mailbox.probe_oauth_availability.side_effect = [
                        {"ok": True, "reason": "ok", "message": "微软邮箱可用性检测通过", "access_token": "token-a"},
                        {"ok": False, "reason": "service_abuse_mode", "message": "微软邮箱可用性检测未通过，账号处于 service abuse mode"},
                    ]

                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content=(
                                "first@outlook.com----password----client-a----refresh-a\n"
                                "second@hotmail.com----password----client-b----refresh-b"
                            ),
                        )
                    )

                    self.assertEqual(response.summary.total, 2)
                    self.assertEqual(response.summary.success, 1)
                    self.assertEqual(response.summary.failed, 1)
                    self.assertEqual(response.snapshot.count, 1)
                    self.assertEqual(response.snapshot.items[0].email, "first@outlook.com")
                    self.assertIn("service abuse mode", response.errors[0])
            finally:
                test_engine.dispose()

    def test_microsoft_strategy_supports_mixed_oauth_and_mailapi_rows(self):
        from services.mail_imports.schemas import MailImportExecuteRequest
        from services.mail_imports.providers import MicrosoftMailImportStrategy

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with patch("core.db.engine", test_engine), \
                     _use_pool_db(test_engine), \
                     patch("services.mail_imports.providers.OutlookMailbox") as mailbox_cls:
                    mailbox = mailbox_cls.return_value
                    mailbox.probe_oauth_availability.return_value = {
                        "ok": True,
                        "reason": "ok",
                        "message": "微软邮箱可用性检测通过",
                        "access_token": "token-a",
                    }

                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content=(
                                "oauth@outlook.com----password----client-a----refresh-a\n"
                                "mailapi@hotmail.com----https://mailapi.icu/key?type=html&orderNo=abc123"
                            ),
                        )
                    )

                    self.assertEqual(response.summary.total, 2)
                    self.assertEqual(response.summary.success, 2)
                    self.assertEqual(response.summary.failed, 0)
                    self.assertEqual(response.snapshot.count, 2)
                    account_types = {item.email: item.account_type for item in response.snapshot.items}
                    self.assertEqual(account_types.get("oauth@outlook.com"), "microsoft_oauth")
                    self.assertEqual(account_types.get("mailapi@hotmail.com"), "mailapi_url")
            finally:
                test_engine.dispose()

    def test_microsoft_strategy_alias_split_generates_alias_emails(self):
        from services.mail_imports.schemas import MailImportExecuteRequest
        from services.mail_imports.providers import MicrosoftMailImportStrategy

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with patch("core.db.engine", test_engine), \
                     _use_pool_db(test_engine), \
                     patch("services.mail_imports.providers.OutlookMailbox") as mailbox_cls, \
                     patch("services.mail_imports.providers.random.choices") as mock_choices:
                    mailbox = mailbox_cls.return_value
                    mailbox.probe_oauth_availability.return_value = {
                        "ok": True,
                        "reason": "ok",
                        "message": "微软邮箱可用性检测通过",
                        "access_token": "token-a",
                    }
                    mock_choices.side_effect = [
                        list("abcdef"),
                        list("ghijkl"),
                    ]

                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content="alias@outlook.com----password----client-a----refresh-a",
                            alias_split_enabled=True,
                            alias_split_count=2,
                            alias_include_original=False,
                        )
                    )

                    self.assertEqual(response.summary.total, 2)
                    self.assertEqual(response.summary.success, 2)
                    self.assertEqual(response.summary.failed, 0)
                    imported_emails = sorted(item.email for item in response.snapshot.items)
                    self.assertEqual(
                        imported_emails,
                        sorted(
                            [
                                "alias+abcdef@outlook.com",
                                "alias+ghijkl@outlook.com",
                            ]
                        ),
                    )
            finally:
                test_engine.dispose()


    def test_microsoft_strategy_skips_addresses_that_already_registered(self):
        from core.db import AccountModel
        from services.mail_imports.schemas import MailImportExecuteRequest
        from services.mail_imports.providers import MicrosoftMailImportStrategy

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'mail-imports.db'}")
            SQLModel.metadata.create_all(test_engine)

            from sqlmodel import Session

            with Session(test_engine) as session:
                # ① 只注册过 chatgpt —— 还能入池（后面能拿去注册 grok）
                session.add(
                    AccountModel(
                        platform="chatgpt",
                        email="used+wbuuax@outlook.com",
                        password="pwd",
                    )
                )
                # ② 对所有平台都注册过 —— 才真的该拒绝
                for platform in ("chatgpt", "grok"):
                    session.add(
                        AccountModel(
                            platform=platform,
                            email="spent@outlook.com",
                            password="pwd",
                        )
                    )
                session.commit()

            try:
                with patch("core.db.engine", test_engine), \
                     _use_pool_db(test_engine), \
                     patch("services.mail_imports.providers.OutlookMailbox") as mailbox_cls:
                    mailbox = mailbox_cls.return_value
                    mailbox.probe_oauth_availability.return_value = {
                        "ok": True,
                        "reason": "ok",
                        "message": "微软邮箱可用性检测通过",
                    }

                    response = strategy.execute(
                        MailImportExecuteRequest(
                            type="microsoft",
                            content=(
                                "used+wbuuax@outlook.com----password----client-a----refresh-a\n"
                                "spent@outlook.com----password----client-c----refresh-c\n"
                                "fresh@outlook.com----password----client-b----refresh-b"
                            ),
                        )
                    )

                    # 只注册过 chatgpt 的那个**要放行** —— 邮箱只对用掉它的平台
                    # 一次性，跨平台复用正是用户要的。
                    self.assertEqual(response.summary.success, 2)
                    self.assertEqual(response.summary.failed, 1)
                    self.assertIn("所有平台", " ".join(response.errors))
                    self.assertEqual(
                        sorted(item.email for item in response.snapshot.items),
                        ["fresh@outlook.com", "used+wbuuax@outlook.com"],
                    )
            finally:
                test_engine.dispose()

    def test_pool_pop_walks_past_addresses_that_already_registered(self):
        from sqlmodel import Session, select
        from core.base_mailbox import OutlookMailbox
        from core.db import AccountModel, OutlookAccountModel

        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'pool.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with Session(test_engine) as session:
                    session.add(
                        AccountModel(
                            platform="chatgpt",
                            email="Used+wbuuax@outlook.com",
                            password="pwd",
                        )
                    )
                    session.add(
                        OutlookAccountModel(email="used+wbuuax@outlook.com", password="pwd")
                    )
                    session.add(OutlookAccountModel(email="fresh@outlook.com", password="pwd"))
                    session.commit()

                with patch("core.db.engine", test_engine), \
                     _use_pool_db(test_engine):
                    payload = OutlookMailbox()._pop_account()

                self.assertEqual(payload["email"], "fresh@outlook.com")

                with Session(test_engine) as session:
                    rows = {
                        row.email: row
                        for row in session.exec(select(OutlookAccountModel)).all()
                    }
                self.assertEqual(
                    sorted(rows), ["fresh@outlook.com", "used+wbuuax@outlook.com"]
                )
                self.assertEqual(rows["fresh@outlook.com"].status, "in_use")
            finally:
                test_engine.dispose()

    def test_pool_pop_says_so_when_everything_left_is_already_registered(self):
        from sqlmodel import Session
        from core.base_mailbox import OutlookMailbox
        from core.db import AccountModel, OutlookAccountModel

        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'pool-used.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                with Session(test_engine) as session:
                    session.add(
                        AccountModel(platform="chatgpt", email="used@outlook.com", password="pwd")
                    )
                    session.add(OutlookAccountModel(email="used@outlook.com", password="pwd"))
                    session.commit()

                with patch("core.db.engine", test_engine), \
                     _use_pool_db(test_engine):
                    with self.assertRaisesRegex(RuntimeError, "都已经注册过了"):
                        OutlookMailbox()._pop_account()
            finally:
                test_engine.dispose()




class RegisteredRuleCrossPlatformTests(unittest.TestCase):
    """`RegisteredMicrosoftMailboxRule` 必须按平台判定，不能全局拉黑。

    用户要求：一个邮箱注册了 gpt 还能注册 grok。旧实现「注册过就不入池」
    会让那个地址永远进不了池子 —— 跨平台复用直接失效。
    """

    def _rule(self):
        from services.mail_imports import microsoft_import_rules as rules_module

        return rules_module.RegisteredMicrosoftMailboxRule()

    def _record(self, email: str):
        from services.mail_imports.microsoft_import_rules import MicrosoftMailImportRecord

        return MicrosoftMailImportRecord(line_number=1, email=email, password="p")

    def test_email_registered_on_one_platform_is_still_importable(self):
        """只注册过 chatgpt 的地址，仍可入池（后面能拿去注册 grok）。"""
        result = self._rule().evaluate(
            self._record("shared@outlook.com"),
            {
                "existing_emails": set(),
                "registered_emails": {"shared@outlook.com"},
                # 调用方给的「对所有平台都注册过」名单里没有它
                "registered_owner_emails": set(),
            },
        )
        self.assertTrue(result["ok"], f"不该拒绝：{result}")

    def test_email_registered_on_every_platform_is_rejected(self):
        """对所有平台都注册过的地址才拒绝入池。"""
        result = self._rule().evaluate(
            self._record("spent@outlook.com"),
            {
                "existing_emails": set(),
                "registered_emails": {"spent@outlook.com"},
                "registered_owner_emails": {"spent@outlook.com"},
            },
        )
        self.assertFalse(result["ok"])
        self.assertIn("所有平台", result["message"])

    def test_without_owner_list_falls_back_to_conservative(self):
        """调用方没给全平台名单时退回旧行为（保守）—— 不能放宽成「都放行」。"""
        result = self._rule().evaluate(
            self._record("unknown@outlook.com"),
            {
                "existing_emails": set(),
                "registered_emails": {"unknown@outlook.com"},
            },
        )
        self.assertFalse(result["ok"], "拿不准时必须保守拒绝")

    def test_never_registered_passes(self):
        result = self._rule().evaluate(
            self._record("new@outlook.com"),
            {"existing_emails": set(), "registered_emails": set()},
        )
        self.assertTrue(result["ok"])


class SnapshotPlatformEvidenceTests(unittest.TestCase):
    """预览快照要带「这个地址注册过哪些平台」的证据。

    用户诉求：一个邮箱可以被多个平台使用，只显示「已使用」区分不出是哪个平台
    用过 —— 预览表要能按平台筛、也要能一眼看出已注册平台。证据在 `accounts`
    表（跨库），池里的 `used_platforms` 只是记账、可能缺项。
    """

    def test_snapshot_items_expose_used_and_registered_platforms(self):
        from services.mail_imports.providers import MicrosoftMailImportStrategy
        from services.mail_imports.schemas import MailImportSnapshotRequest

        strategy = MicrosoftMailImportStrategy()
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_engine = create_engine(f"sqlite:///{Path(tmp_dir) / 'evidence.db'}")
            SQLModel.metadata.create_all(test_engine)

            try:
                from core.db.models_account import AccountModel
                from core.db.models_platform import OutlookAccountModel

                with Session(test_engine) as session:
                    session.add(
                        AccountModel(platform="chatgpt", email="shared@outlook.com", password="p")
                    )
                    session.add(
                        AccountModel(platform="grok", email="shared@outlook.com", password="p")
                    )
                    session.add(
                        OutlookAccountModel(
                            email="shared@outlook.com",
                            password="p",
                            status="used",
                            used_platforms=",chatgpt,",
                        )
                    )
                    session.add(
                        OutlookAccountModel(email="fresh@outlook.com", password="p", status="available")
                    )
                    session.commit()

                with patch("core.db.engine", test_engine), _use_pool_db(test_engine):
                    snapshot = strategy.get_snapshot(
                        MailImportSnapshotRequest(type="microsoft")
                    )

                by_email = {item.email: item for item in snapshot.items}
                shared = by_email["shared@outlook.com"]
                self.assertEqual(shared.used_platforms, ",chatgpt,")
                self.assertEqual(
                    shared.registered_platforms,
                    ["chatgpt", "grok"],
                    "两个平台的账号记录都要出现在证据里（跨库比对，不只信池子记账）",
                )
                self.assertEqual(by_email["fresh@outlook.com"].registered_platforms, [])
                self.assertEqual(by_email["fresh@outlook.com"].used_platforms, "")
            finally:
                test_engine.dispose()


if __name__ == "__main__":
    unittest.main()
