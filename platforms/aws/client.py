"""AWS Builder ID 注册状态机（纯协议，无浏览器）。

一步对一步照着参考实现写，每一步的 HTTP 形状都是实测出来的 —— 这里的注释只
记「为什么是这个形状」，协议细节见参考实现：

    reference/register/kirox/internal/core/   （Go 原版，15 步）
    reference/register/reg-factory/register_kiro.py（Python 移植）

流程（`register()` 内部顺序）：

1.  `oidc_client_register`   拿 clientId / clientSecret（后续所有 token 操作都用它）
2.  `device_authorization`   拿 deviceCode / userCode（SSO 端用 user_code 认设备）
3.  `fetch_app_js`           取 XXTEA 密钥与 TES 版本（取不到就用回退值）
4.  `portal_login`           拿 workflowStateHandle + loginCsrfToken
5.  `workflow_init`          指纹握手（"" → "start"，按服务端 stepId 决定是否第二步）
6.  `submit_email`           200 = 邮箱已注册（换个号），400 = 可注册（继续）
7.  `signup` / `signup_init` 确认注册意图 → profile 的 workflowID
8.  `profile_start`          建 profile 工作流（拿 workflowState）
9.  `send_otp`               发验证码（**这一步之后才有邮件**）
10. `create_identity`        提交 OTP → registrationCode / signInState
11. `set_password`           两步：取加密公钥 → 提交 JWE 密码
12. `complete_signup`        结束工作流 → authCode / state / wdc_csrf_token
13. `sso_workflow` / `sso_token`  ↑ 同一套 worklow 走第二个目录（view）
14. `device_token`           accept_user_code → associate_token → 轮询 /token

**失败语义**：只有「邮箱已注册」是可判定的业务结论（`AwsEmailAlreadyRegistered`），
其它一律抛原始异常 —— 包装成「注册失败」只会让上游的重试策略瞎猜。
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse

from .constants import (
    CLIENT_NAME,
    D2C_URL,
    DIRECTORY_ID,
    HTTP_TIMEOUT,
    OIDC_BASE,
    PORTAL_BASE,
    PROFILE_BASE,
    PROVIDER,
    REGION,
    SCOPES,
    SIGNIN_BASE,
    START_URL,
    VIEW_BASE,
)
from .crypto import FingerprintBuilder, awsccc, encrypt_password, ubid

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]


class AwsRegistrationError(RuntimeError):
    """注册链路里可识别的业务失败（带阶段名，便于日志定位）。"""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.message = message


class AwsEmailAlreadyRegistered(AwsRegistrationError):
    """提交邮箱时 AWS 回 200 —— 该地址已有 Builder ID，不能重复注册。"""

    def __init__(self, email: str = "") -> None:
        super().__init__("submit_email", f"邮箱已注册: {email}")
        self.email = email


class AwsCaptchaRequired(AwsRegistrationError):
    """被 AWS WAF 拦下，且没有可用的打码 provider（或全部失败）。

    单独一个异常类型：任务层可以据此提示「去配 CapMonster」而不是当成普通的
    网络失败去重试 —— 重试一百次也过不了人机验证。
    """

    def __init__(self, message: str = "", *, detail: str = "") -> None:
        super().__init__("captcha", message or "AWS WAF 要求人机验证")
        self.detail = detail


#: WAF 挑战页里 `window.gokuProps` 的形状（AWS 会内联在 HTML 里）。
_GOKU_RE = re.compile(r"window\.gokuProps\s*=\s*(\{.*?\})\s*;", re.S)
_SCRIPT_SRC_RE = re.compile(r"""<script[^>]+src=["']([^"']+)["']""", re.I)


def _waf_challenge_of(response: Any) -> str:
    """判断响应是不是 AWS WAF 挑战：返回 `captcha` / `challenge` / 空串。"""
    headers = getattr(response, "headers", None) or {}
    getter = getattr(headers, "get", None)
    action = ""
    if callable(getter):
        action = str(getter("x-amzn-waf-action") or getter("X-Amzn-Waf-Action") or "").lower()
    if action in {"captcha", "challenge"}:
        return action
    body = str(getattr(response, "text", "") or "")
    status = int(getattr(response, "status_code", 0) or 0)
    if status in {202, 405} and "gokuProps" in body:
        return "captcha" if "captcha" in body.lower() else "challenge"
    return ""


def _waf_params(body: str) -> dict[str, str]:
    """从挑战页里抠 `gokuProps` 与 challenge/captcha 脚本地址。"""
    import json as _json

    params: dict[str, str] = {"website_key": "", "context": "", "iv": ""}
    match = _GOKU_RE.search(body or "")
    if match:
        try:
            props = _json.loads(match.group(1))
        except (ValueError, TypeError):
            props = {}
        if isinstance(props, dict):
            params["website_key"] = str(props.get("key") or "")
            params["context"] = str(props.get("context") or "")
            params["iv"] = str(props.get("iv") or "")
    for src in _SCRIPT_SRC_RE.findall(body or ""):
        lowered = src.lower()
        if "challenge" in lowered and not params.get("challenge_script"):
            params["challenge_script"] = src
        elif "captcha" in lowered and not params.get("captcha_script"):
            params["captcha_script"] = src
    params.setdefault("challenge_script", "")
    params.setdefault("captcha_script", "")
    return params


def _query(url: str, key: str) -> str:
    """从 URL（含 `#` 后的 fragment 查询串）里取一个参数。"""
    try:
        parsed = urlparse(str(url or ""))
        query = parsed.query
        if not query and parsed.fragment and "?" in parsed.fragment:
            query = parsed.fragment.split("?", 1)[1]
        return (parse_qs(query).get(key) or [""])[0]
    except (TypeError, ValueError):
        return ""


def _json_of(response: Any) -> dict:
    """把响应体解析成 dict（任何异常都返回空字典，由调用方判失败）。"""
    try:
        value = response.json()
    except Exception as exc:  # noqa: BLE001 - 非 JSON 响应体是协议失败的一种
        logger.debug("响应体不是 JSON（按空处理）: %s", exc)
        return {}
    return value if isinstance(value, dict) else {}


class AwsBuilderIdClient:
    """一次 Builder ID 注册会话。

    传入 `session` 时直接用它（测试用 `ScriptedSession` 驱动真实调用链）；
    否则按 `proxy` 建一个 `curl_cffi` 会话（浏览器 TLS 指纹）。
    """

    def __init__(
        self,
        *,
        session: Any = None,
        proxy: str = "",
        log: Optional[LogFn] = None,
        timeout: int = HTTP_TIMEOUT,
        captcha: Any = None,
    ) -> None:
        self._log = log or (lambda _m: None)
        self.timeout = timeout
        #: 打码 provider（`core.captcha_providers.CaptchaChain` 或任意带
        #: `solve_aws_waf` 的对象）。为空时遇到 WAF 挑战直接报
        #: `AwsCaptchaRequired`（配置提示比无限重试有用）。
        self.captcha = captcha
        self.proxy = proxy
        self.session = session if session is not None else self._new_session(proxy)
        self.fingerprint = FingerprintBuilder()
        self.visitor_id = str(uuid.uuid4())
        self.ubid = ""
        self.client_id = ""
        self.client_secret = ""
        self.device_code = ""
        self.user_code = ""
        self.workflow_handle = ""
        self.workflow_id = ""
        self.workflow_state = ""
        self.csrf_token = ""

    # ---------------------------------------------------------------- 传输
    @staticmethod
    def _new_session(proxy: str):
        from curl_cffi import requests as curl_requests

        session = curl_requests.Session(impersonate="chrome131", timeout=HTTP_TIMEOUT)
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}
        session.cookies.set("awsccc", awsccc())
        return session

    def _headers(
        self,
        *,
        referer: str = "",
        origin: str = "",
        content_type: str = "application/json",
    ) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Content-Type": content_type,
            "User-Agent": self.fingerprint.user_agent,
            "sec-ch-ua": self.fingerprint.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        if referer:
            headers["Referer"] = referer
        if origin:
            headers["Origin"] = origin
        return headers

    def _solve_waf(self, response: Any, url: str) -> bool:
        """遇到 AWS WAF 挑战就解一次；返回「是否应该重试原请求」。

        `gokuProps` 里的 key/context/iv + challenge.js/captcha.js 是 CapMonster
        `AmazonTask` 的入参；拿到 `aws-waf-token` 塞进 cookie jar 后原请求重发即可。
        """
        action = _waf_challenge_of(response)
        if not action:
            return False
        if self.captcha is None:
            raise AwsCaptchaRequired(
                "AWS WAF 要求人机验证，但没有配置打码服务（CapMonster）",
                detail=f"action={action} url={url}",
            )
        params = _waf_params(str(getattr(response, "text", "") or ""))
        if not params.get("website_key") or not params.get("context") or not params.get("iv"):
            raise AwsCaptchaRequired(
                "AWS WAF 挑战页里没有 gokuProps（页面结构可能变了）",
                detail=str(getattr(response, "text", ""))[:200],
            )
        self._log(f"[AWS] 命中 WAF {action}，调用打码服务…")
        try:
            token = self.captcha.solve_aws_waf(
                website_url=url,
                challenge_script=params.get("challenge_script", ""),
                captcha_script=params.get("captcha_script", ""),
                website_key=params["website_key"],
                context=params["context"],
                iv=params["iv"],
                user_agent=self.fingerprint.user_agent,
                proxy=self.proxy or "",
            )
        except Exception as exc:  # noqa: BLE001 - 统一转成「需要验证码」的业务失败
            raise AwsCaptchaRequired(f"打码失败: {type(exc).__name__}: {exc}") from exc
        jar = getattr(self.session, "cookies", None)
        if jar is not None:
            jar.set("aws-waf-token", token)
        self._log("[AWS] WAF token 已获取，重试原请求")
        return True

    def _post(self, url: str, payload: dict, *, headers: dict, expected: tuple[int, ...] = (200,)) -> Any:
        response = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        if self._solve_waf(response, url):
            response = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
        if expected and response.status_code not in expected:
            raise AwsRegistrationError(
                "post", f"HTTP {response.status_code}: {str(response.text)[:300]}"
            )
        return response

    def _get(self, url: str, *, headers: dict, allow_redirects: bool = True) -> Any:
        response = self.session.get(
            url, headers=headers, allow_redirects=allow_redirects, timeout=self.timeout
        )
        if self._solve_waf(response, url):
            response = self.session.get(
                url, headers=headers, allow_redirects=allow_redirects, timeout=self.timeout
            )
        return response

    # ---------------------------------------------------------------- 1-3
    def oidc_client_register(self) -> None:
        response = self._post(
            f"{OIDC_BASE}/client/register",
            {
                "clientName": CLIENT_NAME,
                "clientType": "public",
                "scopes": list(SCOPES),
            },
            headers={"Content-Type": "application/json"},
        )
        data = _json_of(response)
        self.client_id = str(data.get("clientId") or "")
        self.client_secret = str(data.get("clientSecret") or "")
        if not self.client_id or not self.client_secret:
            raise AwsRegistrationError("oidc_client_register", "未返回 clientId/clientSecret")
        self._log(f"[AWS] OIDC 客户端已注册（clientId={self.client_id[:8]}…）")

    def device_authorization(self) -> None:
        response = self._post(
            f"{OIDC_BASE}/device_authorization",
            {
                "clientId": self.client_id,
                "clientSecret": self.client_secret,
                "startUrl": START_URL,
            },
            headers={"Content-Type": "application/json"},
        )
        data = _json_of(response)
        self.device_code = str(data.get("deviceCode") or "")
        self.user_code = str(data.get("userCode") or "")
        if not self.device_code or not self.user_code:
            raise AwsRegistrationError("device_authorization", "未返回 deviceCode/userCode")
        self._log(f"[AWS] 设备授权就绪（user_code={self.user_code}）")

    def fetch_app_js(self) -> None:
        """刷新指纹密钥；失败只记日志（回退密钥仍然能跑）。"""
        try:
            response = self._get(
                f"{SIGNIN_BASE}/assets/js/app.js",
                headers={"Accept": "*/*", "Referer": f"{SIGNIN_BASE}/"},
            )
            self.fingerprint.update_app_js(str(response.text or ""))
        except Exception as exc:  # noqa: BLE001 - 取不到不该阻塞注册
            self._log(f"[AWS] app.js 获取失败（沿用回退密钥）: {type(exc).__name__}")

    # ---------------------------------------------------------------- 4-5
    def _fetch_d2c(self, referer: str) -> None:
        origin = "https://" + (urlparse(referer).netloc or "us-east-1.signin.aws")
        response = self._post(
            D2C_URL,
            {},
            headers=self._headers(origin=origin, referer=referer),
        )
        data = _json_of(response)
        token = str(data.get("token") or "")
        if not token:
            return
        jar = getattr(self.session, "cookies", None)
        if jar is not None:
            jar.set("awsd2c-token", token)
            jar.set("awsd2c-token-c", token)
        claims = _decode_jwt_payload(token)
        if claims.get("vid"):
            self.visitor_id = str(claims["vid"])

    def portal_login(self, *, for_sso: bool = False) -> None:
        user_code = self.user_code
        if for_sso:
            redirect = quote(f"{VIEW_BASE}/start/#/", safe="")
        else:
            redirect = quote(f"{VIEW_BASE}/start/#/device?user_code={user_code}", safe="")
        response = self._get(
            f"{PORTAL_BASE}/login?directory_id=view&redirect_url={redirect}",
            headers=self._headers(origin=VIEW_BASE, referer=f"{VIEW_BASE}/"),
        )
        data = _json_of(response)
        location = str(data.get("redirectUrl") or "")
        self.workflow_handle = _query(location, "workflowStateHandle")
        csrf = str(data.get("csrfToken") or "")
        if csrf:
            self.csrf_token = csrf
            jar = getattr(self.session, "cookies", None)
            if jar is not None:
                jar.set("loginCsrfToken", csrf)
        if not self.workflow_handle:
            raise AwsRegistrationError("portal_login", "未返回工作流句柄")
        self._fetch_d2c(
            f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login?workflowStateHandle={self.workflow_handle}"
        )

    def _fingerprint_input(
        self,
        *,
        page_type: str,
        event_type: str,
        time_on_page: int = 0,
        email: str = "",
    ) -> str:
        if page_type == "profile":
            location = (
                f"{PROFILE_BASE}/?workflowID={self.workflow_id}"
                f"#/signup/{'enter-email' if event_type == 'PageSubmit' else 'start'}"
            )
            referer = (
                f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/signup"
                f"?workflowStateHandle={self.workflow_handle}"
            )
        else:
            location = (
                f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/{page_type}"
                f"?workflowStateHandle={self.workflow_handle}"
            )
            referer = f"{VIEW_BASE}/"
        return self.fingerprint.fingerprint(
            page_type=page_type,
            event_type=event_type,
            location=location,
            referrer=referer,
            time_on_page=time_on_page,
            email=email,
        )

    def _workflow_execute(
        self,
        endpoint: str,
        payload: dict,
        *,
        referer: str,
        expected: tuple[int, ...] = (200,),
    ) -> Any:
        request_id = str(uuid.uuid4())
        headers = self._headers(referer=referer, origin=SIGNIN_BASE)
        headers.update(
            {
                "x-amzn-requestid": request_id,
                "x-amz-date": time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime()),
                "priority": "u=1, i",
            }
        )
        payload = dict(payload)
        payload["requestId"] = request_id
        response = self._post(endpoint, payload, headers=headers, expected=expected)
        self._capture_cookies(getattr(response, "headers", {}) or {})
        return response

    def _capture_cookies(self, headers: Mapping[str, Any]) -> None:
        """把 `Set-Cookie` 补进 jar。

        **不能按逗号切**：`Expires=Wed, 21 Oct 2026 07:28:00 GMT` 里就有逗号，
        一按逗号切就得到半截 cookie（参考实现也是逐条处理的）。curl_cffi 自己
        维护 jar，这里只为「测试/兜底会话」补刀，能拿到 `multi_items()` 就用它。
        """
        jar = getattr(self.session, "cookies", None)
        if jar is None or not headers:
            return
        multi = getattr(headers, "multi_items", None)
        if callable(multi):
            items = [value for key, value in multi() if str(key).lower() == "set-cookie"]
        else:
            raw = headers.get("set-cookie") or headers.get("Set-Cookie") or []
            items = [raw] if isinstance(raw, str) else list(raw)
        for item in items:
            head = str(item).split(";", 1)[0]
            if "=" not in head:
                continue
            name, value = head.split("=", 1)
            javax = name.strip()
            if not javax or javax.lower() in {"path", "domain", "expires", "max-age", "secure", "httponly", "samesite"}:
                continue
            jar.set(javax, value.strip())

    def workflow_init(self) -> None:
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/api/execute"
        referer = (
            f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login"
            f"?workflowStateHandle={self.workflow_handle}"
        )
        for step_id, event in (("", "first_load"), ("start", "PageLoad")):
            response = self._workflow_execute(
                endpoint,
                {
                    "stepId": step_id,
                    "workflowStateHandle": self.workflow_handle,
                    "inputs": [
                        {
                            "input_type": "FingerPrintRequestInput",
                            "fingerPrint": self._fingerprint_input(
                                page_type="signin", event_type=event
                            ),
                        }
                    ],
                },
                referer=referer,
            )
            data = _json_of(response)
            self.workflow_handle = str(data.get("workflowStateHandle") or self.workflow_handle)
            if step_id == "" and data.get("stepId") != "start":
                break

    # ---------------------------------------------------------------- 6-8
    def submit_email(self, email: str) -> str:
        """返回 `"signup"`（可注册）或 `"login"`（已注册）。"""
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/api/execute"
        referer = (
            f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login"
            f"?workflowStateHandle={self.workflow_handle}"
        )
        response = self._workflow_execute(
            endpoint,
            {
                "stepId": "get-identity-user",
                "workflowStateHandle": self.workflow_handle,
                "actionId": "SUBMIT",
                "inputs": [
                    {"input_type": "UserRequestInput", "username": email},
                    {
                        "input_type": "ApplicationTypeRequestInput",
                        "applicationType": "SSO_INDIVIDUAL_ID",
                    },
                    {
                        "input_type": "UserEventRequestInput",
                        "directoryId": DIRECTORY_ID,
                        "userName": email,
                        "userEvents": [
                            {
                                "input_type": "UserEvent",
                                "eventType": "PAGE_SUBMIT",
                                "pageName": "IDENTIFICATION",
                                "timeSpentOnPage": 5000,
                            }
                        ],
                    },
                    {
                        "input_type": "FingerPrintRequestInput",
                        "fingerPrint": self._fingerprint_input(
                            page_type="signin", event_type="PageSubmit", email=email
                        ),
                    },
                ],
                "visitorId": self.visitor_id,
            },
            referer=referer,
            expected=(200, 400),
        )
        data = _json_of(response)
        self.workflow_handle = str(data.get("workflowStateHandle") or self.workflow_handle)
        if response.status_code == 400:
            return "signup"
        if response.status_code == 200:
            return "login"
        raise AwsRegistrationError("submit_email", f"意外状态码 {response.status_code}")

    def signup(self, email: str) -> None:
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/api/execute"
        referer = (
            f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login"
            f"?workflowStateHandle={self.workflow_handle}"
        )
        response = self._workflow_execute(
            endpoint,
            {
                "stepId": "get-identity-user",
                "workflowStateHandle": self.workflow_handle,
                "actionId": "SIGNUP",
                "inputs": [
                    {"input_type": "UserRequestInput", "username": email},
                    {
                        "input_type": "FingerPrintRequestInput",
                        "fingerPrint": self._fingerprint_input(
                            page_type="signup", event_type="PageSubmit"
                        ),
                    },
                ],
                "visitorId": self.visitor_id,
            },
            referer=referer,
        )
        data = _json_of(response)
        redirect = str((data.get("redirect") or {}).get("url") or "")
        self.workflow_handle = _query(redirect, "workflowStateHandle") or self.workflow_handle

    def signup_init(self, email: str) -> None:
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/signup/api/execute"
        referer = (
            f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/signup"
            f"?workflowStateHandle={self.workflow_handle}"
        )
        last_body = ""
        for step_id, event in (("", "first_load"), ("start", "PageLoad")):
            response = self._workflow_execute(
                endpoint,
                {
                    "stepId": step_id,
                    "workflowStateHandle": self.workflow_handle,
                    "inputs": [
                        {"input_type": "UserRequestInput", "username": email},
                        {
                            "input_type": "FingerPrintRequestInput",
                            "fingerPrint": self._fingerprint_input(
                                page_type="signup", event_type=event
                            ),
                        },
                    ],
                    "visitorId": self.visitor_id,
                },
                referer=referer,
            )
            last_body = str(response.text or "")[:300]
            data = _json_of(response)
            self.workflow_handle = str(data.get("workflowStateHandle") or self.workflow_handle)
            redirect = str((data.get("redirect") or {}).get("url") or "")
            self.workflow_id = _query(redirect, "workflowID") or self.workflow_id
        if not self.workflow_id:
            raise AwsRegistrationError("signup_init", f"未返回 workflowID: {last_body}")

    def profile_init(self) -> None:
        self.ubid = ubid()
        jar = getattr(self.session, "cookies", None)
        if jar is not None:
            jar.set("aws-user-profile-ubid", self.ubid)
            jar.set("i18next", "en-US")
        url = f"{PROFILE_BASE}/?workflowID={self.workflow_id}"
        self._get(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": self.fingerprint.user_agent,
                "sec-fetch-dest": "document",
                "sec-fetch-mode": "navigate",
            },
        )
        self._fetch_d2c(url)

    def profile_start(self) -> None:
        referer = f"{PROFILE_BASE}/?workflowID={self.workflow_id}"
        response = self._post(
            f"{PROFILE_BASE}/api/start",
            {
                "workflowID": self.workflow_id,
                "browserData": {
                    "attributes": {
                        "fingerprint": self._fingerprint_input(
                            page_type="profile", event_type="PageLoad"
                        ),
                        "eventTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                        "timeSpentOnPage": "38",
                        "eventType": "PageLoad",
                        "ubid": self.ubid,
                        "visitorId": self.visitor_id,
                    },
                    "cookies": {},
                },
            },
            headers=self._headers(origin=PROFILE_BASE, referer=referer),
        )
        data = _json_of(response)
        self.workflow_state = str(data.get("workflowState") or "")
        if not self.workflow_state:
            raise AwsRegistrationError("profile_start", "未返回 workflowState")

    # ---------------------------------------------------------------- 9-12
    def send_otp(self, email: str) -> float:
        """发验证码；返回发送时刻（epoch 秒，供邮箱侧按时间过滤旧邮件）。"""
        sent_at = time.time()
        referer = f"{PROFILE_BASE}/?workflowID={self.workflow_id}"
        time_on_page = 5_000 + int(time.time() * 1000) % 3_001
        response = self._post(
            f"{PROFILE_BASE}/api/send-otp",
            {
                "workflowState": self.workflow_state,
                "email": email,
                "browserData": {
                    "attributes": {
                        "fingerprint": self._fingerprint_input(
                            page_type="profile",
                            event_type="PageSubmit",
                            time_on_page=time_on_page,
                            email=email,
                        ),
                        "eventTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                        "timeSpentOnPage": str(time_on_page),
                        "pageName": "EMAIL_COLLECTION",
                        "eventType": "PageSubmit",
                        "ubid": self.ubid,
                        "visitorId": self.visitor_id,
                    },
                    "cookies": {},
                },
            },
            headers=self._headers(origin=PROFILE_BASE, referer=referer),
        )
        if response.status_code != 200:
            raise AwsRegistrationError("send_otp", f"HTTP {response.status_code}")
        return sent_at

    def create_identity(self, email: str, full_name: str, otp: str) -> tuple[str, str]:
        referer = f"{PROFILE_BASE}/?workflowID={self.workflow_id}"
        response = self._post(
            f"{PROFILE_BASE}/api/create-identity",
            {
                "workflowState": self.workflow_state,
                "userData": {"email": email, "fullName": full_name},
                "otpCode": str(otp or "").strip(),
                "browserData": {
                    "attributes": {
                        "fingerprint": self._fingerprint_input(
                            page_type="profile", event_type="EmailVerification"
                        ),
                        "eventTimestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                        "timeSpentOnPage": "45000",
                        "pageName": "EMAIL_VERIFICATION",
                        "eventType": "EmailVerification",
                        "ubid": self.ubid,
                        "visitorId": self.visitor_id,
                    },
                    "cookies": {},
                },
            },
            headers=self._headers(origin=PROFILE_BASE, referer=referer),
        )
        data = _json_of(response)
        registration_code = str(data.get("registrationCode") or "")
        sign_in_state = str(data.get("signInState") or "")
        if not registration_code:
            raise AwsRegistrationError(
                "create_identity", f"未返回 registrationCode（验证码可能无效）: {str(response.text)[:200]}"
            )
        return registration_code, sign_in_state

    def set_password(
        self, email: str, password: str, registration_code: str, sign_in_state: str
    ) -> tuple[str, str, str]:
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/signup/api/execute"
        referer = (
            f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/signup"
            f"?registrationCode={registration_code}&state={sign_in_state}"
        )
        response = self._workflow_execute(
            endpoint,
            {
                "stepId": "",
                "state": sign_in_state,
                "inputs": [
                    {
                        "input_type": "UserRegistrationRequestInput",
                        "registrationCode": registration_code,
                        "state": sign_in_state,
                    },
                    {
                        "input_type": "FingerPrintRequestInput",
                        "fingerPrint": self._fingerprint_input(
                            page_type="signup", event_type="PageSubmit"
                        ),
                    },
                ],
            },
            referer=referer,
        )
        data = _json_of(response)
        self.workflow_handle = str(data.get("workflowStateHandle") or self.workflow_handle)
        context = ((data.get("workflowResponseData") or {}).get("encryptionContextResponse") or {})
        public_key = context.get("publicKey") if isinstance(context, dict) else None
        if not isinstance(public_key, dict) or not public_key.get("n"):
            raise AwsRegistrationError("set_password", "未获取到密码加密公钥")
        encrypted = encrypt_password(
            password,
            public_key,
            issuer=str(context.get("issuer") or "signin"),
            audience=str(context.get("audience") or "AWSPasswordService"),
            region=str(context.get("region") or REGION),
        )

        response = self._workflow_execute(
            endpoint,
            {
                "stepId": "get-new-password-for-password-creation",
                "workflowStateHandle": self.workflow_handle,
                "actionId": "SUBMIT",
                "inputs": [
                    {
                        "input_type": "PasswordRequestInput",
                        "password": encrypted,
                        "successfullyEncrypted": "SUCCESSFUL",
                    },
                    {"input_type": "UserRequestInput", "username": email},
                    {
                        "input_type": "FingerPrintRequestInput",
                        "fingerPrint": self._fingerprint_input(
                            page_type="signup", event_type="PageSubmit"
                        ),
                    },
                ],
                "visitorId": self.visitor_id,
            },
            referer=referer,
        )
        redirect = str((_json_of(response).get("redirect") or {}).get("url") or "")
        if not redirect:
            raise AwsRegistrationError("set_password", "密码设置未返回 redirect")
        return (
            _query(redirect, "workflowStateHandle"),
            _query(redirect, "state"),
            _query(redirect, "workflowResultHandle"),
        )

    def complete_signup(
        self, email: str, handle: str, state: str, result_handle: str
    ) -> dict[str, str]:
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/api/execute"
        referer = (
            f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login"
            f"?workflowStateHandle={handle}&state={state}&workflowResultHandle={result_handle}"
        )
        response = self._workflow_execute(
            endpoint,
            {
                "stepId": "",
                "workflowStateHandle": handle,
                "workflowResultHandle": result_handle,
                "state": state,
                "inputs": [
                    {"input_type": "UserRequestInput", "username": email},
                    {
                        "input_type": "FingerPrintRequestInput",
                        "fingerPrint": self._fingerprint_input(
                            page_type="signin", event_type="PageLoad"
                        ),
                    },
                ],
                "visitorId": self.visitor_id,
            },
            referer=referer,
        )
        data = _json_of(response)
        if data.get("stepId") != "end-of-workflow-success":
            raise AwsRegistrationError(
                "complete_signup", f"工作流未完成: {data.get('stepId')!r}"
            )
        redirect = str((data.get("redirect") or {}).get("url") or "")
        return {
            "auth_code": _query(redirect, "workflowResultHandle"),
            "sso_state": _query(redirect, "state"),
            "wdc_csrf_token": _query(redirect, "wdc_csrf_token"),
        }

    # ---------------------------------------------------------------- 13-14
    def sso_workflow(self) -> dict[str, str]:
        """在 view 目录上再走一遍工作流，拿 authCode / state / csrf。"""
        self.portal_login(for_sso=True)
        endpoint = f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/api/execute"
        referer = (
            f"{SIGNIN_BASE}/platform/{DIRECTORY_ID}/login"
            f"?workflowStateHandle={self.workflow_handle}"
        )
        response = self._workflow_execute(
            endpoint,
            {
                "stepId": "",
                "workflowStateHandle": self.workflow_handle,
                "inputs": [
                    {
                        "input_type": "FingerPrintRequestInput",
                        "fingerPrint": self._fingerprint_input(
                            page_type="signin", event_type="PageLoad"
                        ),
                    }
                ],
            },
            referer=referer,
        )
        data = _json_of(response)
        if data.get("stepId") == "start":
            self.workflow_handle = str(data.get("workflowStateHandle") or self.workflow_handle)
            response = self._workflow_execute(
                endpoint,
                {
                    "stepId": "start",
                    "workflowStateHandle": self.workflow_handle,
                    "inputs": [
                        {
                            "input_type": "FingerPrintRequestInput",
                            "fingerPrint": self._fingerprint_input(
                                page_type="signin", event_type="PageLoad"
                            ),
                        }
                    ],
                },
                referer=referer,
            )
            data = _json_of(response)

        redirect = str((data.get("redirect") or {}).get("url") or "")
        auth_code = _query(redirect, "workflowResultHandle")
        state = _query(redirect, "state")
        wdc = _query(redirect, "wdc_csrf_token")
        if data.get("stepId") == "end-of-workflow-success" and auth_code:
            params = {k: v for k, v in {"state": state, "workflowResultHandle": auth_code, "wdc_csrf_token": wdc}.items() if v}
            self._get(
                f"{VIEW_BASE}/start/?{urlencode(params)}",
                headers=self._headers(origin=VIEW_BASE, referer=f"{SIGNIN_BASE}/"),
            )
        if not auth_code:
            raise AwsRegistrationError("sso_workflow", "未返回授权码")
        return {"auth_code": auth_code, "state": state, "wdc_csrf_token": wdc}

    def sso_token(self, auth_code: str, state: str) -> str:
        headers = self._headers(
            origin=VIEW_BASE,
            referer=f"{VIEW_BASE}/",
            content_type="application/x-www-form-urlencoded",
        )
        if self.csrf_token:
            headers["x-amz-sso-csrf-token"] = self.csrf_token
        body = urlencode({"authCode": auth_code, "state": state, "orgId": "view"})
        token = ""
        for _ in range(5):
            response = self.session.post(
                f"{PORTAL_BASE}/auth/sso-token",
                data=body,
                headers=headers,
                timeout=self.timeout,
            )
            data = _json_of(response)
            token = str(data.get("token") or "")
            if token or response.status_code != 401:
                break
            time.sleep(3)
        if not token:
            raise AwsRegistrationError("sso_token", "SSO token 获取失败")
        return token

    def device_token(self, sso_token: str) -> dict[str, Any]:
        accept = _json_of(
            self._post(
                f"{OIDC_BASE}/device_authorization/accept_user_code",
                {"userCode": self.user_code, "userSessionId": sso_token},
                headers={"Content-Type": "application/json"},
            )
        )
        self._post(
            f"{OIDC_BASE}/device_authorization/associate_token",
            {
                "deviceContext": accept.get("deviceContext"),
                "userSessionId": sso_token,
            },
            headers={"Content-Type": "application/json"},
        )
        for _ in range(30):
            response = self._post(
                f"{OIDC_BASE}/token",
                {
                    "clientId": self.client_id,
                    "clientSecret": self.client_secret,
                    "deviceCode": self.device_code,
                    "grantType": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers={"Content-Type": "application/json"},
                expected=(200, 400),
            )
            if response.status_code == 200:
                data = _json_of(response)
                if data.get("refreshToken"):
                    return data
            time.sleep(2)
        raise AwsRegistrationError("device_token", "Builder ID token 轮询超时")

    # ---------------------------------------------------------------- 编排
    def register(
        self,
        *,
        email: str,
        password: str,
        full_name: str = "Test User",
        otp_provider: Callable[[], str],
    ) -> dict[str, Any]:
        """跑完整条注册链；返回可落库的凭证字典。"""
        self.fetch_app_js()
        self.oidc_client_register()
        self.device_authorization()
        self.portal_login()
        self.workflow_init()

        status = self.submit_email(email)
        if status == "login":
            raise AwsEmailAlreadyRegistered(email)
        if status != "signup":
            raise AwsRegistrationError("submit_email", f"未知状态: {status}")

        self.signup(email)
        self.signup_init(email)
        self.profile_init()
        self.profile_start()
        self.send_otp(email)
        self._log("[AWS] 验证码已发送，等待收件…")
        otp = str(otp_provider() or "").strip()
        if not otp:
            raise AwsRegistrationError("otp", "未取到验证码")

        registration_code, sign_in_state = self.create_identity(email, full_name, otp)
        handle, state, result_handle = self.set_password(
            email, password, registration_code, sign_in_state
        )
        self._log("[AWS] 密码已设置")
        self.complete_signup(email, handle, state, result_handle)

        sso = self.sso_workflow()
        sso_token = self.sso_token(sso["auth_code"], sso["state"])
        tokens = self.device_token(sso_token)

        return {
            "email": email,
            "password": password,
            "provider": PROVIDER,
            "region": REGION,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "device_code": self.device_code,
            "user_code": self.user_code,
            "access_token": str(tokens.get("accessToken") or ""),
            "refresh_token": str(tokens.get("refreshToken") or ""),
            "expires_in": int(tokens.get("expiresIn") or 0),
            "token_type": str(tokens.get("tokenType") or "Bearer"),
            "sso": sso_token,
        }


def _decode_jwt_payload(token: str) -> dict:
    """解 JWT 的 payload（不验签）；解不出返回空字典。"""
    import base64

    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    segment = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment))
    except (ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "AwsBuilderIdClient",
    "AwsEmailAlreadyRegistered",
    "AwsRegistrationError",
]