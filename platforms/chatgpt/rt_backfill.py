"""给缺 refresh_token 的老号补 RT。

历史上有两类号会缺 RT：早期按 ``access_token_only`` 模式注册的，以及注册末段
Codex 交换失败被 ``registration_engine._salvage`` 抢救回来的（凭证可用但没 RT）。
这两类号本身是好的，只差最后那一次 Codex OAuth 交换。

按代价从低到高试两条路，第一条成了就不跑第二条：

    ① 会话复用：拿库里的 session_token/access_token 恢复登录态，直接跑
       ``oauth_codex_rt_exchange``。不发邮件、不碰密码页，十几秒完事，
       对风控几乎没有痕迹 —— 大部分号都能在这一步拿到 RT。

    ② 协议重登：会话过期时才走。邮箱 + 密码重新跑一遍 ``run_protocol_login``，
       开 ``OAUTH_REFRESH_ONLY`` 只要 RT 不折腾多余的 session 请求。这条路
       可能撞上邮箱 OTP —— 收不到码就明确报错，不要装作成功。

协议层不认识本仓库的库表，邮箱与接码都由调用方以注入点形式传进来。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Optional

from core.base_platform import resolve_mailbox_otp_timeout
from core.task_runtime import TaskInterruption
from platforms.chatgpt.protocol import AuthFlow, Config, MailProvider
from platforms.chatgpt.protocol.banned_signals import looks_like_banned
from platforms.chatgpt.protocol_log_relay import mirror_protocol_logs

logger = logging.getLogger(__name__)

STRATEGY_SESSION = "session"
STRATEGY_LOGIN = "login"
#: 走 platform client_id 的**密码登录**路线换 RT。
#:
#: 与 `STRATEGY_LOGIN`（"协议重登"）不是一回事：后者的终点仍然是 Codex 交换，
#: 只是把登录态先弄好；本策略换的是端点与 client_id，全程不碰 Codex、不碰
#: add-phone（2026-10-26 实测：新号 3 次、存量号 1 次拿到 RT）。
STRATEGY_PLATFORM_LOGIN = "platform_login"

# 两条路共用的协议开关：只为拿 RT，别的一律省掉。
# OAUTH_CODEX_RT_ALLOW_RETRY 打开是因为补 RT 场景下一轮里可能要试两次
# authorize（例如第一次被打回 /log-in），默认的"本轮只试一次"会把第二次吞掉。
_BASE_OVERRIDES = {
    "OAUTH_CODEX_RT_EXCHANGE": "1",
    "OAUTH_CODEX_RT_ALLOW_RETRY": "1",
}

_SESSION_OVERRIDES = {
    **_BASE_OVERRIDES,
    # 复用会话的全部意义就在于不重新登录，而 Codex authorize 默认带的
    # prompt=login 恰恰是"忽略现有会话，重走登录页"。这里必须清掉，否则
    # 第一次授权必然被打到 /log-in，白跑一趟才轮到去掉 prompt 的兜底。
    "OAUTH_CODEX_PROMPT": "",
}

_LOGIN_OVERRIDES = {
    **_BASE_OVERRIDES,
    # 只要 RT：跳过 get_auth_session 这些为了刷 session 才做的请求
    "OAUTH_REFRESH_ONLY": "1",
    "OAUTH_CODEX_RT_BEFORE_CALLBACK": "1",
    # 补 RT 的对象必然是已有账号，别让协议层把"这邮箱已注册"当失败
    "WEBUI_ALLOW_LOGIN": "1",
}


#: 密码登录路线**不需要** Codex 相关开关：它自己换端点与 client_id。
#: 关掉 Codex 交换是为了让 `run_protocol_login` 内部不要顺手多打一次。
_PLATFORM_LOGIN_OVERRIDES = {
    "OAUTH_CODEX_RT_EXCHANGE": "0",
    "OAUTH_REFRESH_ONLY": "1",
    "WEBUI_ALLOW_LOGIN": "1",
}


class MailboxUnavailableProvider(MailProvider):
    """收不到码的占位邮箱。

    补 RT 时账号邮箱未必还能读（临时邮箱早过期、微软号已从池里弹出）。这种情况
    仍然值得试一把密码登录 —— OpenAI 不是每次都要邮箱验证码。真要码时抛一个说
    人话的错误，好过让上层看到一个空洞的超时。
    """

    kind = "unavailable"
    display_name = "不可用邮箱"
    accepts_existing_account = True

    def __init__(self, address: str, reason: str = ""):
        self._address = address
        self._reason = reason or "没有找到这个邮箱对应的收件通道"

    def create_mailbox(self) -> str:
        return self._address

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 120,
        issued_after: Optional[float] = None,
        exclude_codes: Optional[set] = None,
    ) -> str:
        raise RuntimeError(
            f"OpenAI 要求邮箱验证码，但读不到 {email_addr} 的收件箱（{self._reason}）"
        )


@dataclass
class BackfillAttempt:
    """一条策略的执行结果，失败原因要能直接展示给用户。"""

    strategy: str
    ok: bool
    message: str = ""


@dataclass
class BackfillResult:
    """补 RT 的最终结果。

    ``success`` 只在真的拿到 refresh_token 时为 True；顺带刷新到的
    access_token / session_token 无论成败都带回来，调用方可以一起落库。
    """

    success: bool
    email: str = ""
    strategy: str = ""
    refresh_token: str = ""
    access_token: str = ""
    session_token: str = ""
    id_token: str = ""
    cookie_header: str = ""
    error_message: str = ""
    attempts: list[BackfillAttempt] = field(default_factory=list)
    #: 登录链认出「号没了」（用户要求：禁用靠登录流程发掘）。
    #: 命中后不再尝试后续策略 —— 号都废了，协议重登救不回，白耗一轮风控额度。
    banned: bool = False
    #: 设备标识（oai-did）：会话/重登沿用或收敛到的值，落库供后续复用。
    device_id: str = ""

    def summary(self) -> str:
        if self.success:
            label = "复用会话" if self.strategy == STRATEGY_SESSION else "协议重登"
            return f"补 RT 成功（{label}）"
        if self.banned:
            return f"账号已封禁：{self.error_message or '登录链认出账号已停用'}"
        return self.error_message or "补 RT 失败"


class RefreshTokenBackfiller:
    """把一个已注册但缺 RT 的号补齐 refresh_token。"""

    def __init__(
        self,
        *,
        email: str,
        password: str = "",
        session_token: str = "",
        access_token: str = "",
        device_id: str = "",
        totp_secret: str = "",
        proxy: Optional[str] = None,
        extra_config: Optional[dict] = None,
        mail_provider: Optional[MailProvider] = None,
        mail_unavailable_reason: str = "",
        allow_login: bool = True,
        log_fn: Optional[Callable[[str], None]] = None,
    ):
        self.email = (email or "").strip()
        self.password = (password or "").strip()
        self.session_token = (session_token or "").strip()
        self.access_token = (access_token or "").strip()
        self.device_id = (device_id or "").strip()
        self.totp_secret = (totp_secret or "").strip()
        self.proxy = (proxy or "").strip() or None
        self.extra_config = dict(extra_config or {})
        self.mail_provider = mail_provider
        self.mail_unavailable_reason = mail_unavailable_reason
        self.allow_login = allow_login
        self._log_fn = log_fn
        self.log = log_fn or logger.info
        # 当前正在跑的 flow，报错后还要从它身上把已到手的凭证捞回来
        self._active_flow: Optional[AuthFlow] = None

    # ── 主流程 ──

    def run(self) -> BackfillResult:
        if not self.email:
            return BackfillResult(success=False, error_message="账号没有邮箱，无法补 RT")

        result = BackfillResult(success=False, email=self.email)

        for strategy, runner in self._strategy_runners():
            skip_reason = self._skip_reason(strategy)
            if skip_reason:
                result.attempts.append(BackfillAttempt(strategy, False, skip_reason))
                continue

            self._active_flow = None
            failure = ""
            try:
                with mirror_protocol_logs(self._log_fn):
                    runner()
            except TaskInterruption:
                # 手动停止/跳过。不能按"这条策略失败了"处理往下走 —— 接着跑协议
                # 重登正是按停止的人想躲开的那几十秒。
                self.log("[补RT] 收到中断请求，不再尝试后续策略")
                raise
            except Exception as exc:
                failure = str(exc) or exc.__class__.__name__
                self.log(f"[补RT] {self._label(strategy)}报错: {failure}")
                # 「号没了」的措辞在任意一步出现都要当场定性：封号不进重试队列，
                # 也不值得再跑下一条策略（协议重登同样会被拒）。
                if looks_like_banned(failure):
                    result.banned = True
                    self.log("[补RT] 账号已封禁（登录链明确拒绝），不再尝试后续策略")

            # 即使抛了异常也要看一眼手上的凭证：RT 是在链路中段换到的，末段
            # 再炸（拉 session、写 cookie 之类）不该把已经到手的 RT 一起扔掉。
            if self._active_flow is not None:
                self._absorb(result, self._active_flow)

            if result.refresh_token:
                result.success = True
                result.strategy = strategy
                note = f"末段报错但 RT 已到手：{failure}" if failure else "拿到 refresh_token"
                result.attempts.append(BackfillAttempt(strategy, True, note))
                self.log(f"[补RT] {self._label(strategy)}成功: {self.email}")
                return result

            message = failure or "流程跑完但没拿到 refresh_token"
            self.log(f"[补RT] {self._label(strategy)}未果: {message}")
            result.attempts.append(BackfillAttempt(strategy, False, message))

            # 封号是终局结论：下一条策略（协议重登）只会被同样拒绝，
            # 再跑一遍纯属白耗一轮风控额度。到此为止。
            if result.banned:
                break

        result.error_message = self._compose_error(result)
        return result

    # ── 策略一：复用已有会话 ──

    def _try_session(self) -> None:
        self.log(f"[补RT] 尝试复用已有会话: {self.email}")
        flow = self._build_flow(_SESSION_OVERRIDES)
        self._active_flow = flow
        flow.from_existing_credentials(self.session_token, self.access_token, self.device_id)
        if not (flow.result.access_token or flow.result.session_token):
            raise RuntimeError("库里的 session/access token 已失效")
        # 授权链被打回 /log-in 时协议层会自己补一次登录，届时可能要邮箱验证码
        flow.oauth_codex_rt_exchange(mail_provider=self.mail_provider)

    # ── 策略二：协议重新登录 ──

    def _try_login(self) -> None:
        self.log(f"[补RT] 会话不可用，改走协议重登: {self.email}")
        flow = self._build_flow(_LOGIN_OVERRIDES)
        self._active_flow = flow
        # 设备标识复用：重登沿用注册时落库的 oai-did（会话复用那条路
        # `from_existing_credentials` 已复用；这里此前没有，等于每次重登
        # 都换一台「新设备」）。
        flow.seed_device_id(self.device_id)
        provider = self.mail_provider or MailboxUnavailableProvider(
            self.email, self.mail_unavailable_reason
        )
        flow.run_protocol_login(provider, self.email, self.password)

    # ── 组装 ──

    def _build_flow(self, overrides: dict) -> AuthFlow:
        flow = AuthFlow(
            Config(proxy=self.proxy),
            sms_callback=self._build_sms_callback(),
            env_overrides=self._env_overrides(overrides),
            account_callback=self._account_callback,
        )
        if self.totp_secret:
            flow.result.totp_secret = self.totp_secret
        return flow

    def _account_callback(self, email: str) -> dict:
        """协议层撞上 mfa-challenge 时来要 2FA 密钥。"""
        return {"password": self.password, "totp_secret": self.totp_secret}

    def _build_sms_callback(self):
        """Codex 授权链可能被打到 add-phone，配了接码就顺手过掉。"""
        from services.sms_service import build_phone_callback, resolve_sms_settings

        settings = resolve_sms_settings(self.extra_config)
        return build_phone_callback(
            settings,
            log_fn=lambda message: self.log(f"[补RT][接码] {message}"),
            proxy=self.proxy,
        )

    def _env_overrides(self, overrides: dict) -> dict:
        merged = dict(overrides)
        merged["OTP_TIMEOUT"] = str(self._otp_timeout())
        for config_key, env_key in (
            ("sms_per_phone_timeout", "OPENAI_PHONE_OTP_TIMEOUT"),
            ("sms_max_phone_attempts", "OPENAI_PHONE_MAX_ATTEMPTS"),
            ("sms_code_retries_per_phone", "OPENAI_PHONE_OTP_CODE_RETRIES"),
            ("chatgpt_phone_number", "OPENAI_PHONE_NUMBER"),
        ):
            value = str(self.extra_config.get(config_key) or "").strip()
            if value:
                merged[env_key] = value
        return merged

    def _otp_timeout(self) -> int:
        return resolve_mailbox_otp_timeout(self.extra_config)

    # ── 结果整理 ──

    def _strategy_runners(self):
        """按代价从低到高排：复用会话 → 协议重登 → 密码登录。

        密码登录放最后：它要重新走一遍 authorize + sentinel + 密码 + 2FA，
        比前两条都重；前面能成就不该跑它。

        绑定实例方法（`self._try_xxx`）而不是类上的裸函数 —— 后者丢 `self`，
        调用时直接 TypeError（实测踩过）。
        """
        return (
            (STRATEGY_SESSION, self._try_session),
            (STRATEGY_LOGIN, self._try_login),
            (STRATEGY_PLATFORM_LOGIN, self._try_platform_login),
        )

    def _strategy_order(self) -> list:
        return [strategy for strategy, _ in self._strategy_runners()]

    def _try_platform_login(self) -> None:
        """策略三：platform 密码登录路线换 RT（不碰 Codex、不碰 add-phone）。"""
        self.log(f"[补RT] 尝试密码登录路线: {self.email}")
        flow = self._build_flow(_PLATFORM_LOGIN_OVERRIDES)
        self._active_flow = flow
        # 设备标识复用：沿用注册时落库的 oai-did，别让重登看起来像新设备
        flow.seed_device_id(self.device_id)
        flow.result.email = self.email or flow.result.email
        if self.totp_secret:
            flow.result.totp_secret = self.totp_secret
        ok = self._run_platform_login(flow)
        if not ok:
            reason = getattr(flow, "last_platform_route_error", "") or "未知原因"
            raise RuntimeError(reason)

    def _run_platform_login(self, flow: AuthFlow) -> bool:
        """调用密码登录路线。

        单独一层是为了可测：测试替换本方法即可，不必让假 flow 假装整个协议栈
        —— 桩打在别处会让测试**真的发网络请求**（实测踩过：401 + 28 秒）。
        """
        return bool(flow.acquire_rt_via_platform_login(
            email=self.email, password=self.password, totp_secret=self.totp_secret,
        ))

    def _skip_reason(self, strategy: str) -> str:
        if strategy == STRATEGY_SESSION and not (self.session_token or self.access_token):
            return "库里没有 session_token / access_token，跳过会话复用"
        if strategy == STRATEGY_LOGIN:
            if not self.allow_login:
                return "已关闭协议重登"
            if not self.password:
                return "库里没有密码，无法协议重登"
        if strategy == STRATEGY_PLATFORM_LOGIN:
            if not self.password:
                return "库里没有密码，无法走密码登录路线"
            # 服务端若已绑 2FA 就会要动态码，而密钥只有本地有。缺了必然卡死，
            # 提前跳过并说明原因，比跑一轮再报「没拿到 code」清楚。
            if not self.totp_secret:
                return "库里没有 totp_secret，密码登录路线无法过 2FA"
        return ""

    def _absorb(self, result: BackfillResult, flow: AuthFlow) -> None:
        """把 flow 上拿到的凭证并进结果，只覆盖非空值。

        两条策略可能各拿到一半（会话复用刷新了 access_token 但没换到 RT，重登
        才拿到 RT），谁先跑到的不该被后面的空值抹掉。
        """
        auth = flow.result
        for attr in ("refresh_token", "access_token", "session_token", "id_token", "cookie_header", "device_id"):
            value = str(getattr(auth, attr, "") or "").strip()
            if value:
                setattr(result, attr, value)

    def _compose_error(self, result: BackfillResult) -> str:
        details = "；".join(
            f"{self._label(item.strategy)}：{item.message}"
            for item in result.attempts
            if not item.ok
        )
        return f"补 RT 失败（{details}）" if details else "补 RT 失败"

    @staticmethod
    def _label(strategy: str) -> str:
        return {
            STRATEGY_SESSION: "复用会话",
            STRATEGY_LOGIN: "协议重登",
            STRATEGY_PLATFORM_LOGIN: "密码登录",
        }.get(strategy, strategy)


def backfill_refresh_token(**kwargs) -> BackfillResult:
    """``RefreshTokenBackfiller(**kwargs).run()`` 的简写。"""
    return RefreshTokenBackfiller(**kwargs).run()
