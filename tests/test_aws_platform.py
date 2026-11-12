"""AWS Builder ID 平台（`platforms/aws`）的契约与协议。

三层，自下而上：

1. **加密原语** —— XXTEA 指纹信封、JWE 密码加密（能用私钥解回 claims）、
   `awsccc` / ubid / visitorId / PKCE 的形态。这些是「协议能不能被受理」的
   前提，纯函数，可直接断言字节；
2. **注册协议** —— `ScriptedSession` 按 URL + 负载路由响应，驱动**真实调用链**
   走完 OIDC → device → portal → workflow → signup → OTP → 密码 → SSO → device
   token，只替换网络出口。断言状态机的关键跳转与最终凭据；
3. **插件契约** —— 平台注册表、执行器、动作列表、账号落库形态。

参考实现：`reference/register/kirox/`（Go，原始协议）与
`reference/register/reg-factory/register_kiro.py`（Python 移植）。

运行：`.venv/bin/python -m pytest tests/test_aws_platform.py -q`
"""
from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class _RsaFixture:
    """测试用 RSA 密钥对 + 对应 JWK（AWS 在 encryptionContextResponse 里下发同款）。"""

    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = self.private_key.public_key().public_numbers()
        self.jwk = {
            "kty": "RSA",
            "kid": "test-kid",
            "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }

    def decrypt_jwe(self, token: str) -> dict:
        """把 JWE compact 串解回 claims —— 只有真的按 AWS 的算法加密才解得开。"""
        header_b64, enc_key_b64, iv_b64, ct_b64, tag_b64 = token.split(".")
        header = json.loads(_b64url_decode(header_b64))
        assert header["alg"] == "RSA-OAEP-256", header
        assert header["enc"] == "A256GCM", header
        assert header["typ"] == "application/aws+signin+jwe", header
        assert header["kid"] == self.jwk["kid"], header
        cek = self.private_key.decrypt(
            _b64url_decode(enc_key_b64),
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        plaintext = AESGCM(cek).decrypt(
            _b64url_decode(iv_b64),
            _b64url_decode(ct_b64) + _b64url_decode(tag_b64),
            header_b64.encode("ascii"),
        )
        return json.loads(plaintext)


class Response:
    """最小响应替身：线路只碰 status_code / text / json() / headers。"""

    def __init__(self, status_code: int = 200, body: dict | None = None, text: str = "", headers=None):
        self.status_code = status_code
        self._body = body
        self.text = text if body is None else json.dumps(body, ensure_ascii=False)
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            return {}
        return json.loads(json.dumps(self._body))


class _CookieJar:
    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    def set(self, name: str, value: str, domain: str = "", path: str = "", **_) -> None:
        self._items[str(name)] = str(value)

    def get(self, name: str, default=None, **_) -> str:
        return self._items.get(str(name), default)

    def get_dict(self, **_) -> dict[str, str]:
        return dict(self._items)

    def __iter__(self):
        return iter(())


class ScriptedSession:
    """按 (method, url, payload) 路由的假 session；同时记录每次请求。"""

    def __init__(self, router) -> None:
        self._router = router
        self.requests: list[dict] = []
        self.cookies = _CookieJar()

    def get(self, url: str, **kwargs):
        return self._call("GET", url, kwargs)

    def post(self, url: str, **kwargs):
        return self._call("POST", url, kwargs)

    def _call(self, method: str, url: str, kwargs: dict):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self._router(method, url, kwargs)


# ---------------------------------------------------------------------------
# 1. 加密原语
# ---------------------------------------------------------------------------


class XxteaTests(unittest.TestCase):
    def test_round_trip(self):
        from platforms.aws import crypto

        key = (1, 2, 3, 4)
        payload = "0000ABCD#" + json.dumps({"location": "https://x", "n": 1})
        sealed = crypto.xxtea_encrypt(payload, key)
        self.assertEqual(crypto.xxtea_decrypt(sealed, key), payload)

    def test_round_trip_long_text(self):
        """长度不是 4 的倍数时要补零 —— 解回来必须去掉尾部填充。"""
        from platforms.aws import crypto

        key = (1888420705, 2576816180, 2347232058, 874813317)
        payload = "x" * 777
        self.assertEqual(crypto.xxtea_decrypt(crypto.xxtea_encrypt(payload, key), key), payload)


class JwePasswordTests(unittest.TestCase):
    def test_password_is_recoverable_with_the_private_key(self):
        from platforms.aws.crypto import encrypt_password

        fixture = _RsaFixture()
        token = encrypt_password(
            "Pw-123!",
            fixture.jwk,
            issuer="signin",
            audience="AWSPasswordService",
            region="us-east-1",
        )
        self.assertEqual(len(token.split(".")), 5, "JWE compact 必须是五段")
        claims = fixture.decrypt_jwe(token)
        self.assertEqual(claims["password"], "Pw-123!")
        self.assertEqual(claims["iss"], "us-east-1.signin")
        self.assertEqual(claims["aud"], "us-east-1.AWSPasswordService")
        self.assertGreater(claims["exp"], claims["iat"])
        self.assertEqual(claims["exp"] - claims["iat"], 300)
        self.assertTrue(claims["jti"])


class FingerprintTests(unittest.TestCase):
    def test_envelope_structure_and_checksum(self):
        from platforms.aws.crypto import FingerprintBuilder, xxtea_decrypt

        builder = FingerprintBuilder()
        envelope = builder.fingerprint(
            page_type="signin",
            event_type="PageSubmit",
            location="https://us-east-1.signin.aws/platform/d-x/login?workflowStateHandle=w",
            referrer="https://view.awsapps.com/",
            email="a@x.ai",
        )
        identifier, _, encoded = envelope.partition(":")
        self.assertEqual(identifier, builder.identifier)
        plain = xxtea_decrypt(base64.b64decode(encoded), builder.key)
        checksum, _, payload = plain.partition("#")
        self.assertEqual(len(checksum), 8, "前缀是 8 位大写 CRC32")
        self.assertEqual(checksum, checksum.upper())
        data = json.loads(payload)
        self.assertEqual(data["location"], "https://us-east-1.signin.aws/platform/d-x/login?workflowStateHandle=w")
        self.assertEqual(data["referrer"], "https://view.awsapps.com/")
        self.assertIn("performance", data)
        # canvas 直方图：256 个桶、和固定 36000（服务端按它判「像不像真浏览器」）
        self.assertEqual(len(data["canvas"]["histogramBins"]), 256)
        self.assertEqual(sum(data["canvas"]["histogramBins"]), 36_000)
        self.assertEqual(
            set(data["metrics"]),
            {
                "el", "script", "h", "batt", "perf", "auto", "tz", "fp2", "lsubid",
                "browser", "capabilities", "gpu", "dnt", "math", "tts", "input",
                "canvas", "captchainput", "pow",
            },
        )
        # 带邮箱时要生成 form 字段（AWS 用它做人类输入校验）
        self.assertTrue(data["form"], "提交邮箱时 form 不能为空")

    def test_page_load_has_no_form(self):
        from platforms.aws.crypto import FingerprintBuilder, xxtea_decrypt

        builder = FingerprintBuilder()
        envelope = builder.fingerprint(
            page_type="signin",
            event_type="PageLoad",
            location="https://us-east-1.signin.aws/",
            referrer="https://view.awsapps.com/",
        )
        _, _, encoded = envelope.partition(":")
        _, _, payload = xxtea_decrypt(base64.b64decode(encoded), builder.key).partition("#")
        self.assertEqual(json.loads(payload)["form"], {})


class IdentityValueTests(unittest.TestCase):
    def test_awsccc_is_a_base64_json_cookie(self):
        from platforms.aws.crypto import awsccc

        payload = json.loads(base64.b64decode(awsccc()))
        self.assertEqual(payload["v"], "1")
        for key in ("e", "p", "f", "a", "i"):
            self.assertIn(key, payload)

    def test_ubid_and_visitor_shapes(self):
        from platforms.aws.crypto import ubid, visitor_id

        self.assertRegex(ubid(), r"^186-\d{7}-\d{6}$")
        self.assertRegex(visitor_id(), r"^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_pkce_challenge_is_sha256_of_verifier(self):
        import hashlib

        from platforms.aws.crypto import pkce

        verifier, challenge = pkce()
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        self.assertEqual(challenge, expected)


# ---------------------------------------------------------------------------
# 2. 注册协议状态机
# ---------------------------------------------------------------------------


class _AwsFlowRouter:
    """把 AWS 的 15 步路由成响应；用计数器区分同一 URL 的不同阶段。"""

    def __init__(self, *, submit_status: int = 400, otp: str = "123456") -> None:
        self.fixture = _RsaFixture()
        self.submit_status = submit_status
        self.otp = otp
        self.portal_logins = 0

    def __call__(self, method: str, url: str, kwargs: dict):
        if "assets/js/app.js" in url:
            return Response(text="FWCIM_VERSION = '4.0.0';")
        if url.endswith("/client/register"):
            return Response(body={"clientId": "client-1", "clientSecret": "secret-1"})
        if url.endswith("/device_authorization"):
            return Response(body={"deviceCode": "device-1", "userCode": "USER-CODE"})
        if "vs.aws.amazon.com/token" in url:
            return Response(body={"token": "d2c-token-1"})
        if "/token/whoAmI" in url:
            return Response(body={"userId": "u-1"})
        if "portal.sso.us-east-1.amazonaws.com/login" in url:
            self.portal_logins += 1
            handle = "handle-1" if self.portal_logins == 1 else "sso-handle"
            return Response(
                body={
                    "redirectUrl": f"https://us-east-1.signin.aws/platform/d-9067642ac7/login?workflowStateHandle={handle}",
                    "csrfToken": f"csrf-{self.portal_logins}",
                }
            )
        if url.endswith("/auth/sso-token"):
            return Response(body={"token": "sso-token-1", "redirectUrl": "https://view.awsapps.com/start/#/"})
        if url.startswith("https://profile.aws.amazon.com/?workflowID="):
            return Response(text="<html>profile</html>")
        if url.startswith("https://view.awsapps.com/start/"):
            return Response(text="<html>start</html>")
        if url.endswith("/api/start"):
            return Response(body={"workflowState": "profile-state-1"})
        if url.endswith("/api/send-otp"):
            return Response(body={"ok": True})
        if url.endswith("/api/create-identity"):
            return Response(body={"registrationCode": "reg-code-1", "signInState": "sign-state-1"})
        if "/api/execute" in url:
            payload = kwargs.get("json") or {}
            step = payload.get("stepId")
            action = payload.get("actionId")
            kinds = {str(item.get("input_type")) for item in (payload.get("inputs") or []) if isinstance(item, dict)}
            handle = payload.get("workflowStateHandle")
            if "UserRegistrationRequestInput" in kinds:
                return Response(
                    body={
                        "workflowStateHandle": "handle-2",
                        "workflowResponseData": {
                            "encryptionContextResponse": {
                                "publicKey": self.fixture.jwk,
                                "issuer": "signin",
                                "audience": "AWSPasswordService",
                                "region": "us-east-1",
                            }
                        },
                    }
                )
            if "PasswordRequestInput" in kinds:
                return Response(
                    body={
                        "redirect": {
                            "url": "https://us-east-1.signin.aws/platform/d-9067642ac7/login"
                            "?workflowStateHandle=handle-3&state=state-3&workflowResultHandle=result-3"
                        }
                    }
                )
            if payload.get("workflowResultHandle") or handle == "sso-handle":
                return Response(
                    body={
                        "stepId": "end-of-workflow-success",
                        "redirect": {
                            "url": "https://view.awsapps.com/start/?workflowResultHandle=auth-code-1"
                            "&state=sso-state-1&wdc_csrf_token=wdc-1"
                        },
                    }
                )
            if "/signup/api/execute" in url:
                # signup 目录："" → start，start → profile redirect（带 workflowID）
                if step == "":
                    return Response(body={"workflowStateHandle": "handle-s1", "stepId": "start"})
                return Response(
                    body={
                        "workflowStateHandle": "handle-s2",
                        "stepId": "start",
                        "redirect": {"url": "https://profile.aws.amazon.com/?workflowID=wf-1"},
                    }
                )
            if action == "SUBMIT":
                return Response(status_code=self.submit_status, body={"workflowStateHandle": "handle-1"})
            if action == "SIGNUP":
                return Response(
                    body={
                        "workflowStateHandle": "handle-2",
                        "redirect": {"url": "https://profile.aws.amazon.com/?workflowID=wf-1"},
                    }
                )
            if step == "":
                return Response(body={"workflowStateHandle": handle or "handle-1", "stepId": "start"})
            if step == "start":
                return Response(body={"workflowStateHandle": handle or "handle-1", "stepId": "get-identity-user"})
            return Response(body={"stepId": "end-of-workflow-success"})
        if url.endswith("/accept_user_code"):
            return Response(body={"deviceContext": {"id": "ctx-1"}})
        if url.endswith("/associate_token"):
            return Response(body={"ok": True})
        if url.endswith("/token"):
            return Response(
                body={
                    "accessToken": "at-1",
                    "refreshToken": "rt-1",
                    "expiresIn": 3600,
                    "tokenType": "Bearer",
                }
            )
        raise AssertionError(f"未预设响应: {method} {url}")


class AwsProtocolTests(unittest.TestCase):
    def _run(self, router=None, *, otp="123456"):
        from platforms.aws.client import AwsBuilderIdClient

        router = router or _AwsFlowRouter(otp=otp)
        session = ScriptedSession(router)
        client = AwsBuilderIdClient(session=session, log=lambda _m: None)
        result = client.register(
            email="a@x.ai",
            password="Pw-123!",
            full_name="Test User",
            otp_provider=lambda: otp,
        )
        return result, session, router

    def test_full_flow_returns_builder_id_credentials(self):
        result, _, _ = self._run()
        self.assertEqual(result["email"], "a@x.ai")
        self.assertEqual(result["password"], "Pw-123!")
        self.assertEqual(result["client_id"], "client-1")
        self.assertEqual(result["client_secret"], "secret-1")
        self.assertEqual(result["device_code"], "device-1")
        self.assertEqual(result["user_code"], "USER-CODE")
        self.assertEqual(result["access_token"], "at-1")
        self.assertEqual(result["refresh_token"], "rt-1")
        self.assertEqual(result["expires_in"], 3600)
        self.assertEqual(result["sso"], "sso-token-1")
        self.assertEqual(result["region"], "us-east-1")

    def test_request_sequence_contains_the_six_load_bearing_calls(self):
        _, session, _ = self._run()
        urls = [item["url"] for item in session.requests]
        for needle in (
            "oidc.us-east-1.amazonaws.com/client/register",
            "oidc.us-east-1.amazonaws.com/device_authorization",
            "portal.sso.us-east-1.amazonaws.com/login",
            "us-east-1.signin.aws/platform/d-9067642ac7/api/execute",
            "profile.aws.amazon.com/api/create-identity",
            "portal.sso.us-east-1.amazonaws.com/auth/sso-token",
        ):
            self.assertTrue(any(needle in url for url in urls), f"缺少请求: {needle}")

    def test_password_is_sent_as_jwe_not_plaintext(self):
        _, session, router = self._run()
        password_payloads = []
        for item in session.requests:
            payload = item.get("json") or {}
            for node in payload.get("inputs") or []:
                if isinstance(node, dict) and node.get("input_type") == "PasswordRequestInput":
                    password_payloads.append(node)
        self.assertEqual(len(password_payloads), 1, "设置密码的请求必须出现一次")
        node = password_payloads[0]
        self.assertEqual(node["successfullyEncrypted"], "SUCCESSFUL")
        self.assertNotEqual(node["password"], "Pw-123!", "密码不能明文上行")
        claims = router.fixture.decrypt_jwe(node["password"])
        self.assertEqual(claims["password"], "Pw-123!")

    def test_otp_from_provider_lands_in_create_identity(self):
        _, session, _ = self._run(otp="654321")
        create = [
            item for item in session.requests
            if item["url"].endswith("/api/create-identity")
        ]
        self.assertEqual(len(create), 1)
        self.assertEqual(create[0]["json"]["otpCode"], "654321")

    def test_already_registered_email_raises(self):
        from platforms.aws.client import AwsEmailAlreadyRegistered

        router = _AwsFlowRouter(submit_status=200)
        with self.assertRaises(AwsEmailAlreadyRegistered):
            self._run(router)

    def test_otp_provider_timeout_surfaces(self):
        from platforms.aws.client import AwsBuilderIdClient

        session = ScriptedSession(_AwsFlowRouter())

        def _boom():
            raise TimeoutError("等待验证码超时 (180s)")

        client = AwsBuilderIdClient(session=session, log=lambda _m: None)
        with self.assertRaises(TimeoutError):
            client.register(email="a@x.ai", password="Pw-1!", otp_provider=_boom)


class WafChallengeTests(unittest.TestCase):
    """AWS WAF 挑战：识别 → 交给打码 provider 换 token → 带 cookie 重试。"""

    GOKU_HTML = (
        "<html><head>"
        '<script src="https://abc123.captcha-sdk.awswaf.com/abc123/challenge.js"></script>'
        '<script src="https://abc123.captcha-sdk.awswaf.com/abc123/captcha.js"></script>'
        "</head><body>"
        '<script>window.gokuProps = {"key":"waf-key","iv":"waf-iv","context":"waf-ctx"};</script>'
        "</body></html>"
    )

    def _router(self):
        state = {"challenged": False}
        # 同一个 router 实例贯穿整条链（portal 登录要计数，实例重建会丢状态）
        base = _AwsFlowRouter()

        def router(method, url, kwargs):
            if not state["challenged"] and "api/execute" in url:
                state["challenged"] = True
                return Response(
                    status_code=405,
                    text=self.GOKU_HTML,
                    headers={"x-amzn-waf-action": "captcha"},
                )
            return base(method, url, kwargs)

        return router, state

    def test_challenge_is_solved_and_the_request_retried(self):
        from platforms.aws.client import AwsBuilderIdClient

        class _StubSolver:
            name = "stub"

            def __init__(self):
                self.calls = []

            def solve_aws_waf(self, **kwargs):
                self.calls.append(kwargs)
                return "waf-token-1"

        router, state = self._router()
        session = ScriptedSession(router)
        solver = _StubSolver()
        client = AwsBuilderIdClient(session=session, log=lambda _m: None, captcha=solver)
        result = client.register(
            email="a@x.ai", password="Pw-1!", otp_provider=lambda: "123456"
        )
        self.assertEqual(result["refresh_token"], "rt-1")
        self.assertTrue(state["challenged"], "测试没有真的触发 WAF 挑战")
        self.assertEqual(len(solver.calls), 1)
        call = solver.calls[0]
        self.assertEqual(call["website_key"], "waf-key")
        self.assertEqual(call["context"], "waf-ctx")
        self.assertEqual(call["iv"], "waf-iv")
        self.assertTrue(call["challenge_script"].endswith("/challenge.js"))
        self.assertTrue(call["captcha_script"].endswith("/captcha.js"))
        self.assertTrue(call["user_agent"])
        self.assertEqual(session.cookies.get("aws-waf-token"), "waf-token-1")

    def test_missing_solver_raises_a_clear_error(self):
        from platforms.aws.client import AwsBuilderIdClient, AwsCaptchaRequired

        router = lambda method, url, kwargs: Response(
            status_code=405,
            text=self.GOKU_HTML,
            headers={"x-amzn-waf-action": "captcha"},
        )
        client = AwsBuilderIdClient(session=ScriptedSession(router), log=lambda _m: None)
        with self.assertRaises(AwsCaptchaRequired):
            client.oidc_client_register()


# ---------------------------------------------------------------------------
# 3. 插件契约
# ---------------------------------------------------------------------------


class _StubClient:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.calls: list[dict] = []

    def register(self, **kwargs):
        self.calls.append(kwargs)
        kwargs["otp_provider"]()
        # 真实现会把调用方传入的邮箱/密码原样回显 —— 替身也照做，
        # 否则「平台用了哪个密码」这件事在测试里是假的。
        result = dict(self.result)
        result["email"] = kwargs.get("email") or result.get("email")
        result["password"] = kwargs.get("password") or result.get("password")
        return result


class _StubMailbox:
    _platform_name = ""

    def __init__(self, email="aws@x.ai", code="123456") -> None:
        self.email = email
        self.code = code
        self.waited = 0

    def get_email(self):
        from core.mailboxes.base import MailboxAccount

        return MailboxAccount(email=self.email, account_id="token-1")

    def wait_for_code(self, account, **_kwargs):
        self.waited += 1
        return self.code


class AwsCapabilityWiringTests(unittest.TestCase):
    """新增平台的**接线**：能力登记表、状态落库、维护任务。

    这三处漏一处的症状分别是：能力分派走进 `else`（探测不落状态）、
    `apply_action_result` 不认平台（动作结果写不回库）、自动维护不跑。
    """

    def test_aws_actions_are_wired_everywhere(self):
        from core.capabilities import ACTION_CAPABILITIES
        from services.action_execution import _STATUS_APPLIERS
        from services.local_maintenance import PLATFORM_ACTIONS

        for action_id in ("probe", "refresh_token"):
            self.assertIn(action_id, ACTION_CAPABILITIES)
        self.assertIn("aws", _STATUS_APPLIERS, "动作结果没有平台策略 → 状态写不回库")
        self.assertIn("aws", PLATFORM_ACTIONS, "自动维护枚举不到该平台")

    def test_kiro_actions_are_wired_everywhere(self):
        from core.capabilities import ACTION_CAPABILITIES
        from services.action_execution import _STATUS_APPLIERS
        from services.local_maintenance import PLATFORM_ACTIONS

        for action_id in ("probe", "refresh_token"):
            self.assertIn(action_id, ACTION_CAPABILITIES)
        self.assertIn("kiro", _STATUS_APPLIERS)
        self.assertIn("kiro", PLATFORM_ACTIONS)

    def test_token_column_mapping_registered(self):
        """token 列镜像规则：aws/kiro 都镜像 RT（长期凭据）。"""
        from core.credential_fields import token_column_field

        self.assertEqual(token_column_field("aws"), "refresh_token")
        self.assertEqual(token_column_field("kiro"), "refresh_token")


class AwsPluginContractTests(unittest.TestCase):
    def test_registered_and_declares_protocol_only(self):
        from core.registry import get, load_all

        load_all()
        cls = get("aws")
        self.assertEqual(cls.name, "aws")
        self.assertTrue(cls.display_name)
        self.assertEqual(cls.supported_executors, ["protocol"])
        self.assertTrue(cls.executor_labels.get("protocol"))
        self.assertTrue(cls.uses_mailbox)

    def test_declared_actions_are_registered_capabilities(self):
        from core.base_platform import RegisterConfig
        from core.capabilities import capability_of
        from core.registry import get, load_all

        load_all()
        instance = get("aws")(config=RegisterConfig())
        actions = [item["id"] for item in instance.get_platform_actions()]
        self.assertEqual(sorted(actions), ["probe", "refresh_token"])
        for action_id in actions:
            self.assertIsNotNone(capability_of(action_id), f"{action_id} 没登记能力")

    def test_register_builds_a_persisted_aws_account(self):
        from core.base_platform import RegisterConfig
        from core.db import account_repository
        from platforms.aws import plugin as aws_plugin

        client = _StubClient(
            {
                "email": "aws@x.ai",
                "password": "Pw-1!",
                "client_id": "cid",
                "client_secret": "csec",
                "access_token": "at-1",
                "refresh_token": "rt-1",
                "expires_in": 3600,
                "sso": "sso-1",
                "region": "us-east-1",
                "user_code": "USER",
                "device_code": "dev",
            }
        )
        mailbox = _StubMailbox()
        platform = aws_plugin.AwsPlatform(config=RegisterConfig(), mailbox=mailbox)
        with mock.patch.object(
            aws_plugin.AwsPlatform, "_make_client", lambda self, proxy=None: client
        ):
            account = platform.register(password="Pw-1!")

        self.assertEqual(account.platform, "aws")
        self.assertEqual(account.email, "aws@x.ai")
        self.assertEqual(account.password, "Pw-1!")
        # token 列镜像**长期凭据**（RT）：AT 只有 1 小时，镜像它等于镜像一个
        # 明天就过期的值；RT 刷新一次就能换回 AT。
        self.assertEqual(account.token, "rt-1")
        self.assertEqual(account.extra["client_id"], "cid")
        self.assertEqual(account.extra["client_secret"], "csec")
        self.assertEqual(account.extra["refresh_token"], "rt-1")
        self.assertEqual(account.extra["sso"], "sso-1")
        self.assertEqual(account.extra["region"], "us-east-1")
        self.assertEqual(mailbox.waited, 1, "必须真的去邮箱取码")

        platform.save_account(account)
        row = account_repository.find_by_email("aws", "aws@x.ai")
        self.assertIsNotNone(row)
        self.assertEqual(row.password, "Pw-1!")
        self.assertEqual(row.get_extra()["refresh_token"], "rt-1")

    def test_make_client_wires_the_captcha_chain(self):
        from core.base_platform import RegisterConfig
        from platforms.aws import plugin as aws_plugin

        sentinel = object()
        platform = aws_plugin.AwsPlatform(config=RegisterConfig())
        with mock.patch.object(
            aws_plugin, "build_captcha_chain", lambda: sentinel
        ):
            client = platform._make_client(proxy=None)
        self.assertIs(client.captcha, sentinel, "打码链没接到 client 上 —— WAF 一定会失败")

    def test_check_valid_uses_refresh_endpoint(self):
        from core.base_platform import RegisterConfig
        from core.models.account import AccountRecord
        from platforms.aws import plugin as aws_plugin

        platform = aws_plugin.AwsPlatform(config=RegisterConfig())
        account = AccountRecord(
            platform="aws",
            email="aws@x.ai",
            extra={"client_id": "cid", "client_secret": "csec", "refresh_token": "rt-1"},
        )
        with mock.patch.object(
            aws_plugin,
            "probe_builder_id",
            lambda **kwargs: {"code": 200, "usable": True, "summary": ""},
        ):
            self.assertTrue(platform.check_valid(account))
        with mock.patch.object(
            aws_plugin,
            "probe_builder_id",
            lambda **kwargs: {"code": 400, "usable": False, "summary": "invalid_grant"},
        ):
            self.assertFalse(platform.check_valid(account))

    def test_refresh_action_returns_refreshed_tokens(self):
        from core.base_platform import RegisterConfig
        from core.models.account import AccountRecord
        from platforms.aws import plugin as aws_plugin

        platform = aws_plugin.AwsPlatform(config=RegisterConfig())
        account = AccountRecord(
            platform="aws",
            email="aws@x.ai",
            extra={"client_id": "cid", "client_secret": "csec", "refresh_token": "rt-1"},
        )
        with mock.patch.object(
            aws_plugin,
            "refresh_builder_id",
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
        self.assertEqual(result["data"]["refresh_token"], "rt-2")


if __name__ == "__main__":
    unittest.main()
