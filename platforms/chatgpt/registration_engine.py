"""ChatGPT 注册引擎。

驱动 ``platforms.chatgpt.protocol`` 里的 authorize 状态机跑完整条注册链，把
本仓库的邮箱池、接码配置、任务日志接到协议层的三个注入点上：

    mail_provider     邮箱适配器，负责要地址、等 6 位邮件验证码
    sms_callback      接码控制器，命中 add-phone 时自动租号收短信
    on_password       密码一在 OpenAI 侧生效就回调，避免中途失败把号跑丢
    on_session_ready  session 一到手就回调，开了 2FA 绑定时用来插入 enroll

协议层不认识本仓库的任何东西（config_store、任务运行时、账号表都不认识），
所有环境相关的开关都通过 ``env_overrides`` 以实例级配置传进去，进程环境变量
一个字节都不动 —— 多个注册任务并发跑时互不污染。
"""

from __future__ import annotations

import logging
import random
import string
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.base_mailbox import BaseMailbox
from core.base_platform import resolve_mailbox_otp_timeout
from platforms.chatgpt.protocol import AuthFlow, AuthResult, Config
from platforms.chatgpt.protocol.mailbox_adapter import MailboxProviderAdapter
from platforms.chatgpt.protocol.two_factor import (
    TwoFactorBindResult,
    bind_totp_inline,
    bind_totp_via_login,
)
from platforms.chatgpt.protocol_log_relay import mirror_protocol_logs
from services.sms_service import build_phone_callback, resolve_sms_settings

logger = logging.getLogger(__name__)

REGISTRATION_MODE_REFRESH_TOKEN = "refresh_token"
REGISTRATION_MODE_ACCESS_TOKEN_ONLY = "access_token_only"

# 注册身份用什么：邮箱、手机号、手机号注册完再把邮箱绑上去
REGISTER_FLOW_EMAIL = "email"
REGISTER_FLOW_PHONE = "phone"
REGISTER_FLOW_PHONE_WITH_EMAIL = "phone_with_email"

_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$"


@dataclass
class RegistrationResult:
    """注册结果。"""

    success: bool
    email: str = ""
    password: str = ""
    account_id: str = ""
    workspace_id: str = ""
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    session_token: str = ""
    cookie_header: str = ""
    error_message: str = ""
    source: str = "register"
    # 失败重开一轮有没有意义：号源被静默拦下这种，重开只是再赔一次
    retryable: bool = True
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_auth_result(cls, result: AuthResult, *, source: str = "register") -> "RegistrationResult":
        phone_number = getattr(result, "phone_number", "") or ""
        bound_email = getattr(result, "bound_email", "") or ""
        return cls(
            success=True,
            # 手机号注册的号，绑上邮箱之后按邮箱记账，没绑上就只能用手机号当标识
            email=bound_email or result.email,
            password=result.password,
            access_token=result.access_token,
            refresh_token=result.refresh_token,
            id_token=result.id_token,
            session_token=result.session_token,
            cookie_header=result.cookie_header,
            source=source,
            metadata={
                "device_id": result.device_id,
                "totp_secret": result.totp_secret,
                "phone_number": phone_number,
                "bound_email": bound_email,
            },
        )


def generate_password(length: int = 16) -> str:
    return "".join(random.choices(_PASSWORD_ALPHABET, k=length))


class ChatGPTRegistrationEngine:
    """把一个邮箱跑成一个可用的 ChatGPT 账号。"""

    def __init__(
        self,
        *,
        mailbox: BaseMailbox,
        mode: str = REGISTRATION_MODE_REFRESH_TOKEN,
        proxy: Optional[str] = None,
        email: str = "",
        password: str = "",
        extra_config: Optional[dict] = None,
        log_fn: Optional[Callable[[str], None]] = None,
        mailbox_kind: str = "mailbox",
        register_flow: str = REGISTER_FLOW_EMAIL,
        bind_2fa: bool = False,
    ):
        self.mailbox = mailbox
        self.mode = mode
        self.proxy = (proxy or "").strip() or None
        self.email = (email or "").strip()
        self.password = (password or "").strip() or generate_password()
        self.extra_config = dict(extra_config or {})
        self.log = log_fn or logger.info
        self.mailbox_kind = mailbox_kind
        self.register_flow = register_flow or REGISTER_FLOW_EMAIL
        self.bind_2fa = bool(bind_2fa)
        self.flow: Optional[AuthFlow] = None
        self.two_factor: Optional[TwoFactorBindResult] = None

    def run(self) -> RegistrationResult:
        if self.register_flow in (REGISTER_FLOW_PHONE, REGISTER_FLOW_PHONE_WITH_EMAIL):
            return self._run_phone()
        return self._run_email()

    def _run_email(self) -> RegistrationResult:
        provider = self._build_mail_provider()
        flow = self._build_flow()

        # 协议链的每一步都只写在协议层的 logger 上，不镜像出来的话任务日志里
        # 只有「开始/成功」两行 —— 实测踩过：`refresh_token` 模式跑完，日志
        # 全绿、账号也建好了，但库里 `refresh_token` 是空的，而**没有任何一行**
        # 说明 Codex 交换到底跑没跑、失败在哪。手机链路早就镜像了（见
        # `_run_phone`），邮箱链路漏了。
        try:
            with mirror_protocol_logs(self.log):
                result = flow.run_register(provider)
        except Exception as exc:
            registration = self._salvage(flow, exc)
            self._set_mailbox_status(provider, "used" if registration.success else "failed")
            return self._attach_mailbox_credentials(registration, provider)

        self._ensure_two_factor(flow, provider)
        registration = self._attach_two_factor(RegistrationResult.from_auth_result(result))
        self._note_missing_refresh_token(registration)
        self._set_mailbox_status(provider, "used")
        return self._attach_mailbox_credentials(registration, provider)

    def _note_missing_refresh_token(self, registration: RegistrationResult) -> None:
        """`refresh_token` 模式却没拿到 RT 时，把原因摆到明面上。

        RT 由 Codex OAuth 交换提供，而那条链路会撞上 **add-phone 墙**：OpenAI
        要求先绑手机号才给 refresh_token（实测日志原文：「命中 add-phone，但未配置
        SMS 接码 / OPENAI_PHONE_NUMBER，无法继续推进」）。没配接码时这个模式
        只能拿到 access_token —— 账号可用，但**没有 RT**。

        这必须说出来：此前它按成功收场，库里 RT 是空的、界面一片绿，用户以为
        自己有一批带 RT 的号（实测踩过，三个号全空）。配置了接码时这条路能走通，
        所以不删模式，只把降级讲清楚。
        """
        if self.mode != REGISTRATION_MODE_REFRESH_TOKEN:
            return
        if registration.refresh_token:
            return
        registration.metadata["partial"] = True
        registration.metadata["missing"] = "refresh_token"
        self.log(
            "⚠ 本模式要求 refresh_token，但 Codex OAuth 交换没拿到（通常卡在 "
            "add-phone：OpenAI 要绑手机号才发 RT）。账号已注册、access_token 可用；"
            "要 RT 请配置接码平台后走「补 RT」，或改用「无 RT」模式。"
        )

    def _run_phone(self) -> RegistrationResult:
        """手机号注册。邮箱只在「手机注册 + 绑定邮箱」模式下才会去池子里领。"""
        bind_email = self.register_flow == REGISTER_FLOW_PHONE_WITH_EMAIL
        sms_callback = self._build_sms_callback()
        if sms_callback is None:
            return RegistrationResult(
                success=False,
                error_message=(
                    "手机注册需要接码平台：请先在「设置 → 接码」里启用接码并填好 API Key"
                ),
            )

        provider = self._build_mail_provider() if bind_email else None
        flow = self._build_flow(sms_callback=sms_callback)

        # 手机链路的每一步都只写在协议层的 logger 上，不镜像出来的话任务日志里
        # 只有"开始/失败"两行，出事了连服务端回的是哪个 page 都看不见。
        try:
            with mirror_protocol_logs(self.log):
                result = flow.run_phone_register(mail_provider=provider, bind_email=bind_email)
        except Exception as exc:
            registration = self._salvage(flow, exc)
            self._set_mailbox_status(provider, "used" if registration.success else "failed")
            return self._attach_mailbox_credentials(registration, provider)

        self._ensure_two_factor(flow, provider)
        registration = self._attach_two_factor(
            RegistrationResult.from_auth_result(result, source="phone_register")
        )
        self._set_mailbox_status(provider, "used")
        registration = self._attach_mailbox_credentials(registration, provider)
        bind_error = getattr(flow, "_bind_email_error", "")
        if bind_email and not registration.metadata.get("bound_email"):
            registration.metadata["bind_email_error"] = bind_error or "未绑定邮箱"
            self.log(f"手机号注册成功但邮箱未绑上：{bind_error or '服务端未提供 add-email 步骤'}")
        return registration

    def _build_mail_provider(self) -> MailboxProviderAdapter:
        return MailboxProviderAdapter(
            self.mailbox,
            kind=self.mailbox_kind,
            fixed_email=self.email,
            pooled=True,
            ephemeral=not self.email,
            otp_timeout=self._otp_timeout(),
        )

    def _attach_mailbox_credentials(
        self,
        registration: RegistrationResult,
        provider: Optional[MailboxProviderAdapter],
    ) -> RegistrationResult:
        """保留号池收件凭据，供补 RT / 补 2FA 按地址找回邮箱。"""
        account = getattr(provider, "account", None)
        if account is None:
            mailbox = getattr(provider, "_mailbox", None)
            account = getattr(mailbox, "account", None)
        source = getattr(account, "extra", None)
        if not isinstance(source, dict):
            return registration

        keys = (
            "mail_provider",
            "provider",
            "account_type",
            "password",
            "client_id",
            "refresh_token",
            "mailapi_url",
            "mailbox_token",
        )
        for key in keys:
            value = source.get(key)
            if value not in (None, "", False):
                registration.metadata.setdefault(key, value)

        # OutlookMailbox 的内部凭据使用 provider=microsoft；补 RT 解析器识别
        # 的是 mail_provider，统一补一份标准字段。
        if "mail_provider" not in registration.metadata:
            provider_name = str(source.get("provider") or "").strip()
            if provider_name:
                registration.metadata["mail_provider"] = provider_name
        return registration

    @staticmethod
    def _set_mailbox_status(
        provider: Optional[MailboxProviderAdapter], status: str
    ) -> None:
        if provider is None:
            return
        mailbox = getattr(provider, "_mailbox", None)
        account = getattr(provider, "account", None)
        setter = getattr(mailbox, "set_account_status", None)
        if callable(setter) and account is not None:
            try:
                setter(account, status)
            except Exception:
                logger.debug("更新邮箱池状态失败", exc_info=True)

    def _build_flow(self, sms_callback: Optional[object] = None) -> AuthFlow:
        flow = AuthFlow(
            Config(proxy=self.proxy),
            sms_callback=sms_callback if sms_callback is not None else self._build_sms_callback(),
            env_overrides=self._env_overrides(),
            on_password=self._on_password,
            # 不开 2FA 就一个钩子都不挂：挂上会顺带关掉 callback 前那次 Codex 抢跑
            on_session_ready=self._on_session_ready if self.bind_2fa else None,
        )
        self.flow = flow
        return flow

    # ── 协议层注入点 ──

    def _on_password(self, email: str, password: str) -> None:
        """密码在 OpenAI 侧一生效就记下来。

        协议层是在 ``POST user/register`` 成功后立刻回调的，此时账号已经在
        OpenAI 那边建好了。后续任何一步失败（最常见的是 OTP 超时），这行日志
        就是这个号唯一的线索 —— 没有它，号既登不进去也找不回来。
        """
        self.password = password
        self.log(f"密码已在 OpenAI 侧生效: {email} / {password}")

    def _on_session_ready(self, flow: AuthFlow, access_token: str) -> None:
        """session 一到手就绑 2FA（Codex 授权之前）。

        这是快路径：注册链几十秒前刚做完验证，服务端认这是"最近认证过"，直接
        enroll 就行，不用重跑登录链、不用再收一封邮件。
        """
        self.log("绑定 2FA：复用注册会话直接申请 TOTP 密钥…")
        result = bind_totp_inline(flow, access_token, on_secret=self._journal_secret(flow))
        # enroll 内部已经写过一次，这里再兜一道：密钥丢了这个号的 2FA 就永久锁死，
        # 不值得赌它一定被写进去了
        if result.secret:
            flow.result.totp_secret = result.secret
        self._record_two_factor(result)

    def _journal_secret(self, flow: AuthFlow) -> Optional[Callable[[str], None]]:
        """注册链的密钥写前日志回调。

        注册链拿到密钥时账号行**还不存在**（号要等整条链跑完才落库），所以回调
        只写文件日志 —— 启动时 ``recover_pending_totp_secrets`` 会在账号行出现
        之后把它补进库。这一环专治「注册成功、但绑 2FA 那步崩了」：此时号已经
        在 OpenAI 那边带上了一个 pending factor，没有密钥就再也进不去了。
        """
        email = ""
        for candidate in (
            getattr(flow.result, "bound_email", "") or "",
            getattr(flow.result, "email", "") or "",
        ):
            if "@" in str(candidate):
                email = str(candidate).strip()
                break
        if not email:
            return None

        def _journal(secret: str) -> None:
            from services.chatgpt_two_factor import _write_secret_journal

            _write_secret_journal(email, secret)
            self.log("[2FA] 密钥已写入写前日志（账号落库后自动补写）")

        return _journal

    def _ensure_two_factor(self, flow: AuthFlow, mail_provider: Optional[MailboxProviderAdapter]) -> None:
        """快路径没绑上时回落到重新登录再绑。

        慢路径要一次 PoW、可能还要一封验证码邮件，所以只在真有必要时跑：号已经
        绑过、或者身份是手机号（登录链认邮箱）都直接跳过。
        """
        if not self.bind_2fa:
            return
        if flow.result.totp_secret or (self.two_factor and self.two_factor.already_bound):
            return

        email = (getattr(flow.result, "bound_email", "") or flow.result.email or "").strip()
        password = (flow.result.password or self.password or "").strip()
        if "@" not in email:
            self.log("绑定 2FA：账号没有邮箱身份，无法重新登录补绑")
            return
        if not password:
            self.log("绑定 2FA：没有密码，无法重新登录补绑")
            return

        self.log("绑定 2FA：快路径没成，改走重新登录再绑（会多花一次 PoW，可能要收一封验证码）…")
        if mail_provider is not None:
            # 注册那封码信还躺在收件箱里，不重新打基线会被当成绑定链的码用
            mail_provider.prime()
        result = bind_totp_via_login(
            Config(proxy=self.proxy),
            email,
            password,
            mail_provider=mail_provider,
            env_overrides=self._env_overrides(),
            on_secret=self._journal_secret(flow),
        )
        self._record_two_factor(result)
        if result.secret:
            flow.result.totp_secret = result.secret

    def _record_two_factor(self, result: TwoFactorBindResult) -> None:
        self.two_factor = result
        if result.ok:
            # 密钥只下发这一次，任务日志是用户导入验证器的唯一途径
            self.log(f"2FA 绑定成功，TOTP 密钥: {result.secret}")
        else:
            self.log(f"绑定 2FA：{result.summary()}")

    def _attach_two_factor(self, registration: RegistrationResult) -> RegistrationResult:
        """把绑定结果记进 metadata，账号表里能看出这号试过没、成没成。"""
        if self.two_factor is None:
            return registration
        registration.metadata["chatgpt_2fa"] = {
            "bound": self.two_factor.ok or self.two_factor.already_bound,
            "message": self.two_factor.summary(),
        }
        return registration

    def _build_sms_callback(self):
        settings = resolve_sms_settings(self.extra_config)
        controller = build_phone_callback(
            settings,
            log_fn=lambda message: self.log(f"[接码] {message}"),
            proxy=self.proxy,
        )
        if controller is not None:
            self.log(f"手机接码已启用: {controller.provider_key}")
        return controller

    def _env_overrides(self) -> dict:
        """把本仓库的配置翻译成协议层认识的开关。"""
        overrides: dict[str, str] = {
            "OTP_TIMEOUT": str(self._otp_timeout()),
            # 邮箱池里的地址常被 OpenAI 判成"已有账号"（二手号或 passwordless_signup
            # 流程），默认走 OTP 登录拿凭证而不是直接判失败 —— 单任务场景下 fast-fail
            # 没有意义，外层本来就会换下一个邮箱重试。
            "WEBUI_ALLOW_LOGIN": "1",
        }

        if self.mode == REGISTRATION_MODE_ACCESS_TOKEN_ONLY:
            # 不要 refresh_token 就别跑 Codex OAuth：每次都要多花约 10 秒且必然告警
            overrides["OAUTH_CODEX_RT_EXCHANGE"] = "0"
            overrides["OAUTH_CODEX_RT_BEFORE_CALLBACK"] = "0"

        for config_key, env_key in (
            ("sms_per_phone_timeout", "OPENAI_PHONE_OTP_TIMEOUT"),
            ("sms_max_phone_attempts", "OPENAI_PHONE_MAX_ATTEMPTS"),
            ("sms_code_retries_per_phone", "OPENAI_PHONE_OTP_CODE_RETRIES"),
            ("chatgpt_phone_number", "OPENAI_PHONE_NUMBER"),
        ):
            value = str(self.extra_config.get(config_key) or "").strip()
            if value:
                overrides[env_key] = value

        return overrides

    def _otp_timeout(self) -> int:
        return resolve_mailbox_otp_timeout(self.extra_config)

    # ── 部分成功的抢救 ──

    def _salvage(self, flow: AuthFlow, exc: Exception) -> RegistrationResult:
        """流程末段炸了但凭证已经到手时，别把号一起扔掉。

        典型场景是 Codex OAuth 交换失败：access_token 和 session_token 早就拿到了，
        账号完全可用，只是没有 refresh_token。这种情况按成功处理，把缺什么记在
        metadata 里，比让调用方重跑一遍浪费一个邮箱划算。
        """
        result = flow.result
        has_credentials = bool(result.access_token or result.session_token or result.refresh_token)
        if not has_credentials:
            return RegistrationResult(
                success=False,
                email=getattr(result, "bound_email", "") or result.email or self.email,
                password=result.password or self.password,
                error_message=str(exc),
                retryable=bool(getattr(exc, "retryable", True)),
            )

        needs_refresh_token = self.mode == REGISTRATION_MODE_REFRESH_TOKEN
        partial = needs_refresh_token and not result.refresh_token
        if partial:
            self.log(f"注册末段异常但凭证部分可用（缺 refresh_token）: {exc}")
        else:
            self.log(f"注册末段异常但所需凭证已齐: {exc}")

        salvaged = RegistrationResult.from_auth_result(result)
        salvaged.metadata["partial"] = partial
        salvaged.metadata["last_error"] = str(exc)
        # 炸在 2FA 绑定之后是常态（Codex 换 RT 那步最容易出事），绑定结论得跟着号走，
        # 否则账号页会把一个已经绑好的号显示成没绑过
        return self._attach_two_factor(salvaged)
