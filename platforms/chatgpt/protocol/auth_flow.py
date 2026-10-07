"""
注册/登录流程 - 协议直连方式
完整链路:
  chatgpt_csrf -> chatgpt_signin_openai -> auth_oauth_init -> sentinel
  -> signup -> send_otp -> verify_otp -> create_account
  -> redirect_chain -> auth_session -> oauth_codex_rt_exchange

refresh_token 只由 oauth_codex_rt_exchange 提供：它自建一条带 PKCE 的 Codex
authorize 链路，verifier 攥在自己手里。
"""
import json
import base64
import hashlib
import logging
import os
import random
import re
import secrets
import subprocess
import time
import uuid
from datetime import datetime
from typing import Optional, Any
from urllib.parse import urlparse, parse_qs, parse_qsl, urljoin, urlencode, urlunparse

from platforms.chatgpt.protocol.config import Config
from platforms.chatgpt.protocol.banned_signals import looks_like_banned
from platforms.chatgpt.protocol.fingerprint import (
    generate_fingerprint,
    ua_for_impersonate,
    fingerprint_for_impersonate,
    cross_family_impersonates,
    family_impersonates,
)
from platforms.chatgpt.protocol.mail_provider import MailProvider
from platforms.chatgpt.protocol.http_client import create_http_session, USER_AGENT
from platforms.chatgpt.protocol.phone_flow import PhoneRegisterMixin
from platforms.chatgpt.protocol.mixins import add_phone as __add_phone_mixin
from platforms.chatgpt.protocol.mixins import codex as __codex_mixin
from platforms.chatgpt.protocol.mixins import redirect as __redirect_mixin
from platforms.chatgpt.protocol.mixins import session as __session_mixin
from platforms.chatgpt.protocol.mixins import signup as __signup_mixin
from platforms.chatgpt.protocol.mixins import trace as __trace_mixin
from platforms.chatgpt.protocol.response_summary import describe_error
from platforms.chatgpt.protocol.totp import totp_now as _totp_now

logger = logging.getLogger(__name__)


class AuthResult:
    """认证结果"""

    def __init__(self):
        self.email: str = ""
        self.password: str = ""
        self.session_token: str = ""
        self.access_token: str = ""
        self.device_id: str = ""
        self.csrf_token: str = ""
        self.id_token: str = ""
        self.refresh_token: str = ""
        self.cookie_header: str = ""
        self.totp_secret: str = ""
        # 手机号注册链路专用：账号身份是手机号，邮箱是后来绑上去的（可能没绑上）
        self.phone_number: str = ""
        self.bound_email: str = ""

    def is_valid(self) -> bool:
        return bool(self.session_token and self.access_token)

    def to_dict(self) -> dict:
        return {
            "email": self.email,
            "password": self.password,
            "session_token": self.session_token,
            "access_token": self.access_token,
            "device_id": self.device_id,
            "csrf_token": self.csrf_token,
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "cookie_header": self.cookie_header,
            "totp_secret": self.totp_secret,
            "phone_number": self.phone_number,
            "bound_email": self.bound_email,
        }


class AuthFlow(
    __trace_mixin.TraceMixin,
    __redirect_mixin.RedirectMixin,
    __add_phone_mixin.AddPhoneMixin,
    __codex_mixin.CodexMixin,
    __session_mixin.SessionMixin,
    __signup_mixin.SignupMixin,
    PhoneRegisterMixin,
):
    """注册/登录协议流"""

    def __init__(
        self,
        config: Config,
        sms_callback: Optional[Any] = None,
        env_overrides: Optional[dict] = None,
        on_password: Optional[Any] = None,
        on_session_ready: Optional[Any] = None,
        account_callback: Optional[Any] = None,
    ):
        # 本次流程专属的配置覆盖（WEBUI_ALLOW_LOGIN / OTP_TIMEOUT / OAuth 开关等）。
        # ⚠️ 以前 registrar 是直接写 os.environ 再在 finally 里还原的，
        #    但 auto_loop 会并发跑多个 worker —— A 写的 OTP_TIMEOUT 会被 B 看见，
        #    B 跑完还原成 A 之前的值，A 后半程就读到别人的配置了。
        #    现在覆盖值只挂在实例上，进程全局环境一个字节都不动。
        self._env_overrides = dict(env_overrides or {})
        self.config = config
        self._country_code = ""  # IP 地理国家码，check_proxy() 时填充
        # Codex/登录协议固定使用参考项目同款的 Chrome 146 指纹；只有 TLS
        # 传输异常时才在同一 Chrome 家族内回退到旧版本。
        preferred_impersonate = (
            self._get_env("OAUTH_IMPERSONATE", "chrome146").strip() or "chrome146"
        )
        self._fingerprint = fingerprint_for_impersonate(
            preferred_impersonate, generate_fingerprint()
        )
        self._ua = self._fingerprint["user_agent"]
        if preferred_impersonate.startswith("chrome"):
            self._impersonate_candidates = [preferred_impersonate, "chrome142", "chrome136"]
        else:
            self._impersonate_candidates = self._fingerprint.get(
                "fallback_impersonates",
                [self._fingerprint["impersonate"], "safari17_0", "safari15_5"],
            )
        self._impersonate_idx = 0
        self.session = create_http_session(
            proxy=config.proxy,
            impersonate=self._impersonate_candidates[self._impersonate_idx],
            user_agent=self._ua,
        )
        self.result = AuthResult()
        # 可选 SMS 接码控制器（sms_provider.PhoneCallbackController 实例）
        # 命中 add-phone 时自动租手机号 + 接 SMS 验证码，否则回退到环境变量路径
        self._sms_callback = sms_callback
        # 密码一在 OpenAI 侧生效就回调出去，调用方负责立刻落盘。
        # 签名 (email: str, password: str) -> None，异常由 register_password 吞掉。
        # ⚠️ 协议层不认识 webui.db，所以只给回调，"存哪"留给调用方决定，
        #    auth_flow 单独当 CLI 用时不传就是了，行为和以前一模一样。
        self._on_password = on_password
        # 拿到 session（access_token）之后、Codex 授权之前的钩子。
        # 签名 (flow: AuthFlow, access_token: str) -> None，异常由调用点吞掉。
        # 为的是把 2FA 绑定插进主人指定的顺序：
        #     创建账户 → 重定向链 → 拿 session → ★绑 2FA★ → Codex 授权 → 接码
        # ⚠️ 传了这个钩子会**顺带关掉** run_register 里 callback 前那次 Codex 抢跑
        #    （:3051 OAUTH_CODEX_RT_BEFORE_CALLBACK），否则 Codex 会跑在钩子前面，
        #    顺序就白调了。不传则一个字节都不变，老行为。
        self._on_session_ready = on_session_ready
        # 账号凭证回调：已有账号登录时从数据库加载密码和 totp_secret。
        # 签名 (email: str) -> dict，返回 {"password": "...", "totp_secret": "..."}。
        # 用于 mfa-challenge 路径：密码验证后需要 TOTP 码，从库里读 secret。
        self._account_callback = account_callback
        self._http_trace_enabled = self._env_flag("AUTH_HTTP_TRACE", "0")
        # signup() 会在分支里 set；run_protocol_login 命中已有账号路径会跳过 signup，
        # 导致 kickoff_otp_delivery 读未初始化属性 AttributeError。这里给个默认值。
        self._is_existing_account = False
        self._existing_email_verification_mode = ""
        self._existing_page_type = ""
        # 手机号链路会在 login_hint 已经把身份定下来时跳过 authorize/continue，
        # 那条路上没人给这两个字段赋过值，读到就是 AttributeError。
        self._last_sentinel_token = ""
        self._last_sentinel_so_token = ""
        # auth_oauth_init 跟完 302 之后真正落在哪一页
        self._last_auth_landing_url = ""
        self._oauth_client_id = "YOUR_OPENAI_WEB_CLIENT_ID"
        self._oauth_redirect_uri = "https://chatgpt.com/api/auth/callback/openai"
        self._oauth_scope = ""
        self._oauth_state = ""
        self._oauth_auth_url = ""
        self._client_auth_session_dump: dict[str, Any] = {}
        self._client_auth_session_id: str = ""
        self._codex_rt_attempted: bool = False
        self._trace_dump_enabled = self._env_flag("AUTH_TRACE_DUMP", "0")
        self._trace_include_cookie = self._env_flag("AUTH_TRACE_INCLUDE_COOKIE", "0")
        self._trace_dump_path = ""
        logger.debug(
            f"指纹: impersonate={self._fingerprint['impersonate']} "
            f"screen={self._fingerprint['screen']} lang={self._fingerprint['lang']} "
            f"ua={self._ua}"
        )






    @staticmethod
    def _is_registration_disallowed_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "registration_disallowed" in msg





    def _get_env(self, name: str, default: str = "") -> str:
        """读配置：本次流程的 env_overrides 优先，回退进程环境变量。

        registrar 通过 AuthFlow(env_overrides=...) 传入，不再写 os.environ，
        所以并发跑多个号时互不干扰。命令行入口（register_outlook.py）没传
        overrides，行为跟以前完全一样。
        """
        v = self._env_overrides.get(name)
        return os.getenv(name, default) if v is None else str(v)

    def _env_flag(self, name: str, default: str = "0") -> bool:
        # 原本是 @staticmethod，为了读 self._env_overrides 改成实例方法。
        # 调用点全是 self._env_flag(...)，签名不变。
        return self._get_env(name, default).lower() in ("1", "true", "yes", "on")






























    def warmup(self) -> bool:
        """GET chatgpt.com 种全套 cookie（含 oai-did），成功返回 True。

        为什么这步不能失败（2026-08-10 实测 26 轮，跨 40+ 出口 IP）：
        `POST /api/auth/signin/openai` 依据 chatgpt.com 的 cookie 决定返回什么——
        有 oai-did 就返 auth.openai.com/authorize URL，没有就返 NextAuth 页，
        后者到 authorize/continue 必然 409 invalid_state。
        实测：无 oai-did 的 5 轮 **5/5 全 409**；有 oai-did 的 17 轮只有 3 次 409。

        旧实现两个问题，实测各占一半失败：
        1. 单次无重试 + timeout=15。实测种 cookie 失败率 19%，形态有三种：
           TLS curl(35) 断连、15s 超时、CF 403。成功轮实际耗时 3.4~10.9s，
           15s 卡边缘，40s 才有富余。
        2. **返回值和实际结果对不上**：只 catch 异常，不看 status_code——
           403 照样 return True（实测 3 轮 True 但没 cookie），
           而超时前 cookie 其实已经种上了却 return False（实测 1 轮）。
           所以判据改成直接查 cookie jar，这是唯一可信的信号。

        3. **没发 client hints，自称 Chrome 却不带 sec-ch-ua —— CF 一眼假。**
           这是 403 的真因。真 Chrome 每个导航请求必带 sec-ch-ua/-mobile/-platform，
           而旧 warmup 是手搓 headers、一个都没带（_common_headers 带了，只有这里漏）。
           2026-08-10 实测，同一 impersonate 各打 5 次（每次新 IP）：

               impersonate   裸头(旧)   补 CH 全套
               chrome146      1/5        5/5
               chrome136      1/5        5/5
               chrome142      4/5        4/5   ← 唯一失败是 SSL 断连，不是 403

           补齐后 403 **全部消失**。此前"chrome 族被 CF 拦"的结论是误判：
           safari/firefox 当时 4/4 不是因为它们更干净，而是**它们本来就不该发
           client hints**，裸头对它们恰好是正确的头。所以修法是把头补齐，
           不是换成 safari —— 换指纹只是绕开症状，且会让 self._fingerprint 与
           self._ua 不一致（后续 _common_headers 会拿旧家族的 CH 配新 UA，更假）。

        4. **【2026-08-21 更新】CF 改成按家族封，chrome 全族 403。**
           上面第 3 条"补齐 client hints 后 chrome 就好了"已经过期。线上注册连挂，
           同一台机器（无代理）当场实测：

               impersonate                  结果
               chrome136 / 142 / 146        403，只给 __cf_bm
               mac_safari / ios_safari      200，oai-did 正常种下
               firefox133                   200，oai-did 正常种下

           头是对的、IP 是好的（5 小时前同一 IP 刚跑通过一轮补 RT），封的是
           chrome 的 TLS/HTTP2 指纹本身。所以重试**必须换家族**——原来那句
           "只换出口 IP，不换指纹"在这种封锁下等于拿同一张脸连撞 4 次。

        重试同时换出口 IP 和浏览器家族：代理池按会话分配出口，新 session ≈ 新 IP，
        绕开连不上的坏 IP；换家族绕开整族封锁。cookie 跟着 session 一起清掉是对的：
        失败轮本来就没种到有用的东西。家族顺序是随机的，不写死"safari 更安全"——
        今天挂的是 chrome，明天可能轮到别的。

        注：URL 保持首页 `/`。实测对比过 `/auth/login`（16 轮 vs 10 轮），
        失败率 18.75% vs 20%，无差异，不值得换。

        【最终验证 2026-08-10】本处 + auth_oauth_init + _follow_redirects 三处
        统一走 _navigation_headers 后，用真实 CF 域名跑完整 run_register
        **3/3 全成功**（各约 100s，password + access_token 齐全），409 = 0。
        """
        headers = self._navigation_headers()
        family_fallbacks = cross_family_impersonates(self._fingerprint.get("impersonate", ""))

        for attempt in range(4):
            if attempt:
                time.sleep(3 + attempt * 2)
                # 换出口 IP（新 session = 新出口）的同时换浏览器家族
                if family_fallbacks:
                    self._switch_browser_family(family_fallbacks.pop(0))
                    headers = self._navigation_headers()
                self.session = create_http_session(
                    proxy=self.config.proxy,
                    impersonate=self._impersonate_candidates[self._impersonate_idx],
                    user_agent=self._ua,
                )
            try:
                resp = self.session.get(
                    "https://chatgpt.com", headers=headers, timeout=40,
                )
                status = resp.status_code
            except Exception as e:
                status = None
                logger.warning(f"warmup 第 {attempt + 1}/4 次请求失败: {e}")

            # 唯一判据：cookie 到底种上没有。HTTP 200 不代表拿到 oai-did（CF 403 只给
            # __cf_bm），请求抛异常也不代表没拿到（超时前可能已经种上了）。
            try:
                cookies = self.session.cookies.get_dict()
            except Exception:
                cookies = {}
            imp = self._fingerprint.get("impersonate", "")
            if "oai-did" in cookies:
                logger.info(
                    f"chatgpt.com warmup 完成（第 {attempt + 1} 次，impersonate={imp}，"
                    f"oai-did 已种，共 {len(cookies)} 个 cookie）"
                )
                return True

            logger.warning(
                f"warmup 第 {attempt + 1}/4 次未种到 oai-did（impersonate={imp}"
                + (f"，HTTP {status}）" if status is not None else "）")
                + (f"，已有 cookie: {sorted(cookies)}" if cookies else "，无任何 cookie")
            )

        logger.error(
            "warmup 4 次均未种到 oai-did cookie（已轮换浏览器家族）"
            " —— 此时继续走注册链必然 409 invalid_state"
        )
        return False

    # ── Step 1: 检查代理连通性 ──
    def check_proxy(self) -> bool:
        logger.info("检查网络连通性...")
        try:
            resp = self.session.get("https://cloudflare.com/cdn-cgi/trace", timeout=15)
            if resp.status_code == 200:
                loc = re.search(r"loc=(\w+)", resp.text)
                ip = re.search(r"ip=([^\n]+)", resp.text)
                country_code = loc.group(1) if loc else ""
                logger.info(f"网络正常 - IP: {ip.group(1) if ip else 'N/A'}, "
                            f"地区: {country_code or 'N/A'}")

                # IP 地理联动：检测到国家码后，重新生成指纹（带时区/语言联动）
                if country_code and country_code != self._country_code:
                    self._country_code = country_code
                    import random
                    session_seed = id(self.session) % (2**32)
                    rng = random.Random(session_seed)
                    self._fingerprint = generate_fingerprint(rng=rng, country_code=country_code)
                    self._ua = self._fingerprint["user_agent"]
                    new_imp = self._fingerprint["impersonate"]
                    self._impersonate_candidates = self._fingerprint.get(
                        "fallback_impersonates",
                        [new_imp, "safari17_0", "safari15_5"],
                    )
                    self._impersonate_idx = 0
                    self.session = create_http_session(
                        proxy=self.config.proxy,
                        impersonate=new_imp,
                        user_agent=self._ua,
                    )
            else:
                logger.warning(f"网络探测异常: cloudflare trace {resp.status_code}")

            return True
        except Exception as e:
            logger.error(f"网络检查失败: {e}")
        return False

    # ── Step 2: 获取 CSRF Token ──

    # ── Step 3: 获取 auth URL ──

    # ── Step 4: OAuth 初始化 & 获取 device_id ──

    # ── Step 5: 获取 Sentinel Token ──


    # ── Step 6: 提交注册邮箱 ──


    # ── Step 6.5: 注册密码 ──

    # ── Step 7: 发送 OTP ──








    # ── Step 7.5: 提交 TOTP 2FA 验证码 ──

    # ── Step 8: 验证 OTP ──

    # ── Step 9: 创建账户 ──






    # ── Step 10: 跟踪重定向链 ──


    # ── Step 11: 获取 session ──



    # ── 完整注册流程 ──
    def run_register(self, mail_provider: MailProvider) -> AuthResult:
        """执行完整注册流程"""
        # 检查网络
        if not self.check_proxy():
            logger.warning("网络预检查未通过，继续尝试注册链路以获取精确错误...")
        # warmup 失败 = 没拿到 oai-did = 后面 authorize/continue 必 409（实测 5/5）。
        # 必须在 create_mailbox 之前拦掉：邮箱是花钱的，不能为一个注定 409 的轮次浪费。
        if not self.warmup():
            raise RuntimeError(
                "warmup 失败：4 次重试均未拿到 chatgpt.com 的 oai-did cookie，"
                "继续注册必然 409 invalid_state（多为代理出口 IP 不通或被 CF 拦），"
                "请检查代理后重试"
            )

        # 创建邮箱
        email = mail_provider.create_mailbox()
        self.result.email = email

        # 登录/注册链路
        csrf_token = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf_token, email=email)
        device_id = self.auth_oauth_init(auth_url)
        sentinel = self.get_sentinel_token(device_id)
        is_new = self.signup(email, sentinel)

        # 号池邮箱被 OpenAI 标"已有账号" 处理策略:
        #   WEBUI_ALLOW_LOGIN=1 (promo-link 等需要拿 access_token 的模式) → 走 OTP login 拿凭证
        #   WEBUI_ALLOW_LOGIN 未设 (register-only 模式) → fast-fail mark dead 换下一个号
        # 这样 register-only 不被 honeypot 拖死, promo-link 又能复用已存在账号.
        #
        # ⚠️ 这个分支对**所有号池型 provider** 生效（outlook / icloud_relay / 以后新增的），
        #    不是 outlook 专属 —— 日志前缀用 provider 自己的 kind，别写死。
        pool_tag = getattr(mail_provider, "kind", "pool")
        is_pooled_existing = not is_new and getattr(mail_provider, "pooled", False)
        if is_pooled_existing:
            _allow_login = self._get_env("WEBUI_ALLOW_LOGIN", "").strip() in (
                "1", "true", "yes",
            )
            if _allow_login:
                logger.info(
                    f"[{pool_tag}] '已有账号' 分支但 WEBUI_ALLOW_LOGIN=1 → 走 OTP login 拿凭证 ({email})"
                )
            else:
                logger.warning(
                    f"[{pool_tag}] '已有账号' 分支检测到号池邮箱 ({email}) → fast-fail mark dead, "
                    f"让外层 register() 自动 claim 下一个号 (设 WEBUI_ALLOW_LOGIN=1 改走 OTP login)"
                )
                try:
                    mail_provider.mark_dead(
                        "OpenAI 识别为已有账号 (接码池二手 / honeypot, 协议层 fast-fail)"
                    )
                except Exception:
                    pass
                raise RuntimeError(
                    f"OpenAI 静默拒绝发 OTP (识别 {email} 为已有账号, {pool_tag} 池 fast-fail)"
                )

        # ⚠️ passwordless_signup **也是新账号**，只是服务端选择了"不设密码直接发码"的注册流程。
        #    signup() 用一个 bool 表达三种服务端状态，把它和"已有账号"压成了同一个 False，
        #    结果新号全部走进下面的 else 分支 —— register_password 从没被调用过，
        #    注册出来的号全是无密码号（只能靠临时邮箱收码登录，域名一失效就永久丢失）。
        #    实测 2026-08-06: 这类号照样能走 POST user/register 设密码并成功。
        if is_new or self._existing_email_verification_mode == "passwordless_signup":
            # 新账号：注册密码 → 主动重新发码 → 验证 OTP → 创建账户
            password_registered = self.register_password(email)
            if password_registered:
                # ⚠️ POST user/register 成功后服务端**切换流程**到 email_otp_send 页
                #    （实测响应 continue_url=/api/accounts/email-otp/send, page.type=email_otp_send），
                #    signup 阶段自动发的那封 OTP 立即失效，拿它 verify 会 409 invalid_state。
                #    所以必须主动重新发码，且以本次发码时间为准，不能再用 -8 偏移。
                #    时间戳在发码**之前**取：邮件是服务端在这次请求里发出的，
                #    先发再取时间会让 otp_sent_at 晚于邮件时间戳，被 issued_after 过滤掉。
                otp_sent_at = time.time()
                try:
                    self.send_otp()
                except Exception as e:
                    # 429 等发码失败时别让整个注册崩掉，退回 resend 兜底
                    logger.warning(f"密码注册后主动发码失败，回退 resend: {e}")
                    self.kickoff_otp_delivery("post_register_password_send_failed")
            else:
                logger.warning("注册密码失败，回退到已有账号 OTP 路径")
                self.fetch_client_auth_session_dump("post_register_password_failed_new")
                # 密码注册失败时 fallback 主动发码
                if not self.kickoff_otp_delivery("register_password_failed_fallback"):
                    self.send_otp()
                otp_sent_at = time.time()

            try:
                otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 180
            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            try:
                self.verify_otp(otp_code)
                self.fetch_client_auth_session_dump("post_verify_otp_new")
            except RuntimeError as e:
                # 偶发 401 错码，补发一次 OTP 并重试。
                # 重试时必须把刚试过的码排除：补发的新邮件还没到时，收件端会把
                # 同一封信再读一遍、拿同一个错码再验一次 —— 白耗一次机会，还可能
                # 直接撞上服务端的 max_check_attempts 锁死（实测踩过）。
                if "401" in str(e):
                    logger.warning(f"OTP 首次验证失败，补发重试: {e}")
                    otp_sent_at = time.time()
                    # 与首轮同因：`POST user/register` 之后服务端流程在
                    # `email_otp_send` 页，此时 `passwordless/send-otp` 必然
                    # 409 invalid_state（实测日志里就有这条，白跑一趟还刷一条
                    # 吓人的 warning）。首轮走的就是 `send_otp()`，重试保持一致。
                    try:
                        self.send_otp()
                    except Exception as send_err:
                        logger.warning(f"补发失败，回退 kickoff: {send_err}")
                        self.kickoff_otp_delivery("verify_otp_retry_new")
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                        exclude_codes={otp_code},
                    )
                    self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_retry_new")
                else:
                    raise

            try:
                continue_url = self.create_account()
            except Exception as e:
                # registration_disallowed 时尝试 reauthorize 兜底，若仍不可用再抛出
                if self._is_registration_disallowed_error(e):
                    logger.warning("create_account 被拒绝，尝试 reauthorize 兜底获取 session ...")
                    continue_url = self._reauthorize_for_session(auth_url) or ""
                    if not continue_url:
                        raise
                else:
                    raise
        else:
            # 已有账号：直接发 OTP → 验证 → 获取 session
            mode = (self._existing_email_verification_mode or "").lower()
            page_type = (self._existing_page_type or "").lower()
            continue_url = ""

            try:
                otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 180

            if page_type == "login_password":
                logger.info("已有账号进入 login_password 分支，先走密码校验再 OTP")
                login_password, pw_is_real = self._resolve_login_password(email)
                if pw_is_real:
                    self.result.password = login_password
                else:
                    logger.info("该号无已知密码，用默认规则猜一个试试（多半 401）")
                login_resp = self.login_password_verify(login_password)
                login_page_type = self._extract_page_type(login_resp)
                continue_url = self._normalize_continue_url(
                    (login_resp or {}).get("continue_url", "") if isinstance(login_resp, dict) else ""
                )

                # mfa-challenge 分支（密码验证后需要 TOTP 2FA）
                if self._is_mfa_challenge_state(login_page_type, continue_url):
                    totp_secret = (self.result.totp_secret or "").strip()
                    if not totp_secret and self._account_callback:
                        # 从数据库加载凭证
                        try:
                            cred = self._account_callback(email)
                            if cred and cred.get("totp_secret"):
                                totp_secret = cred["totp_secret"]
                                self.result.totp_secret = totp_secret
                                logger.info("已从数据库加载 totp_secret")
                        except Exception as e:
                            logger.warning(f"account_callback 异常: {e}")
                    if not totp_secret:
                        logger.warning("进入 mfa-challenge 但没有 totp_secret，无法继续")
                    else:
                        challenge_id = continue_url.split("/")[-1] if "/mfa-challenge/" in continue_url else ""
                        if challenge_id:
                            totp_code = _totp_now(totp_secret)
                            logger.info(f"提交 TOTP 码进行 2FA 验证（challenge_id={challenge_id[:16]}...）")
                            mfa_resp = self.submit_mfa_totp(totp_code, challenge_id)
                            continue_url = self._normalize_continue_url(
                                (mfa_resp or {}).get("continue_url", "") if isinstance(mfa_resp, dict) else ""
                            )
                        else:
                            logger.warning("无法从 continue_url 提取 challenge_id")

                # 部分账号密码校验后仍需 email otp（二次校验）
                elif not continue_url or "/email-verification" in continue_url:
                    # password/verify 后推荐使用 resend，而不是 /email-otp/send
                    otp_sent_at = time.time()
                    self.kickoff_otp_delivery("existing_login_password")
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                    otp_resp = self.verify_otp(otp_code)
                    continue_url = self._normalize_continue_url(
                        (otp_resp or {}).get("continue_url", "") if isinstance(otp_resp, dict) else ""
                    )
            else:
                need_send_otp = mode not in ("passwordless_signup", "passwordless_login")
                if need_send_otp:
                    otp_sent_at = time.time()
                    self.send_otp()
                else:
                    # 某些模式在 /authorize/continue 已触发发码，不要重复 /email-otp/send 以免破坏 state
                    # 默认先尝试 /email-otp/resend 获取新码，失败再回看短窗口
                    forced_resend = self._env_flag("OTP_FORCE_RESEND", "0")
                    if forced_resend and self.kickoff_otp_delivery("existing_forced_resend"):
                        otp_sent_at = time.time()
                        logger.debug(f"已有账号验证码模式={mode}，已主动 resend OTP")
                    else:
                        # 回看短窗口，避免误读上一轮旧验证码
                        otp_sent_at = time.time() - 8
                        logger.info(f"已有账号验证码模式={mode}，跳过额外 send_otp，直接等邮件")

                try:
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                    )
                except TimeoutError:
                    # provider 判定自己已无可用收件链路时会设 exhausted=True 并 mark_dead，
                    # 不 retry 直接 raise，避免再次等待无效收件链路。
                    # （outlook 是 IMAP-only 纯协议失败；别的 provider 有自己的判据。）
                    if getattr(mail_provider, "exhausted", False):
                        logger.warning(
                            f"[{getattr(mail_provider, 'kind', 'mail')}] "
                            f"收码链路已失效并 mark dead, 跳过 retry resend"
                        )
                        raise
                    # 否则 (非号池场景, 如 catch_all CF KV) 给一次 resend retry
                    logger.warning("未等到已有账号 OTP，先重发后重试等待")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("existing_timeout_retry"):
                        self.send_otp()
                    try:
                        otp_code = mail_provider.wait_for_otp(
                            email,
                            timeout=otp_timeout,
                            issued_after=otp_sent_at,
                        )
                    except TimeoutError:
                        # 号池 + "已有账号" 分支 + 两次 timeout = OpenAI 反欺诈
                        # 静默拒绝（页面声称已注册但不真发邮件）→ mark dead 该邮箱
                        # 让池下次跳过，user 重新点 ▶ 自动 claim 下一个 available。
                        # （非号池 provider 如 CF 临时邮箱 mark_dead 是空操作，不用额外判断。）
                        if getattr(mail_provider, "pooled", False):
                            try:
                                mail_provider.mark_dead(
                                    "OpenAI 静默拒绝发 OTP（'已有账号'但 INBOX 无邮件）"
                                )
                            except Exception:
                                pass
                        raise
                try:
                    otp_resp = self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_existing")
                except RuntimeError as e:
                    if any(code in str(e) for code in ("401", "409")):
                        logger.warning(f"OTP 首次验证失败，重发重试: {e}")
                        otp_sent_at = time.time()
                        if not self.kickoff_otp_delivery("existing_verify_retry"):
                            self.send_otp()
                        # 排除刚试过的码：否则会把同一封信再读一遍、拿同一个错码
                        # 再验一次（实测撞上 max_check_attempts 锁死）。
                        otp_code = mail_provider.wait_for_otp(
                            email,
                            timeout=otp_timeout,
                            issued_after=otp_sent_at,
                            exclude_codes={otp_code},
                        )
                        otp_resp = self.verify_otp(otp_code)
                        self.fetch_client_auth_session_dump("post_verify_otp_retry_existing")
                    else:
                        raise
                continue_url = (otp_resp or {}).get("continue_url", "") if isinstance(otp_resp, dict) else ""
                continue_url = self._normalize_continue_url(continue_url)
                if self._is_add_phone_state(page_type=self._extract_page_type(otp_resp), continue_url=continue_url):
                    continue_url = self._normalize_continue_url(
                        self._handle_add_phone_verification(continue_url=continue_url)
                    )

            # 某些已有账号在 OTP 后会进入 about-you，需要补一次 create_account
            if continue_url and "/about-you" in continue_url:
                try:
                    continue_url = self.create_account()
                except Exception as e:
                    if self._is_registration_disallowed_error(e):
                        logger.warning("about-you create_account 被拒绝，尝试 reauthorize 兜底获取 session ...")
                        continue_url = self._reauthorize_for_session(auth_url) or ""
                        if continue_url:
                            logger.info("reauthorize 兜底成功，继续后续 session 获取")
                            # 下游会走 follow_redirect_chain + get_auth_session
                            pass
                        else:
                            raise
                    else:
                        # 同探测块：封禁措辞必须当场终止，不能回退 reauthorize。
                        if looks_like_banned(str(e)):
                            logger.warning(f"about-you 创建撞上封禁措辞，直接终止: {e}")
                            raise
                        logger.warning(f"已有账号 about-you 创建信息失败，回退 reauthorize: {e}")
                        continue_url = ""

            # 若 otp 响应未给可用 continue_url，则回退到 reauthorize
            if not continue_url:
                # auth.openai.com 的 session cookie 已设置，直接拿 code
                continue_url = self._reauthorize_for_session(auth_url)

        if continue_url:
            continue_url = self._normalize_continue_url(continue_url)
            # 关键尝试：在 chatgpt callback 被消费前，先走一次 Codex OAuth（有助于保留 auth.openai 登录态）
            # ⚠️ 挂了 on_session_ready 钩子时**跳过这次抢跑**：钩子（绑 2FA）要等
            #    get_auth_session 拿到 access_token 才能跑，而那步在下面 :callback 之后；
            #    这次抢跑成功的话 Codex 就跑到钩子前面去了，指定的顺序等于没改。
            #    跳过后 Codex 落到后面那个调用点（get_auth_session 之后），顺序才是
            #    创建账户 → 重定向链 → 拿 session → 绑 2FA → Codex 授权 → 接码。
            if (
                (not self.result.refresh_token)
                and self._on_session_ready is None
                and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1")
            ):
                # `speculative=True`：抢跑失败不能占掉本轮名额，否则下面
                # （callback 链跑完之后）那次真正有条件的尝试会被跳过。
                self.oauth_codex_rt_exchange(
                    mail_provider=mail_provider, speculative=True
                )
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)
        else:
            callback_url, final_url = None, None

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")

        # callback 的 code 归 NextAuth 独占：先 _consume_callback 让 chatgpt.com 自己
        # 消费它并 set 全套 cookie（含 session-token），再 get_auth_session 拿
        # access_token。Codex RT exchange 走独立 authorize 链路，不跟这个 code 抢。
        if (not refresh_only_mode) and callback_url:
            logger.debug("消费 callback 触发 NextAuth Set-Cookie (session-token)")
            self._consume_callback_for_session(callback_url)

        if not refresh_only_mode:
            self.get_auth_session()

        # ── 钩子：session 到手、Codex 授权之前 ──
        # 主人指定的顺序是「注册完 → 绑 2FA → Codex 授权 → 接码」。这里是唯一同时满足
        # 「已经有 access_token」和「Codex 还没跑」的位置，所以插在这。
        # 失败绝不能拖垮已注册成功的号 —— 异常吞掉，继续往下走 Codex。
        if self._on_session_ready is not None and self.result.access_token:
            try:
                self._on_session_ready(self, self.result.access_token)
            except Exception as e:
                logger.warning(f"session_ready 回调失败（不影响注册）: {e}")

        # Codex OAuth refresh_token 交换（独立 authorize 链路，不依赖上面 callback 的 code）
        if callback_url or continue_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_register")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            # 最终再拉一次 session（Codex 流程可能更新 cookie/access_token）
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("流程完成但未获取 refresh_token/access_token")
        elif not self.result.is_valid():
            raise RuntimeError("注册完成但未获取有效凭证")

        logger.info("注册流程完成!")
        return self.result

    # ── 纯协议已有账号登录流程（目标：拿 callback/session/refresh） ──
    def run_protocol_login(self, mail_provider: MailProvider, email: str, password: str = "") -> AuthResult:
        """
        纯协议登录（不创建随机邮箱）：
        - 适配 passwordless / login_password 两类已有账号入口
        - 可配合 OAUTH_REFRESH_ONLY 只要 refresh_token，跳过 session 相关请求
        """
        if not (email or "").strip():
            raise RuntimeError("run_protocol_login 缺少邮箱")

        if not self.check_proxy():
            logger.warning("网络预检查未通过，继续尝试登录链路以获取精确错误...")
        # 同 run_register：没 oai-did 就走不通 authorize 链，早失败早换 IP。
        # 这里不花钱建邮箱，但报错说清原因，省得当成"密码错"排查。
        if not self.warmup():
            raise RuntimeError(
                "warmup 失败：4 次重试均未拿到 chatgpt.com 的 oai-did cookie，"
                "继续登录必然 409 invalid_state（多为代理出口 IP 不通或被 CF 拦），"
                "请检查代理后重试"
            )

        # run_protocol_login 的语义即"登录已有账号"（docstring 明写）。kickoff_otp_delivery
        # 依据 _is_existing_account 选 resend vs send_passwordless_otp 分支；落到 send
        # 分支会把 server-side state 弄坏 → 之后 IMAP 抓到的 OTP X 已失效 → verify 401
        # wrong_email_otp_code。这里入口统一 set True，覆盖 passwordless 这类 page_type
        # 不在 ("login_password","email_otp_verification") 集合的情况；signup() 回退
        # 路径会基于 OpenAI 真实响应再次覆盖（True/False），无副作用。
        self._is_existing_account = True

        email = email.strip()
        self.result.email = email
        login_password = (password or "").strip()
        if login_password:
            self.result.password = login_password
        else:
            login_password, pw_is_real = self._resolve_login_password(email)
            if pw_is_real:
                self.result.password = login_password
            else:
                logger.info("协议登录：调用方没给密码、库里也没有，用默认规则猜一个试试")

        csrf_token = self.get_csrf_token()
        auth_url = self.get_auth_url(csrf_token, email=email)
        device_id = self.auth_oauth_init(auth_url)
        sentinel = self.get_sentinel_token(device_id)

        continue_url = ""
        try:
            otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
        except Exception:
            otp_timeout = 180

        page_type = ""
        mode = ""
        prefer_login_screen_first = str(
            self._get_env("LOCALAUTH_EXISTING_LOGIN_USE_LOGIN_HINT", "1")
        ).lower() in ("1", "true", "yes", "on")

        if prefer_login_screen_first:
            try:
                logger.info("已有账号协议登录：优先走 login screen_hint 探测 password/otp 分支")
                login_step = self.authorize_continue(
                    email=email,
                    sentinel_token=sentinel,
                    screen_hint="login",
                    referer="https://auth.openai.com/log-in",
                    trace_step="authorize_continue_login_protocol",
                )
                page_type = (self._extract_page_type(login_step) or "").lower()
                continue_url = self._normalize_continue_url(
                    self._extract_continue_url_from_step(login_step)
                )
                page = (login_step.get("page") or {}) if isinstance(login_step, dict) else {}
                payload = (page.get("payload") or {}) if isinstance(page, dict) else {}
                mode = (payload.get("email_verification_mode", "") or "").lower()
                self._existing_page_type = page_type
                self._existing_email_verification_mode = mode

                if page_type == "login_password" or "/log-in/password" in (continue_url or ""):
                    logger.info("登录分支: login_password -> password/verify")
                    # 命中已有账号 password 路径：标记之，让 kickoff_otp_delivery 走 resend
                    # 分支（避免 send_passwordless_otp 把 state 弄坏 → wrong_email_otp_code）
                    self._is_existing_account = True
                    login_resp = self.login_password_verify(login_password)
                    page_type = (self._extract_page_type(login_resp) or "").lower()
                    continue_url = self._normalize_continue_url(
                        self._extract_continue_url_from_step(login_resp)
                    )

                    # mfa-challenge 分支（密码验证后需要 TOTP 2FA）
                    if self._is_mfa_challenge_state(page_type, continue_url):
                        totp_secret = (self.result.totp_secret or "").strip()
                        if not totp_secret and self._account_callback:
                            # 从数据库加载凭证
                            try:
                                cred = self._account_callback(email)
                                if cred and cred.get("totp_secret"):
                                    totp_secret = cred["totp_secret"]
                                    self.result.totp_secret = totp_secret
                                    logger.info("已从数据库加载 totp_secret")
                            except Exception as e:
                                logger.warning(f"account_callback 异常: {e}")
                        if not totp_secret:
                            logger.warning("进入 mfa-challenge 但没有 totp_secret，无法继续")
                        else:
                            challenge_id = continue_url.split("/")[-1] if "/mfa-challenge/" in continue_url else ""
                            if challenge_id:
                                totp_code = _totp_now(totp_secret)
                                logger.info(f"提交 TOTP 码进行 2FA 验证（challenge_id={challenge_id[:16]}...）")
                                mfa_resp = self.submit_mfa_totp(totp_code, challenge_id)
                                page_type = (self._extract_page_type(mfa_resp) or "").lower()
                                continue_url = self._normalize_continue_url(
                                    self._extract_continue_url_from_step(mfa_resp)
                                )
                            else:
                                logger.warning("无法从 continue_url 提取 challenge_id")

                elif page_type == "email_otp_verification" or "/email-verification" in (continue_url or ""):
                    logger.info("登录分支: email_otp_verification")
                    # 同上：authorize/continue 已 trigger 发码，kickoff_otp_delivery 必须只 resend。
                    self._is_existing_account = True
                else:
                    logger.info(
                        "login screen_hint 未直接命中已有账号完成态: page_type=%s continue_url=%s",
                        page_type or "(empty)",
                        (continue_url or "")[:180] or "(empty)",
                    )
            except Exception as e:
                # 封禁是终局结论，不能被当作「探测失败」回退：TOTP 提交等步骤
                # 撞上「deleted or deactivated」时若吞掉异常，回退链会以无关的
                # 409 invalid_state 收场 —— 封禁措辞在最终错误里消失，账号被
                # 误标「失效」而不是「禁用」（用户报告 2026-10-07：一批账号
                # 服务端已明确回停用措辞，状态却没变）。
                if looks_like_banned(str(e)):
                    logger.warning(f"探测链撞上封禁措辞，直接终止（不回退）: {e}")
                    raise
                logger.warning(f"login screen_hint 探测失败，回退 signup 探测: {e}")
                continue_url = ""
                page_type = ""
                mode = ""

        if not continue_url and page_type not in ("login_password", "email_otp_verification"):
            is_new = self.signup(email, sentinel)
            if is_new:
                logger.warning("目标邮箱未命中已有账号分支，回退到注册链路")
                self.register_password(email)
                otp_sent_at = time.time()
                self.send_otp()
                otp_code = mail_provider.wait_for_otp(
                    email,
                    timeout=otp_timeout,
                    issued_after=otp_sent_at,
                )
                self.verify_otp(otp_code)
                continue_url = self.create_account()
            else:
                page_type = (self._existing_page_type or "").lower()
                mode = (self._existing_email_verification_mode or "").lower()
        else:
            page_type = (page_type or self._existing_page_type or "").lower()
            mode = (mode or self._existing_email_verification_mode or "").lower()

        if not continue_url or "/email-verification" in continue_url:
            # 仍需 OTP：优先 resend 获取新码
            otp_sent_at = time.time()
            resend_ok = self.kickoff_otp_delivery("protocol_need_otp")
            if not resend_ok and mode not in ("passwordless_signup", "passwordless_login"):
                self.send_otp()
                otp_sent_at = time.time()

            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            try:
                otp_resp = self.verify_otp(otp_code)
                self.fetch_client_auth_session_dump("post_verify_otp_protocol")
            except RuntimeError as e:
                if any(code in str(e) for code in ("401", "409")):
                    logger.warning(f"OTP 首次验证失败，重发重试: {e}")
                    otp_sent_at = time.time()
                    if not self.kickoff_otp_delivery("protocol_verify_retry"):
                        self.send_otp()
                    # 排除刚试过的码（同注册链路：不排除会把同一封信再读一遍）。
                    otp_code = mail_provider.wait_for_otp(
                        email,
                        timeout=otp_timeout,
                        issued_after=otp_sent_at,
                        exclude_codes={otp_code},
                    )
                    otp_resp = self.verify_otp(otp_code)
                    self.fetch_client_auth_session_dump("post_verify_otp_retry_protocol")
                else:
                    raise
            continue_url = self._extract_continue_url_from_step(otp_resp)
            continue_url = self._normalize_continue_url(continue_url)
            if self._is_add_phone_state(page_type=self._extract_page_type(otp_resp), continue_url=continue_url):
                continue_url = self._normalize_continue_url(
                    self._handle_add_phone_verification(continue_url=continue_url)
                )

        continue_url = self._normalize_continue_url(continue_url)
        # 某些边缘态 OTP 后未返回 callback，回退 reauthorize
        if not continue_url:
            continue_url = self._reauthorize_for_session(auth_url) or ""

        refresh_only_mode = self._env_flag("OAUTH_REFRESH_ONLY", "0")
        callback_url = ""
        if continue_url:
            continue_url = self._normalize_continue_url(continue_url)
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_BEFORE_CALLBACK", "1"):
                # `speculative=True`：抢跑失败不占本轮名额（见该方法 docstring）。
                self.oauth_codex_rt_exchange(
                    mail_provider=mail_provider, speculative=True
                )
            callback_url, final_url = self.follow_redirect_chain(continue_url)
            if (not callback_url) and final_url and ("/workspace" in final_url):
                normalized = self._normalize_continue_url(final_url)
                if normalized and normalized != final_url:
                    callback_url, final_url = self.follow_redirect_chain(normalized)

        if not refresh_only_mode:
            self.get_auth_session()

        if callback_url or continue_url:
            self.fetch_client_auth_session_dump("pre_oauth_exchange_protocol")
            if (not self.result.refresh_token) and self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"):
                self.oauth_codex_rt_exchange(mail_provider=mail_provider)
            if not refresh_only_mode:
                self.get_auth_session()

        if refresh_only_mode:
            if not (self.result.refresh_token or self.result.access_token):
                raise RuntimeError("协议登录完成，但未拿到 refresh_token/access_token")
        elif not self.result.is_valid():
            raise RuntimeError("协议登录完成，但未拿到有效 session/access token")

        logger.info("纯协议登录流程完成")
        return self.result

    # ── 从已有凭证初始化 ──
    def from_existing_credentials(
        self, session_token: str, access_token: str, device_id: str
    ) -> AuthResult:
        """使用已有凭证（跳过注册）"""
        self.result.device_id = device_id or str(uuid.uuid4())
        self.session.cookies.set("oai-did", self.result.device_id, domain=".chatgpt.com")
        detected_email = ""

        # 如果有 session_token, 用它刷新 access_token (旧 access_token 可能已过期)
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
            logger.info("使用 session_token 刷新 access_token...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_data = resp.json() if resp is not None else {}
                new_access_token = session_data.get("accessToken", "")
                user_obj = session_data.get("user", {}) if isinstance(session_data, dict) else {}
                if isinstance(user_obj, dict):
                    detected_email = detected_email or (user_obj.get("email", "") or "")
                new_session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if new_access_token:
                    access_token = new_access_token
                    logger.info("access_token 刷新成功")
                else:
                    logger.warning(f"access_token 刷新失败 (status={resp.status_code}), 使用原 token")
                if new_session_token:
                    session_token = new_session_token
            except Exception as e:
                logger.warning(f"刷新 access_token 失败: {e}, 使用原 token")
        elif access_token:
            # 没有 session_token, 尝试通过 access_token 获取
            logger.info("未提供 session_token, 尝试通过 access_token 获取...")
            try:
                headers = self._common_headers("https://chatgpt.com/")
                headers["Authorization"] = f"Bearer {access_token}"
                resp = self.session.get(
                    "https://chatgpt.com/api/auth/session",
                    headers=headers,
                    timeout=30,
                )
                session_data = resp.json() if resp is not None else {}
                user_obj = session_data.get("user", {}) if isinstance(session_data, dict) else {}
                if isinstance(user_obj, dict):
                    detected_email = detected_email or (user_obj.get("email", "") or "")
                session_token = self.session.cookies.get("__Secure-next-auth.session-token", "")
                if session_token:
                    logger.info("通过 access_token 获取 session_token 成功")
                else:
                    logger.warning("未能获取 session_token, 可能需要手动提供")
            except Exception as e:
                logger.warning(f"获取 session_token 失败: {e}")

        self.result.access_token = access_token
        self.result.session_token = session_token
        if session_token:
            self.session.cookies.set(
                "__Secure-next-auth.session-token",
                session_token,
                domain=".chatgpt.com",
            )
        self.result.cookie_header = self._build_chatgpt_cookie_header()

        # 回填 email（skip-register 模式下常用于账单 email）
        if not detected_email and access_token and access_token.count(".") >= 2:
            try:
                payload_b64 = access_token.split(".")[1]
                payload_b64 += "=" * (-len(payload_b64) % 4)
                payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8"))
                prof = payload.get("https://api.openai.com/profile", {}) if isinstance(payload, dict) else {}
                if isinstance(prof, dict):
                    detected_email = detected_email or (prof.get("email", "") or "")
            except Exception:
                pass
        self.result.email = detected_email or ""
        logger.info("使用已有凭证初始化完成")
        return self.result

