"""`platforms/chatgpt/sub2api_upload.py` 纯函数 + `platforms/grok/oauth_device.py` 设备流。

两个模块此前覆盖缺口大（17% / 26%）。这里全部用假会话/假 HTTP 驱动，
不碰网络：

- sub2api_upload：payload 构造依赖 JWT 解析与分组解析 —— 构造错的表现是
  「Sub2API 里账号导入成功但字段缺失/挂错分组」；
- oauth_device：SSO → OAuth Device Flow 状态机。轮询分支（pending/slow_down/
  denied/expired）错一个就是「明明授权了却永远拿不到 token」或「白等一小时」。
"""

from __future__ import annotations

import base64
import json
import unittest
from unittest import mock

import platforms.grok.oauth_device as oauth_device


def _b64url(data: dict) -> str:
    raw = json.dumps(data).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _jwt(payload: dict) -> str:
    """构造形如 header.payload.sig 的 JWT（签名段随意，解析只看 payload）。"""
    return f"{_b64url({'alg': 'RS256'})}.{_b64url(payload)}.sig"


# ────────────────── sub2api_upload ──────────────────


class ParseGroupIdsTests(unittest.TestCase):
    def test_comma_separated_string(self):
        from platforms.chatgpt.sub2api_upload import _parse_group_ids

        self.assertEqual(_parse_group_ids("1, 2,3"), [1, 2, 3])

    def test_list_and_tuple_and_set(self):
        from platforms.chatgpt.sub2api_upload import _parse_group_ids

        self.assertEqual(_parse_group_ids([4, "5"]), [4, 5])
        self.assertEqual(_parse_group_ids((6,)), [6])
        self.assertEqual(_parse_group_ids({7}), [7])

    def test_garbage_entries_skipped(self):
        from platforms.chatgpt.sub2api_upload import _parse_group_ids

        self.assertEqual(_parse_group_ids("1, abc, 3"), [1, 3])

    def test_empty_falls_back_to_default(self):
        from platforms.chatgpt.sub2api_upload import _parse_group_ids

        self.assertEqual(_parse_group_ids(""), [2])
        self.assertEqual(_parse_group_ids(None), [2])
        self.assertEqual(_parse_group_ids([]), [2])

    def test_custom_fallback(self):
        from platforms.chatgpt.sub2api_upload import _parse_group_ids

        self.assertEqual(_parse_group_ids("", fallback=[9]), [9])

    def test_single_scalar(self):
        from platforms.chatgpt.sub2api_upload import _parse_group_ids

        self.assertEqual(_parse_group_ids(8), [8])


class DecodeJwtPayloadTests(unittest.TestCase):
    def test_valid_payload(self):
        from platforms.chatgpt.sub2api_upload import _decode_jwt_payload

        token = _jwt({"sub": "u-1", "exp": 1700000000})
        self.assertEqual(_decode_jwt_payload(token), {"sub": "u-1", "exp": 1700000000})

    def test_unpadded_payload(self):
        """base64url 无填充必须能解（真实 JWT 从不带 =）。"""
        from platforms.chatgpt.sub2api_upload import _decode_jwt_payload

        token = f"a.{_b64url({'a': 1})}.c"
        self.assertEqual(_decode_jwt_payload(token), {"a": 1})

    def test_garbage_returns_empty(self):
        from platforms.chatgpt.sub2api_upload import _decode_jwt_payload

        for token in ("", "not-a-jwt", "only.two", "a.!!!.c", None):
            self.assertEqual(_decode_jwt_payload(token), {}, repr(token))

    def test_non_dict_json_returns_empty(self):
        from platforms.chatgpt.sub2api_upload import _decode_jwt_payload

        token = f"a.{base64.urlsafe_b64encode(b'[1,2]').rstrip(b'=').decode()}.c"
        self.assertEqual(_decode_jwt_payload(token), {})


class ExtractOrgIdTests(unittest.TestCase):
    def test_direct_organization_id_wins(self):
        from platforms.chatgpt.sub2api_upload import _extract_organization_id

        payload = {"https://api.openai.com/auth": {"organization_id": "org-1", "organizations": [{"id": "org-2"}]}}
        self.assertEqual(_extract_organization_id(payload), "org-1")

    def test_first_org_from_list(self):
        from platforms.chatgpt.sub2api_upload import _extract_organization_id

        payload = {"https://api.openai.com/auth": {"organizations": [{"id": "org-a"}, {"id": "org-b"}]}}
        self.assertEqual(_extract_organization_id(payload), "org-a")

    def test_missing_returns_empty(self):
        from platforms.chatgpt.sub2api_upload import _extract_organization_id

        self.assertEqual(_extract_organization_id({}), "")
        self.assertEqual(_extract_organization_id({"https://api.openai.com/auth": {}}), "")


class BuildSub2ApiPayloadTests(unittest.TestCase):
    def _build(self, token_data, account_extra=None):
        from platforms.chatgpt import sub2api_upload

        class _Account:
            email = "fallback@example.com"
            client_id = ""

            def __init__(self):
                self.extra = account_extra or {}

        with mock.patch.object(sub2api_upload, "generate_token_json", return_value=token_data):
            return sub2api_upload._build_sub2api_account_payload(_Account())

    def test_exp_from_jwt_is_used(self):
        access = _jwt({"exp": 1800000000, "https://api.openai.com/auth": {"chatgpt_account_id": "acc-1"}})
        payload = self._build({"access_token": access, "refresh_token": "rt", "id_token": "", "email": "a@b.com"})

        self.assertEqual(payload["credentials"]["expires_at"], 1800000000)
        self.assertEqual(payload["credentials"]["chatgpt_account_id"], "acc-1")
        self.assertEqual(payload["name"], "a@b.com")
        self.assertEqual(payload["platform"], "openai")
        self.assertEqual(payload["type"], "oauth")

    def test_invalid_exp_falls_back_to_now_plus(self):
        """exp 缺失/非法时用 now+863999 兜底（不能给 0 让面板立即判过期）。"""
        import time

        access = _jwt({"no_exp": True})
        before = int(time.time())
        payload = self._build({"access_token": access, "refresh_token": "rt", "id_token": "", "email": "a@b.com"})

        self.assertGreaterEqual(payload["credentials"]["expires_at"], before + 863000)

    def test_account_id_falls_back_to_token_data(self):
        access = _jwt({"exp": 1800000000})
        payload = self._build({
            "access_token": access, "refresh_token": "rt", "id_token": "",
            "email": "a@b.com", "account_id": "acc-from-token",
        })

        self.assertEqual(payload["credentials"]["chatgpt_account_id"], "acc-from-token")

    def test_organization_id_from_id_token(self):
        access = _jwt({"exp": 1800000000})
        id_token = _jwt({"https://api.openai.com/auth": {"organization_id": "org-idt"}})
        payload = self._build({
            "access_token": access, "refresh_token": "rt", "id_token": id_token, "email": "a@b.com",
        })

        self.assertEqual(payload["credentials"]["organization_id"], "org-idt")

    def test_group_ids_default(self):
        access = _jwt({"exp": 1800000000})
        payload = self._build({"access_token": access, "refresh_token": "", "id_token": "", "email": "a@b.com"})
        self.assertEqual(payload["group_ids"], [2])


class GetConfigValueTests(unittest.TestCase):
    def test_reads_from_config_store(self):
        from platforms.chatgpt.sub2api_upload import _get_config_value

        with mock.patch("core.config_store.config_store.get", return_value="  v  "):
            self.assertEqual(_get_config_value("sub2api_api_url"), "v")

    def test_exception_returns_empty(self):
        from platforms.chatgpt.sub2api_upload import _get_config_value

        with mock.patch("core.config_store.config_store.get", side_effect=RuntimeError("boom")):
            self.assertEqual(_get_config_value("x"), "")


# ────────────────── oauth_device ──────────────────


class DeviceLocationErrorTests(unittest.TestCase):
    def test_extracts_error_param(self):
        self.assertEqual(
            oauth_device._device_location_error("https://x.ai/done?error=access_denied&state=1"),
            "access_denied",
        )

    def test_no_error_returns_none(self):
        self.assertIsNone(oauth_device._device_location_error("https://x.ai/oauth2/device/done"))
        self.assertIsNone(oauth_device._device_location_error(""))

    def test_garbage_returns_none(self):
        self.assertIsNone(oauth_device._device_location_error("::::"))


class TokenToCpaRecordTests(unittest.TestCase):
    def test_exp_from_jwt(self):
        access = _jwt({"exp": 1800000000, "sub": "s-1"})
        record = oauth_device.token_to_cpa_record({"access_token": access}, email="a@b.com")

        self.assertEqual(record["email"], "a@b.com")
        self.assertEqual(record["sub"], "s-1")
        self.assertEqual(record["expired"], "2027-01-15T08:00:00Z")
        self.assertEqual(record["type"], "xai")

    def test_exp_from_expires_in_when_no_jwt_exp(self):
        import time

        access = _jwt({"sub": "s-2"})
        before = int(time.time())
        record = oauth_device.token_to_cpa_record(
            {"access_token": access, "expires_in": 3600}, email="a@b.com"
        )

        # 2026+3600s 的 ISO 串 —— 只验证在合理窗口内（时区串以 Z 结尾）
        self.assertTrue(record["expired"].endswith("Z"), record["expired"])
        self.assertGreater(len(record["expired"]), 10)
        # 大致接近 now+3600（允许格式化误差）
        import datetime as _dt
        parsed = _dt.datetime.strptime(record["expired"], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=_dt.timezone.utc
        )
        self.assertGreaterEqual(parsed.timestamp(), before + 3500)

    def test_email_falls_back_to_jwt_payload(self):
        access = _jwt({"exp": 1800000000, "email": "jwt@example.com"})
        record = oauth_device.token_to_cpa_record({"access_token": access})
        self.assertEqual(record["email"], "jwt@example.com")

    def test_sso_included_only_when_enabled(self):
        access = _jwt({"exp": 1800000000})
        with mock.patch("platforms.grok.constants.cpa_include_sso", return_value=True):
            with_sso = oauth_device.token_to_cpa_record(
                {"access_token": access}, email="a@b.com", sso="sso-1"
            )
        self.assertEqual(with_sso.get("sso"), "sso-1")

        with mock.patch("platforms.grok.constants.cpa_include_sso", return_value=False):
            without = oauth_device.token_to_cpa_record(
                {"access_token": access}, email="a@b.com", sso="sso-1"
            )
        self.assertNotIn("sso", without)


class CpaAuthFilenameTests(unittest.TestCase):
    def test_email_based(self):
        self.assertEqual(oauth_device.cpa_auth_filename({"email": "a@b.com"}), "xai-a@b.com.json")

    def test_already_prefixed_not_doubled(self):
        self.assertEqual(oauth_device.cpa_auth_filename({"email": "xai-a@b.com"}), "xai-a@b.com.json")

    def test_sub_fallback_and_sanitize(self):
        name = oauth_device.cpa_auth_filename({"sub": "weird/name:1"})
        self.assertTrue(name.endswith(".json"))
        self.assertNotIn("/", name)

    def test_unknown_fallback(self):
        self.assertEqual(oauth_device.cpa_auth_filename({}), "xai-unknown.json")


class TokenToAuthEntryTests(unittest.TestCase):
    def test_entry_shape(self):
        access = _jwt({"exp": 1800000000, "sub": "u-1", "iat": 1700000000})
        key, entry = oauth_device.token_to_auth_entry({"access_token": access}, email="a@b.com")

        self.assertIn("::", key)
        self.assertEqual(entry["key"], access)
        self.assertEqual(entry["auth_mode"], "oidc")
        self.assertEqual(entry["user_id"], "u-1")
        self.assertEqual(entry["email"], "a@b.com")
        self.assertTrue(entry["expires_at"].endswith("Z"))

    def test_expires_at_falls_back_to_expires_in(self):
        access = _jwt({"sub": "u-2"})  # 无 exp
        _key, entry = oauth_device.token_to_auth_entry({"access_token": access, "expires_in": 100})
        self.assertTrue(entry["expires_at"].endswith("Z"))
        self.assertTrue(entry["create_time"].endswith("Z"))


class SsoToTokenFlowTests(unittest.TestCase):
    """设备流状态机：用假会话驱动完整分支（不碰网络）。"""

    def _fake_session_factory(self, responses):
        """responses: list of (status, json_or_text, headers) 按调用顺序消费。"""
        calls = []

        class _FakeResp:
            def __init__(self, status, payload, headers):
                self.status_code = status
                self._payload = payload
                self.headers = headers or {}
                self.text = payload if isinstance(payload, str) else json.dumps(payload)

            def json(self):
                if isinstance(self._payload, str):
                    raise ValueError("not json")
                return self._payload

        class _FakeSession:
            def __init__(self, proxy=""):
                self.set_cookies = []
                self.closed = False

            def set_cookie_for_domains(self, name, value, domains):
                self.set_cookies.append((name, value, domains))

            def post(self, url, **kwargs):
                calls.append((url, kwargs))
                if not responses:
                    raise AssertionError("fake session 的响应队列已空")
                status, payload, headers = responses.pop(0)
                return _FakeResp(status, payload, headers)

            def close(self):
                self.closed = True

        return _FakeSession, calls

    def test_happy_path_returns_token(self):
        device_doc = {
            "device_code": "dc-1", "user_code": "UC-1",
            "verification_uri": "https://accounts.x.ai/oauth2/device",
            "expires_in": 600, "interval": 1,
        }
        token_doc = {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 21600}
        responses = [
            (200, device_doc, {}),                        # device/code
            (302, "", {"Location": "https://accounts.x.ai/oauth2/device/done"}),  # verify（直接 done）
            (200, token_doc, {}),                          # token poll
        ]
        fake_cls, calls = self._fake_session_factory(responses)

        with (
            mock.patch.object(oauth_device, "_NoRedirectSession", fake_cls),
            mock.patch.object(oauth_device.time, "sleep"),
        ):
            token = oauth_device.sso_to_token("sso-abc", executor=None)

        self.assertEqual(token["access_token"], "at-1")
        self.assertEqual(token["token_type"], "Bearer")
        self.assertEqual(len(calls), 3)

    def test_pending_then_success(self):
        device_doc = {"device_code": "dc-2", "user_code": "UC-2", "expires_in": 600, "interval": 1}
        responses = [
            (200, device_doc, {}),
            (302, "", {"Location": "https://accounts.x.ai/oauth2/device/done"}),
            (200, {"error": "authorization_pending"}, {}),
            (200, {"access_token": "at-2"}, {}),
        ]
        fake_cls, _calls = self._fake_session_factory(responses)

        with (
            mock.patch.object(oauth_device, "_NoRedirectSession", fake_cls),
            mock.patch.object(oauth_device.time, "sleep"),
        ):
            token = oauth_device.sso_to_token("sso-abc", executor=None)

        self.assertEqual(token["access_token"], "at-2")

    def test_access_denied_terminates(self):
        device_doc = {"device_code": "dc-3", "user_code": "UC-3", "expires_in": 600, "interval": 1}
        responses = [
            (200, device_doc, {}),
            (302, "", {"Location": "https://accounts.x.ai/oauth2/device/done"}),
            (200, {"error": "access_denied"}, {}),
        ]
        fake_cls, _calls = self._fake_session_factory(responses)
        logs: list[str] = []

        with (
            mock.patch.object(oauth_device, "_NoRedirectSession", fake_cls),
            mock.patch.object(oauth_device.time, "sleep"),
        ):
            token = oauth_device.sso_to_token("sso-abc", executor=None, log=logs.append)

        self.assertIsNone(token)
        # 语义区分：显式「轮询终止」而非落到未知错误的「轮询失败」兜底 ——
        # 两者都返回 None，但日志措辞是排查时区分「被拒」与「未知错误」的唯一线索。
        self.assertIn("轮询终止", "\n".join(logs), "access_denied 应走显式终止分支")
        self.assertIn("access_denied", "\n".join(logs))

    def test_expired_token_terminates(self):
        device_doc = {"device_code": "dc-3b", "user_code": "UC-3b", "expires_in": 600, "interval": 1}
        responses = [
            (200, device_doc, {}),
            (302, "", {"Location": "https://accounts.x.ai/oauth2/device/done"}),
            (200, {"error": "expired_token"}, {}),
        ]
        fake_cls, _calls = self._fake_session_factory(responses)
        logs: list[str] = []

        with (
            mock.patch.object(oauth_device, "_NoRedirectSession", fake_cls),
            mock.patch.object(oauth_device.time, "sleep"),
        ):
            token = oauth_device.sso_to_token("sso-abc", executor=None, log=logs.append)

        self.assertIsNone(token)
        self.assertIn("轮询终止", "\n".join(logs))

    def test_verify_403_terminates(self):
        device_doc = {"device_code": "dc-4", "user_code": "UC-4", "expires_in": 600, "interval": 1}
        responses = [
            (200, device_doc, {}),
            (403, "", {}),
        ]
        fake_cls, _calls = self._fake_session_factory(responses)

        with (
            mock.patch.object(oauth_device, "_NoRedirectSession", fake_cls),
            mock.patch.object(oauth_device.time, "sleep"),
        ):
            token = oauth_device.sso_to_token("sso-abc", executor=None)

        self.assertIsNone(token)

    def test_empty_sso_returns_none_without_request(self):
        fake_cls, calls = self._fake_session_factory([])
        with mock.patch.object(oauth_device, "_NoRedirectSession", fake_cls):
            self.assertIsNone(oauth_device.sso_to_token("", executor=None))
        self.assertEqual(calls, [])

    def test_device_code_5xx_is_retried(self):
        device_doc = {"device_code": "dc-5", "user_code": "UC-5", "expires_in": 600, "interval": 1}
        responses = [
            (520, "", {}),   # CF 瞬时错误
            (200, device_doc, {}),
            (302, "", {"Location": "https://accounts.x.ai/oauth2/device/done"}),
            (200, {"access_token": "at-5"}, {}),
        ]
        fake_cls, calls = self._fake_session_factory(responses)

        with (
            mock.patch.object(oauth_device, "_NoRedirectSession", fake_cls),
            mock.patch.object(oauth_device.time, "sleep"),
        ):
            token = oauth_device.sso_to_token("sso-abc", executor=None)

        self.assertEqual(token["access_token"], "at-5")
        # 第一次 device/code 失败后重试（共 2 次 device/code 调用）
        dc_calls = [c for c in calls if c[0] == oauth_device.DEVICE_CODE_URL]
        self.assertEqual(len(dc_calls), 2)


if __name__ == "__main__":
    unittest.main()
