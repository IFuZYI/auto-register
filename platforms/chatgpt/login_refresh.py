"""走登录流程重新拿 access_token。

`token_refresh` 的两条路（session token / OAuth refresh token）都拿不到可用 AT
时才走这里：邮箱 + 密码重新跑一遍登录链，必要时过 2FA / 邮箱验证码。

    ① 账号密码 + 2FA（`totp_secret` 在库）—— 最常见也最快的一条。
    ② 服务端要邮箱验证码时，按用户要求**先判断邮箱是否入池**：没入池就直接
       报错说清（而不是去撞一个必然超时的收件箱），入池了才用邮箱池取码。
    ③ 两条都不通时，用登录链的响应判断账号是不是**已被封禁** —— OpenAI 对
       停用账号会明确回 "deleted or deactivated"，这是本模块最有价值的产出：
       它能把「凭证过期」和「号没了」分开，后者不该再进重试队列。

设计要点：登录链**必须复用** `AuthFlow` 而不是自己拼 HTTP —— 那条链里有 PoW
sentinel、device_id、oai-did cookie 等一串服务端校验，手搓必然 409 invalid_state。
本模块只做三件事：组装 `AuthFlow`、把「邮箱从哪来」接成协议层要的 `MailProvider`、
把结果翻译成 `LoginRefreshResult`。

与 `rt_backfill` 的关系：那个模块是「拿 RT」优先（它的登录链开了
`OAUTH_REFRESH_ONLY`，只要 RT 不要 AT），本模块是「拿 AT」优先，用同一套
`AuthFlow` 但不开那个开关 —— 目标不同，不能互相替代。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.task_runtime import TaskInterruption
from platforms.chatgpt.protocol import AuthFlow, Config, MailProvider
from platforms.chatgpt.protocol_log_relay import mirror_protocol_logs
from platforms.chatgpt.rt_backfill import MailboxUnavailableProvider

logger = logging.getLogger(__name__)

#: 登录链里 OpenAI 对「号没了」的措辞。命中即判定账号已封禁 —— 见
#: `services.chatgpt_account_state.is_account_deactivated_message`（同一套判定，
#: 这里再引一次是为了让本模块不依赖 service 层）。
_BANNED_MARKERS = (
    "deleted or deactivated",
    "account_deactivated",
    "account has been deactivated",
    "you do not have an account",
)


@dataclass
class LoginRefreshResult:
    """登录流程重取 AT 的结果。"""

    success: bool = False
    access_token: str = ""
    session_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    cookie_header: str = ""
    error_message: str = ""
    #: 账号已被封禁/停用（OpenAI 明确这么说）。调用方据此标 `banned` 而不是
    #: 普通失败 —— 封号不该再进重试队列。
    banned: bool = False
    #: 走的哪条路：password_2fa / password_only / otp / unknown
    strategy: str = ""

    def summary(self) -> str:
        if self.success:
            return f"登录流程刷新成功（{self.strategy or '未知方式'}）"
        if self.banned:
            return f"账号已封禁：{self.error_message}"
        return self.error_message or "登录流程刷新失败"


def looks_like_banned(text: Any) -> bool:
    """响应文本读起来像不像「账号已封禁」。"""
    value = str(text or "").strip().lower()
    if not value:
        return False
    return any(marker in value for marker in _BANNED_MARKERS)


class LoginAccessTokenRefresher:
    """用邮箱 + 密码（+ 2FA / 邮箱验证码）重新登录，换取新的 access_token。"""

    def __init__(
        self,
        *,
        email: str,
        password: str = "",
        totp_secret: str = "",
        proxy: Optional[str] = None,
        extra_config: Optional[dict] = None,
        mail_provider: Optional[MailProvider] = None,
        mail_unavailable_reason: str = "",
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self.email = (email or "").strip()
        self.password = (password or "").strip()
        self.totp_secret = (totp_secret or "").strip()
        self.proxy = (proxy or "").strip() or None
        self.extra_config = dict(extra_config or {})
        self.mail_provider = mail_provider
        self.mail_unavailable_reason = mail_unavailable_reason
        self._log_fn = log_fn
        self.log = log_fn or logger.info
        self._active_flow: Optional[AuthFlow] = None

    # ── 主流程 ──

    def run(self) -> LoginRefreshResult:
        result = LoginRefreshResult()
        if not self.email:
            result.error_message = "账号没有邮箱，无法走登录流程"
            return result
        if not self.password:
            # 有 2FA 但没密码同样走不通：密码是登录链的第一道。
            result.error_message = "账号没有密码，无法走登录流程（需要密码 + 2FA）"
            return result

        provider = self.mail_provider or MailboxUnavailableProvider(
            self.email, self.mail_unavailable_reason
        )

        try:
            with mirror_protocol_logs(self._log_fn):
                self._run_login(provider, result)
        except TaskInterruption:
            # 手动停止/跳过：照实往上报，不当作失败重试
            raise
        except Exception as exc:  # noqa: BLE001 - 登录链的异常都要变成可展示的原因
            message = str(exc) or exc.__class__.__name__
            result.error_message = message
            # 封禁判定：异常文本里也可能带着服务端原话
            if looks_like_banned(message):
                result.banned = True
                self.log(f"[登录刷新] 账号已封禁: {message}")
            else:
                self.log(f"[登录刷新] 失败: {message}")

        self._absorb(result)
        if result.access_token and not result.success:
            result.success = True
            result.error_message = ""
        if not result.success and not result.error_message:
            result.error_message = "登录流程跑完但没拿到 access_token"
        return result

    def _run_login(self, provider: MailProvider, result: LoginRefreshResult) -> None:
        flow = self._build_flow()
        self._active_flow = flow
        result.strategy = "password_2fa" if self.totp_secret else "password_only"
        self.log(
            f"[登录刷新] 重走登录链: {self.email}"
            f"（{'密码 + 2FA' if self.totp_secret else '仅密码'}）"
        )
        flow.run_protocol_login(provider, self.email, self.password)

    def _build_flow(self) -> AuthFlow:
        flow = AuthFlow(
            Config(proxy=self.proxy),
            env_overrides=self._env_overrides(),
            account_callback=self._account_callback,
        )
        if self.totp_secret:
            flow.result.totp_secret = self.totp_secret
        return flow

    def _account_callback(self, email: str) -> dict:
        """协议层撞上 mfa-challenge 时来要 2FA 密钥。"""
        return {"password": self.password, "totp_secret": self.totp_secret}

    def _env_overrides(self) -> dict:
        merged = {
            "OTP_TIMEOUT": str(self._otp_timeout()),
            # 登录已有账号；别让协议层把「这邮箱已注册」当失败
            "WEBUI_ALLOW_LOGIN": "1",
        }
        return merged

    def _otp_timeout(self) -> int:
        for key in ("mailbox_otp_timeout_seconds", "email_otp_timeout_seconds", "otp_timeout"):
            try:
                seconds = int(str(self.extra_config.get(key) or "").strip())
            except ValueError:
                continue
            if seconds > 0:
                return seconds
        return 180

    def _absorb(self, result: LoginRefreshResult) -> None:
        """把 flow 上拿到的凭证并进结果（只覆盖非空值）。"""
        flow = self._active_flow
        if flow is None:
            return
        auth = flow.result
        for attr in ("access_token", "session_token", "refresh_token", "id_token", "cookie_header"):
            value = str(getattr(auth, attr, "") or "").strip()
            if value:
                setattr(result, attr, value)


__all__ = [
    "LoginAccessTokenRefresher",
    "LoginRefreshResult",
    "looks_like_banned",
]
