"""给 ChatGPT 账号程序化绑定 TOTP 双因素。

一共三个接口：``enroll_totp`` 是真正干活的三步（查已绑 → enroll → activate），
``bind_totp_inline`` 和 ``bind_totp_via_login`` 只是两种「怎么弄到 access_token」
的办法，弄到之后动作完全一样。

    快路径 ``bind_totp_inline``
        复用刚跑完注册的那条 flow 和它的 access_token 直接 enroll。注册链几十秒前
        才做完 OTP 验证 + create_account，服务端眼里这就是"最近认证过"，
        enroll 要的 recent-auth 条件本来就满足。零 PoW、零邮件，几秒钟完事。

    慢路径 ``bind_totp_via_login``
        快路径失败、或者给库里的老号补绑时才走：新起一条 AuthFlow 重跑登录正式链
        （PoW + 可能一封验证码邮件），拿到 access_token 再 enroll。

★ secret 只在 enroll 响应里下发一次，服务端不存明文、任何接口都取不回。丢了
  等于这个号的 2FA 永久锁死。所以两条路都在拿到 secret 的第一时间写进
  ``flow.result.totp_secret``，调用方负责立刻落库。

★ 绑定即生效：之后该号所有登录都要过 mfa-challenge，靠
  ``AuthFlow.submit_mfa_totp`` 用同一个 secret 算码通过。
"""

from __future__ import annotations

import logging
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Callable, Optional

from platforms.chatgpt.protocol.auth_flow import AuthFlow
from platforms.chatgpt.protocol.banned_signals import looks_like_banned
from platforms.chatgpt.protocol.config import Config
from platforms.chatgpt.protocol.mail_provider import MailProvider
from platforms.chatgpt.protocol.response_summary import describe_error
from platforms.chatgpt.protocol.totp import totp_now

logger = logging.getLogger(__name__)

MFA_INFO_URL = "https://chatgpt.com/backend-api/accounts/mfa_info"
MFA_ENROLL_URL = "https://chatgpt.com/backend-api/accounts/mfa/enroll"
MFA_ACTIVATE_URL = "https://chatgpt.com/backend-api/accounts/mfa/user/activate_enrollment"

# 登录链走到一半发现号早就绑过了。这不是失败，调用方要能和真失败分开。
_ALREADY_BOUND = "该号已启用 2FA（登录直接进 mfa-challenge），无需重复绑定"


@dataclass
class TwoFactorBindResult:
    """绑定结果。

    ``already_bound`` 和失败要分开看：服务端不下发第二次 secret，已绑的号再
    enroll 一遍只会把它的验证器废掉，所以这种情况必须当"不用做"而不是"没做成"。
    """

    ok: bool = False
    secret: str = ""
    factor_id: str = ""
    already_bound: bool = False
    error_message: str = ""
    #: 登录链认出「号没了」（用户要求：禁用靠登录流程发掘）。
    banned: bool = False
    #: 设备标识（oai-did）：慢路径重登沿用/收敛到的值，落库供后续复用。
    device_id: str = ""

    def summary(self) -> str:
        if self.already_bound:
            return "账号已绑定 2FA（密钥无法从服务端取回）"
        if self.ok:
            return "2FA 绑定成功"
        if self.banned:
            return f"账号已封禁：{self.error_message or '登录链认出账号已停用'}"
        return self.error_message or "2FA 绑定失败"


def enroll_totp(
    flow: AuthFlow,
    access_token: str,
    *,
    on_secret: Optional[Callable[[str], None]] = None,
) -> TwoFactorBindResult:
    """查已绑 → enroll → activate → 复核。

    整条链只用 ``flow.session`` 和 Bearer token，不碰 authorize 状态机，所以
    快慢两条路都能直接调。

    ``on_secret`` 在**拿到密钥的那一刻**立刻回调（早于 activate）。密钥只在
    enroll 响应里下发一次、服务端取不回，而「拿到」与「调用方落库」之间隔着
    activate + 复核两次网络往返 —— 中间崩了、被中断了、或调用方忘了落库，
    这个号的 2FA 就永久锁死（实测踩过：手工调 API 时没走落库，密钥直接丢了）。
    回调抛异常不中断绑定（密钥还在 ``result.secret`` 里，调用方仍可补救），
    但要留一条 error 日志。
    """
    token = (access_token or "").strip()
    if not token:
        return TwoFactorBindResult(error_message="没有 access_token，无法调用 mfa 接口")

    already, probe_error = _probe_existing_totp(flow, token)
    if already:
        return TwoFactorBindResult(ok=False, already_bound=True)
    if probe_error:
        logger.info("[2fa] 已绑检查未给出结论（%s），继续尝试 enroll", probe_error)

    enrollment, error = _enroll(flow, token)
    if error:
        return TwoFactorBindResult(error_message=error)

    secret = enrollment["secret"]
    # 把 secret 挂到 result 上要抢在 activate 之前：activate 再失败这个号也已经
    # 有了一个待激活的 totp factor，密钥丢了就再也补不回来。
    flow.result.totp_secret = secret
    # 同一个理由，再往前抢一步：直接回调让调用方此刻就落库。
    _notify_secret(on_secret, secret)

    try:
        code = totp_now(secret)
    except Exception as exc:  # noqa: BLE001
        return TwoFactorBindResult(
            secret=secret, error_message=f"enroll 给的密钥算不出动态码（不是合法 Base32）: {exc}"
        )

    activated = _activate(flow, token, code, enrollment["session_id"])
    if activated:
        return TwoFactorBindResult(
            secret=secret, error_message=activated,
            device_id=str(getattr(flow.result, "device_id", "") or "").strip(),
        )

    _confirm(flow, token)
    return TwoFactorBindResult(
        ok=True, secret=secret, factor_id=enrollment["factor_id"],
        device_id=str(getattr(flow.result, "device_id", "") or "").strip(),
    )


def _notify_secret(on_secret: Optional[Callable[[str], None]], secret: str) -> None:
    """把密钥交给调用方的落库回调。回调失败只记日志，不中断绑定。"""
    if on_secret is None or not secret:
        return
    try:
        on_secret(secret)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[2fa] 密钥落库回调失败（密钥仍在 result.secret 里，请调用方补救）: %s", exc
        )


def bind_totp_inline(
    flow: AuthFlow,
    access_token: str = "",
    *,
    on_secret: Optional[Callable[[str], None]] = None,
) -> TwoFactorBindResult:
    """快路径：直接用注册那条 flow 绑，不重新登录。

    任何异常都收成失败结果返回 —— 这一步跑在"号已经注册成功"之后，绝不能因为
    没绑上 2FA 就把号一起丢掉，调用方看结果决定要不要回落慢路径。
    """
    try:
        token = (access_token or "").strip() or (flow.result.access_token or "").strip()
        if not token:
            return TwoFactorBindResult(error_message="注册会话没有 access_token")
        return enroll_totp(flow, token, on_secret=on_secret)
    except Exception as exc:  # noqa: BLE001
        return TwoFactorBindResult(error_message=f"快路径异常: {exc}")


def bind_totp_via_login(
    config: Config,
    email: str,
    password: str,
    *,
    mail_provider: Optional[MailProvider] = None,
    env_overrides: Optional[dict] = None,
    sms_callback: Optional[Any] = None,
    on_secret: Optional[Callable[[str], None]] = None,
    device_id: str = "",
) -> TwoFactorBindResult:
    """慢路径：新起一条 flow 重走登录正式链，拿到 access_token 再 enroll。

    独立实例意味着独立指纹；但设备标识（oai-did）要复用库里那份
    （`device_id`）—— 同一个账号重登却换「新设备」是要避免的风控特征
    （用户问题 2026-10-07「指纹能复用吗」）。批量补绑时指纹各异、设备标识
    随账号，与真实用户换浏览器版本但设备不变的行为一致。
    整条链跑下来要一次 PoW，低信任新号还会被要求收一封邮件验证码。
    """
    if not email or not password:
        return TwoFactorBindResult(error_message="缺邮箱或密码，无法重新登录绑定")

    try:
        flow = AuthFlow(
            config,
            sms_callback=sms_callback,
            env_overrides=dict(env_overrides or {}),
        )
        flow.seed_device_id(device_id)
        access_token, error = _login_for_access_token(flow, email, password, mail_provider)
        # 设备标识：无论成败都带回（没有存量时它是服务端收敛值，落库后
        # 后续登录才有稳定设备可复用）。
        flow_device_id = str(getattr(flow.result, "device_id", "") or "").strip()
        if error == _ALREADY_BOUND:
            return TwoFactorBindResult(already_bound=True, device_id=flow_device_id)
        if error:
            return TwoFactorBindResult(
                error_message=error, banned=looks_like_banned(error),
                device_id=flow_device_id,
            )
        result = enroll_totp(flow, access_token, on_secret=on_secret)
        if not result.device_id:
            result.device_id = flow_device_id
        return result
    except Exception as exc:  # noqa: BLE001
        message = str(exc) or exc.__class__.__name__
        # 登录链在任意一步撞上「号没了」的措辞都要定性为封禁（用户要求：
        # 禁用靠登录流程发掘）—— 普通失败与封禁在重试语义上不是一回事。
        return TwoFactorBindResult(
            error_message=f"重新登录绑定异常: {message}",
            banned=looks_like_banned(message),
        )


# ── mfa 接口三步 ──


def _mfa_headers(flow: AuthFlow, url: str, token: str, *, json_body: bool = False) -> dict:
    headers = flow._common_headers(url)
    headers["Authorization"] = f"Bearer {token}"
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _probe_existing_totp(flow: AuthFlow, token: str) -> tuple[bool, str]:
    """返回 ``(是否已绑 totp, 查不出来的原因)``。"""
    resp = flow.session.get(MFA_INFO_URL, headers=_mfa_headers(flow, MFA_INFO_URL, token), timeout=30)
    if resp.status_code != 200:
        return False, f"mfa_info {resp.status_code}"
    try:
        info = resp.json() or {}
    except Exception:
        return False, "mfa_info 响应不是 JSON"
    factors = info.get("factors") if isinstance(info.get("factors"), dict) else {}
    return bool(info.get("mfa_enabled") and factors.get("totp")), ""


def _enroll(flow: AuthFlow, token: str) -> tuple[dict, str]:
    """POST enroll，返回 ``({secret, session_id, factor_id}, 失败原因)``。"""
    logger.info("[2fa] 申请 TOTP 密钥（服务端只下发这一次）...")
    resp = flow.session.post(
        MFA_ENROLL_URL,
        headers=_mfa_headers(flow, MFA_ENROLL_URL, token, json_body=True),
        json={"factor_type": "totp"},
        timeout=30,
    )
    # 这条响应体里就是明文密钥，不进 trace dump —— 那个文件是给人翻着看的
    if resp.status_code != 200:
        return {}, f"enroll {resp.status_code}: {describe_error(resp.text)}"
    try:
        payload = resp.json() or {}
    except Exception:
        return {}, "enroll 响应不是 JSON"

    secret = str(payload.get("secret") or "").strip()
    session_id = str(payload.get("session_id") or "").strip()
    factor = payload.get("factor") if isinstance(payload.get("factor"), dict) else {}
    if not secret or not session_id:
        return {}, "enroll 响应里没有 secret/session_id"
    return {"secret": secret, "session_id": session_id, "factor_id": str(factor.get("id") or "")}, ""


def _activate(flow: AuthFlow, token: str, code: str, session_id: str) -> str:
    """POST activate_enrollment，成功返回空串。"""
    logger.info("[2fa] 提交动态码激活...")
    resp = flow.session.post(
        MFA_ACTIVATE_URL,
        headers=_mfa_headers(flow, MFA_ACTIVATE_URL, token, json_body=True),
        json={"code": code, "factor_type": "totp", "session_id": session_id},
        timeout=30,
    )
    flow._trace_http("mfa_activate_enrollment", resp)
    if resp.status_code == 200:
        return ""
    hint = "（429 是提交太频繁，等一个 30 秒窗口换新码再试）" if resp.status_code == 429 else ""
    return f"激活失败 {resp.status_code}: {describe_error(resp.text)}{hint}"


def _confirm(flow: AuthFlow, token: str) -> None:
    """复核一次 mfa_enabled。只记日志：enroll + activate 都 200 就算成功了。"""
    time.sleep(2)
    try:
        enabled, reason = _probe_existing_totp(flow, token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[2fa] 复核 mfa_info 异常（不影响已到手的密钥）: %s", exc)
        return
    if enabled:
        logger.info("[2fa] 复核通过：mfa_enabled=true")
    else:
        logger.warning("[2fa] enroll/activate 已 200，但复核未看到 mfa_enabled（%s）", reason or "服务端说未启用")


# ── 慢路径的登录链 ──


def _login_for_access_token(
    flow: AuthFlow,
    email: str,
    password: str,
    mail_provider: Optional[MailProvider],
) -> tuple[str, str]:
    """重走一遍登录正式链，返回 ``(access_token, 失败原因)``。

    水印在整条链开始前就打：服务端早在 oauth_init 阶段就可能把码发出去了，卡在
    authorize/continue 前一刻反而会把唯一那封带对码的信判成旧信。同一次 challenge
    内多封信的码是一样的，放宽窗口不会抓错。
    """
    chain_started_at = time.time()

    flow.check_proxy()
    if not flow.warmup():
        return "", "warmup 没拿到 oai-did cookie，登录链必然 409 invalid_state"

    # 这条链登录的必然是已有账号，发码要走 resend 复用同一个 challenge state
    flow._is_existing_account = True

    csrf = flow.get_csrf_token()
    auth_url = flow.get_auth_url(csrf, email=email)
    device_id = flow.auth_oauth_init(auth_url)
    flow.get_sentinel_token(device_id)

    step = flow.authorize_continue(
        email,
        flow._last_sentinel_token,
        screen_hint="login",
        referer="https://auth.openai.com/log-in",
        trace_step="bind_2fa",
    )
    page_type = flow._extract_page_type(step)
    continue_url = flow._normalize_continue_url(flow._extract_continue_url_from_step(step))

    if page_type == "login_password" or "/log-in/password" in continue_url:
        flow.session.get(
            f"https://auth.openai.com/log-in/password?email={urllib.parse.quote(email)}",
            headers=flow._common_headers("https://auth.openai.com/log-in/password"),
            timeout=30,
        )
        step = flow.login_password_verify(password)
        page_type = flow._extract_page_type(step)
        continue_url = flow._normalize_continue_url(flow._extract_continue_url_from_step(step))

    need_otp = (page_type == "email_otp_verification") or ("/email-verification" in (continue_url or ""))
    if need_otp:
        if mail_provider is None:
            return "", "服务端要求邮箱验证码，但没有可用的收件通道"
        otp_code = _obtain_otp(flow, mail_provider, email, chain_started_at)
        otp_resp = flow.verify_otp(otp_code)
        page_type = flow._extract_page_type(otp_resp)
        continue_url = flow._normalize_continue_url(flow._extract_continue_url_from_step(otp_resp))

    if not continue_url:
        return "", f"登录链没给出 continue_url（page={page_type or '未知'}）"
    if flow._is_mfa_challenge_state(page_type, continue_url):
        return "", _ALREADY_BOUND

    # 消费 callback 让 NextAuth 写 session cookie。**不能拿它的返回值当致命判据**：
    # 那是个「是否读到 cookie」的探测，而 curl_cffi 分域隔离时朴素读法读不到
    # （`_extract_session_cookie` 专门有三路兜底）。注册链与手机链都忽略返回值、
    # 直接走 `get_auth_session()` —— 这里原先把它当致命错误，于是「cookie 没读出来」
    # 被报成「拿不到会话」，整轮绑定白跑（实测日志：OTP 验证成功后卡在这一步）。
    if not flow._consume_callback_for_session(continue_url):
        logger.warning("[绑2FA] 消费 callback 未读到 session cookie，继续尝试拉 session")
    _session_token, access_token = flow.get_auth_session()
    if not access_token:
        return "", "登录完成但没拿到 access_token"
    return access_token, ""


def _obtain_otp(
    flow: AuthFlow,
    mail_provider: MailProvider,
    email: str,
    chain_started_at: float,
) -> str:
    """先瞄一眼信箱，没有再主动发码。

    走到这一步时服务端往往已经投了一两封码信（get_auth_url 带 login_hint 和
    authorize/continue 各触发一次），再 resend 一封纯属给风控送素材。绑定链的
    resend 只是把同一个 challenge 的码再投一遍、不改服务端状态，所以已投递的
    那封本来就有效，省掉无损。
    """
    try:
        peek = getattr(mail_provider, "peek_otp", None)
        if callable(peek):
            code = peek(email, issued_after=chain_started_at, wait=4)
            if code:
                return code
    except Exception as exc:  # noqa: BLE001
        logger.debug("[2fa] 预读验证码失败，改走主动发码: %s", exc)

    try:
        otp_timeout = max(10, int(flow._get_env("OTP_TIMEOUT", "60")))
    except Exception:
        otp_timeout = 60
    if not flow.kickoff_otp_delivery("existing_bind_2fa"):
        flow.send_otp(referer="https://auth.openai.com/email-verification")
    return mail_provider.wait_for_otp(email, timeout=otp_timeout, issued_after=chain_started_at)
