"""CodexMixin：PKCE + Codex authorize 链路 + oauth_codex_rt_exchange。

从 3541 行的 auth_flow.py 拆出（纯搬家，方法体逐字节不变）。

这些方法会被 `AuthFlow.__new__(AuthFlow)` 风格的测试直接调用，
所以方法之间的 `self.xxx` 调用关系必须完整保留 —— 不能改成模块级函数。
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
import uuid
from typing import Optional
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse, urlunparse

from platforms.chatgpt.protocol.mail_provider import MailProvider
from platforms.chatgpt.protocol.response_summary import describe_error
from platforms.chatgpt.protocol.totp import totp_now as _totp_now

logger = logging.getLogger(__name__)


class CodexMixin:
    @staticmethod
    def _b64url_no_pad(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("utf-8").rstrip("=")


    def _remember_oauth_params(self, auth_url: str):
        """从 authorize URL 记住 OAuth 参数，供后续 token exchange 使用。"""
        if not auth_url:
            return
        self._oauth_auth_url = auth_url
        try:
            qs = parse_qs(urlparse(auth_url).query)
            self._oauth_client_id = (qs.get("client_id", [self._oauth_client_id])[0] or self._oauth_client_id).strip()
            self._oauth_redirect_uri = (
                qs.get("redirect_uri", [self._oauth_redirect_uri])[0] or self._oauth_redirect_uri
            ).strip()
            self._oauth_scope = (qs.get("scope", [""])[0] or "").strip()
            self._oauth_state = (qs.get("state", [""])[0] or "").strip()
        except Exception:
            return


    def _build_pkce_pair(self, raw_bytes: int = 64) -> tuple[str, str]:
        """生成 (code_verifier, code_challenge)。"""
        verifier = self._b64url_no_pad(secrets.token_bytes(max(32, int(raw_bytes))))
        if len(verifier) < 43:
            verifier = (verifier + ("A" * 43))[:43]
        if len(verifier) > 128:
            verifier = verifier[:128]
        challenge = self._b64url_no_pad(hashlib.sha256(verifier.encode("utf-8")).digest())
        return verifier, challenge


    def _build_codex_authorize(self, prompt_override: Optional[str] = None) -> tuple[str, str, str, str, str]:
        """
        构建用于获取 refresh_token 的 Codex OAuth 授权 URL。
        使用独立 client_id + redirect_uri + 可控 PKCE。
        """
        client_id = self._get_env("OAUTH_CODEX_CLIENT_ID", "").strip() or "app_EMoamEEZ73f0CkXaXp7hrann"
        redirect_uri = self._get_env("OAUTH_CODEX_REDIRECT_URI", "").strip() or "http://localhost:1455/auth/callback"
        scope = self._get_env("OAUTH_CODEX_SCOPE", "").strip() or "openid email profile offline_access"
        state = self._b64url_no_pad(secrets.token_bytes(24))
        verifier, challenge = self._build_pkce_pair()
        prompt = (
            self._get_env("OAUTH_CODEX_PROMPT", "login").strip()
            if prompt_override is None
            else (prompt_override or "").strip()
        )
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": scope,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        if prompt:
            params["prompt"] = prompt
        auth_url = f"https://auth.openai.com/oauth/authorize?{urlencode(params)}"
        return auth_url, state, verifier, redirect_uri, client_id


    @staticmethod
    def _callback_has_code(url: str, redirect_uri: str) -> bool:
        if not url:
            return False
        try:
            cb_base = (redirect_uri or "").split("?", 1)[0].rstrip("/")
            target = url.split("?", 1)[0].rstrip("/")
            if cb_base and target == cb_base:
                qs = parse_qs(urlparse(url).query)
                return bool((qs.get("code", [""])[0] or "").strip())
        except Exception:
            return False
        return False


    def _follow_authorize_for_callback(self, start_url: str, redirect_uri: str, trace_prefix: str) -> tuple[str, str]:
        """
        跟随 auth.openai.com 授权链路，捕获 callback（不消费 callback）。
        返回 (callback_url, final_url)。
        """
        current = start_url
        callback_url = ""
        chose_account = False  # /choose-an-account 每条链路只选一次，防 200/同 URL 循环
        for i in range(12):
            if self._callback_has_code(current, redirect_uri):
                callback_url = current
                break
            resp = self.session.get(
                current,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://chatgpt.com/",
                    "User-Agent": self._ua,
                },
                timeout=30,
                allow_redirects=False,
            )
            self._trace_http(f"{trace_prefix}_hop_{i+1}", resp)

            # workspace/consent 页面 200 时，主动选择 workspace，拿下一跳 continue_url
            if resp.status_code == 200:
                is_workspace_like = (
                    ("/workspace" in current)
                    or ("/sign-in-with-chatgpt/" in current)
                    or ("/consent" in current)
                )
                if is_workspace_like:
                    workspace_id = self._extract_workspace_id() or self._extract_workspace_id_from_html(resp.text or "")
                    if workspace_id:
                        next_url = self._workspace_select(workspace_id)
                        if next_url:
                            if next_url.startswith("/"):
                                next_url = urljoin("https://auth.openai.com", next_url)
                            current = next_url
                            continue

                # /choose-an-account：OpenAI 已登录多账号的选择页（react-router SSR）。
                # HTML 里 streamController.enqueue 注入 unified_sessions[].id (us_*) 和
                # authsess_*。protocol 端要主动选第一个 us_*，否则 codex callback 拿不到。
                if "/choose-an-account" in current and not chose_account:
                    chose_account = True
                    next_url = self._choose_account_select(resp.text or "", current)
                    if next_url:
                        if next_url.startswith("/"):
                            next_url = urljoin("https://auth.openai.com", next_url)
                        current = next_url
                        continue

            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            loc = (resp.headers.get("Location", "") or "").strip()
            if not loc:
                break
            if loc.startswith("/"):
                loc = urljoin(current, loc)
            if self._callback_has_code(loc, redirect_uri):
                callback_url = loc
                current = loc
                break
            current = loc
        return callback_url, current


    @staticmethod
    def _drop_query_keys(url: str, drop_keys: set[str]) -> str:
        if not url:
            return ""
        try:
            parsed = urlparse(url)
            params = parse_qsl(parsed.query, keep_blank_values=True)
            kept = [(k, v) for (k, v) in params if (k or "").strip() not in drop_keys]
            return urlunparse(parsed._replace(query=urlencode(kept)))
        except Exception:
            return url


    def _exchange_codex_callback_code(
        self,
        callback_url: str,
        expected_state: str,
        verifier: str,
        redirect_uri: str,
        client_id: str,
    ) -> bool:
        qs = parse_qs(urlparse(callback_url).query)
        code = (qs.get("code", [""])[0] or "").strip()
        got_state = (qs.get("state", [""])[0] or "").strip()
        if not code:
            logger.warning("Codex callback 缺少 code")
            return False
        if expected_state and got_state and got_state != expected_state:
            logger.warning("Codex callback state 不匹配，期望=%s 实际=%s", expected_state[:20], got_state[:20])
            return False

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "Origin": "https://auth.openai.com",
            "Referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            "User-Agent": self._ua,
        }
        form = {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }
        encoded_form = urlencode(form)
        resp = self.session.post(
            "https://auth.openai.com/oauth/token",
            headers=headers,
            data=encoded_form,
            timeout=30,
        )
        self._trace_http(
            "oauth_token_exchange_codex_pkce",
            resp,
            extra_request={
                "method": "POST",
                "url": "https://auth.openai.com/oauth/token",
                "body": encoded_form,
                "headers": headers,
            },
        )
        if resp.status_code != 200:
            logger.warning("Codex oauth/token 失败: %s - %s", resp.status_code, describe_error(resp.text))
            return False
        data = resp.json() if resp is not None else {}
        self.result.id_token = data.get("id_token", self.result.id_token)
        self.result.access_token = data.get("access_token", self.result.access_token)
        self.result.refresh_token = data.get("refresh_token", self.result.refresh_token)
        logger.info(
            "Codex OAuth 交换成功: access=%s refresh=%s",
            "有" if self.result.access_token else "无",
            "有" if self.result.refresh_token else "无",
        )
        return True


    def _codex_drive_login_from_log_in(self, mail_provider: Optional[MailProvider] = None) -> str:
        """
        当 Codex 授权回落到 /log-in 时，补走一次纯协议登录推进状态机。
        返回可继续跟随的 continue_url（若无则返回空字符串）。
        """
        email = (self.result.email or "").strip()
        if not email:
            logger.warning("Codex 登录推进缺少 email")
            return ""
        password, pw_is_real = self._resolve_login_password(email)
        if pw_is_real:
            self.result.password = password
        else:
            # 猜的密码只拿去碰一下 401，不落进 result（否则会被当成真密码存库/打日志）
            logger.info("Codex 登录推进：该号无已知密码，用默认规则猜一个试试（多半 401）")

        device_id = (self.result.device_id or "").strip() or self._get_cookie_value_by_name("oai-did")
        if not device_id:
            device_id = str(uuid.uuid4())
            self.result.device_id = device_id

        sentinel = self.get_sentinel_token(device_id)
        step = self.authorize_continue(
            email=email,
            sentinel_token=sentinel,
            screen_hint="login",
            referer="https://auth.openai.com/log-in",
            trace_step="authorize_continue_login_codex",
        )
        page_type = self._extract_page_type(step)
        continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(step))

        if page_type == "login_password" or "/log-in/password" in continue_url:
            step = self.login_password_verify(password)
            page_type = self._extract_page_type(step)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(step))

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
                return continue_url or ""
            # 从 continue_url 提取 challenge_id
            challenge_id = continue_url.split("/")[-1] if "/mfa-challenge/" in continue_url else ""
            if not challenge_id:
                logger.warning("无法从 continue_url 提取 challenge_id")
                return continue_url or ""
            # 计算当前 TOTP 码并提交
            totp_code = _totp_now(totp_secret)
            logger.info(f"提交 TOTP 码进行 2FA 验证（challenge_id={challenge_id[:16]}...）")
            mfa_resp = self.submit_mfa_totp(totp_code, challenge_id)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(mfa_resp))

        need_otp = (page_type == "email_otp_verification") or ("/email-verification" in (continue_url or ""))
        if need_otp:
            if mail_provider is None:
                logger.warning("Codex 登录推进需要 OTP，但未提供 mail_provider")
                return continue_url or ""
            try:
                otp_timeout = max(10, int(self._get_env("OTP_TIMEOUT", "60")))
            except Exception:
                otp_timeout = 180
            otp_sent_at = time.time()
            if not self.kickoff_otp_delivery("codex_login_need_otp"):
                self.send_otp()
            otp_code = mail_provider.wait_for_otp(
                email,
                timeout=otp_timeout,
                issued_after=otp_sent_at,
            )
            otp_resp = self.verify_otp(otp_code)
            continue_url = self._normalize_continue_url(self._extract_continue_url_from_step(otp_resp))

        # add-phone 分支（可选）：
        # 仅在配置了手机号与验证码获取方式时尝试自动推进
        if self._is_add_phone_state(page_type="", continue_url=continue_url):
            next_url = self._handle_add_phone_verification(continue_url=continue_url)
            if next_url:
                continue_url = self._normalize_continue_url(next_url)

        return continue_url or ""


    def _codex_refresh_retry_after_add_phone(
        self,
        auth_url: str,
        redirect_uri: str,
        attempts: int = 3,
        sleep_seconds: float = 1.2,
    ) -> tuple[str, str]:
        """
        当命中 add-phone 时，按“刷新重试”策略重复发起 authorize，
        期望命中不需要 add-phone 的分支并直接拿 callback code。
        """
        callback_url = ""
        final_url = ""
        start_url = self._drop_query_keys(auth_url, {"prompt"}) or auth_url
        rounds = max(1, int(attempts))
        wait_s = max(0.0, float(sleep_seconds))

        for i in range(rounds):
            callback_url, final_url = self._follow_authorize_for_callback(
                start_url,
                redirect_uri,
                f"codex_add_phone_refresh_retry_{i+1}",
            )
            if callback_url:
                return callback_url, final_url
            if i < rounds - 1 and wait_s > 0:
                time.sleep(wait_s)

        return callback_url, final_url


    def oauth_codex_rt_exchange(
        self,
        mail_provider: Optional[MailProvider] = None,
        *,
        speculative: bool = False,
    ) -> bool:
        """
        纯协议方式获取 RT：
        - 使用独立 Codex OAuth 参数重新授权（可控 PKCE）
        - 捕获 callback code（不消费）
        - 直接调 /oauth/token 交换 access_token + refresh_token

        `speculative=True` 表示这是**抢跑**尝试（在 callback 链路跑完之前先试一把，
        目的是保留 auth.openai 登录态）。抢跑失败**不消耗**本轮的尝试名额 ——
        否则它把 `_codex_rt_attempted` 一置，后面真正有条件成功的那次就会被
        当成「重复尝试」跳过（实测：`refresh_token` 模式跑完库里 RT 是空的，
        而日志里只有一行 DEBUG 说明原因，任务却报成功）。
        """
        allow_retry = self._env_flag("OAUTH_CODEX_RT_ALLOW_RETRY", "0")
        if self._codex_rt_attempted and (not allow_retry):
            # INFO 而不是 DEBUG：这一跳过会让整个 `refresh_token` 模式白跑，
            # 必须能从任务日志里看见（协议日志按 INFO 镜像）。
            logger.info("Codex RT 本轮已尝试过，跳过重复尝试")
            return False
        if not speculative:
            self._codex_rt_attempted = True

        logger.info("尝试 Codex OAuth 直连换取 refresh_token ...")
        try:
            auth_url, state, verifier, redirect_uri, client_id = self._build_codex_authorize()
            self._oauth_auth_url = auth_url
            self._oauth_client_id = client_id
            self._oauth_redirect_uri = redirect_uri
            self._oauth_state = state
            callback_url, final_url = self._follow_authorize_for_callback(
                auth_url, redirect_uri, "codex_authorize"
            )

            # 若被打回 /log-in，补走一次协议登录，再继续授权链路
            if (not callback_url) and "/log-in" in (final_url or ""):
                logger.info("Codex 授权回落到 /log-in，尝试协议推进登录状态...")
                continue_url = ""
                try:
                    continue_url = self._codex_drive_login_from_log_in(mail_provider=mail_provider)
                except Exception as e:
                    logger.warning(f"Codex 登录推进失败，改走 no-prompt 兜底: {e}")
                if continue_url:
                    # 命中 add-phone 时，支持“刷新重试”策略（不立刻放弃）
                    if self._is_add_phone_state(page_type="", continue_url=continue_url) and self._env_flag(
                        "OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY", "1"
                    ):
                        try:
                            retry_count = max(1, int(self._get_env("OAUTH_CODEX_ADD_PHONE_REFRESH_RETRY_COUNT", "3")))
                        except Exception:
                            retry_count = 3
                        try:
                            retry_sleep = max(0.0, float(self._get_env("OAUTH_CODEX_ADD_PHONE_REFRESH_SLEEP", "1.2")))
                        except Exception:
                            retry_sleep = 1.2
                        logger.info("命中 add-phone，执行 authorize 刷新重试: count=%s sleep=%.1fs", retry_count, retry_sleep)
                        callback_url, final_url = self._codex_refresh_retry_after_add_phone(
                            auth_url=auth_url,
                            redirect_uri=redirect_uri,
                            attempts=retry_count,
                            sleep_seconds=retry_sleep,
                        )
                    else:
                        callback_url, final_url = self._follow_authorize_for_callback(
                            continue_url,
                            redirect_uri,
                            "codex_post_login",
                        )

            # Codex authorize 直接被打到 /add-phone（不经过 /log-in）：
            # 如果配了 SMS 接码 controller，先把手机号绑了再重新 authorize
            if (not callback_url) and self._is_add_phone_state(page_type="", continue_url=final_url or "") \
                    and self._sms_callback is not None:
                logger.info("Codex 授权直接落到 /add-phone，尝试 SMS 接码绑号 ...")
                try:
                    self._handle_add_phone_via_sms(continue_url=final_url)
                    # 绑号成功后重新 authorize 拿 callback code
                    callback_url, final_url = self._follow_authorize_for_callback(
                        auth_url, redirect_uri, "codex_authorize_after_add_phone"
                    )
                    if not callback_url:
                        no_prompt_url = self._drop_query_keys(auth_url, {"prompt"})
                        if no_prompt_url and no_prompt_url != auth_url:
                            callback_url, final_url = self._follow_authorize_for_callback(
                                no_prompt_url,
                                redirect_uri,
                                "codex_authorize_noprompt_after_add_phone",
                            )
                except Exception as e:
                    logger.warning(f"SMS 接码绑号失败: {e}")

            # 兜底：去掉 prompt=login 再发起一次授权
            if not callback_url:
                no_prompt_url = self._drop_query_keys(auth_url, {"prompt"})
                if no_prompt_url and no_prompt_url != auth_url:
                    callback_url, final_url = self._follow_authorize_for_callback(
                        no_prompt_url,
                        redirect_uri,
                        "codex_authorize_noprompt",
                    )

            if not callback_url:
                logger.debug("Codex OAuth 未捕获 callback code, final=%s", (final_url or "")[:180])
                return False
            return self._exchange_codex_callback_code(
                callback_url=callback_url,
                expected_state=state,
                verifier=verifier,
                redirect_uri=redirect_uri,
                client_id=client_id,
            )
        except Exception as e:
            logger.warning(f"Codex OAuth 交换异常: {e}")
            return False
