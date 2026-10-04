"""手机号注册链路，以及注册后把邮箱绑到账号上的那几步。

和邮箱注册的区别只在"身份"这一段：``authorize/continue`` 提交的
``username.kind`` 是 ``phone_number``，验证码走短信而不是邮件。身份验完之后
（create_account → workspace/select → callback → session → Codex）两条链完全
一样，所以这里只实现前半段，后半段调 ``AuthFlow`` 已有的方法。

浏览器抓包下来，手机号这一段的接口顺序是：

    GET  /authorize?...&login_hint=%2B56...    → 302 落到 /create-account/password
    （login_hint 已经把身份带过去了，浏览器**不会**再打一次 authorize/continue）
    POST /api/accounts/user/register     {"username": "+56...", "password": "..."}
    （服务端要么直接把人放到 /contact-verification 并把短信发出去，要么先停在
      phone_otp_send，等客户端打一次 phone-otp/send 才发）
    GET  /api/accounts/phone-otp/send    整页跳转（不是 XHR），302 到 /contact-verification
    POST /api/accounts/phone-otp/validate {"code": "869328"}   referer=/contact-verification
    POST /api/accounts/create_account     {"name": ..., "birthdate": ...}  referer=/about-you

注意 ``contact_verification`` 就是手机验证码页面本身，不是异常状态——OpenAI 把
邮箱/手机的验证码页统一收到了 /contact-verification 这一个路由下。

绑定邮箱是独立的一段，OpenAI 把它放在 authorize 流程内部（页面 ``/add-email``）：

    POST /api/accounts/add-email/send      {"email": "..."}   不带 sentinel
    POST /api/accounts/email-otp/validate  {"code": "123456"} 不带 sentinel

第二步和邮箱注册用的是同一个接口，所以直接复用 ``AuthFlow.verify_otp``。
绑定只在服务端确实处于 add-email 步骤时才可能成功，其余时刻会被判 invalid
state —— 这类失败不该把已经注册好的号一起判死，调用方按"号留下、绑定失败"
处理。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from platforms.chatgpt.protocol.mail_provider import MailProvider
from platforms.chatgpt.protocol.response_summary import describe_error, describe_page

logger = logging.getLogger(__name__)

PHONE_USERNAME_KIND = "phone_number"

# 短信验证码页面：手机和邮箱的验证码页在 OpenAI 那边共用 /contact-verification
PHONE_VERIFICATION_REFERER = "https://auth.openai.com/contact-verification"

# 这个号不能用，换下一个再试。接码平台的号是回收再卖的，"已被占用"是常态，
# 服务端的说法有下划线（code）和大白话（message）两种，两种都得认。
_PHONE_REJECTED_PATTERNS = (
    "phone_number_already_in_use",
    "phone_number_in_use",
    "already_in_use",
    "already in use",
    "number_in_use",
    "already_taken",
    "already taken",
    "phone_already_verified",
    "already_verified",
    "disallowed_phone",
    "invalid_phone_number",
    "phone_number_invalid",
    "blocked_phone",
    "phone_number_blocked",
    "suspicious behavior from phone",
)

# 和号码无关的服务端状态错，换号只是按秒烧租号额度
_FLOW_STATE_PATTERNS = (
    "invalid authorization step",
    "invalid_authorization_step",
    "invalid state",
    "invalid_state",
)


def _matches(text: str, patterns: tuple) -> bool:
    lowered = (text or "").lower()
    return any(pattern in lowered for pattern in patterns)


def is_phone_rejected_error(text: str) -> bool:
    return _matches(text, _PHONE_REJECTED_PATTERNS)


def is_flow_state_error(text: str) -> bool:
    return _matches(text, _FLOW_STATE_PATTERNS)


class PhoneRegisterMixin:
    """手机号注册 + 绑定邮箱。混进 ``AuthFlow``，共用同一个 session 和结果对象。"""

    # 这一轮里接码平台有没有吐出过验证码。零短信是判定"号源被静默拦下"的唯一
    # 硬证据，外层要靠它决定还值不值得再开一轮。
    _phone_sms_ever_received = False

    # ── 状态判定 ──

    @staticmethod
    def _is_add_email_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "add_email") or ("/add-email" in cu)

    @staticmethod
    def _is_phone_otp_state(page_type: str = "", continue_url: str = "") -> bool:
        """短信已经发出去了，可以开始等码。

        ``contact_verification`` 是手机号注册链路上真正的验证码页（浏览器里
        phone-otp/validate 的 referer 就是它），和绑手机用的
        ``phone_otp_verification`` 等价，别当成异常状态。
        """
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (
            pt in ("phone_otp_verification", "contact_verification", "phone_verification")
            or "phone-verification" in cu
            or "contact-verification" in cu
        )

    @staticmethod
    def _is_phone_otp_send_state(page_type: str = "", continue_url: str = "") -> bool:
        """服务端把流程停在"该发短信了"，等我们自己去打发码接口。

        和邮箱注册同构：``POST user/register`` 成功后服务端切到 email_otp_send，
        由客户端主动 ``GET /api/accounts/email-otp/send`` 才真正发出验证码。
        手机号这条链停在 phone_otp_send 上，发码接口是 phone-otp/send —— 这时候
        去打 add-phone/send 会被判 invalid authorization step，因为那是另一个步骤。
        """
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "phone_otp_send") or ("phone-otp/send" in cu)

    @staticmethod
    def _is_forward_state(page_type: str = "", continue_url: str = "") -> bool:
        """服务端已经放行到验证之后的步骤，不该再纠结发码。"""
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (
            pt in ("about_you", "create_account", "add_email", "external_url", "oauth_consent")
            or "/about-you" in cu
            or "/add-email" in cu
            or "/consent" in cu
            or "/auth/callback" in cu
        )

    @staticmethod
    def _is_create_password_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "create_account_password") or ("/create-account/password" in cu)

    @staticmethod
    def _is_existing_identity_state(page_type: str = "", continue_url: str = "") -> bool:
        """服务端认这个号已经有账号了（要密码或要登录验证码）。"""
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (
            pt in ("login_password", "mfa_challenge")
            or "/log-in/password" in cu
            or "/mfa-challenge/" in cu
        )

    # ── 手机号身份 ──

    def phone_authorize_continue(self, phone_number: str, sentinel_token: str) -> dict:
        """把手机号当成注册身份提交。"""
        return self.authorize_continue(
            email=phone_number,
            sentinel_token=sentinel_token,
            screen_hint="signup",
            referer="https://auth.openai.com/log-in?usernameKind=phone_number",
            trace_step="authorize_continue_phone_signup",
            username_kind=PHONE_USERNAME_KIND,
        )

    def phone_register_user(self, phone_number: str) -> dict:
        """``POST user/register``，用户名就是手机号。成功即代表账号已在 OpenAI 侧建好。"""
        password = self._random_password()
        self.result.password = password

        if self.result.device_id:
            try:
                from platforms.chatgpt.protocol.sentinel import get_sentinel_token as _get_st

                token, so_token = _get_st(
                    self.session,
                    device_id=self.result.device_id,
                    flow="username_password_create",
                    **self._sentinel_fp_kwargs(),
                )
                self._last_sentinel_token = token or ""
                if so_token:
                    self._last_sentinel_so_token = so_token
            except Exception as exc:
                logger.warning("注册前刷新 sentinel 失败，沿用现有 token 提交: %s", exc)

        headers = self._common_headers("https://auth.openai.com/create-account/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token

        resp = self.session.post(
            "https://auth.openai.com/api/accounts/user/register",
            headers=headers,
            json={"password": password, "username": phone_number},
            timeout=30,
        )
        self._trace_http("phone_register_user", resp)
        if resp.status_code != 200:
            raise RuntimeError(
                f"手机号注册失败: HTTP {resp.status_code} - {describe_error(resp.text)}"
            )

        # 密码只活在内存里，后面任何一步挂掉这个号就再也登不进去了，先落盘。
        if self._on_password is not None:
            try:
                self._on_password(phone_number, password)
            except Exception as exc:
                logger.warning("密码落盘回调失败（不影响注册，日志里还有兜底）: %s", exc)

        try:
            return resp.json() or {}
        except Exception:
            return {}

    def _phone_send_navigation_headers(self, referer: str) -> dict:
        """GET phone-otp/send 那一枪的头：整页跳转，不是 XHR。

        抓包里这一步是浏览器把地址栏换过去（Accept: text/html、
        Sec-Fetch-Dest: document、Sec-Fetch-Mode: navigate、带
        upgrade-insecure-requests，既没有 Origin 也没有 Content-Type），
        服务端 302 回 /contact-verification 才算这一步走完。按 XHR 发过去
        形状对不上，短信就发不出来。
        """
        headers = self._navigation_headers()
        headers["Referer"] = referer
        # auth.openai.com 站内跳转；302 跟随不是用户点击，不发 sec-fetch-user
        headers["sec-fetch-site"] = "same-origin"
        headers.pop("sec-fetch-user", None)
        return headers

    @staticmethod
    def _landed_auth_page(resp) -> str:
        """跟完 302 之后停在哪张页面上。"""
        url = str(getattr(resp, "url", "") or "").split("?")[0].strip()
        if url.startswith("https://auth.openai.com/") and "/api/" not in url:
            return url
        return ""

    def send_phone_otp(self, phone_number: str, continue_url: str = "") -> dict:
        """让 OpenAI 把验证码发到这个号上。

        正路只有一条：按浏览器的样子整页跳到 phone-otp/send。两个 POST 是兜底，
        只在导航版没成的时候才轮到它们（服务端换形状时至少还有条活路）。
        ``continue_url`` 是服务端给的指引，指到哪个 send 就打哪个。
        每一发都记下服务端的原话，全失败时一起抛出去，免得只剩一句没有上下文的报错。

        这里只负责把码发出去一次，之后就是纯等：resend 换不来第二条短信。
        """
        send_url = "https://auth.openai.com/api/accounts/phone-otp/send"
        hinted = (continue_url or "").split("?")[0].strip()
        if hinted.startswith("/"):
            hinted = f"https://auth.openai.com{hinted}"
        if hinted.startswith("https://auth.openai.com/api/accounts/") and hinted.endswith("/send"):
            send_url = hinted

        body = {"phone_number": phone_number, "channel": "sms"}
        candidates = [
            ("GET", send_url, None, "https://auth.openai.com/create-account/password"),
            ("POST", send_url, body, "https://auth.openai.com/create-account/password"),
            (
                "POST",
                "https://auth.openai.com/api/accounts/add-phone/send",
                body,
                "https://auth.openai.com/add-phone",
            ),
        ]

        failures = []
        for method, url, payload, referer in candidates:
            try:
                if method == "GET":
                    resp = self.session.get(
                        url,
                        headers=self._phone_send_navigation_headers(referer),
                        timeout=30,
                        allow_redirects=True,
                    )
                else:
                    resp = self.session.post(
                        url, headers=self._phone_headers(referer), json=payload, timeout=30
                    )
            except Exception as exc:
                failures.append(f"{method} {url.rsplit('/', 2)[-2:]} 网络异常: {exc}")
                continue

            self._trace_http(f"phone_otp_send_{method.lower()}", resp)
            if resp.status_code == 200:
                raw = (resp.text or "").strip()
                try:
                    result = resp.json() or {}
                except Exception:
                    result = {}
                if not isinstance(result, dict):
                    result = {}
                if not result:
                    # 导航版回的是 /contact-verification 那张 HTML 页，没有 JSON，
                    # 落点 URL 就是服务端给的下一步
                    landed = self._landed_auth_page(resp)
                    if landed:
                        result = {"continue_url": landed}
                # HTTP 200 只代表服务端受理了这一步，真正指示下一步的是 page/continue；
                # 整段 JSON 留给 AUTH_HTTP_TRACE，别刷进任务日志。
                logger.info(
                    "[手机注册] 发码请求已被受理: %s %s → HTTP 200 %s",
                    method,
                    url,
                    describe_page(result) or "(无 page 信息)",
                )
                # HTML 落地页里的 whatsapp/voice 是页面上的其它选项，不是本次通道
                if raw.startswith("{"):
                    self._warn_if_not_sms_channel(raw)
                return result

            detail = describe_error(resp.text)
            failures.append(f"{method} {url} → HTTP {resp.status_code} {detail}")
            logger.warning(
                "[手机注册] 发码未成功: %s %s → HTTP %s %s", method, url, resp.status_code, detail
            )

        raise RuntimeError("触发短信验证码失败；" + " | ".join(failures))

    @staticmethod
    def _warn_if_not_sms_channel(body: str) -> None:
        """服务端如果说了走 WhatsApp / 语音，就别让人对着"发码成功"干等 120 秒。"""
        lowered = (body or "").lower()
        for keyword, channel in (
            ("whatsapp", "WhatsApp"),
            ("voice", "语音电话"),
            ("flash_call", "闪信来电"),
            ("flashcall", "闪信来电"),
        ):
            if keyword in lowered:
                logger.warning(
                    "[手机注册] 服务端提到 %s 通道：验证码可能不是以短信发出的，"
                    "接码平台收不到属正常，换 52（泰国）这类纯短信号段再试",
                    channel,
                )
                return

    # ── 绑定邮箱 ──

    def add_email_send(self, email: str) -> dict:
        """把邮箱提交给 add-email 步骤，OpenAI 会往这个地址发 6 位验证码。"""
        headers = self._common_headers("https://auth.openai.com/add-email")
        headers["Accept"] = "application/json"
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/add-email/send",
            headers=headers,
            json={"email": email},
            timeout=30,
        )
        self._trace_http("add_email_send", resp)
        if resp.status_code != 200:
            raise RuntimeError(
                describe_error(resp.text)
                or f"add-email/send 失败: HTTP {resp.status_code}"
            )
        try:
            return resp.json() or {}
        except Exception:
            return {}

    def bind_email(self, mail_provider: MailProvider, continue_url: str = "") -> str:
        """要一个邮箱 → 发码 → 验码。返回下一步的 continue_url。"""
        email = mail_provider.create_mailbox()
        logger.info("[绑定邮箱] 准备把 %s 绑到当前账号", email)

        sent_at = time.time()
        send_resp = self.add_email_send(email)
        next_url = self._normalize_continue_url(
            self._extract_continue_url_from_step(send_resp)
        )
        page_type = self._extract_page_type(send_resp)
        if not self._is_email_verification_state(page_type, next_url):
            logger.warning(
                "[绑定邮箱] add-email/send 未进入邮箱验证页: page=%s continue=%s",
                page_type or "(empty)",
                (next_url or "")[:160],
            )

        try:
            otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "180")))
        except Exception:
            otp_timeout = 180

        code = mail_provider.wait_for_otp(email, timeout=otp_timeout, issued_after=sent_at)
        try:
            validate_resp = self.verify_otp(code)
        except RuntimeError as exc:
            if not any(status in str(exc) for status in ("401", "409")):
                raise
            # 错码/过期码：重新发一封再等一次，别为此丢掉整个号。
            # 重试要排除刚试过的码，否则会把同一封信再读一遍、拿同一个错码
            # 再验一次（与注册链路同因，实测撞上 max_check_attempts）。
            logger.warning("[绑定邮箱] 验证码校验失败，重发一次再试: %s", exc)
            sent_at = time.time()
            self.add_email_send(email)
            code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=sent_at,
                exclude_codes={code},
            )
            validate_resp = self.verify_otp(code)

        self.result.bound_email = email
        logger.info("[绑定邮箱] %s 绑定成功", email)
        return self._normalize_continue_url(
            self._extract_continue_url_from_step(validate_resp)
        ) or next_url or continue_url or ""

    @staticmethod
    def _is_email_verification_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (
            pt in ("email_otp_verification", "contact_verification", "external_url")
            or "email-verification" in cu
            or "contact-verification" in cu
        )

    def _try_bind_email(self, mail_provider: Optional[MailProvider], continue_url: str) -> str:
        """绑定失败不判死账号：号已经注册好了，缺个邮箱而已。"""
        if mail_provider is None:
            return continue_url
        if self.result.bound_email:
            return continue_url
        try:
            return self.bind_email(mail_provider, continue_url=continue_url)
        except Exception as exc:
            self._bind_email_error = str(exc)
            logger.warning("[绑定邮箱] 失败（账号本身已注册成功，可稍后重试绑定）: %s", exc)
            return continue_url

    # ── 主流程 ──

    def run_phone_register(
        self,
        mail_provider: Optional[MailProvider] = None,
        bind_email: bool = False,
    ) -> Any:
        """用接码平台的手机号注册一个账号，可选把邮箱绑上去。"""
        if self._sms_callback is None:
            raise RuntimeError(
                "手机注册需要接码平台：请先在「设置 → 接码」里启用并填好 API Key"
            )
        self._bind_email_error = ""
        self._phone_verification_referer = PHONE_VERIFICATION_REFERER
        binder = mail_provider if bind_email else None

        if not self.check_proxy():
            logger.warning("网络预检查未通过，继续尝试注册链路以获取精确错误...")
        if not self.warmup():
            raise RuntimeError(
                "warmup 失败：4 次重试均未拿到 chatgpt.com 的 oai-did cookie，"
                "继续注册必然 409 invalid_state（多为代理出口 IP 不通或被 CF 拦），"
                "请检查代理后重试"
            )

        ctrl = self._sms_callback
        try:
            continue_url, auth_url = self._do_phone_register_loop(ctrl)
        finally:
            for cleanup in (getattr(ctrl, "cleanup", None), getattr(ctrl, "_release_lock", None)):
                if callable(cleanup):
                    try:
                        cleanup()
                    except Exception:
                        pass

        # 手机验完之后服务端可能直接把 add-email 摆在下一步
        if self._is_add_email_state(continue_url=continue_url):
            continue_url = self._try_bind_email(binder, continue_url)

        if (not continue_url) or "/about-you" in continue_url:
            continue_url = self.create_account()
            if self._is_add_email_state(continue_url=continue_url):
                continue_url = self._try_bind_email(binder, continue_url)

        # 服务端没主动要求绑邮箱时，也在跟重定向链之前试一次：这一步只在
        # authorize 流程还停在 add-email 上时才会被接受，被拒了就当没绑。
        continue_url = self._try_bind_email(binder, continue_url)

        return self._finish_authorized_flow(continue_url, auth_url, mail_provider)

    def _do_phone_register_loop(self, ctrl) -> tuple:
        """一个号一个号地试：租号 → 提交身份 → 注册 → 收短信 → 验码。

        返回 ``(continue_url, auth_url)``；auth_url 留给后面 reauthorize 兜底用。
        """
        ctrl_cfg = getattr(ctrl, "config", None) or {}

        def _read_int(cfg_key: str, env_key: str, default: str, min_v: int = 1) -> int:
            raw = str(ctrl_cfg.get(cfg_key) or "").strip()
            if not raw:
                raw = self._get_env(env_key, default)
            try:
                return max(min_v, int(raw))
            except Exception:
                return int(default)

        per_phone_timeout = max(
            40, _read_int("sms_per_phone_timeout", "OPENAI_PHONE_OTP_TIMEOUT", "120", min_v=40)
        )
        max_phone_attempts = _read_int("sms_max_phone_attempts", "OPENAI_PHONE_MAX_ATTEMPTS", "3")
        max_code_retries = _read_int(
            "sms_code_retries_per_phone", "OPENAI_PHONE_OTP_CODE_RETRIES", "2"
        )

        logger.info(
            "[手机注册] 配置: 单号窗口=%ds 最多换号=%d 单号内验证重试=%d",
            per_phone_timeout,
            max_phone_attempts,
            max_code_retries,
        )

        last_err: Optional[Exception] = None
        repeated_err = ""
        repeated_count = 0
        self._phone_sms_ever_received = False

        for attempt in range(1, max_phone_attempts + 1):
            logger.info("[手机注册] 🔁 第 %d/%d 个号尝试...", attempt, max_phone_attempts)
            try:
                phone = ctrl.get_phone()
            except Exception as exc:
                last_err = exc
                logger.warning("[手机注册] 第 %d 个号租号失败: %s", attempt, exc)
                continue
            if not phone:
                last_err = RuntimeError("接码平台未返回手机号")
                continue

            self.result.phone_number = phone
            self.result.email = phone

            try:
                continue_url, auth_url = self._register_with_phone(phone, ctrl, per_phone_timeout, max_code_retries)
            except _PhoneUnusable as exc:
                last_err = exc.cause
                logger.warning("[手机注册] 号 %s 不可用，换下一个: %s", phone, str(exc.cause)[:200])
                try:
                    ctrl.mark_send_failed(str(exc.cause))
                except Exception:
                    pass
                self._cleanup_phone(ctrl)
                continue
            except PhoneAccountCreatedError as exc:
                # 账号已经建好了，换号只会再造一个孤号。这里既不上报"号码有问题"
                # （号码是好的，退款理由不成立），也不再往下试。
                logger.warning(
                    "[手机注册] 账号已在 OpenAI 侧创建但后续步骤失败，停止换号: %s", exc
                )
                # 这个号已经挂在那个半成品账号上了，外层再开一轮必须换新号，
                # 否则只会一直撞 phone_number_in_use
                try:
                    ctrl.stop_reuse(f"号 {phone} 已注册出账号")
                except Exception:
                    pass
                raise
            except _PhoneFlowBroken as exc:
                logger.warning(
                    "[手机注册] 服务端流程状态已失效（%s）：这不是号码问题，本轮到此为止",
                    str(exc.cause)[:200],
                )
                try:
                    ctrl.mark_send_failed(str(exc.cause))
                except Exception:
                    pass
                self._cleanup_phone(ctrl)
                raise exc.cause
            except Exception as exc:
                last_err = exc
                text = str(exc)[:300]
                logger.warning("[手机注册] 号 %s 失败（未识别错误）: %s", phone, text)
                try:
                    ctrl.mark_send_failed(text)
                except Exception:
                    pass
                self._cleanup_phone(ctrl)
                if text == repeated_err:
                    repeated_count += 1
                else:
                    repeated_err = text
                    repeated_count = 1
                if repeated_count >= 3:
                    logger.warning(
                        "[手机注册] 同一个错误连续 %d 个号了，判定与号码无关，停止换号",
                        repeated_count,
                    )
                    break
                continue

            try:
                ctrl.report_success()
            except Exception:
                pass
            return continue_url, auth_url

        if last_err:
            raise last_err
        raise RuntimeError(f"手机注册 {max_phone_attempts} 个号均失败")

    def _cleanup_phone(self, ctrl) -> None:
        """退掉当前这个号，下一轮 get_phone 才会租新的。"""
        try:
            ctrl.cleanup()
        except Exception:
            pass

    def _register_with_phone(
        self,
        phone: str,
        ctrl,
        per_phone_timeout: int,
        max_code_retries: int,
    ) -> tuple:
        """单个号码的完整尝试。

        号本身不行就抛 ``_PhoneUnusable``（换下一个）；``user/register`` 一旦成功，
        这个号在 OpenAI 那边就已经是一个真账号了，之后任何失败都改抛
        ``PhoneAccountCreatedError`` —— 换号重试只会再造一个没人认领的孤号。
        """
        # 每个号都重开一条 authorize 链：login_hint 带着号码，服务端状态也干净
        csrf_token = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf_token, login_hint=phone)
        device_id = self.auth_oauth_init(auth_url)
        landing_url = str(getattr(self, "_last_auth_landing_url", "") or "").strip()

        if self._is_create_password_state(continue_url=landing_url):
            # login_hint 已经把手机号交给服务端了，authorize 直接落在设密码页 ——
            # 抓包里浏览器此时不会再打一次 authorize/continue（也就不会为它算 PoW），
            # 多打那一枪等于把同一个身份提交两遍，服务端有理由判 invalid state。
            page_type = "create_account_password"
            continue_url = landing_url
            logger.info("[手机注册] authorize 已凭 login_hint 落到设密码页，跳过 authorize/continue")
        else:
            sentinel = self.get_sentinel_token(device_id)
            try:
                data = self.phone_authorize_continue(phone, sentinel)
            except Exception as exc:
                if is_phone_rejected_error(str(exc)):
                    raise _PhoneUnusable(exc)
                if is_flow_state_error(str(exc)):
                    raise _PhoneFlowBroken(exc)
                raise

            page_type = self._extract_page_type(data)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(data))
            logger.info(
                "[手机注册] authorize/continue 返回 page=%s continue=%s",
                page_type or "(empty)",
                (continue_url or "(empty)")[:160],
            )

        if self._is_existing_identity_state(page_type, continue_url):
            raise _PhoneUnusable(RuntimeError(f"手机号 {phone} 已被注册过（phone_number_already_in_use）"))

        if not self._is_create_password_state(page_type, continue_url):
            return (
                self._continue_after_phone_verified(
                    phone, ctrl, per_phone_timeout, max_code_retries, page_type, continue_url
                ),
                auth_url,
            )

        register_resp = self.phone_register_user(phone)
        page_type = self._extract_page_type(register_resp)
        continue_url = self._normalize_continue_url(
            self._extract_continue_url_from_step(register_resp)
        )
        logger.info(
            "[手机注册] user/register 成功，服务端下一步 page=%s continue=%s",
            page_type or "(empty)",
            (continue_url or "(empty)")[:160],
        )

        # 过了这一行，账号已经存在于 OpenAI，密码也已经回调落盘
        try:
            return (
                self._continue_after_phone_verified(
                    phone, ctrl, per_phone_timeout, max_code_retries, page_type, continue_url
                ),
                auth_url,
            )
        except PhoneAccountCreatedError:
            raise
        except Exception as exc:
            cause = getattr(exc, "cause", exc)
            raise PhoneAccountCreatedError(
                str(cause),
                phone=phone,
                password=self.result.password,
                sms_ever_received=self._phone_sms_ever_received,
            ) from cause

    def _continue_after_phone_verified(
        self,
        phone: str,
        ctrl,
        per_phone_timeout: int,
        max_code_retries: int,
        page_type: str,
        continue_url: str,
    ) -> str:
        """从"服务端给的下一步"推进到短信验证通过。"""
        if self._is_forward_state(page_type, continue_url):
            # 服务端没要求验短信就直接放行了，别再多发一条码去破坏状态
            logger.info(
                "[手机注册] 服务端未要求短信验证，直接进入下一步: page=%s", page_type or "(empty)"
            )
            return continue_url

        if self._is_phone_otp_state(page_type, continue_url):
            # user/register 直接落到验证码页，短信是服务端自己发的
            logger.info(
                "[手机注册] 服务端已把短信发出，进入验证码页: page=%s", page_type or "(empty)"
            )
            self._remember_phone_verification_page(continue_url)
        else:
            if not self._is_phone_otp_send_state(page_type, continue_url):
                logger.warning(
                    "[手机注册] 未识别的下一步 page=%s continue=%s，按「该发码了」处理",
                    page_type or "(empty)",
                    (continue_url or "(empty)")[:160],
                )
            send_resp = self.send_phone_otp(phone, continue_url)
            send_page = self._extract_page_type(send_resp)
            send_continue = self._normalize_continue_url(
                self._extract_continue_url_from_step(send_resp)
            )
            if self._is_forward_state(send_page, send_continue):
                return send_continue
            if not (self._is_phone_otp_state(send_page, send_continue) or not send_page):
                # 发码接口已经 200，码大概率已经在路上。这时候放弃等于白扔一个
                # 已经建好的账号，先照常等短信，验不过再按失败收场。
                logger.warning(
                    "[手机注册] 发码成功但下一步页面不认识: page=%s continue=%s，仍按等短信处理",
                    send_page or "(empty)",
                    (send_continue or "(empty)")[:160],
                )
            continue_url = send_continue or continue_url
            self._remember_phone_verification_page(send_continue)

        try:
            ctrl.mark_send_succeeded()
        except Exception:
            pass

        return self._wait_and_validate_sms(
            phone, ctrl, per_phone_timeout, max_code_retries, continue_url
        )

    def _remember_phone_verification_page(self, continue_url: str = "") -> None:
        """记下验证码页地址，validate 用它当 referer。"""
        url = (continue_url or "").split("?")[0].strip()
        if url.startswith("/"):
            url = f"https://auth.openai.com{url}"
        if url.startswith("https://auth.openai.com/") and "/api/" not in url:
            self._phone_verification_referer = url
        else:
            self._phone_verification_referer = PHONE_VERIFICATION_REFERER

    def _wait_and_validate_sms(
        self,
        phone: str,
        ctrl,
        per_phone_timeout: int,
        max_code_retries: int,
        continue_url: str,
    ) -> str:
        started = time.time()
        seen_codes: set = set()
        code_attempt = 0
        last_err: Optional[Exception] = None

        while time.time() - started < per_phone_timeout and code_attempt < max_code_retries:
            remaining = per_phone_timeout - (time.time() - started)
            if remaining < 10:
                break
            code_attempt += 1
            logger.info(
                "[手机注册] 号 %s 第 %d/%d 次等短信 (剩余 %ds)",
                phone,
                code_attempt,
                max_code_retries,
                int(remaining),
            )
            code = ctrl.get_code(timeout=int(remaining))
            if not code:
                break
            self._phone_sms_ever_received = True
            if code in seen_codes:
                logger.warning("[手机注册] 收到重复验证码 %s，跳过", code)
                continue
            seen_codes.add(code)

            try:
                validate_resp = self._phone_otp_validate(
                    code,
                    referer=getattr(self, "_phone_verification_referer", "")
                    or PHONE_VERIFICATION_REFERER,
                )
            except Exception as exc:
                last_err = exc
                logger.warning("[手机注册] 短信验证码校验失败 (code=%s): %s", code, str(exc)[:200])
                try:
                    ctrl.mark_code_failed(str(exc))
                except Exception:
                    pass
                continue

            next_url = self._normalize_continue_url(
                self._extract_continue_url_from_step(validate_resp)
            )
            next_page = self._extract_page_type(validate_resp)
            logger.info(
                "[手机注册] ✅ 手机号 %s 验证通过，服务端下一步 page=%s continue=%s",
                phone,
                next_page or "(empty)",
                (next_url or "(empty)")[:160],
            )
            if self._is_phone_otp_state(next_page, next_url):
                # 验完还停在验证码页说明没有后续指引，按浏览器的走法进 about-you
                return ""
            return next_url or ""

        raise _PhoneUnusable(last_err or TimeoutError(f"号 {phone} 在 {per_phone_timeout}s 内没收到短信"))

    def _finish_authorized_flow(
        self,
        continue_url: str,
        auth_url: str,
        mail_provider: Optional[MailProvider],
    ) -> Any:
        """身份验完之后的收尾：跟重定向链拿 callback、换 session、换 refresh_token。"""
        continue_url = self._normalize_continue_url(continue_url or "")
        if not continue_url:
            continue_url = self._reauthorize_for_session(auth_url) or ""

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
        callback_url = ""
        if continue_url:
            # 和 run_register 同一条规矩：挂了 session_ready 钩子（绑 2FA）就跳过这次
            # Codex 抢跑，否则 Codex 会跑到钩子前面去，指定的顺序等于没改。
            if (
                (not self.result.refresh_token)
                and self._on_session_ready is None
                and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1")
            ):
                # `speculative=True`：抢跑失败不占本轮名额（见该方法 docstring）。
                self.oauth_codex_rt_exchange(
                    mail_provider=mail_provider, speculative=True
                )
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)

        if (not refresh_only_mode) and callback_url:
            self._consume_callback_for_session(callback_url)

        if not refresh_only_mode:
            self.get_auth_session()

        # 钩子：session 到手、Codex 授权之前。失败绝不能拖垮已注册成功的号。
        if self._on_session_ready is not None and self.result.access_token:
            try:
                self._on_session_ready(self, self.result.access_token)
            except Exception as e:
                logger.warning(f"session_ready 回调失败（不影响注册）: {e}")

        if callback_url or continue_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_phone_register")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("手机注册流程完成，但未拿到 refresh_token/access_token")
        elif not self.result.is_valid():
            raise RuntimeError("手机注册流程完成，但未拿到有效凭证")

        logger.info("手机注册流程完成!")
        return self.result


class PhoneAccountCreatedError(RuntimeError):
    """账号已经在 OpenAI 侧建好，但后面的步骤没走完。

    这个号从此被那个账号占着，换号重试只会再造一个没人认领的孤号，所以整轮到
    此为止。报错里带上手机号和密码：这两样是把号找回来的唯一线索。
    """

    def __init__(
        self,
        reason: str,
        *,
        phone: str,
        password: str = "",
        sms_ever_received: Optional[bool] = None,
    ):
        self.phone = phone
        self.password = password
        self.reason = reason
        # 发码请求被受理、接码平台却一条短信都没收到 —— 这是号源被静默拦下，
        # 不是这一次运气差，所以标成"重开也是同样结局"让外层少赔几轮。
        #
        # 判据只有一个：这一轮从头到尾有没有从接码平台拿到过验证码。以前是拿
        # 错误文本猜的，"超时"两个字太宽 —— 换 RT 超时、网络超时都会被误判成
        # 号源问题，用户填的重试轮数就这么被无声地作废了。
        self.sms_never_arrived = "没收到短信" in reason and sms_ever_received is not True
        self.retryable = not self.sms_never_arrived
        detail = f"手机号 {phone} 的账号已在 OpenAI 侧创建"
        if password:
            detail += f"（密码 {password}）"
        hint = ""
        if self.sms_never_arrived:
            # 实测（尼日利亚 19 与泰国 52 都试过）：OpenAI 收下发码请求、返回 200，
            # 接码平台却全程 Waiting for SMS —— 短信根本没进这些虚拟号。
            # 换国家救不了，得换号源，所以别再让人对着国家号反复烧钱。
            hint = (
                "接码平台全程没收到任何短信：OpenAI 受理了发码请求但没往这个虚拟号发，"
                "接码平台的号被静默拦下是常态，换国家不一定有用，换号源/换更贵的实号池才有效。"
            )
        super().__init__(
            f"{detail}，但短信验证没走完：{reason}。"
            f"这个号已被该账号占用，重试会注册出新账号；"
            f"可以用手机号 + 密码单独登录把凭证补回来。{hint}"
        )


class _PhoneUnusable(Exception):
    """这个号码不能用，换下一个。"""

    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause


class _PhoneFlowBroken(Exception):
    """服务端流程状态坏了，换号没意义。"""

    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause
