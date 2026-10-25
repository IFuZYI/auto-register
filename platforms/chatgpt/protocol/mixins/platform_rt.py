"""PlatformRtMixin：走 platform client_id 的「密码登录」路线换 refresh_token。

**为什么不复用 Codex 路线**（2026-10-26 实测）：

| | Codex 路线 | platform 路线（本模块） |
|---|---|---|
| client_id | `app_EMoamEEZ73f0CkXaXp7hrann` | `app_2SKx67EdpoN0G6j64rFvigXD` |
| redirect_uri | `http://localhost:1455/auth/callback` | `https://platform.openai.com/auth/callback` |
| 换 code 的方式 | 跟随 authorize 302 链 | 从登录响应里直接读 `?code=` |
| 新号结果 | 卡在 choose-an-account / session.select | **拿到 RT** |
| 是否需要手机号 | 可能撞 add-phone | **全程不碰** |

参照实现：`/home/chatgpt2api/internal/service/password_login.go`（Go）。
本模块只补「platform 侧特有」的两步（构造 authorize URL、用 platform 参数换 token），
中间的 `authorize_continue` / `login_password_verify` / `submit_mfa_totp` 复用本仓
既有实现 —— 那三处已由 2026-10-26 的 4 次成功交换验证可用，不照搬 Go 的写法。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from typing import Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

#: platform 侧的 client_id —— 与 Codex CLI 用的不是同一个。
PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"
#: platform 侧回调；换 token 时**必须**与 authorize 时一致。
PLATFORM_REDIRECT_URI = "https://platform.openai.com/auth/callback"
PLATFORM_AUTHORIZE_URL = "https://auth.openai.com/api/accounts/authorize"
#: 注意与 Codex 的 `/oauth/token` 不是一个端点：code 与 (client_id, redirect_uri,
#: code_verifier) 绑定，混用必然 invalid_grant。
PLATFORM_TOKEN_URL = "https://auth.openai.com/api/accounts/oauth/token"
PLATFORM_AUDIENCE = "https://api.openai.com/v1"
PLATFORM_SCOPE = "openid profile email offline_access"


class PlatformRtMixin:
    def _build_platform_authorize(self, email: str) -> tuple[str, str, str, str, str]:
        """构造 platform 侧 authorize URL。

        与 Codex 版的关键差异：带 `login_hint` + `screen_hint=login_or_signup`，
        服务端据此直接落到 `/log-in/password`（Codex 版带 `prompt=login`、不带
        login_hint，落点是 `/log-in`）。
        """
        verifier = self._b64url_no_pad(secrets.token_bytes(64))
        challenge = self._b64url_no_pad(hashlib.sha256(verifier.encode("utf-8")).digest())
        state = self._b64url_no_pad(secrets.token_bytes(24))

        params = {
            "issuer": "https://auth.openai.com",
            "client_id": PLATFORM_CLIENT_ID,
            "audience": PLATFORM_AUDIENCE,
            "redirect_uri": PLATFORM_REDIRECT_URI,
            "device_id": (self.result.device_id or "").strip(),
            "screen_hint": "login_or_signup",
            "max_age": "0",
            "login_hint": str(email or "").strip(),
            "scope": PLATFORM_SCOPE,
            "response_type": "code",
            "response_mode": "query",
            "state": state,
            "nonce": self._b64url_no_pad(secrets.token_bytes(24)),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "auth0Client": "eyJ2ZXJzaW9uIjoiMS40LjAiLCJuYW1lIjoiYXV0aDAuanMifQ",
        }
        auth_url = f"{PLATFORM_AUTHORIZE_URL}?{urlencode(params)}"
        return auth_url, state, verifier, PLATFORM_REDIRECT_URI, PLATFORM_CLIENT_ID

    def _exchange_platform_code(
        self,
        code: str,
        verifier: str,
        redirect_uri: str,
        client_id: str,
    ) -> bool:
        """用 authorization code 换三件套（AT / RT / IDT）。

        三点必须与产出 code 的 authorize 完全一致：`client_id`、`redirect_uri`、
        `code_verifier` —— 任一不符即 `invalid_grant`，且 code 是一次性的。

        端点也不同：platform 是 `/api/accounts/oauth/token`，Codex 是 `/oauth/token`。

        失败返回 False（不抛）：调用方多半还要回退另一条路线，异常会把整条链炸掉。
        """
        from urllib.parse import urlencode as _urlencode

        from platforms.chatgpt.protocol.response_summary import describe_error

        if not str(code or "").strip():
            logger.warning("platform 换 token 缺少 code")
            return False

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://auth.openai.com",
            "Referer": "https://platform.openai.com/",
            "User-Agent": self._ua,
        }
        form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        encoded = _urlencode(form)
        resp = self.session.post(
            PLATFORM_TOKEN_URL, headers=headers, data=encoded, timeout=30
        )
        self._trace_http(
            "platform_oauth_token_exchange",
            resp,
            extra_request={"method": "POST", "url": PLATFORM_TOKEN_URL,
                           "body": encoded, "headers": headers},
        )
        if resp.status_code != 200:
            logger.warning("platform oauth/token 失败: %s - %s",
                           resp.status_code, describe_error(resp.text))
            return False
        try:
            data = resp.json() or {}
        except Exception:
            logger.warning("platform oauth/token 返回非 JSON")
            return False

        self.result.access_token = str(data.get("access_token") or self.result.access_token or "")
        self.result.refresh_token = str(data.get("refresh_token") or self.result.refresh_token or "")
        if data.get("id_token"):
            self.result.id_token = str(data["id_token"])
        logger.info(
            "platform 换 token 成功（refresh_token=%s）",
            "有" if self.result.refresh_token else "无",
        )
        return bool(self.result.refresh_token)

    def acquire_rt_via_platform_login(
        self,
        *,
        email: str,
        password: str,
        totp_secret: str = "",
    ) -> bool:
        """走 platform 密码登录路线换 refresh_token。成功时 `self.result.refresh_token` 有值。

        步骤：platform authorize → authorize_continue（落 /log-in/password）
        → password/verify → （若要）mfa/verify → 从响应里直接读 code → 换 token。

        **不跟随重定向**：`follow_redirect_chain` 只保护 chatgpt 自己的回调路径，
        platform 的回调会被它真的 GET 掉，一次性 code 随即作废（复审指出的陷阱）。
        这里像参照实现一样，从响应 JSON 的 `continue_url` 里读 `?code=`。

        缺凭据时**明确失败**，不去猜密码 —— 猜出来的密码只会 401，把
        「这个号没有密码」误报成「密码错误」。
        """
        self.last_platform_route_error = ""
        email = str(email or "").strip()
        password = str(password or "")
        totp_secret = str(totp_secret or "").strip()

        if not email:
            self.last_platform_route_error = "缺少邮箱，无法走密码登录路线"
            logger.warning(self.last_platform_route_error)
            return False
        if not password:
            self.last_platform_route_error = (
                "账号没有密码，无法走密码登录路线（该号可能是无密码注册）"
            )
            logger.warning(self.last_platform_route_error)
            return False

        from urllib.parse import parse_qs, urlparse

        from platforms.chatgpt.protocol.totp import totp_now as _totp_now

        if not (self.result.device_id or "").strip():
            device_id = self._get_cookie_value_by_name("oai-did")
            if device_id:
                self.result.device_id = device_id

        auth_url, _state, verifier, redirect_uri, client_id = self._build_platform_authorize(email)
        self.auth_oauth_init(auth_url)

        sentinel = self.get_sentinel_token(self.result.device_id or "")
        step = self.authorize_continue(
            email=email,
            sentinel_token=sentinel,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            trace_step="platform_rt_authorize_continue",
        )
        page_type = self._extract_page_type(step)
        continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(step))

        # 已经是已有账号：密码校验
        if page_type == "login_password" or "/log-in/password" in (continue_url or ""):
            step = self.login_password_verify(password)
            page_type = self._extract_page_type(step)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(step))

        # 2FA：服务端要动态码时必须持有密钥，否则这条路走不通
        if self._is_mfa_challenge_state(page_type, continue_url):
            if not totp_secret:
                self.last_platform_route_error = (
                    "服务端要求 2FA（mfa_challenge），但账号没有 totp_secret —— "
                    "该路线不可用；密钥无法从服务端取回，需改用 Codex 路线或恢复密钥"
                )
                logger.warning(self.last_platform_route_error)
                return False
            challenge_id = continue_url.split("/")[-1] if "/mfa-challenge/" in (continue_url or "") else ""
            if not challenge_id:
                self.last_platform_route_error = "mfa_challenge 的 continue_url 里取不到 challenge id"
                logger.warning(self.last_platform_route_error)
                return False
            mfa_resp = self.submit_mfa_totp(_totp_now(totp_secret), challenge_id)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(mfa_resp))

        code = ""
        if continue_url:
            code = (parse_qs(urlparse(continue_url).query).get("code", [""])[0] or "").strip()
        if not code:
            self.last_platform_route_error = (
                f"登录链没给出 authorization code（page_type={page_type}, "
                f"落点={str(continue_url)[:80]}）"
            )
            logger.warning(self.last_platform_route_error)
            return False

        return self._exchange_platform_code(
            code=code, verifier=verifier, redirect_uri=redirect_uri, client_id=client_id
        )
