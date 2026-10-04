"""Apple ID 应用内登录（SRP + 双重认证）。

登录只在内存中保存会话，成功后产出可导入的 iCloud Web Session；
Apple ID 密码只参与本次 SRP 握手，不会落盘。
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

import requests
from requests.cookies import get_cookie_header

from .build_info import BuildInfoCache
from .constants import (
    DELIVERY_PUSH,
    DELIVERY_SMS,
    DELIVERY_SMS_SELECT,
    FALLBACK_CLOUD_BUILD,
    FALLBACK_CLOUD_MASTERING,
    LOGIN_STATUS_COMPLETED,
    LOGIN_STATUS_VERIFICATION_REQUIRED,
    OAUTH_CLIENT_ID,
    USER_AGENT,
    endpoints_for,
    normalize_region,
)
from .cookies import quote_cookie_header
from .errors import ICloudError, invalid_config, invalid_response, upstream_unavailable
from .models import SessionImportRequest, TrustedPhone
from .srp import AppleSRPClient, derive_password
from .transport import WebTransport, nested_value
from .utils import new_uuid, six_digit_code

LOGIN_TTL_SECONDS = 600
MAX_CONCURRENT_SESSIONS = 32
MAX_CODE_FAILURES = 6
MAX_DELIVERIES = 5
DELIVERY_COOLDOWN_SECONDS = 30
ATTEMPT_WINDOW_SECONDS = 600
MAX_ATTEMPTS_PER_EMAIL = 10
GLOBAL_ATTEMPT_WINDOW_SECONDS = 60
MAX_GLOBAL_ATTEMPTS = 64


@dataclass
class LoginRequest:
    email: str
    password: str
    display_name: str = ""
    region: str = ""
    imap_host: str = ""
    imap_port: int = 0
    imap_username: str = ""
    imap_password: str = ""


@dataclass
class LoginState:
    login_id: str
    status: str
    expires_at: float
    delivery: str = ""
    trusted_phone_numbers: list[TrustedPhone] = field(default_factory=list)
    email: str = ""
    display_name: str = ""
    region: str = ""
    session: Optional[SessionImportRequest] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "login_id": self.login_id,
            "status": self.status,
            "expires_at": self.expires_at,
            "delivery": self.delivery,
            "trusted_phone_numbers": [phone.to_dict() for phone in self.trusted_phone_numbers],
            "email": self.email,
            "display_name": self.display_name,
            "region": self.region,
        }


class AppleLoginSession:
    """单次 Apple ID 登录握手的可变状态。"""

    def __init__(self, request: LoginRequest, builds: BuildInfoCache, proxy: str | None = None) -> None:
        self.lock = threading.Lock()
        self.id = new_uuid()
        self.email = request.email.strip().lower()
        self.display_name = request.display_name.strip()
        self.region = normalize_region(request.region)
        self.imap_host = request.imap_host.strip()
        self.imap_port = int(request.imap_port or 0)
        self.imap_username = request.imap_username.strip()
        self.imap_password = request.imap_password
        self.client_id = new_uuid()
        self.expires_at = time.time() + LOGIN_TTL_SECONDS
        self.status = LOGIN_STATUS_VERIFICATION_REQUIRED
        self.delivery = ""
        self.phones: list[TrustedPhone] = []

        self._endpoints = endpoints_for(self.region)
        self._frame_tag = "auth-" + new_uuid().lower()
        self._transport = WebTransport(proxy=proxy)
        self._builds = builds
        self._cloud_build = FALLBACK_CLOUD_BUILD
        self._cloud_mastering = FALLBACK_CLOUD_MASTERING
        self._scnt = ""
        self._session_id = ""
        self._session_token = ""
        self._account_country = ""
        self._trust_token = ""
        self._auth_attributes = ""
        self._needs_2fa = False
        self._sms_phone_id = 0
        self._sms_mode = ""
        self._cookie_header = ""
        self._validate_cookie_header = ""
        self._code_failures = 0
        self._code_verified = False
        self._trusted = False
        self._push_deliveries = 0
        self._sms_deliveries = 0
        self._last_push_at = 0.0
        self._last_sms_at = 0.0

    # ------------------------------------------------------------------ 状态

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at

    def public_state(self) -> LoginState:
        state = LoginState(
            login_id=self.id,
            status=self.status,
            expires_at=self.expires_at,
            delivery=self.delivery,
            trusted_phone_numbers=list(self.phones),
            email=self.email,
            display_name=self.display_name,
            region=self.region,
        )
        if self.status == LOGIN_STATUS_COMPLETED:
            state.session = SessionImportRequest(
                region=self.region,
                cookie_header=self._cookie_header,
                validate_cookie_header=self._validate_cookie_header,
                client_id=self.client_id,
                imap_host=self.imap_host,
                imap_port=self.imap_port,
                imap_username=self.imap_username,
                imap_password=self.imap_password,
            )
        return state

    def discard_secrets(self) -> None:
        self.imap_password = ""
        self._transport.close()

    # ------------------------------------------------------------- 登录主流程

    def sign_in(self, password: str) -> None:
        builds = self._builds.get(self._transport, self.region)
        self._cloud_build = builds.cloud_build or FALLBACK_CLOUD_BUILD
        self._cloud_mastering = builds.cloud_mastering or FALLBACK_CLOUD_MASTERING

        self._start_auth()
        self._federate()

        client = AppleSRPClient()
        init = self._initialize_srp(base64.b64encode(client.public_bytes).decode())
        salt = _decode_base64(init.get("salt"), "salt")
        server_public = _decode_base64(init.get("b"), "SRP challenge")
        derived = derive_password(
            password, salt, int(init.get("iteration") or 0), str(init.get("protocol") or "")
        )
        proofs = client.process_challenge(self.email.encode("utf-8"), derived, salt, server_public)
        self._complete_srp(
            str(init.get("c") or ""),
            base64.b64encode(proofs.client_proof).decode(),
            base64.b64encode(proofs.server_proof).decode(),
        )

    def prepare_verification(self) -> None:
        """SRP 通过后决定验证码投递方式，或直接完成无双重认证的登录。"""
        if not self._needs_2fa:
            self._authenticate_with_token()
            self.status = LOGIN_STATUS_COMPLETED
            return

        state = self._auth_state()
        self.phones = _trusted_phones(state)
        no_trusted_devices = bool(state.get("noTrustedDevices"))
        if no_trusted_devices and len(self.phones) == 1:
            phone = self.phones[0]
            self._record_sms_delivery()
            self._request_sms_code(phone.id, phone.push_mode)
            self.delivery = DELIVERY_SMS
        elif no_trusted_devices and len(self.phones) > 1:
            self.delivery = DELIVERY_SMS_SELECT
        elif no_trusted_devices:
            raise invalid_config("Apple 账号没有可用的双重认证设备或手机号")
        else:
            self.delivery = DELIVERY_PUSH
            self._record_push_delivery()
            self._request_push_code()

    def verify(self, code: str) -> None:
        if not six_digit_code(code):
            raise ICloudError("invalid_verification_code", "请输入 6 位验证码")
        if self._code_failures >= MAX_CODE_FAILURES:
            raise ICloudError(
                "provider_rate_limited",
                "验证码尝试次数过多，请重新登录",
                retry_after=max(self.expires_at - time.time(), 0),
            )

        if not self._code_verified:
            try:
                if self._sms_phone_id > 0:
                    self._validate_code(
                        f"{self._endpoints.auth}/verify/phone/securitycode",
                        {
                            "securityCode": {"code": code},
                            "phoneNumber": {"id": self._sms_phone_id},
                            "mode": self._sms_mode,
                        },
                    )
                else:
                    self._validate_code(
                        f"{self._endpoints.auth}/verify/trusteddevice/securitycode",
                        {"securityCode": {"code": code}},
                    )
            except ICloudError as exc:
                if exc.code == "invalid_verification_code":
                    self._code_failures += 1
                raise
            self._code_verified = True

        if not self._trusted:
            self._trust_session()
            self._trusted = True
        self._authenticate_with_token()
        self.status = LOGIN_STATUS_COMPLETED

    def resend_code(self) -> None:
        if self.delivery == DELIVERY_SMS_SELECT:
            raise invalid_config("请先选择接收验证码的手机号")
        if self._sms_phone_id > 0:
            self._check_delivery_limit(self._last_sms_at, self._sms_deliveries)
            self._record_sms_delivery()
            self._request_sms_code(self._sms_phone_id, self._sms_mode)
            self.delivery = DELIVERY_SMS
        else:
            self._check_delivery_limit(self._last_push_at, self._push_deliveries)
            self._record_push_delivery()
            self._request_push_code()
            self.delivery = DELIVERY_PUSH

    def send_sms(self, phone_id: int, mode: str = "") -> None:
        self._check_delivery_limit(self._last_sms_at, self._sms_deliveries)
        phone = next((item for item in self.phones if item.id == phone_id), None)
        if phone is None:
            raise invalid_config("请选择有效的受信任手机号")
        self._record_sms_delivery()
        self._request_sms_code(phone_id, mode.strip() or phone.push_mode or "sms")
        self.delivery = DELIVERY_SMS

    # ------------------------------------------------------------ Apple 接口

    def _start_auth(self) -> None:
        query = {
            "frame_id": self._frame_tag,
            "language": "zh_CN",
            "skVersion": "7",
            "iframeId": self._frame_tag,
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": "https://www.icloud.com",
            "response_type": "code",
            "response_mode": "web_message",
            "state": self._frame_tag,
            "authVersion": "latest",
        }
        response = self._call(
            "GET", f"{self._endpoints.auth}/authorize/signin", headers={"Accept": "*/*"}, params=query
        )
        if not response.ok:
            raise _status_error(response, "初始化 Apple 登录失败")

    def _federate(self) -> None:
        response = self._call(
            "POST",
            f"{self._endpoints.auth}/federate?isRememberMeEnabled=true",
            payload={"accountName": self.email, "rememberMe": True},
        )
        if not response.ok:
            raise _status_error(response, "Apple 账号校验失败")

    def _initialize_srp(self, public_value: str) -> Mapping[str, Any]:
        response = self._call(
            "POST",
            f"{self._endpoints.auth}/signin/init",
            payload={
                "a": public_value,
                "accountName": self.email,
                "protocols": ["s2k", "s2k_fo"],
            },
        )
        if not response.ok:
            raise _status_error(response, "Apple 登录握手失败")
        payload = _json_body(response)
        if not all(payload.get(key) for key in ("b", "salt", "c")) or int(payload.get("iteration") or 0) <= 0:
            raise invalid_response("Apple 登录握手返回了无效数据")
        return payload

    def _complete_srp(self, challenge: str, client_proof: str, server_proof: str) -> None:
        response = self._call(
            "POST",
            f"{self._endpoints.auth}/signin/complete?isRememberMeEnabled=true",
            payload={
                "accountName": self.email,
                "c": challenge,
                "m1": client_proof,
                "m2": server_proof,
                "rememberMe": True,
                "trustTokens": [],
            },
        )
        if response.status_code == 200:
            return
        if response.status_code == 409:
            if not self._scnt or not self._session_id:
                raise invalid_response("Apple 双重认证响应缺少会话信息")
            self._needs_2fa = True
            return
        if response.status_code == 412:
            repair = self._call("POST", f"{self._endpoints.auth}/repair/complete", payload={})
            if repair.status_code not in (200, 204):
                raise _status_error(repair, "Apple 账号需要先完成安全设置")
            return
        if response.status_code in (401, 403):
            raise ICloudError("invalid_credentials", "Apple 账号或密码错误")
        raise _status_error(response, "Apple 登录失败")

    def _auth_state(self) -> Mapping[str, Any]:
        response = self._call("GET", self._endpoints.auth)
        if not response.ok:
            raise _status_error(response, "读取 Apple 双重认证状态失败")
        payload = _json_body(response)
        nested = payload.get("phoneNumberVerification")
        if isinstance(nested, Mapping) and not payload.get("authenticationType"):
            payload = nested
        return payload

    def _request_push_code(self) -> None:
        response = self._call("PUT", f"{self._endpoints.auth}/verify/trusteddevice/securitycode")
        if not response.ok:
            raise _status_error(response, "发送 Apple 验证码失败")

    def _request_sms_code(self, phone_id: int, mode: str) -> None:
        response = self._call(
            "PUT",
            f"{self._endpoints.auth}/verify/phone",
            payload={"phoneNumber": {"id": phone_id}, "mode": mode},
        )
        if not response.ok:
            raise _status_error(response, "发送短信验证码失败")
        self._sms_phone_id = phone_id
        self._sms_mode = mode

    def _validate_code(self, endpoint: str, payload: Mapping[str, Any]) -> None:
        response = self._call("POST", endpoint, payload=payload)
        accepted = response.ok or (
            response.status_code == 409 and response.headers.get("X-Apple-Session-Token")
        )
        if accepted:
            return
        if response.status_code in (408, 429) or response.status_code >= 500:
            raise _status_error(response, "校验 Apple 验证码失败")
        raise ICloudError("invalid_verification_code", "验证码错误或已失效")

    def _trust_session(self) -> None:
        response = self._call("GET", f"{self._endpoints.auth}/2sv/trust")
        if not response.ok:
            raise _status_error(response, "信任 Apple 登录会话失败")

    def _authenticate_with_token(self) -> None:
        if not self._session_token:
            raise invalid_response("Apple 登录响应缺少会话令牌")
        url = f"{self._endpoints.setup}/accountLogin"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self._endpoints.home,
            "Referer": self._endpoints.home + "/",
            "User-Agent": USER_AGENT,
        }
        try:
            response = self._transport.request(
                "POST",
                url,
                params={
                    "clientBuildNumber": self._cloud_build,
                    "clientMasteringNumber": self._cloud_mastering,
                    "clientId": self.client_id,
                },
                data=json.dumps(
                    {
                        "accountCountryCode": self._account_country,
                        "dsWebAuthToken": self._session_token,
                        "extended_login": True,
                        "trustToken": self._trust_token,
                    }
                ),
                headers=headers,
            )
        except requests.RequestException as exc:
            raise upstream_unavailable("获取 iCloud Session Cookie 失败", exc) from exc
        self._absorb_headers(response)
        if not response.ok:
            raise _status_error(response, "获取 iCloud Session Cookie 失败")
        if nested_value(_json_body(response), "dsInfo", "termsUpdateNeeded") is True:
            raise ICloudError(
                "upstream_rejected",
                "iCloud 账号需要先接受最新版服务条款：请在浏览器登录 iCloud 网页接受条款后重试",
            )

        self._validate_cookie_header = self._cookie_header_for(f"{self._endpoints.setup}/validate")
        # HME 请求走 icloud.com 根域，需要去掉 www 前缀才能取到共享 Cookie。
        self._cookie_header = self._cookie_header_for(
            self._endpoints.home.replace("://www.", "://") + "/"
        )
        if not self._cookie_header:
            raise invalid_response("Apple 登录成功但未返回可供 iCloud HME 使用的共享 Session Cookie")

    # ------------------------------------------------------------------ 传输

    def _call(
        self,
        method: str,
        url: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, str]] = None,
        params: Optional[Mapping[str, str]] = None,
    ) -> requests.Response:
        merged = self._auth_headers()
        merged.update(headers or {})
        body = json.dumps(payload) if payload is not None else None
        try:
            response = self._transport.request(
                method, url, data=body, headers=merged, params=params
            )
        except requests.RequestException as exc:
            raise upstream_unavailable("连接 Apple 登录服务失败", exc) from exc
        self._absorb_headers(response)
        return response

    def _auth_headers(self) -> dict[str, str]:
        origin = self._endpoints.auth.removesuffix("/appleauth/auth")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Origin": origin,
            "Referer": origin + "/",
            "X-Apple-Widget-Key": OAUTH_CLIENT_ID,
            "X-Apple-OAuth-Client-Id": OAUTH_CLIENT_ID,
            "X-Apple-OAuth-Client-Type": "firstPartyAuth",
            "X-Apple-OAuth-Redirect-URI": "https://www.icloud.com",
            "X-Apple-OAuth-Require-Grant-Code": "true",
            "X-Apple-OAuth-Response-Mode": "web_message",
            "X-Apple-OAuth-Response-Type": "code",
            "X-Apple-OAuth-State": self._frame_tag,
            "X-Apple-Frame-Id": self._frame_tag,
            "X-Requested-With": "XMLHttpRequest",
            "X-Apple-Mandate-Security-Upgrade": "0",
            "X-Apple-I-Require-UE": "true",
            "X-Apple-I-FD-Client-Info": json.dumps(
                {"U": USER_AGENT, "L": "zh-CN", "Z": "GMT+08:00", "V": "1.1", "F": ""},
                separators=(",", ":"),
            ),
        }
        if self._auth_attributes:
            headers["X-Apple-Auth-Attributes"] = self._auth_attributes
        if self._scnt:
            headers["scnt"] = self._scnt
        if self._session_id:
            headers["X-Apple-ID-Session-Id"] = self._session_id
        return headers

    def _absorb_headers(self, response: requests.Response) -> None:
        for header, attribute in (
            ("scnt", "_scnt"),
            ("X-Apple-ID-Session-Id", "_session_id"),
            ("X-Apple-Session-Token", "_session_token"),
            ("X-Apple-ID-Account-Country", "_account_country"),
            ("X-Apple-TwoSV-Trust-Token", "_trust_token"),
            ("X-Apple-Auth-Attributes", "_auth_attributes"),
        ):
            value = response.headers.get(header)
            if value:
                setattr(self, attribute, value)

    def _cookie_header_for(self, url: str) -> str:
        prepared = requests.Request("GET", url).prepare()
        header = get_cookie_header(self._transport.session.cookies, prepared) or ""
        return quote_cookie_header(header)[0]

    # -------------------------------------------------------------- 投递限流

    def _check_delivery_limit(self, last_delivery: float, deliveries: int) -> None:
        if deliveries >= MAX_DELIVERIES:
            raise ICloudError(
                "provider_rate_limited",
                "验证码发送次数过多，请稍后重试",
                retry_after=max(self.expires_at - time.time(), 0),
            )
        remaining = DELIVERY_COOLDOWN_SECONDS - (time.time() - last_delivery)
        if last_delivery > 0 and remaining > 0:
            raise ICloudError(
                "provider_rate_limited", "验证码发送过于频繁，请稍后重试", retry_after=remaining
            )

    def _record_push_delivery(self) -> None:
        self._push_deliveries += 1
        self._last_push_at = time.time()

    def _record_sms_delivery(self) -> None:
        self._sms_deliveries += 1
        self._last_sms_at = time.time()


class LoginSessionManager:
    """维护进行中的 Apple ID 登录会话，并施加登录频率限制。"""

    def __init__(self, builds: BuildInfoCache) -> None:
        self._builds = builds
        self._lock = threading.Lock()
        self._sessions: dict[str, AppleLoginSession] = {}
        self._global_attempts: list[float] = []
        self._attempts_by_email: dict[str, list[float]] = {}

    def start(self, request: LoginRequest, *, proxy: str | None = None) -> LoginState:
        if not request.email.strip() or not request.password:
            raise invalid_config("Apple 账号和密码为必填项")

        session = AppleLoginSession(request, self._builds, proxy=proxy)
        self._admit(session)
        try:
            session.sign_in(request.password)
            session.prepare_verification()
        except Exception:
            self.cancel(session.id)
            raise
        return session.public_state()

    def get(self, login_id: str) -> AppleLoginSession:
        with self._lock:
            self._purge_expired()
            session = self._sessions.get(str(login_id or "").strip())
        if session is None:
            raise ICloudError("login_session_expired", "登录会话已过期，请重新登录")
        return session

    def state(self, login_id: str) -> LoginState:
        session = self.get(login_id)
        with session.lock:
            return session.public_state()

    def verify(self, login_id: str, code: str) -> LoginState:
        session = self.get(login_id)
        with session.lock:
            if session.status != LOGIN_STATUS_COMPLETED:
                session.verify(code)
            return session.public_state()

    def resend(self, login_id: str) -> LoginState:
        session = self.get(login_id)
        with session.lock:
            if session.status != LOGIN_STATUS_COMPLETED:
                session.resend_code()
            return session.public_state()

    def send_sms(self, login_id: str, phone_id: int, mode: str = "") -> LoginState:
        session = self.get(login_id)
        with session.lock:
            if session.status != LOGIN_STATUS_COMPLETED:
                session.send_sms(phone_id, mode)
            return session.public_state()

    def cancel(self, login_id: str) -> None:
        with self._lock:
            session = self._sessions.pop(str(login_id or "").strip(), None)
        if session is not None:
            session.discard_secrets()

    def _admit(self, session: AppleLoginSession) -> None:
        with self._lock:
            self._purge_expired()
            if len(self._sessions) >= MAX_CONCURRENT_SESSIONS:
                raise ICloudError(
                    "provider_rate_limited", "正在进行的登录会话过多，请稍后重试", retry_after=60
                )
            now = time.time()
            self._global_attempts = _recent(self._global_attempts, now - GLOBAL_ATTEMPT_WINDOW_SECONDS)
            if len(self._global_attempts) >= MAX_GLOBAL_ATTEMPTS:
                raise ICloudError(
                    "provider_rate_limited",
                    "登录尝试过于频繁，请稍后重试",
                    retry_after=self._global_attempts[0] + GLOBAL_ATTEMPT_WINDOW_SECONDS - now,
                )
            attempts = _recent(
                self._attempts_by_email.get(session.email, []), now - ATTEMPT_WINDOW_SECONDS
            )
            if len(attempts) >= MAX_ATTEMPTS_PER_EMAIL:
                raise ICloudError(
                    "provider_rate_limited",
                    "该 Apple 账号的登录尝试过于频繁，请稍后重试",
                    retry_after=attempts[0] + ATTEMPT_WINDOW_SECONDS - now,
                )
            self._global_attempts.append(now)
            self._attempts_by_email[session.email] = [*attempts, now]
            self._sessions[session.id] = session

    def _purge_expired(self) -> None:
        for login_id, session in list(self._sessions.items()):
            if session.expired:
                del self._sessions[login_id]
                session.discard_secrets()


def _recent(values: list[float], cutoff: float) -> list[float]:
    return [value for value in values if value > cutoff]


def _trusted_phones(state: Mapping[str, Any]) -> list[TrustedPhone]:
    raw = state.get("trustedPhoneNumbers")
    if not isinstance(raw, list) or not raw:
        single = state.get("trustedPhoneNumber")
        raw = [single] if isinstance(single, Mapping) else []
    phones = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        phones.append(
            TrustedPhone(
                id=int(item.get("id") or 0),
                number=str(item.get("obfuscatedNumber") or item.get("numberWithDialCode") or ""),
                push_mode=str(item.get("pushMode") or "").strip() or "sms",
            )
        )
    return phones


def _decode_base64(value: Any, field_name: str) -> bytes:
    try:
        return base64.b64decode(str(value or ""), validate=True)
    except Exception as exc:
        raise invalid_response(f"Apple 登录响应中的 {field_name} 无效", exc) from exc


def _json_body(response: requests.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _status_error(response: requests.Response, fallback: str) -> ICloudError:
    status = response.status_code
    if status in (401, 403):
        code = "invalid_credentials"
    elif status == 429:
        code = "provider_rate_limited"
    elif status == 408 or status >= 500:
        code = "upstream_unavailable"
    else:
        code = "upstream_rejected"
    payload = _json_body(response)
    detail = str(
        payload.get("errorMessage")
        or payload.get("reason")
        or nested_value(payload, "error", "message")
        or ""
    ).strip()
    message = f"{fallback}（HTTP {status}）"
    if detail:
        message = f"{message}：{detail}"
    return ICloudError(code, message)
