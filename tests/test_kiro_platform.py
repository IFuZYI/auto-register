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
        # 导出写 `plugin`：这是账号管理器 `kiro.json` 的取值（样例文件就是
        # plugin），写 `aws` 管理器不认；我们的来源 `aws` 留在库里，导入时映射回来。
        self.assertEqual(entry["source"], "plugin")
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


class KiroSourceSelectionTests(unittest.TestCase):
    """Kiro 注册的 AWS 来源：优先复用未注册 Kiro 的号，不足时按开关自动新建。

    选择逻辑用**内存假数据**测（不碰真库）：测试机上的库里有真实注册的号，
    依赖库状态会让「有没有未用 AWS」随环境漂移。
    """

    @staticmethod
    def _row(email: str, *, password: str = "Pw-1!", status: str = "registered", created=None):
        from datetime import datetime
        from types import SimpleNamespace

        return SimpleNamespace(
            email=email,
            password=password,
            status=status,
            created_at=created or datetime(2026, 1, 1),
            get_extra=lambda: {},
        )

    def _platform(self, **extra):
        from core.base_platform import RegisterConfig
        from platforms.kiro import plugin as kiro_plugin

        return kiro_plugin.KiroPlatform(
            config=RegisterConfig(extra=extra),
            mailbox=_StubMailbox(),
        )

    def test_pick_prefers_an_aws_not_yet_used_by_kiro(self):
        used_aws = self._row("used-aws@x.ai")
        free_aws = self._row("free-aws@x.ai")
        kiro_row = self._row("used-kiro@x.ai")
        kiro_row.get_extra = lambda: {"source_email": "used-aws@x.ai"}
        with mock.patch(
            "core.db.account_repository.list_all_accounts",
            side_effect=lambda platform: [used_aws, free_aws] if platform == "aws" else [kiro_row],
        ):
            picked = self._platform(kiro_source="reuse")._pick_unused_aws()
        self.assertEqual(picked.email, "free-aws@x.ai")

    def test_pick_skips_accounts_without_password_and_banned(self):
        no_password = self._row("no-pw@x.ai", password="")
        banned = self._row("banned@x.ai", status="banned")
        ok = self._row("ok@x.ai")
        with mock.patch(
            "core.db.account_repository.list_all_accounts",
            side_effect=lambda platform: [no_password, banned, ok] if platform == "aws" else [],
        ):
            picked = self._platform(kiro_source="reuse")._pick_unused_aws()
        self.assertEqual(picked.email, "ok@x.ai")

    def test_reuse_uses_the_picked_account(self):
        row = self._row("free-aws@x.ai")
        platform = self._platform(kiro_source="reuse")
        seen: list[str] = []

        def _fake_reuse(chosen, **kwargs):
            seen.append(chosen.email)
            return "reused"

        with mock.patch.object(platform, "_pick_unused_aws", lambda: row), mock.patch.object(
            platform, "_reuse_aws_account", _fake_reuse
        ):
            self.assertEqual(platform.register(), "reused")
        self.assertEqual(seen, ["free-aws@x.ai"])

    def test_no_free_aws_falls_back_to_auto_register(self):
        platform = self._platform(kiro_source="reuse", kiro_auto_register="1")
        with mock.patch.object(platform, "_pick_unused_aws", lambda: None), mock.patch.object(
            platform, "_register_new_aws_and_kiro", lambda **kwargs: "auto"
        ):
            self.assertEqual(platform.register(), "auto")

    def test_no_free_aws_without_auto_register_raises(self):
        platform = self._platform(kiro_source="reuse", kiro_auto_register="0")
        with mock.patch.object(platform, "_pick_unused_aws", lambda: None):
            with self.assertRaises(RuntimeError):
                platform.register()

    def test_explicit_aws_email_is_used(self):
        email = "chosen-aws@x.ai"
        platform = self._platform(kiro_aws_email=email)
        row = self._row(email)
        seen: list[str] = []

        def _fake_reuse(chosen, **kwargs):
            seen.append(chosen.email)
            return "chosen"

        with mock.patch(
            "core.db.account_repository.find_by_email", return_value=row
        ), mock.patch.object(platform, "_reuse_aws_account", _fake_reuse):
            self.assertEqual(platform.register(), "chosen")
        self.assertEqual(seen, [email])

    def test_manual_without_aws_email_raises(self):
        platform = self._platform(kiro_source="manual")
        with self.assertRaises(RuntimeError) as ctx:
            platform.register()
        self.assertIn("kiro_aws_email", str(ctx.exception))

    def test_explicit_email_forces_a_new_aws_account(self):
        """点名注册某邮箱时不能被「复用未用号」抢走。"""
        platform = self._platform(kiro_source="reuse")
        seen: list[str] = []

        def _fake_new(**kwargs):
            seen.append(str(kwargs.get("email") or ""))
            return "new"

        with mock.patch.object(platform, "_register_new_aws_and_kiro", _fake_new), mock.patch.object(
            platform, "_pick_unused_aws", lambda: self._row("unused@x.ai")
        ):
            self.assertEqual(platform.register(email="named@x.ai"), "new")
        self.assertEqual(seen, ["named@x.ai"])

    def test_explicit_aws_email_must_exist(self):
        platform = self._platform(kiro_aws_email="missing-aws@x.ai")
        with mock.patch("core.db.account_repository.find_by_email", return_value=None):
            with self.assertRaises(RuntimeError):
                platform.register()


class KiroSourceSnapshotTests(unittest.TestCase):
    def test_snapshot_includes_the_source_email(self):
        from platforms.kiro.plugin import _source_snapshot

        snapshot = _source_snapshot({"email": "a@x.ai", "access_token": "at"})
        self.assertEqual(snapshot["kiro_aws_email"], "a@x.ai")
        self.assertEqual(snapshot["kiro_aws_access_token"], "at")


class KiroParseNameFallbackTests(unittest.TestCase):
    """`name` 兜底 accountId 时不能把 "user@x.com (GitHub)" 当邮箱。"""

    def test_name_with_provider_suffix_is_not_used_as_email(self):
        from platforms.kiro.credentials import parse_entry

        record = parse_entry(
            {
                "name": "user@x.com (GitHub)",
                "pluginSecret": {"method": "social", "loginProvider": "github"},
            }
        )
        self.assertFalse(record.get("account_id"))
        self.assertNotIn("(github)", record.get("email", ""))

    def test_plain_email_name_is_still_used(self):
        from platforms.kiro.credentials import parse_entry

        record = parse_entry(
            {
                "name": "user@x.com",
                "pluginSecret": {"method": "social", "loginProvider": "github"},
            }
        )
        self.assertEqual(record["account_id"], "user@x.com")


class KiroReuseLoginTests(unittest.TestCase):
    """复用登录主链路：SSO（含 OTP）→ Kiro 授权 → 来源快照。"""

    def test_reuse_logs_in_with_otp_and_builds_source_snapshot(self):
        from types import SimpleNamespace
        from unittest import mock

        from core.base_platform import RegisterConfig
        from platforms.kiro.plugin import KiroPlatform

        row = SimpleNamespace(
            email="aws-reuse@x.ai",
            password="Pw-1!",
            get_extra=lambda: {
                "client_id": "aws-cid",
                "client_secret": "aws-cs",
                "refresh_token": "aws-rt",
                "access_token": "aws-at",
                "sso": "aws-sso",
                "region": "us-east-1",
            },
        )
        mailbox = _StubMailbox(email="aws-reuse@x.ai", code="654321")
        platform = KiroPlatform(
            config=RegisterConfig(extra={"mail_provider": "icloud_remote"}),
            mailbox=mailbox,
        )

        class _FakeClient:
            client_id = "cid"
            client_secret = "cs"

            def __init__(self, **_kwargs) -> None:
                self.otp = ""

            def sso_workflow(self, *, email, password, otp_provider):
                self.otp = otp_provider()
                return {"auth_code": "code-1", "state": "st-1"}

            def sso_token(self, code, state):
                return "sso-token-1"

        tokens = {
            "access_token": "kiro-at",
            "refresh_token": "kiro-rt",
            "client_id": "kiro-cid",
            "client_secret": "kiro-cs",
            "expires_in": 3600,
            "profile_arn": "arn:aws:codewhisperer:us-east-1:1:profile/X",
            "region": "us-east-1",
        }
        with (
            mock.patch("platforms.aws.client.AwsBuilderIdClient", _FakeClient),
            mock.patch("platforms.kiro.plugin.authorize_kiro", lambda **_kw: dict(tokens)),
            mock.patch.object(platform, "_mailbox_account_for_reuse", lambda _m, _e: object()),
        ):
            account = platform._reuse_aws_account(
                row,
                extra={"mail_provider": "icloud_remote"},
                proxy="",
                plan="Kiro FREE",
            )

        self.assertEqual(account.email, "aws-reuse@x.ai")
        self.assertEqual(account.password, "Pw-1!")
        self.assertEqual(mailbox.waited, 1, "登录 OTP 必须真的去邮箱取")
        extra = account.extra
        self.assertEqual(extra["source"], "aws")
        self.assertEqual(extra["source_email"], "aws-reuse@x.ai")
        self.assertEqual(extra["plan"], "Kiro FREE")
        self.assertEqual(extra["refresh_token"], "kiro-rt")
        # 来源快照（供审计/回滚），不是明文密码
        self.assertEqual(extra["kiro_aws_sso"], "aws-sso")
        self.assertEqual(extra["kiro_aws_refresh_token"], "aws-rt")
        self.assertEqual(extra["kiro_aws_email"], "aws-reuse@x.ai")
        self.assertNotIn("password", extra)


class KiroAgent2ApiExportTests(unittest.TestCase):
    """导出的凭据是给 Agent2API 导入的（Kiro 提供商的文件导入 / 账号导入）。

    Agent2API 的 Kiro 解析器读的是扁平字段（accessToken / refreshToken /
    expiresAt / region / profileArn / clientId / clientSecret），不是 Kiro
    账号管理器的 pluginSecret 嵌套格式。
    """

    def _account(
        self,
        *,
        auth_method="idc",
        login_provider="BuilderId",
        client_id="kid",
        client_secret="ksecret",
        aws=True,
    ):
        from core.models.account import AccountRecord
        from platforms.kiro.credentials import build_extra

        extra = build_extra(
            access_token="at-1",
            refresh_token="rt-1",
            client_id=client_id,
            client_secret=client_secret,
            profile_arn="arn:aws:codewhisperer:us-east-1:1:profile/X",
            region="us-east-1",
            auth_method=auth_method,
            login_provider=login_provider,
            plan="Kiro FREE",
            account_id="a@x.ai",
            email="a@x.ai",
            expires_at=1794478346773,
        )
        if aws and auth_method == "idc":
            extra.update(
                {
                    "kiro_aws_client_id": "aws-cid",
                    "kiro_aws_client_secret": "aws-csecret",
                    "kiro_aws_refresh_token": "aws-rt",
                    "kiro_aws_access_token": "aws-at",
                    "kiro_aws_region": "us-east-1",
                }
            )
        return AccountRecord(platform="kiro", email="a@x.ai", password="pw", token="rt-1", extra=extra)

    def test_single_account_is_the_flat_auth_token_file(self):
        from services.account_export import render_accounts

        doc = json.loads(render_accounts([self._account()], "kiro_auth_token"))
        # idc 走 AWS SSO：client/RT 必须是 AWS 的，profile 留空、expires 置过去。
        self.assertEqual(doc["accessToken"], "aws-at")
        self.assertEqual(doc["refreshToken"], "aws-rt")
        self.assertEqual(doc["profileArn"], "")
        self.assertEqual(doc["clientId"], "aws-cid")
        self.assertEqual(doc["clientSecret"], "aws-csecret")
        self.assertEqual(doc["region"], "us-east-1")
        self.assertEqual(doc["method"], "idc")
        self.assertEqual(doc["expires"], 1)
        self.assertEqual(doc["expiresAt"], "1970-01-01T00:00:00.001Z")
        self.assertNotIn("pluginSecret", doc)
        # Agent2API 的账号导入要求每条有唯一 id；身份回退读 email。
        self.assertTrue(doc["id"].startswith("kiro-"))
        self.assertEqual(doc["email"], "a@x.ai")
        # 插件的 refresh 分支比较小写 login_provider；uid 会把网关身份压成
        # "builderid"（23 个号互相覆盖），所以不导出 uid。
        self.assertEqual(doc["loginProvider"], "builderid")
        self.assertNotIn("uid", doc)

    def test_multiple_accounts_is_the_agent2api_transfer_document(self):
        from services.account_export import render_accounts

        social = self._account(
            auth_method="social", login_provider="github", client_id="", client_secret=""
        )
        doc = json.loads(render_accounts([self._account(), social], "kiro_auth_token"))
        self.assertEqual(doc["version"], 2)
        self.assertEqual(doc["customProviders"], [])
        self.assertEqual(len(doc["accounts"]), 2)
        entry = doc["accounts"][0]
        self.assertEqual(entry["provider"], "kiro")
        self.assertEqual(entry["accessToken"], "aws-at")
        self.assertEqual(entry["method"], "idc")
        ids = [item["id"] for item in doc["accounts"]]
        self.assertEqual(len(set(ids)), 2, "每条账号的 id 必须唯一")
        self.assertEqual([item["email"] for item in doc["accounts"]], ["a@x.ai", "a@x.ai"])
        self.assertTrue(all("uid" not in item for item in doc["accounts"]))

    def test_social_account_has_no_client_credentials(self):
        from services.account_export import render_accounts

        social = self._account(
            auth_method="social", login_provider="github", client_id="", client_secret=""
        )
        doc = json.loads(render_accounts([social], "kiro_auth_token"))
        self.assertEqual(doc["method"], "social")
        self.assertEqual(doc["clientId"], "")
        self.assertEqual(doc["loginProvider"], "github")
        self.assertNotIn("uid", doc)

    def test_legacy_camel_case_row_exports_its_credentials(self):
        """旧行把 camelCase 原样落进 extra —— 新导出路径不能比旧路径少读字段。"""
        from core.models.account import AccountRecord
        from services.account_export import render_accounts

        legacy = AccountRecord(
            platform="kiro",
            email="legacy@x.ai",
            password="pw",
            token="",
            extra={
                "accessToken": "at-legacy",
                "refreshToken": "rt-legacy",
                "clientId": "cid-legacy",
                "clientSecret": "cs-legacy",
                "profileArn": "arn:legacy",
                "authMethod": "social",
                "loginProvider": "github",
                "expiresAt": 1794478346773,
            },
        )
        doc = json.loads(render_accounts([legacy], "kiro_auth_token"))
        self.assertEqual(doc["accessToken"], "at-legacy")
        self.assertEqual(doc["refreshToken"], "rt-legacy")
        self.assertEqual(doc["clientId"], "cid-legacy")
        self.assertEqual(doc["clientSecret"], "cs-legacy")
        self.assertEqual(doc["profileArn"], "arn:legacy")
        self.assertEqual(doc["method"], "social")
        self.assertEqual(doc["loginProvider"], "github")
        self.assertEqual(doc["expires"], 1794478346773)

    def test_idc_without_aws_snapshot_raises_instead_of_exporting_broken_creds(self):
        from services.account_export import render_accounts

        with self.assertRaisesRegex(ValueError, "缺少 AWS SSO 快照"):
            render_accounts([self._account(aws=False)], "kiro_auth_token")



class KiroFlatParseTests(unittest.TestCase):
    def test_parse_document_accepts_a_single_flat_agent2api_object(self):
        from platforms.kiro.credentials import parse_document

        flat = {
            "provider": "kiro",
            "email": "a@x.ai",
            "accessToken": "at",
            "refreshToken": "rt",
            "clientId": "cid",
            "clientSecret": "cs",
            "profileArn": "arn",
            "method": "social",
            "loginProvider": "github",
            "accountId": "a@x.ai",
            "expires": 1794478346773,
            "region": "us-east-1",
        }
        records = parse_document(flat)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["email"], "a@x.ai")
        self.assertEqual(records[0]["client_id"], "cid")
        self.assertEqual(records[0]["client_secret"], "cs")
        self.assertEqual(records[0]["profile_arn"], "arn")
        self.assertEqual(records[0]["auth_method"], "social")
        self.assertEqual(records[0]["login_provider"], "github")
        self.assertEqual(records[0]["expires_at"], 1794478346773)

    def test_parse_document_reads_the_batch_shape(self):
        from platforms.kiro.credentials import parse_document

        doc = {"version": 2, "accounts": [{"email": "b@x.ai", "accessToken": "at", "clientId": "cid"}]}
        records = parse_document(doc)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["email"], "b@x.ai")
        self.assertEqual(records[0]["client_id"], "cid")


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

    def test_kiro_json_format_is_registered_but_hidden_from_the_menu(self):
        from services.account_export import EXPORT_FORMATS, list_export_formats

        # 注册表里仍在（API-only，无 UI 入口），但不进通用导出的下拉。
        self.assertIn("kiro_json", EXPORT_FORMATS)
        ids = {item["id"] for item in list_export_formats()}
        self.assertNotIn("kiro_json", ids)
        self.assertNotIn("kiro_auth_token", ids)

    def test_kiro_json_format_renders_version_2_document(self):
        from services.account_export import render_accounts

        text = render_accounts([self._account()], "kiro_json")
        payload = json.loads(text)
        self.assertEqual(payload["version"], 2)
        self.assertEqual(len(payload["accounts"]), 1)
        entry = payload["accounts"][0]
        self.assertEqual(entry["provider"], "kiro")
        self.assertEqual(entry["source"], "plugin")
        self.assertEqual(entry["pluginSecret"]["access"], "at-1")
        self.assertEqual(entry["pluginSecret"]["refresh"], "rt-1")

    def test_export_document_shape_matches_the_fixture(self):
        """导出的键集必须与样例文件逐层一致（顶层/entry/public/secret）。"""
        from services.account_export import render_accounts

        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        document = json.loads(render_accounts([self._account()], "kiro_json"))
        self.assertEqual(set(document), set(fixture))
        entry = document["accounts"][0]
        sample = fixture["accounts"][0]
        self.assertEqual(set(entry), set(sample))
        self.assertEqual(set(entry["pluginPublic"]), set(sample["pluginPublic"]))
        self.assertEqual(set(entry["pluginSecret"]), set(sample["pluginSecret"]))
        self.assertEqual(document["version"], fixture["version"])
        self.assertEqual(document["customProviders"], fixture["customProviders"])

    def test_idc_export_round_trips_source_back_to_aws(self):
        """导出写 plugin，导回来必须还是 aws（idc 账号）。"""
        from platforms.kiro.credentials import parse_entry, to_account_entry
        from services.account_export import render_accounts

        entry = json.loads(render_accounts([self._account()], "kiro_json"))["accounts"][0]
        parsed = parse_entry(entry)
        self.assertEqual(parsed["source"], "aws")
        self.assertEqual(parsed["auth_method"], "idc")

    def test_kiro_json_format_skips_other_platforms(self):
        from core.models.account import AccountRecord
        from services.account_export import render_accounts

        other = AccountRecord(platform="chatgpt", email="gpt@x.ai", password="p")
        payload = json.loads(render_accounts([other], "kiro_json"))
        self.assertEqual(payload["accounts"], [])

    def test_legacy_camel_case_row_exports_its_credentials(self):
        """旧行只有 camelCase 拼写时，`kiro.json` 导出不能是空凭据。

        旧版 JSON 导入的 catch-all 会把 `clientId` / `clientSecret` /
        `accessToken` / `refreshToken` / `profileArn` 原样落进 extra（当时
        还没有归一表）。`to_account_entry` 原来只读 snake_case，这类行导出
        出来的 `pluginSecret` 是空串 —— 恢复后既刷新不了也调不了 Kiro 后端。
        """
        from core.models.account import AccountRecord
        from services.account_export import render_accounts

        legacy = AccountRecord(
            platform="kiro",
            email="legacy@x.ai",
            password="pw",
            extra={
                "accessToken": "at-legacy",
                "refreshToken": "rt-legacy",
                "clientId": "cid-legacy",
                "clientSecret": "csec-legacy",
                "profileArn": "arn:aws:codewhisperer:us-east-1:699475941385:profile/LEGACY",
                "region": "us-east-1",
                "auth_method": "idc",
                "login_provider": "BuilderId",
                "plan": "Kiro FREE",
                "account_id": "legacy@x.ai",
                "uid": "builderid",
                "source": "aws",
            },
        )
        secret = json.loads(render_accounts([legacy], "kiro_json"))["accounts"][0]["pluginSecret"]
        self.assertEqual(secret["access"], "at-legacy", "camelCase 的 AT 被漏读，导出为空")
        self.assertEqual(secret["refresh"], "rt-legacy", "camelCase 的 RT 被漏读，导出为空")
        self.assertEqual(secret["clientId"], "cid-legacy")
        self.assertEqual(secret["clientSecret"], "csec-legacy")
        self.assertEqual(
            secret["profileArn"],
            "arn:aws:codewhisperer:us-east-1:699475941385:profile/LEGACY",
            "camelCase 的 profileArn 被漏读，回落成了默认 ARN（指错 profile）",
        )

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
    def tearDown(self):
        from core.db import account_repository

        row = account_repository.find_by_email("kiro", "kiro@x.ai")
        if row is not None:
            account_repository.delete(row.id, "kiro")

    def test_social_probe_is_indeterminate_and_keeps_status(self):
        """social 号（有 RT、无 IdC 客户端）测活应「测不了」，不能打成 invalid。"""
        from core.base_platform import Account, AccountStatus, RegisterConfig
        from platforms.kiro.plugin import KiroPlatform

        platform = KiroPlatform(config=RegisterConfig(), mailbox=None)
        account = Account(
            platform="kiro",
            email="social@x.ai",
            password="",
            extra={"refresh_token": "aor-social"},
        )
        detail = platform.probe_account_detail(account)
        self.assertIsNone(detail.get("usable"))
        self.assertTrue(detail.get("indeterminate"))

        class _Row:
            status = AccountStatus.REGISTERED

        self.assertEqual(platform.apply_probe_status_policy(_Row(), detail=detail), "")
        self.assertEqual(_Row.status, AccountStatus.REGISTERED)

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
            # 库里可能残留真实注册的 AWS 号（测试机不是干净库），钉住 picker
            # 走「没有可用号 → 自动新建」这条路径，测试不随环境漂移。
            mock.patch.object(kiro_plugin.KiroPlatform, "_pick_unused_aws", lambda self: None),
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
