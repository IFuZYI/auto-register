"""Kiro 平台（`platforms/kiro`）的契约、凭据格式与授权协议。

三块：

1. **凭据格式** —— 用户给的 `kiro.json`（Kiro 账号管理器的导出格式，v2）就是
   目标形状：`accounts[]` 里每条带 `pluginSecret`（access / refresh / clientId /
   clientSecret / expires / profileArn / region / method …）与 `pluginPublic`
   （从 secret 派生出来的展示字段）。本模块要保证「注册产物 → 这份 JSON」以及
   「这份 JSON → 账号记录」两个方向都不丢字段；
2. **来源** —— Kiro 账号必须记下它从哪来：`source = aws`，并把 AWS 的邮箱+密码
   一起保存（Kiro 的登录就是 AWS Builder ID）；
3. **授权协议** —— AWS Builder ID 换 Kiro IDE 凭据（client/register → authorize →
   authentication_result → associate_token → code → token）。

运行：`.venv/bin/python -m pytest tests/test_kiro_platform.py -q`
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest import mock

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "kiro_credentials_sample.json"


def _response(status_code: int = 200, body: dict | None = None, text: str = "", headers=None):
    class _R:
        pass

    item = _R()
    item.status_code = status_code
    item._body = body
    item.text = text if body is None else json.dumps(body)
    item.headers = headers or {}

    def _json():
        return json.loads(json.dumps(item._body)) if item._body is not None else {}

    item.json = _json
    return item


class ScriptedSession:
    def __init__(self, router) -> None:
        self._router = router
        self.requests: list[dict] = []
        self.cookies = _Jar()

    def get(self, url, **kwargs):
        self.requests.append({"method": "GET", "url": url, **kwargs})
        return self._router("GET", url, kwargs)

    def post(self, url, **kwargs):
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return self._router("POST", url, kwargs)

    def close(self):
        pass


class _Jar:
    def set(self, name, value, domain="", path="", **_):
        pass

    def get(self, name, default=None, **_):
        return default

    def get_dict(self, **_):
        return {}


# ---------------------------------------------------------------------------
# 1. 凭据格式
# ---------------------------------------------------------------------------


class KiroCredentialFormatTests(unittest.TestCase):
    def _build(self, **overrides):
        from platforms.kiro.credentials import build_extra

        params = dict(
            access_token="access-1",
            refresh_token="refresh-1",
            client_id="kid-1",
            client_secret="ksecret-1",
            expires_in=3600,
            profile_arn="arn:aws:codewhisperer:us-east-1:699475941385:profile/ABC",
            region="us-east-1",
            account_id="user@x.ai",
            email="user@x.ai",
            source="aws",
        )
        params.update(overrides)
        return build_extra(**params)

    def test_source_is_aws_by_default(self):
        from platforms.kiro.credentials import build_extra

        extra = build_extra(
            access_token="a", refresh_token="r", client_id="c", client_secret="s"
        )
        self.assertEqual(extra["source"], "aws")

    def test_canonical_keys_are_snake_case(self):
        extra = self._build()
        for key in (
            "access_token",
            "refresh_token",
            "client_id",
            "client_secret",
            "profile_arn",
            "region",
            "auth_method",
            "login_provider",
            "plan",
            "account_id",
            "uid",
            "source",
            "expires_at",
        ):
            self.assertIn(key, extra, f"缺少 {key}")
        self.assertEqual(extra["auth_method"], "idc")
        self.assertEqual(extra["source"], "aws")
        self.assertIsInstance(extra["expires_at"], int)
        self.assertGreater(extra["expires_at"], 10**12, "expires_at 用毫秒（与 kiro.json 一致）")

    def test_export_entry_matches_kiro_json_shape(self):
        from platforms.kiro.credentials import to_account_entry

        class _Account:
            platform = "kiro"
            email = "user@x.ai"
            password = "pw-1"
            token = "refresh-1"
            extra = self._build()
            updated_at = None
            created_at = 1794414790

        entry = to_account_entry(_Account())

        # 顶层
        for key in ("accessToken", "addedAt", "desktop", "id", "name", "pluginIdentity",
                    "pluginPublic", "pluginSecret", "provider", "refreshToken",
                    "source", "uid", "updatedAt"):
            self.assertIn(key, entry, f"顶层缺少 {key}")
        self.assertEqual(entry["provider"], "kiro")
        self.assertEqual(entry["source"], "aws", "来源要落成 aws（不是参考文件里的 plugin）")
        self.assertTrue(entry["id"].startswith("kiro-account-"))
        self.assertFalse(entry["desktop"])

        # pluginSecret：与 kiro.json 的字段逐一对上
        secret = entry["pluginSecret"]
        self.assertEqual(secret["access"], "access-1")
        self.assertEqual(secret["refresh"], "refresh-1")
        self.assertEqual(secret["clientId"], "kid-1")
        self.assertEqual(secret["clientSecret"], "ksecret-1")
        self.assertEqual(secret["region"], "us-east-1")
        self.assertEqual(secret["method"], "idc")
        self.assertTrue(secret["profileArn"].startswith("arn:aws:codewhisperer:"))
        self.assertEqual(secret["accountId"], "user@x.ai")
        self.assertIn("expires", secret)

        # pluginPublic：派生字段
        public = entry["pluginPublic"]
        self.assertTrue(public["hasAccessToken"])
        self.assertTrue(public["hasRefreshToken"])
        self.assertTrue(public["hasCredentials"])
        self.assertTrue(public["canRefresh"])
        self.assertEqual(public["tokenTail"], "sh-1", "tokenTail 是 refresh 的后 4 位")
        self.assertEqual(public["accountId"], "user@x.ai")
        self.assertIn("expiresAtMs", public)

    def test_document_wrapper_matches_version_2(self):
        from platforms.kiro.credentials import build_document

        document = build_document([{"email": "a@x.ai"}], exported_at=1_700_000_000_000)
        self.assertEqual(document["version"], 2)
        self.assertEqual(document["customProviders"], [])
        self.assertEqual(document["exportedAt"], 1_700_000_000_000)
        self.assertEqual(len(document["accounts"]), 1)

    def test_parse_sample_attachment(self):
        from platforms.kiro.credentials import parse_document

        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        records = parse_document(payload)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["email"], "sample.user@example.com")
        self.assertTrue(record["access_token"].startswith("aoa"))
        self.assertTrue(record["refresh_token"].startswith("aor"))
        self.assertEqual(record["region"], "us-east-1")
        self.assertEqual(record["auth_method"], "social")
        self.assertEqual(record["login_provider"], "github")
        self.assertEqual(record["plan"], "Kiro FREE")
        self.assertTrue(record["profile_arn"].startswith("arn:aws:codewhisperer:"))
        self.assertIsInstance(record["expires_at"], int)

    def test_round_trip_keeps_every_credential(self):
        from platforms.kiro.credentials import build_document, parse_document

        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        first = parse_document(payload)[0]

        class _Account:
            platform = "kiro"
            email = first["email"]
            password = "pw-1"
            token = first["refresh_token"]
            extra = first
            updated_at = None
            created_at = 1794414790

        from platforms.kiro.credentials import to_account_entry

        document = build_document([to_account_entry(_Account())])
        second = parse_document(document)[0]
        for key in (
            "access_token",
            "refresh_token",
            "client_id",
            "client_secret",
            "region",
            "profile_arn",
            "auth_method",
            "login_provider",
            "plan",
            "account_id",
            "uid",
            "expires_at",
        ):
            self.assertEqual(second.get(key), first.get(key), f"往返丢了 {key}")

    def test_import_creates_account_records(self):
        from core.models.account import AccountRecord
        from platforms.kiro.credentials import parse_document, records_from_entries

        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        records = records_from_entries(parse_document(payload))
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIsInstance(record, AccountRecord)
        self.assertEqual(record.platform, "kiro")
        self.assertEqual(record.email, "sample.user@example.com")
        self.assertEqual(record.extra["source"], "plugin", "导入的要保留文件里的来源")
        self.assertTrue(record.extra["refresh_token"])


# ---------------------------------------------------------------------------
# 2. 授权协议
# ---------------------------------------------------------------------------


class KiroAuthorizeTests(unittest.TestCase):
    def test_authorize_exchanges_code_for_kiro_tokens(self):
        from platforms.kiro.authorize import authorize_kiro

        def router(method, url, kwargs):
            if url.endswith("/client/register"):
                return _response(body={"clientId": "kiro-client", "clientSecret": "kiro-secret"})
            if "/authorize?" in url and "response_type=code" in url:
                return _response(
                    status_code=302,
                    headers={"Location": "https://us-east-1.signin.aws/oauth?orchestrator_id=orch-1"},
                )
            if url.endswith("/authentication_result"):
                return _response(body={"location": "https://oidc.us-east-1.amazonaws.com/authorize?x=1"})
            if "authorize?x=1" in url:
                return _response(
                    status_code=302,
                    headers={"Location": "https://view.awsapps.com/start/?authorizationResumptionContext=ctx-1"},
                )
            if url.endswith("/associate_token"):
                return _response(body={"location": "https://oidc.us-east-1.amazonaws.com/resume?y=2"})
            if "resume?y=2" in url:
                return _response(
                    status_code=302,
                    headers={"Location": "http://127.0.0.1:49152/oauth/callback?code=code-1&state=st-1"},
                )
            if url.endswith("/token"):
                return _response(
                    body={
                        "accessToken": "kiro-at",
                        "refreshToken": "kiro-rt",
                        "expiresIn": 3600,
                        "profileArn": "arn:aws:codewhisperer:us-east-1:699475941385:profile/XYZ",
                        "tokenType": "Bearer",
                    }
                )
            raise AssertionError(f"未预设响应: {method} {url}")

        session = ScriptedSession(router)
        result = authorize_kiro(session=session, sso_token="sso-1", redirect_port=49152)

        self.assertEqual(result["access_token"], "kiro-at")
        self.assertEqual(result["refresh_token"], "kiro-rt")
        self.assertEqual(result["client_id"], "kiro-client")
        self.assertEqual(result["client_secret"], "kiro-secret")
        self.assertEqual(result["expires_in"], 3600)
        self.assertTrue(result["profile_arn"].endswith("/XYZ"))

        # 认证结果请求必须带 SSO bearer（两种拼写都带，服务端认其中之一）
        auth_calls = [r for r in session.requests if r["url"].endswith("/authentication_result")]
        self.assertEqual(len(auth_calls), 1)
        headers = auth_calls[0]["headers"]
        self.assertEqual(headers.get("x-amz-sso-bearer-token"), "sso-1")
        self.assertEqual(headers.get("x-amz-sso_bearer_token"), "sso-1")

    def test_authorize_requires_auth_code(self):
        from platforms.kiro.authorize import KiroAuthorizeError, authorize_kiro

        def router(method, url, kwargs):
            if url.endswith("/client/register"):
                return _response(body={"clientId": "c", "clientSecret": "s"})
            if "/authorize?" in url and "response_type=code" in url:
                return _response(status_code=302, headers={"Location": "https://x/?no_orchestrator=1"})
            raise AssertionError(url)

        with self.assertRaises(KiroAuthorizeError):
            authorize_kiro(session=ScriptedSession(router), sso_token="sso-1")


# ---------------------------------------------------------------------------
# 3. 插件契约
# ---------------------------------------------------------------------------


class _StubMailbox:
    _platform_name = ""

    def __init__(self, email="kiro@x.ai", code="123456") -> None:
        self.email = email
        self.code = code
        self.waited = 0

    def get_email(self):
        from core.mailboxes.base import MailboxAccount

        return MailboxAccount(email=self.email, account_id="token-1")

    def wait_for_code(self, account, **_kwargs):
        self.waited += 1
        return self.code


class KiroExportImportTests(unittest.TestCase):
    """导出/导入：既要能吐 kiro.json，也要能把 kiro.json 收进来。"""

    def _account(self):
        from core.models.account import AccountRecord

        extra = {
            "access_token": "at-1",
            "refresh_token": "rt-1",
            "client_id": "cid-1",
            "client_secret": "csec-1",
            "profile_arn": "arn:aws:codewhisperer:us-east-1:699475941385:profile/ABC",
            "region": "us-east-1",
            "auth_method": "idc",
            "login_provider": "BuilderId",
            "plan": "Kiro FREE",
            "account_id": "kiro-export@x.ai",
            "uid": "builderid",
            "expires_at": 1_794_478_346_773,
            "source": "aws",
            "source_email": "kiro-export@x.ai",
        }
        return AccountRecord(
            platform="kiro", email="kiro-export@x.ai", password="pw", token="rt-1", extra=extra
        )

    def test_kiro_json_format_is_registered(self):
        from services.account_export import EXPORT_FORMATS, list_export_formats

        self.assertIn("kiro_json", EXPORT_FORMATS)
        ids = {item["id"] for item in list_export_formats()}
        self.assertIn("kiro_json", ids)

    def test_kiro_json_format_renders_version_2_document(self):
        from services.account_export import render_accounts

        text = render_accounts([self._account()], "kiro_json")
        payload = json.loads(text)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(len(payload["accounts"]), 1)
        entry = payload["accounts"][0]
        self.assertEqual(entry["provider"], "kiro")
        self.assertEqual(entry["source"], "aws")
        self.assertEqual(entry["pluginSecret"]["access"], "at-1")
        self.assertEqual(entry["pluginSecret"]["refresh"], "rt-1")

    def test_kiro_json_format_skips_other_platforms(self):
        from core.models.account import AccountRecord
        from services.account_export import render_accounts

        other = AccountRecord(platform="chatgpt", email="gpt@x.ai", password="p")
        payload = json.loads(render_accounts([other], "kiro_json"))
        self.assertEqual(payload["accounts"], [])

    def test_generic_json_export_keeps_kiro_credentials(self):
        """「JSON 全字段」导出不能丢 kiro 的 client_secret / profile_arn / source。"""
        from services.account_export import render_accounts

        rows = json.loads(render_accounts([self._account()], "json"))
        row = rows[0]
        self.assertEqual(row["client_id"], "cid-1")
        self.assertEqual(row["client_secret"], "csec-1")
        self.assertTrue(row["profile_arn"].startswith("arn:aws:codewhisperer:"))
        self.assertEqual(row["source"], "aws")

    def test_import_kiro_json_creates_accounts(self):
        """把用户给的 kiro.json 原样贴进「导入 JSON」就该建出 kiro 账号。"""
        from fastapi.testclient import TestClient

        import main as main_mod
        from core.db import account_repository

        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        email = payload["accounts"][0]["pluginSecret"]["accountId"]
        with TestClient(main_mod.app) as client:
            response = client.post(
                "/api/accounts/import",
                json={
                    "platform": "kiro",
                    "lines": [json.dumps(payload, ensure_ascii=False)],
                    "format": "json",
                },
            )
        try:
            self.assertEqual(response.status_code, 200, response.text[:300])
            self.assertEqual(response.json()["created"], 1)
            row = account_repository.find_by_email("kiro", email)
            self.assertIsNotNone(row, "kiro.json 没有落库")
            extra = row.get_extra()
            self.assertTrue(extra["refresh_token"].startswith("aor"))
            self.assertEqual(extra["source"], "plugin")
            self.assertEqual(extra["auth_method"], "social")
            self.assertTrue(extra["profile_arn"].startswith("arn:aws:codewhisperer:"))
        finally:
            row = account_repository.find_by_email("kiro", email)
            if row is not None:
                account_repository.delete(row.id, "kiro")


class KiroPluginTests(unittest.TestCase):
    def test_registered_and_declares_protocol_only(self):
        from core.registry import get, load_all

        load_all()
        cls = get("kiro")
        self.assertEqual(cls.name, "kiro")
        self.assertEqual(cls.supported_executors, ["protocol"])
        self.assertTrue(cls.uses_mailbox)

    def test_declared_actions_are_registered_capabilities(self):
        from core.base_platform import RegisterConfig
        from core.capabilities import capability_of
        from core.registry import get, load_all

        load_all()
        instance = get("kiro")(config=RegisterConfig())
        actions = [item["id"] for item in instance.get_platform_actions()]
        self.assertEqual(sorted(actions), ["probe", "refresh_token"])
        for action_id in actions:
            self.assertIsNotNone(capability_of(action_id))

    def test_register_saves_source_account_and_kiro_credentials(self):
        from core.base_platform import RegisterConfig
        from core.db import account_repository
        from platforms.kiro import plugin as kiro_plugin

        platform = kiro_plugin.KiroPlatform(
            config=RegisterConfig(), mailbox=_StubMailbox()
        )
        aws_identity = {
            "email": "kiro@x.ai",
            "password": "Pw-1!",
            "client_id": "aws-client",
            "client_secret": "aws-secret",
            "access_token": "aws-at",
            "refresh_token": "aws-rt",
            "sso": "aws-sso",
            "region": "us-east-1",
        }
        kiro_tokens = {
            "access_token": "kiro-at",
            "refresh_token": "kiro-rt",
            "client_id": "kiro-client",
            "client_secret": "kiro-secret",
            "expires_in": 3600,
            "profile_arn": "arn:aws:codewhisperer:us-east-1:699475941385:profile/ABC",
        }
        with (
            mock.patch.object(
                kiro_plugin.KiroPlatform, "_register_aws_identity", lambda self, **kw: dict(aws_identity)
            ),
            mock.patch.object(
                kiro_plugin, "authorize_kiro", lambda **kw: dict(kiro_tokens)
            ),
        ):
            account = platform.register(password="Pw-1!")

        self.assertEqual(account.platform, "kiro")
        self.assertEqual(account.email, "kiro@x.ai")
        self.assertEqual(account.password, "Pw-1!", "AWS 的密码要落在 kiro 记录上")
        extra = account.extra
        self.assertEqual(extra["source"], "aws")
        self.assertEqual(extra["source_email"], "kiro@x.ai")
        self.assertEqual(extra["access_token"], "kiro-at")
        self.assertEqual(extra["refresh_token"], "kiro-rt")
        self.assertEqual(extra["client_id"], "kiro-client")
        self.assertEqual(extra["client_secret"], "kiro-secret")
        self.assertEqual(extra["auth_method"], "idc")
        self.assertTrue(extra["profile_arn"].endswith("/ABC"))

        platform.save_account(account)
        row = account_repository.find_by_email("kiro", "kiro@x.ai")
        self.assertIsNotNone(row)
        self.assertEqual(row.password, "Pw-1!")
        self.assertEqual(row.get_extra()["source"], "aws")
        self.assertEqual(row.get_extra()["refresh_token"], "kiro-rt")

    def test_source_aws_account_is_persisted_immediately(self):
        """Kiro 授权失败时 AWS 号也已经真实存在 —— 来源账号必须先落库。

        不落库的后果：Kiro 授权这一环失败（上游波动），AWS 号 + 邮箱白发一次，
        用户在 `aws` 平台也看不到它，只能重新注册 —— 而那个邮箱已经被消耗。
        """
        from core.base_platform import Account as PlatformAccount
        from core.base_platform import RegisterConfig
        from core.db import account_repository
        from platforms.aws import plugin as aws_plugin
        from platforms.kiro import plugin as kiro_plugin

        platform = kiro_plugin.KiroPlatform(config=RegisterConfig(), mailbox=_StubMailbox())
        created = PlatformAccount(
            platform="aws",
            email="kiro-source@x.ai",
            password="Pw-9!",
            token="aws-rt",
            extra={
                "client_id": "aws-client",
                "client_secret": "aws-secret",
                "access_token": "aws-at",
                "refresh_token": "aws-rt",
                "sso": "aws-sso",
                "region": "us-east-1",
            },
        )
        with mock.patch.object(aws_plugin.AwsPlatform, "register", lambda self, **kw: created):
            identity = platform._register_aws_identity(
                mailbox=_StubMailbox(),
                email="",
                password="Pw-9!",
                extra={},
            )
        try:
            self.assertEqual(identity["sso"], "aws-sso")
            row = account_repository.find_by_email("aws", "kiro-source@x.ai")
            self.assertIsNotNone(row, "来源 AWS 账号没有落库")
            self.assertEqual(row.password, "Pw-9!")
            self.assertEqual(row.get_extra()["refresh_token"], "aws-rt")
        finally:
            row = account_repository.find_by_email("aws", "kiro-source@x.ai")
            if row is not None:
                account_repository.delete(row.id, "aws")

    def test_check_valid_refreshes_kiro_token(self):
        from core.base_platform import RegisterConfig
        from core.models.account import AccountRecord
        from platforms.kiro import plugin as kiro_plugin

        platform = kiro_plugin.KiroPlatform(config=RegisterConfig())
        account = AccountRecord(
            platform="kiro",
            email="k@x.ai",
            extra={"client_id": "c", "client_secret": "s", "refresh_token": "rt"},
        )
        with mock.patch.object(
            kiro_plugin,
            "probe_kiro",
            lambda **kwargs: {"code": 200, "usable": True, "summary": ""},
        ):
            self.assertTrue(platform.check_valid(account))
        with mock.patch.object(
            kiro_plugin,
            "probe_kiro",
            lambda **kwargs: {"code": 403, "usable": False, "banned": True, "summary": "suspended"},
        ):
            self.assertFalse(platform.check_valid(account))

    def test_refresh_action_returns_new_tokens(self):
        from core.base_platform import RegisterConfig
        from core.models.account import AccountRecord
        from platforms.kiro import plugin as kiro_plugin

        platform = kiro_plugin.KiroPlatform(config=RegisterConfig())
        account = AccountRecord(
            platform="kiro",
            email="k@x.ai",
            extra={"client_id": "c", "client_secret": "s", "refresh_token": "rt"},
        )
        with mock.patch.object(
            kiro_plugin,
            "refresh_kiro",
            lambda **kwargs: {
                "ok": True,
                "access_token": "at-2",
                "refresh_token": "rt-2",
                "expires_in": 3600,
            },
        ):
            result = platform.execute_action("refresh_token", account, {})
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["access_token"], "at-2")


if __name__ == "__main__":
    unittest.main()
