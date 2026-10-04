"""TOTP 密钥从 enroll 响应一路走到数据库。

密钥只在 enroll 响应里下发一次，中途任何一环把它丢了这个号的 2FA 就废了，
所以注册链和补绑链两条路都要有落库断言，而不是只测到内存里的 result。
"""

import json
import unittest
from unittest import mock

from sqlmodel import Session, delete, select
from sqlalchemy.exc import OperationalError

from core.base_platform import Account, AccountStatus
from core.db import AccountModel, engine, save_account
from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    build_chatgpt_registration_mode_adapter,
)
from platforms.chatgpt.protocol.auth_flow import AuthResult
from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult
from platforms.chatgpt.registration_engine import ChatGPTRegistrationEngine

SECRET = "JBSWY3DPEHPK3PXP"


class _FakeFlow:
    """只保留注册引擎会碰到的那几个面：结果对象和 session_ready 钩子。"""

    def __init__(self, *_args, on_session_ready=None, raise_after_hook=None, **_kwargs):
        self.result = AuthResult()
        self.result.email = "demo@example.com"
        self.result.password = "pw-demo"
        self.result.access_token = "at-1"
        self.result.session_token = "sess-1"
        self._on_session_ready = on_session_ready
        self._raise_after_hook = raise_after_hook

    def run_register(self, _mail_provider):
        if self._on_session_ready is not None:
            self._on_session_ready(self, self.result.access_token)
        if self._raise_after_hook is not None:
            raise self._raise_after_hook
        return self.result


def _run_engine(*, raise_after_hook=None, bind_result=None):
    engine_under_test = ChatGPTRegistrationEngine(
        mailbox=mock.Mock(), email="demo@example.com", bind_2fa=True, log_fn=lambda _m: None
    )
    bind_result = bind_result or TwoFactorBindResult(ok=True, secret=SECRET)

    def _flow_factory(*args, **kwargs):
        return _FakeFlow(*args, raise_after_hook=raise_after_hook, **kwargs)

    with mock.patch(
        "platforms.chatgpt.registration_engine.AuthFlow", side_effect=_flow_factory
    ), mock.patch(
        "platforms.chatgpt.registration_engine.resolve_sms_settings", return_value={}
    ), mock.patch(
        "platforms.chatgpt.registration_engine.build_phone_callback", return_value=None
    ), mock.patch.object(
        ChatGPTRegistrationEngine, "_build_mail_provider", return_value=mock.Mock()
    ), mock.patch(
        "platforms.chatgpt.registration_engine.bind_totp_inline", return_value=bind_result
    ), mock.patch(
        "platforms.chatgpt.registration_engine.bind_totp_via_login",
        return_value=TwoFactorBindResult(error_message="重登未果"),
    ) as slow_path:
        return engine_under_test.run(), slow_path


class RegisterPathPersistenceTests(unittest.TestCase):
    def setUp(self):
        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.commit()

    def test_inline_bind_puts_the_secret_in_the_registration_metadata(self):
        result, slow_path = _run_engine()

        self.assertTrue(result.success)
        self.assertEqual(result.metadata["totp_secret"], SECRET)
        self.assertTrue(result.metadata["chatgpt_2fa"]["bound"])
        # 快路径已经拿到密钥，不该再去跑一次带 PoW 的重登
        slow_path.assert_not_called()

    def test_secret_survives_the_hand_off_to_the_account_record(self):
        result, _ = _run_engine()
        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_bind_2fa": True})

        account = adapter.build_account(result, fallback_password="fallback")

        self.assertEqual(account.extra["totp_secret"], SECRET)

    def test_secret_is_written_to_the_database(self):
        result, _ = _run_engine()
        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_bind_2fa": True})

        save_account(adapter.build_account(result, fallback_password="fallback"))

        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == "demo@example.com")
            ).first()
        self.assertEqual(json.loads(row.extra_json)["totp_secret"], SECRET)

    def test_a_late_crash_does_not_lose_an_already_bound_secret(self):
        # 典型场景：2FA 绑完了，后面 Codex 换 refresh_token 那步炸了
        result, _ = _run_engine(raise_after_hook=RuntimeError("codex oauth 失败"))

        self.assertTrue(result.success)
        self.assertEqual(result.metadata["totp_secret"], SECRET)
        self.assertTrue(result.metadata["chatgpt_2fa"]["bound"])

    def test_a_failed_bind_leaves_no_secret_but_records_the_attempt(self):
        result, slow_path = _run_engine(
            bind_result=TwoFactorBindResult(error_message="enroll 403")
        )

        slow_path.assert_called_once()
        self.assertFalse(result.metadata["chatgpt_2fa"]["bound"])
        # 没绑上时账号 extra 里干脆没有这个键，账号页才不会显示成"已绑"
        adapter = build_chatgpt_registration_mode_adapter({"chatgpt_bind_2fa": True})
        account = adapter.build_account(result, fallback_password="fallback")
        self.assertNotIn("totp_secret", account.extra)


class BindActionPersistenceTests(unittest.TestCase):
    """老号补绑走的是 action 的 ``account_extra_patch`` 通道。"""

    def setUp(self):
        with Session(engine) as session:
            session.exec(delete(AccountModel))
            model = AccountModel(platform="chatgpt", email="old@example.com", password="pw")
            model.set_extra({"access_token": "at-1"})
            session.add(model)
            session.commit()
            self.account_id = model.id

    def _apply(self, result: dict) -> dict:
        from api.actions import _apply_action_result

        with Session(engine) as session:
            row = session.get(AccountModel, self.account_id)
            _apply_action_result("chatgpt", "bind_2fa", row, result, session)
            session.commit()
            session.refresh(row)
            return row.get_extra()

    def test_successful_bind_persists_the_secret_next_to_the_credentials(self):
        from platforms.chatgpt.plugin import ChatGPTPlatform
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult
        from services import chatgpt_two_factor

        account = Account(
            platform="chatgpt",
            email="old@example.com",
            password="pw",
            token="at-1",
            status=AccountStatus.REGISTERED,
            extra={"access_token": "at-1"},
        )
        with mock.patch.object(
            chatgpt_two_factor,
            "bind_account_two_factor",
            return_value=TwoFactorBindResult(ok=True, secret=SECRET),
        ):
            result = ChatGPTPlatform().execute_action("bind_2fa", account, {})

        extra = self._apply(result)
        self.assertEqual(extra["totp_secret"], SECRET)
        self.assertTrue(extra["chatgpt_2fa"]["bound"])
        self.assertEqual(extra["access_token"], "at-1")

    def test_a_failed_bind_never_clears_a_secret_that_is_already_there(self):
        from services.chatgpt_two_factor import build_extra_patch
        from platforms.chatgpt.protocol.two_factor import TwoFactorBindResult

        with Session(engine) as session:
            row = session.get(AccountModel, self.account_id)
            row.set_extra({"access_token": "at-1", "totp_secret": SECRET})
            session.add(row)
            session.commit()

        extra = self._apply(
            {"ok": False, "account_extra_patch": build_extra_patch(TwoFactorBindResult(error_message="enroll 403"))}
        )

        self.assertEqual(extra["totp_secret"], SECRET)
        self.assertFalse(extra["chatgpt_2fa"]["bound"])


class SecretJournalTests(unittest.TestCase):
    """写前日志：密钥在「拿到」到「落库」之间必须有一份不依赖 DB 的副本。

    实测踩过：手工调 ``bind_totp_via_login`` 没走落库，密钥直接丢了，那个号
    的 2FA 永久锁死（服务端不下发第二次）。还有一层更隐蔽的 —— 动作链绑定
    期间占着一条 session，等它 flush 过写事务后另开连接写库会
    ``database is locked``，所以 DB 写本身也不可靠。
    """

    def setUp(self):
        from services import chatgpt_two_factor as mod

        with Session(engine) as session:
            session.exec(delete(AccountModel))
            session.commit()
        # 清掉上一轮留下的日志文件
        directory = mod._totp_journal_dir()
        for path in directory.glob("*.json"):
            path.unlink()

    def test_secret_lands_in_the_journal_before_the_account_row_exists(self):
        """注册链时序：密钥先到，账号行后建 —— 此时文件里必须有。"""
        from services import chatgpt_two_factor as mod

        persist = mod.make_secret_persister(email="later@example.com")
        persist(SECRET)

        payload = json.loads(mod._journal_path("later@example.com").read_text(encoding="utf-8"))
        self.assertEqual(payload["totp_secret"], SECRET)
        self.assertEqual(payload["email"], "later@example.com")

    def test_startup_recovery_backfills_the_secret_once_the_row_appears(self):
        from services import chatgpt_two_factor as mod

        mod.make_secret_persister(email="later@example.com")(SECRET)

        # 账号行还不存在：文件保留，不能被当成"已处理"删掉
        self.assertEqual(mod.recover_pending_totp_secrets(), 0)
        self.assertTrue(mod._journal_path("later@example.com").exists())

        # 行建出来了 → 启动恢复补写
        with Session(engine) as session:
            session.add(AccountModel(platform="chatgpt", email="later@example.com", password="pw"))
            session.commit()
        self.assertEqual(mod.recover_pending_totp_secrets(), 1)

        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == "later@example.com")
            ).first()
        self.assertEqual(row.get_extra()["totp_secret"], SECRET)
        self.assertFalse(mod._journal_path("later@example.com").exists())

    def test_recovery_does_not_overwrite_an_existing_secret(self):
        from services import chatgpt_two_factor as mod

        with Session(engine) as session:
            row = AccountModel(platform="chatgpt", email="kept@example.com", password="pw")
            row.set_extra({"totp_secret": "OLD-SECRET"})
            session.add(row)
            session.commit()

        mod.make_secret_persister(email="kept@example.com")("NEW-SECRET")
        mod.recover_pending_totp_secrets()

        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == "kept@example.com")
            ).first()
        # 已有密钥的号不该被日志覆盖（覆盖会把先绑的验证器废掉）
        self.assertEqual(row.get_extra()["totp_secret"], "OLD-SECRET")
        # 两把密钥不一致 → 文件保留待人工核对，不能静默丢掉其中一把
        self.assertTrue(mod._journal_path("kept@example.com").exists())

    def test_journal_write_survives_a_locked_database(self):
        """DB 撞锁时密钥仍要保住 —— 这正是写前日志存在的理由。"""
        from services import chatgpt_two_factor as mod

        with Session(engine) as session:
            session.add(AccountModel(platform="chatgpt", email="locked@example.com", password="pw"))
            session.commit()

        persist = mod.make_secret_persister(email="locked@example.com")
        with mock.patch.object(
            mod, "_apply_journal_entry", side_effect=OperationalError("stmt", {}, Exception("database is locked"))
        ):
            persist(SECRET)  # 不该抛

        self.assertTrue(mod._journal_path("locked@example.com").exists())
        self.assertEqual(mod.recover_pending_totp_secrets(), 1)

    def test_stale_journal_entries_are_not_auto_applied(self):
        """超期条目不能自动补写：邮箱可能已经换成了另一个号。"""
        from services import chatgpt_two_factor as mod

        with Session(engine) as session:
            session.add(AccountModel(platform="chatgpt", email="recycled@example.com", password="pw"))
            session.commit()

        mod._write_secret_journal("recycled@example.com", SECRET)
        # 把时间戳改到 30 天前
        path = mod._journal_path("recycled@example.com")
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertEqual(mod.recover_pending_totp_secrets(), 0)
        with Session(engine) as session:
            row = session.exec(
                select(AccountModel).where(AccountModel.email == "recycled@example.com")
            ).first()
        self.assertFalse((row.get_extra().get("totp_secret") or "").strip())
        # 文件保留待人工核对，不静默删
        self.assertTrue(path.exists())

    def test_journal_file_is_not_world_readable(self):
        """里面是明文密钥，权限必须是 0600。"""
        import stat as stat_mod

        from services import chatgpt_two_factor as mod

        mod.make_secret_persister(email="perm@example.com")(SECRET)
        mode = stat_mod.S_IMODE(mod._journal_path("perm@example.com").stat().st_mode)
        self.assertEqual(mode, 0o600, f"期望 0600，实际 {oct(mode)}")

    def test_persister_is_wired_into_the_protocol_layer(self):
        """三条调用链都必须把回调传下去，否则等于没修。"""
        import inspect

        from platforms.chatgpt.protocol import two_factor
        from platforms.chatgpt import registration_engine

        src = inspect.getsource(two_factor.enroll_totp)
        self.assertIn("_notify_secret(on_secret, secret)", src)
        # 密钥要在 activate 之前就交给调用方（activate 失败号也已带上 factor）
        self.assertLess(
            src.index("_notify_secret(on_secret, secret)"),
            src.index("_activate("),
            "密钥回调必须早于 activate",
        )
        for fn in (two_factor.bind_totp_inline, two_factor.bind_totp_via_login):
            self.assertIn("on_secret", inspect.signature(fn).parameters)

        engine_src = inspect.getsource(registration_engine.ChatGPTRegistrationEngine)
        self.assertIn("on_secret=self._journal_secret(flow)", engine_src)
        self.assertEqual(engine_src.count("on_secret=self._journal_secret(flow)"), 2)


if __name__ == "__main__":
    unittest.main()
