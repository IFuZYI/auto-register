"""AWS Builder ID 的 SSO 会话 → Kiro IDE 凭据（authorization_code + PKCE）。

七步（与 `reference/register/kirox/internal/core/kiro_auth.go` 一致）：

1. `POST {OIDC}/client/register`      申请一个 **Kiro IDE** 客户端（与注册用的
   Amazon Q 客户端是两个不同的 client，不能混用）；
2. 生成 PKCE `code_verifier` / `challenge` 与随机回调端口；
3. `GET {OIDC}/authorize`（不跟随重定向）→ `Location` 里带 `orchestrator_id`；
4. `POST {OIDC}/authentication_result`（带 SSO bearer）→ 第一段 resume URL；
5. `GET` resume URL（不跟随）→ `authorizationResumptionContext`；
6. `POST {OIDC}/device_authorization/associate_token`（用户会话 + context）
   → 第二段 resume URL；`GET` 它 → `code`；
7. `POST {OIDC}/token`（authorization_code + code_verifier）→ AT / RT / profileArn。

**每一步都是「不跟随重定向 + 读 Location」**：跟随了会落到登录页 HTML，参数就丢了。
"""
from __future__ import annotations

import logging
import random
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlencode, urlparse

from platforms.aws.constants import OIDC_BASE, SCOPES, START_URL

from .constants import (
    AUTHORIZATION_CODE_GRANT,
    DEFAULT_PROFILE_ARN,
    KIRO_CLIENT_NAME,
    KIRO_REDIRECT_URI,
    REGION,
)

logger = logging.getLogger(__name__)

LogFn = Callable[[str], None]


class KiroAuthorizeError(RuntimeError):
    """授权链路的可识别失败（带阶段名）。"""

    def __init__(self, stage: str, message: str) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.message = message


def _json_of(response: Any) -> dict:
    try:
        value = response.json()
    except Exception as exc:  # noqa: BLE001 - 非 JSON 响应按空处理
        logger.debug("响应体不是 JSON（按空处理）: %s", exc)
        return {}
    return value if isinstance(value, dict) else {}


def _query(url: str, key: str) -> str:
    try:
        return (parse_qs(urlparse(str(url or "")).query).get(key) or [""])[0]
    except (TypeError, ValueError):
        return ""


def _header_of(response: Any, name: str) -> str:
    headers = getattr(response, "headers", None) or {}
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return ""
    return str(getter(name) or getter(name.lower()) or "")


_NAV_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


def authorize_kiro(
    *,
    sso_token: str,
    session: Any = None,
    proxy: str = "",
    redirect_port: Optional[int] = None,
    log: Optional[LogFn] = None,
) -> dict[str, Any]:
    """用 Builder ID 的 SSO 会话换 Kiro IDE 的 AT/RT。

    返回：`{access_token, refresh_token, client_id, client_secret, expires_in,
    profile_arn, region}`。没有 code 或没有 AT 一律抛 `KiroAuthorizeError`。
    """
    from platforms.aws.crypto import pkce

    log = log or (lambda _m: None)
    owns_session = session is None
    if owns_session:
        from curl_cffi import requests as curl_requests

        session = curl_requests.Session(impersonate="chrome131", timeout=60)
        if proxy:
            session.proxies = {"http": proxy, "https": proxy}

    try:
        registration = session.post(
            f"{OIDC_BASE}/client/register",
            json={
                "clientName": KIRO_CLIENT_NAME,
                "clientType": "public",
                "scopes": list(SCOPES),
                "redirectUris": [KIRO_REDIRECT_URI],
                "grantTypes": ["authorization_code", "refresh_token"],
                "issuerUrl": START_URL,
            },
            headers={"Content-Type": "application/json"},
        )
        data = _json_of(registration)
        client_id = str(data.get("clientId") or "")
        client_secret = str(data.get("clientSecret") or "")
        if not client_id:
            raise KiroAuthorizeError(
                "client_register", f"未返回 clientId: {str(getattr(registration, 'text', ''))[:200]}"
            )

        verifier, challenge = pkce()
        state = f"{random.getrandbits(128):032x}"
        port = int(redirect_port or random.SystemRandom().randint(49152, 65535))
        redirect_uri = f"http://127.0.0.1:{port}/oauth/callback"

        params = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scopes": ",".join(SCOPES),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        nav_headers = dict(_NAV_HEADERS)
        nav_headers["User-Agent"] = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        )

        first = session.get(
            f"{OIDC_BASE}/authorize?{params}", headers=nav_headers, allow_redirects=False
        )
        orchestrator = _query(_header_of(first, "Location"), "orchestrator_id")
        if not orchestrator:
            raise KiroAuthorizeError("authorize", "未返回 orchestrator_id")

        result = session.post(
            f"{OIDC_BASE}/authentication_result",
            json={"orchestrator_id": orchestrator},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://view.awsapps.com",
                "Referer": "https://view.awsapps.com/",
                "x-amz-sso_bearer_token": sso_token,
                "x-amz-sso-bearer-token": sso_token,
            },
        )
        resume_one = str(_json_of(result).get("location") or "")
        if not resume_one:
            raise KiroAuthorizeError("authentication_result", "未返回 resume location")

        second = session.get(resume_one, headers=nav_headers, allow_redirects=False)
        context = _query(_header_of(second, "Location"), "authorizationResumptionContext")
        if not context:
            raise KiroAuthorizeError("resume", "未返回 authorizationResumptionContext")

        consent = session.post(
            f"{OIDC_BASE}/device_authorization/associate_token",
            json={
                "authorizationResumptionContext": context,
                "userSessionId": sso_token,
            },
            headers={
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": "https://view.awsapps.com",
                "Referer": "https://view.awsapps.com/",
            },
        )
        resume_two = str(_json_of(consent).get("location") or "")
        if not resume_two:
            raise KiroAuthorizeError("associate_token", "未返回 resume location")

        third = session.get(resume_two, headers=nav_headers, allow_redirects=False)
        code = _query(_header_of(third, "Location"), "code")
        if not code:
            raise KiroAuthorizeError("callback", "回调里没有 code")

        token_response = session.post(
            f"{OIDC_BASE}/token",
            json={
                "clientId": client_id,
                "clientSecret": client_secret,
                "grantType": AUTHORIZATION_CODE_GRANT,
                "code": code,
                "redirectUri": redirect_uri,
                "codeVerifier": verifier,
            },
            headers={"Content-Type": "application/json"},
        )
        tokens = _json_of(token_response)
        access_token = str(tokens.get("accessToken") or "")
        if not access_token:
            raise KiroAuthorizeError(
                "token", f"未返回 accessToken: {str(getattr(token_response, 'text', ''))[:200]}"
            )
        log("[Kiro] 已换取 Kiro IDE 凭据")
        return {
            "access_token": access_token,
            "refresh_token": str(tokens.get("refreshToken") or ""),
            "client_id": client_id,
            "client_secret": client_secret,
            "expires_in": int(tokens.get("expiresIn") or 0),
            "profile_arn": str(tokens.get("profileArn") or DEFAULT_PROFILE_ARN),
            "region": REGION,
        }
    finally:
        if owns_session:
            try:
                session.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭会话失败（忽略）: %s", exc)


__all__ = ["KiroAuthorizeError", "authorize_kiro"]