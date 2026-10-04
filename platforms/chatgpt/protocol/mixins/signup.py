"""SignupMixin：注册主链（csrf → auth_url → oauth_init → sentinel → signup →
密码 → OTP → verify_otp → create_account）。

从 3541 行的 auth_flow.py 拆出（纯搬家，方法体逐字节不变）。
这些方法大量互相 `self.xxx` 调用，且被 `AuthFlow.__new__(AuthFlow)` 风格的测试
直接调用，所以只搬位置、不改调用方式。
"""
from __future__ import annotations

import logging
import random
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlencode

from platforms.chatgpt.protocol.response_summary import describe_error

logger = logging.getLogger(__name__)


def _random_birthdate() -> str:
    """随机生日（YYYY-MM-DD）：年龄约束 20–45 岁，且必须是真实存在的日历日。

    相对当前年份计算（不写死年份，避免逐年漂移）；日取 1–28 保证所有月份
    都合法 —— 服务端会拒掉不存在的日期（如 2 月 30 日）。只服务 create_account
    的 about-you 表单；提出来是为了让年龄约束可被测试钉住。
    """
    current_year = datetime.now(timezone.utc).year
    year = random.randint(current_year - 45, current_year - 20)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return f"{year:04d}-{month:02d}-{day:02d}"


class SignupMixin:
    def get_csrf_token(self) -> str:
        logger.info("[1/10] 获取 CSRF Token...")
        headers = self._common_headers("https://chatgpt.com/auth/login")

        # Cloudflare 可能在短时间内多次请求后返回 403，重试 3 次
        for attempt in range(3):
            try:
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/csrf",
                    headers=headers,
                    timeout=30,
                )
            except Exception as e:
                if self._is_tls_error(e) and self._rotate_impersonate_session():
                    continue
                if self._is_tls_error(e):
                    raise RuntimeError(
                        "chatgpt.com TLS 握手失败，当前网络无法建立到 /api/auth/csrf 的 HTTPS 连接。"
                        "请切换可直连 chatgpt.com 的网络或在界面中配置可用代理后重试。"
                    ) from e
                raise
            if resp.status_code == 403 and attempt < 2:
                wait = (attempt + 1) * 5
                logger.warning(f"Cloudflare 403, {wait}s 后重试 ({attempt + 1}/3)...")
                import time
                time.sleep(wait)
                continue
            resp.raise_for_status()
            break

        self._trace_http("chatgpt_csrf", resp)
        csrf = resp.json().get("csrfToken", "")
        if not csrf:
            raise RuntimeError("CSRF Token 获取失败")
        self.result.csrf_token = csrf
        logger.debug(f"CSRF Token: {csrf[:20]}...")
        return csrf


    def get_auth_url(self, csrf_token: str, email: str = "", login_hint: str = "") -> str:
        logger.info("[2/10] 获取 OpenAI 授权地址...")
        headers = self._common_headers("https://chatgpt.com/auth/login")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        if not self.result.device_id:
            self.result.device_id = str(uuid.uuid4())
        query_params: dict[str, str] = {
            "prompt": "login",
            "screen_hint": "login_or_signup",
            "ext-oai-did": self.result.device_id,
            "auth_session_logging_id": str(uuid.uuid4()),
            "ext-passkey-client-capabilities": "1111",
        }
        hint = (login_hint or email or "").strip()
        if hint:
            query_params["login_hint"] = hint
        signin_url = f"https://chatgpt.com/api/auth/signin/openai?{urlencode(query_params)}"
        resp = self.session.post(
            signin_url,
            headers=headers,
            data={
                "csrfToken": csrf_token,
                "callbackUrl": "https://chatgpt.com/",
                "json": "true",
            },
            timeout=30,
        )
        resp.raise_for_status()
        self._trace_http("chatgpt_signin_openai", resp)
        auth_url = resp.json().get("url", "")
        if not auth_url:
            raise RuntimeError("Auth URL 获取失败")
        self._remember_oauth_params(auth_url)
        logger.debug(f"Auth URL: {auth_url[:80]}...")
        return auth_url


    def auth_oauth_init(self, auth_url: str) -> str:
        """跟随 authorize 链，落 authorize 会话状态并取回 oai-did。

        这一步**建立的就是后面 authorize/continue 要用的那个 state**，头不像真
        浏览器就拿不到有效状态，下一步必 409 invalid_state。

        旧实现只发 Accept/Referer/UA，缺 client hints、**整组 Sec-Fetch-* 也没有**
        （真浏览器跳转必带 document/navigate/cross-site）。2026-08-10 实测 A/B
        对照各 6 轮（400 invalid_username 视为会话正常，只是 .test 域名被拒）：

            A 现状裸头        会话正常 2/6，**409 = 3**
            B 补齐 CH+SecFetch 会话正常 5/6，**409 = 0**

        和 warmup 那处是同一个病（详见 warmup docstring），当时只修了 warmup，
        漏了这里，所以主人实跑仍 409。头统一从 _navigation_headers 派生，
        保证 client hints 与 self._fingerprint / self._ua 同族。
        """
        logger.info("[3/10] OAuth 初始化...")
        headers = self._navigation_headers()
        headers["Referer"] = "https://chatgpt.com/"
        # chatgpt.com -> auth.openai.com 是跨站跳转，不是首次直达
        headers["sec-fetch-site"] = "cross-site"
        # 302 自动跟随不是用户手动点击，真浏览器此时不发 sec-fetch-user
        headers.pop("sec-fetch-user", None)
        resp = self.session.get(auth_url, headers=headers, timeout=30, allow_redirects=True)
        self._trace_http("auth_oauth_init", resp)

        # 带 login_hint 时服务端会直接把人放到对应的页面（手机号注册就是
        # /create-account/password），落点决定了下一步还要不要提交身份。
        self._last_auth_landing_url = str(getattr(resp, "url", "") or "")

        # 从 cookie 获取 oai-did
        device_id = ""
        for cookie in self.session.cookies:
            if hasattr(cookie, "name"):
                if cookie.name == "oai-did":
                    device_id = cookie.value
                    break
            elif isinstance(cookie, str) and cookie == "oai-did":
                device_id = self._get_cookie_value_by_name("oai-did")
                break

        # curl_cffi cookies 访问方式
        if not device_id:
            device_id = self._get_cookie_value_by_name("oai-did")

        # fallback: 从 HTML 提取
        if not device_id:
            m = re.search(r'oai-did["\s:=]+([a-f0-9-]{36})', resp.text)
            if m:
                device_id = m.group(1)

        if not device_id:
            device_id = str(uuid.uuid4())
            logger.warning(f"未从响应中获取 device_id，使用生成值: {device_id}")

        self.result.device_id = device_id
        logger.debug(f"Device ID: {device_id}")
        return device_id


    def get_sentinel_token(self, device_id: str) -> str:
        logger.info("[4/10] 获取 Sentinel Token (PoW)...")
        from platforms.chatgpt.protocol.sentinel import get_sentinel_token
        result = get_sentinel_token(
            self.session,
            device_id=device_id,
            flow="authorize_continue",
            **self._sentinel_fp_kwargs(),
        )
        token, so_token = result
        self._last_sentinel_token = token or ""
        self._last_sentinel_so_token = so_token or ""
        logger.debug("Sentinel Token 获取成功")
        return token


    def authorize_continue(
        self,
        email: str,
        sentinel_token: str,
        screen_hint: str = "signup",
        referer: str = "https://auth.openai.com/create-account",
        trace_step: str = "",
        username_kind: str = "email",
    ) -> dict:
        """调用 /api/accounts/authorize/continue，返回 JSON。

        ``username_kind`` 决定这次提交的身份是邮箱还是手机号（``phone_number``），
        服务端按它决定后面走邮件验证码还是短信验证码。
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if sentinel_token:
            headers["openai-sentinel-token"] = sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        payload: dict = {
            "username": {"value": email, "kind": username_kind or "email"},
        }
        if screen_hint:
            payload["screen_hint"] = screen_hint
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/authorize/continue",
            headers=headers,
            json=payload,
            timeout=30,
        )
        self._trace_http(trace_step or f"authorize_continue_{screen_hint}", resp)
        if resp.status_code != 200:
            # 服务端的原话只取 message/code：整段 JSON 换行一多就把任务日志顶爆了
            reason = describe_error(resp.text)
            # 额外打日志：headers/req_id 帮排查是不是 IP 风控
            req_id = (resp.headers.get("x-request-id", "") or "")[:80]
            ct = (resp.headers.get("Content-Type", "") or "")[:60]
            logger.error(
                "authorize/continue 非 200: status=%s screen_hint=%s req_id=%s content_type=%s %s",
                resp.status_code, screen_hint, req_id, ct, reason,
            )
            raise RuntimeError(
                f"authorize/continue 失败(screen_hint={screen_hint}): "
                f"HTTP {resp.status_code} req_id={req_id} {reason}"
            )
        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}


    def signup(self, email: str, sentinel_token: str) -> bool:
        """提交注册邮箱。返回 True 表示走新注册流程，False 表示已有账号走 OTP 登录流程"""
        logger.info("[5/10] 提交注册邮箱...")
        data = self.authorize_continue(
            email=email,
            sentinel_token=sentinel_token,
            screen_hint="signup",
            referer="https://auth.openai.com/create-account",
            trace_step="authorize_continue_signup",
        )

        # 检测 page_type/continue_url，区分新账号与已有账号
        try:
            page = (data.get("page") or {}) if isinstance(data, dict) else {}
            page_type = (page.get("type") or "").strip()
            payload = (page.get("payload") or {}) if isinstance(page, dict) else {}
            continue_url = (data.get("continue_url") or "").strip()

            # 新账号标准分支
            if page_type == "create_account_password" or "/create-account/password" in continue_url:
                self._is_existing_account = False
                self._existing_email_verification_mode = ""
                self._existing_page_type = page_type
                logger.info("注册邮箱已提交")
                return True

            # OTP 验证分支（passwordless 新注册 或 已有账号登录）
            if page_type == "email_otp_verification":
                mode = (payload.get("email_verification_mode", "") or "").strip()
                self._existing_email_verification_mode = mode
                self._existing_page_type = page_type
                if mode == "passwordless_signup":
                    logger.info("服务端选择 passwordless 注册流程（新账号，无密码），等待 OTP")
                    self._is_existing_account = False
                else:
                    logger.info("检测到已有账号，切换到 OTP 登录流程")
                    self._is_existing_account = True
                return False

            # 未知 page_type：通常是社交登录/风控分支，按已有账号处理，避免误进 register_password 导致 invalid_state
            self._existing_email_verification_mode = (payload.get("email_verification_mode", "") or "").strip()
            self._existing_page_type = page_type
            self._is_existing_account = True
            logger.warning(
                "authorize/continue 返回非标准注册页面: page_type=%s continue_url=%s，按已有账号流程处理",
                page_type or "(empty)",
                continue_url[:180] or "(empty)",
            )
            return False
        except Exception:
            # JSON 解析失败时保守按新注册处理
            self._is_existing_account = False
            self._existing_email_verification_mode = ""
            self._existing_page_type = ""
            logger.info("注册邮箱已提交")
            return True


    def register_password(self, email: str) -> bool:
        logger.info("[5.5/10] 注册密码...")
        password = self._random_password()
        self.result.password = password

        # 先访问 create-account/password 页面（HAR 确认需要此步建立服务端状态）
        try:
            pw_page = self.session.get(
                "https://auth.openai.com/create-account/password",
                headers=self._common_headers("https://auth.openai.com/create-account"),
                timeout=15,
            )
            logger.info(f"create-account/password 页面: {pw_page.status_code}")
        except Exception as e:
            logger.warning(f"访问 create-account/password 页面失败: {e}")

        # 注册前需要刷新 sentinel token，且 flow 必须为 username_password_create
        #
        # ⚠️ SO token 只在**本次请求**范围内决定带不带，绝不回写实例上的
        #    _last_sentinel_so_token。原因：send_otp / verify_otp 这些后续步骤
        #    自己不刷 sentinel，直接复用实例字段。本 flow 服务端不要求 SO token
        #    （so_token 为空），要是把空值写回实例，等于顺手把后续所有请求的
        #    SO 头也一起摘掉了 —— 那几步的 flow 服务端是要 SO 的。
        so_token_for_request = getattr(self, "_last_sentinel_so_token", "")
        if self.result.device_id:
            try:
                from platforms.chatgpt.protocol.sentinel import get_sentinel_token as _get_st
                token, so_token = _get_st(self.session, device_id=self.result.device_id,
                                flow="username_password_create",
                                **self._sentinel_fp_kwargs())
                self._last_sentinel_token = token or ""
                so_token_for_request = so_token or ""
                if so_token:
                    self._last_sentinel_so_token = so_token
                logger.debug("Sentinel Token 获取成功")
            except Exception as e:
                # 注：username_password_create 这个 flow 服务端**不下发 so 块**
                #    （实测 2026-08-06，见 sentinel_quickjs.py 里的说明），
                #    所以 so_token 为空是正常的，已在 sentinel_quickjs 按服务端要求判定，
                #    不再走到这个 except。这里只兜网络/子进程一类的真异常。
                #    走到这里说明用的是上一步的 token，flow 对不上是风控特征，
                #    但比当场崩掉（POST 根本发不出去）强，故降级继续。
                logger.warning(
                    f"注册前刷新 sentinel 失败，将改用 flow 不匹配的现有 sentinel token 提交: {e}"
                )

        headers = self._common_headers("https://auth.openai.com/create-account/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        # 服务端对该 flow 没下发 so 块时 so_token_for_request 为空 → 不带这个头，
        # 与真实浏览器一致；不要退回实例字段拿别的 flow 的 SO token 来凑。
        if so_token_for_request:
            headers["openai-sentinel-so-token"] = so_token_for_request
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/user/register",
            headers=headers,
            json={"password": password, "username": email},
            timeout=30,
        )
        self._trace_http("register_password", resp)
        if resp.status_code != 200:
            logger.warning(f"密码注册返回 {resp.status_code}: {describe_error(resp.text)}")
            return False
        logger.info("密码注册成功")
        # ⚠️ 走到这里 = OpenAI 侧账号连同这个密码**已经建好了**，但注册流程后面还有
        #    发码 → 验证 OTP → create_account 三步，任何一步挂掉都到不了 save_registered。
        #    密码是这个方法现生成的、只活在内存里，进程一退就永久没了 ——
        #    号还在 OpenAI 那边好好的，却谁也登不进去（实测 2026-08-07 被 OTP 超时坑过一次）。
        #    所以在这里立刻回调落盘。
        #    位置刻意选在 POST 200 **之后**而不是生成密码时：POST 失败的密码
        #    OpenAI 侧根本没生效，写进库里反而误导人以为能用。
        if self._on_password is not None:
            try:
                self._on_password(email, password)
            except Exception as e:
                logger.warning(f"密码落盘回调失败（不影响注册，日志里还有兜底）: {e}")
        return True


    def send_otp(self, referer: str = "https://auth.openai.com/create-account/password"):
        logger.info(f"[6/10] 发送 OTP (referer={referer.split('/')[-1]})...")
        headers = self._common_headers(referer)
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        # zhuce6 用 GET /api/accounts/email-otp/send
        resp = self.session.get(
            "https://auth.openai.com/api/accounts/email-otp/send",
            headers=headers,
            timeout=30,
        )
        self._trace_http("send_email_otp", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"发送 OTP 失败: {resp.status_code} - {describe_error(resp.text)}")
        logger.info("OTP 已发送到邮箱")


    def send_passwordless_otp(self, referer: str = "https://auth.openai.com/create-account/password") -> bool:
        """
        走 passwordless 发码（create-account/password 页面可触发该路径）。
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/passwordless/send-otp",
            headers=headers,
            timeout=30,
        )
        self._trace_http("send_passwordless_otp", resp)
        if resp.status_code == 200:
            logger.info("passwordless OTP 已发送")
            return True
        logger.warning(f"passwordless 发码失败: {resp.status_code} - {describe_error(resp.text)}")
        return False


    def resend_otp(self, referer: str = "https://auth.openai.com/email-verification") -> bool:
        """
        重发 OTP（适用于已有账号 passwordless/login_challenge）。
        返回 True 代表请求成功。
        """
        headers = self._common_headers(referer)
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/resend",
            headers=headers,
            timeout=30,
        )
        self._trace_http("resend_email_otp", resp)
        if resp.status_code == 200:
            logger.info("OTP 已重发")
            return True
        logger.warning(f"重发 OTP 失败: {resp.status_code} - {describe_error(resp.text)}")
        return False


    def kickoff_otp_delivery(self, mode: str = "") -> bool:
        """
        统一发码策略, 根据 mode hint 区分"新注册" vs "已有账号" referer:

        - 新注册 (create-account/password 页面 state): passwordless/send-otp → email-otp/send
        - 已有账号 / passwordless_login / existing_*: send_otp(referer=email-verification) → resend_otp
          (绕开 passwordless/send-otp 在已有账号场景的 409 invalid_state)
        """
        mode_lc = (mode or "").strip().lower()
        is_existing = (
            "existing" in mode_lc
            or "passwordless_login" in mode_lc
            or "passwordless_signup" in mode_lc  # OpenAI 把 outlook 接码池都打这个 mode
            or self._is_existing_account
        )

        if is_existing:
            # 已有账号 passwordless_signup / passwordless_login: authorize/continue 已经在
            # OpenAI server 端 trigger 了发码 (state S, OTP X, 邮件 X 已在投递). 这里**只能 resend**
            # (复用同 challenge state, 复用同 OTP X 或派生新码但 state 不变). 不能调 send_otp,
            # 它会新建 challenge token 让 state 跳到 Y, 旧邮件 X 在 server 端立即失效 → IMAP 抓到 X
            # verify 时 wrong_email_otp_code.
            if self.resend_otp("https://auth.openai.com/email-verification"):
                return True
            # resend 失败兜底: send_otp 新建 challenge (旧 state 已坏, 不得不重启)
            logger.warning(f"已有账号 resend 失败, 兜底 send_otp 新建 challenge (邮件 X 将失效)")
            try:
                self.send_otp(referer="https://auth.openai.com/email-verification")
                return True
            except Exception as e:
                logger.warning(f"已有账号发码全 fail: {e}")
                return False

        # 新注册 (原顺序)
        if self.send_passwordless_otp("https://auth.openai.com/create-account/password"):
            return True
        if self.resend_otp("https://auth.openai.com/email-verification"):
            return True
        try:
            self.send_otp()
            return True
        except Exception as e:
            logger.warning(f"send_otp 兜底失败(mode={mode_lc or 'unknown'}): {e}")
            return False


    @staticmethod
    def _default_password_from_email(email: str) -> str:
        """⚠️ 这是**猜**出来的密码，不是这个号真的密码。

        只在实在拿不到真密码时用来碰一下运气（碰上早期用这个规则建的号）。
        调用方必须走 _resolve_login_password，别直接调这个 —— 见那边的注释。
        """
        pwd = (email or "").replace("@", "")
        if len(pwd) < 8:
            pwd = f"{pwd}2026OpenAI"
        return pwd


    def _resolve_login_password(self, email: str) -> tuple[str, bool]:
        """找出登录这个号该用的密码。返回 (密码, 是否为真密码)。

        真密码三个来源，按优先级：
            ① self.result.password —— 本轮 register_password 刚设的
            ② LOGIN_PASSWORD 环境变量 —— 主人手动指定
            ③ account_callback —— 数据库里存的（**重跑老号全靠这条**）
        三条都空才退到 _default_password_from_email 猜一个，此时第二个返回值 False。

        ⚠️ 猜出来的密码**绝不能**写回 self.result.password。那个字段有两个下游：
             · to_dict() → registrar 落库，会把假密码存成这个号的密码；
             · registrar 异常兜底那行「该号已生成密码，请自行留存」。
           实测：一个 passwordless 老号（从没设过密码）被打成「邮箱去掉 @」，
           照着存等于存了个死密码。所以赋值一律由调用方按 is_real 决定。
        """
        pwd = (self.result.password or "").strip()
        if pwd:
            return pwd, True
        pwd = self._get_env("LOGIN_PASSWORD", "").strip()
        if pwd:
            return pwd, True
        if self._account_callback:
            try:
                cred = self._account_callback(email) or {}
                pwd = (cred.get("password") or "").strip()
                if pwd:
                    logger.info("已从数据库加载密码")
                    return pwd, True
            except Exception as e:
                logger.warning(f"account_callback 加载密码异常: {e}")
        return self._default_password_from_email(email), False


    @staticmethod
    def _random_password(length: int = 16) -> str:
        import string
        upper = string.ascii_uppercase
        lower = string.ascii_lowercase
        digits = string.digits
        special = "!@#$%^&*"
        must = [
            random.choice(upper),
            random.choice(lower),
            random.choice(digits),
            random.choice(special),
        ]
        all_chars = upper + lower + digits + special
        rest = random.choices(all_chars, k=length - len(must))
        pwd_list = must + rest
        random.shuffle(pwd_list)
        return "".join(pwd_list)


    def login_password_verify(self, password: str) -> dict:
        """已有账号密码登录一步（/password/verify）。"""
        headers = self._common_headers("https://auth.openai.com/log-in/password")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/password/verify",
            headers=headers,
            json={"password": password},
            timeout=30,
        )
        self._trace_http("login_password_verify", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"密码登录失败: {resp.status_code} - {describe_error(resp.text)}")
        try:
            return resp.json()
        except Exception:
            return {}


    def submit_mfa_totp(self, totp_code: str, challenge_id: str) -> dict:
        """提交 TOTP 2FA 验证码（已有账号登录时，密码验证后进入 mfa-challenge 状态）。

        Args:
            totp_code: 6 位 TOTP 动态码
            challenge_id: 从 continue_url 提取的 challenge ID（如 /mfa-challenge/6a76f2e8...）

        Returns:
            服务端响应 dict，包含 continue_url 指向 callback
        """
        headers = self._common_headers("https://auth.openai.com/mfa-challenge")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token

        resp = self.session.post(
            "https://auth.openai.com/api/accounts/mfa/verify",
            headers=headers,
            json={"code": totp_code, "type": "totp", "id": challenge_id},
            timeout=30,
        )
        self._trace_http("submit_mfa_totp", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"TOTP 验证失败: {resp.status_code} - {describe_error(resp.text)}")
        try:
            return resp.json()
        except Exception:
            return {}


    def verify_otp(self, otp_code: str) -> dict:
        logger.info("[7/10] 验证 OTP...")
        headers = self._common_headers("https://auth.openai.com/email-verification")
        headers["Content-Type"] = "application/json"
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            headers=headers,
            json={"code": otp_code},
            timeout=30,
        )
        self._trace_http("validate_email_otp", resp)
        if resp.status_code != 200:
            raise RuntimeError(f"OTP 验证失败: {resp.status_code} - {describe_error(resp.text)}")
        logger.info("OTP 验证成功")
        try:
            return resp.json()
        except Exception:
            return {}


    def create_account(self) -> str:
        logger.info("[8/10] 创建账户...")
        # 创建账户前刷新 sentinel token，flow 为 create_account
        if self.result.device_id:
            try:
                from platforms.chatgpt.protocol.sentinel import get_sentinel_token as _get_st
                token, so_token = _get_st(self.session, device_id=self.result.device_id,
                                flow="oauth_create_account",
                                **self._sentinel_fp_kwargs())
                self._last_sentinel_token = token or ""
                self._last_sentinel_so_token = so_token or ""
                logger.debug("Sentinel Token 获取成功")
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning(f"创建账户前刷新 sentinel 失败: {e}")
        headers = self._common_headers("https://auth.openai.com/about-you")
        headers["Content-Type"] = "application/json"
        if self._last_sentinel_token:
            headers["openai-sentinel-token"] = self._last_sentinel_token
        if getattr(self, "_last_sentinel_so_token", ""):
            headers["openai-sentinel-so-token"] = self._last_sentinel_so_token
        _FIRST = ["James", "John", "Robert", "Michael", "William", "David", "Richard",
                  "Joseph", "Thomas", "Charles", "Mary", "Patricia", "Jennifer", "Linda",
                  "Elizabeth", "Barbara", "Susan", "Jessica", "Sarah", "Karen"]
        _LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
                 "Davis", "Rodriguez", "Martinez", "Wilson", "Anderson", "Taylor", "Thomas"]
        name = f"{random.choice(_FIRST)} {random.choice(_LAST)}"
        birthdate = _random_birthdate()
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/create_account",
            headers=headers,
            json={"name": name, "birthdate": birthdate},
            timeout=30,
        )
        self._trace_http("create_account", resp)
        if resp.status_code != 200:
            reason = describe_error(resp.text)
            logger.error("创建账户失败: http=%s %s", resp.status_code, reason)
            raise RuntimeError(f"创建账户失败: {resp.status_code} - {reason}")
        data = resp.json()
        continue_url = data.get("continue_url", "")

        # 尝试 workspace select
        if not continue_url:
            workspace_id = self._extract_workspace_id()
            if workspace_id:
                continue_url = self._workspace_select(workspace_id)

        if not continue_url:
            raise RuntimeError("创建账户后未获取到 continue_url")

        logger.info("账户创建成功")
        return continue_url
