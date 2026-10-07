"""AddPhoneMixin：ChatGPT 注册链路命中 add-phone 时的手机号兜底路径。

从 3541 行的 auth_flow.py 拆出（纯搬家，方法体逐字节不变）。
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Optional

from platforms.chatgpt.protocol.response_summary import describe_error

logger = logging.getLogger(__name__)


class AddPhoneMixin:
    @staticmethod
    def _is_add_phone_state(page_type: str = "", continue_url: str = "") -> bool:
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "add_phone") or ("add-phone" in cu)


    @staticmethod
    def _is_mfa_challenge_state(page_type: str = "", continue_url: str = "") -> bool:
        """判断是否进入 mfa-challenge 状态（已有账号启用 2FA，密码验证后需要 TOTP 码）。"""
        pt = (page_type or "").strip().lower()
        cu = (continue_url or "").strip().lower()
        return (pt == "mfa_challenge") or ("/mfa-challenge/" in cu)


    def _phone_headers(self, referer: str) -> dict:
        headers = self._common_headers(referer)
        headers["Accept"] = "application/json"
        headers["Content-Type"] = "application/json"
        headers["Origin"] = "https://auth.openai.com"
        return headers


    def _add_phone_send(self, phone_number: str) -> dict:
        headers = self._phone_headers("https://auth.openai.com/add-phone")
        try:
            resp = self.session.post(
                "https://auth.openai.com/api/accounts/add-phone/send",
                headers=headers,
                json={"phone_number": phone_number},
                timeout=30,
            )
        except Exception as e:
            logger.warning("[add-phone] 网络异常: %s (phone=%s)", e, phone_number)
            raise
        self._trace_http("add_phone_send", resp)

        if resp.status_code != 200:
            # 抛异常时只带 message（不带完整 JSON），让上层日志更简洁
            raise RuntimeError(describe_error(resp.text) or f"HTTP {resp.status_code}")

        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}


    def _phone_otp_validate(self, code: str, referer: str = "") -> dict:
        # 同一个接口在不同链路上的来源页不一样：绑手机是 /phone-verification，
        # 手机号注册停在 /contact-verification，referer 对不上容易被风控盯上。
        headers = self._phone_headers(referer or "https://auth.openai.com/phone-verification")
        resp = self.session.post(
            "https://auth.openai.com/api/accounts/phone-otp/validate",
            headers=headers,
            json={"code": code},
            timeout=30,
        )
        self._trace_http("phone_otp_validate", resp)
        if resp.status_code != 200:
            raise RuntimeError(
                f"phone-otp/validate 失败: {resp.status_code} - {describe_error(resp.text)}"
            )
        try:
            return resp.json() if resp is not None else {}
        except Exception:
            return {}


    @staticmethod
    def _extract_otp6(text: str) -> str:
        if not text:
            return ""
        m = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
        return (m.group(1) if m else "").strip()


    def _read_phone_otp_from_cmd(self) -> str:
        """
        从环境变量 OPENAI_PHONE_OTP_CMD 指定的命令读取手机验证码（stdout）。
        命令输出中只要出现 6 位数字即视为命中。
        """
        cmd = self._get_env("OPENAI_PHONE_OTP_CMD", "").strip()
        if not cmd:
            return ""
        try:
            out = subprocess.check_output(cmd, shell=True, text=True, timeout=20)
            return self._extract_otp6(out or "")
        except Exception:
            return ""


    def _wait_phone_otp(self, timeout: int = 180) -> str:
        static_otp = self._extract_otp6(self._get_env("OPENAI_PHONE_OTP", ""))
        if static_otp:
            return static_otp

        deadline = time.time() + max(20, int(timeout))
        while time.time() < deadline:
            code = self._read_phone_otp_from_cmd()
            if code:
                return code
            time.sleep(4)
        raise TimeoutError(f"等待手机 OTP 超时 ({timeout}s)")


    def _handle_add_phone_verification(self, continue_url: str = "") -> str:
        """
        处理 add-phone 验证分支：
        - 优先使用 self._sms_callback（SMS 接码 controller，自动租号 + 接码）
        - 回退到环境变量路径：OPENAI_PHONE_NUMBER + OPENAI_PHONE_OTP_CMD/OPENAI_PHONE_OTP
        """
        if self._sms_callback is not None:
            try:
                return self._handle_add_phone_via_sms(continue_url)
            except Exception as e:
                logger.warning("SMS 接码流程失败，回退环境变量路径: %s", e)
                try:
                    self._sms_callback.cleanup()
                except Exception:
                    pass
        return self._handle_add_phone_via_env(continue_url)


    def _handle_add_phone_via_sms(self, continue_url: str = "") -> str:
        """走 SMS 接码 controller：租号 → add-phone/send → 等 SMS → validate。

        支持平台：SmsBower（smsbower.page）。
        单号窗口 80s，窗口内只轮询接码平台收码；失败自动 cancel + 换新号。
        最多换号次数默认 3，主人可在 WebUI / 环境变量 OPENAI_PHONE_MAX_ATTEMPTS 自定义。
        """
        ctrl = self._sms_callback

        # 用 try/finally 保证即使 for 循环抛异常，也能 release lock + 最后一次 cleanup
        try:
            return self._do_sms_loop(ctrl, continue_url=continue_url)
        finally:
            # 无论成败都释放 lock + cleanup 最后一个号（如果有）
            try:
                ctrl.cleanup()
            except Exception:
                pass
            try:
                ctrl._release_lock()
            except Exception:
                pass


    def _do_sms_loop(self, ctrl, *, continue_url: str = "") -> str:
        """SMS 接码循环逻辑（for 0..max_attempts）。

        `continue_url` 是进入 add-phone 分支时的当前跳转地址：validate 通过但
        服务端响应里没给下一跳（`next_url` 为空）时作为兜底返回值。
        此前该名字在函数里未定义 —— `return next_url or continue_url or ""`
        一走到 `continue_url` 分支就 NameError，被 except 吞成「validate 失败」
        （明明验证成功却通知接码平台码失败、白耗号码窗口）。
        """
        # provider 信息（目前只支持 SmsBower）
        provider_key = (getattr(ctrl, "provider_key", "") or "").lower()

        # 优先从 controller.config 读（前端配置） → 环境变量兜底 → 用默认
        ctrl_cfg = getattr(ctrl, "config", None) or {}

        def _read_int(cfg_key: str, env_key: str, default: str, min_v: int = 1) -> int:
            raw = (str(ctrl_cfg.get(cfg_key) or "")).strip()
            if not raw:
                raw = self._get_env(env_key, default)
            try:
                return max(min_v, int(raw))
            except Exception:
                return int(default)

        # 单号等待窗口（秒）：默认 80 = 20×3 + 20 缓冲
        per_phone_timeout = max(40, _read_int(
            "sms_per_phone_timeout", "OPENAI_PHONE_OTP_TIMEOUT", "80", min_v=40
        ))
        # 最多换几个号（默认 3）
        max_phone_attempts = _read_int(
            "sms_max_phone_attempts", "OPENAI_PHONE_MAX_ATTEMPTS", "3"
        )
        # 单号内 code validate 失败后的重试次数（如果还有时间）
        max_code_retries_per_phone = _read_int(
            "sms_code_retries_per_phone", "OPENAI_PHONE_OTP_CODE_RETRIES", "2"
        )

        logger.info(
            "[sms] 配置: provider=%s 单号窗口=%ds 最多换号=%d 单号内验证重试=%d",
            provider_key, per_phone_timeout, max_phone_attempts, max_code_retries_per_phone,
        )

        # OpenAI "号已被使用 / 不允许" 类错误关键字
        _PHONE_REJECTED_PATTERNS = (
            "phone_number_already_in_use", "already_in_use", "already_taken",
            "phone_already_verified", "already_verified",
            "disallowed_phone", "invalid_phone_number", "phone_number_invalid",
            "blocked_phone", "phone_number_blocked",
            "suspicious behavior from phone",  # OpenAI 风控：号段可疑
        )
        def _is_phone_rejected(s: str) -> bool:
            sl = (s or "").lower()
            return any(p in sl for p in _PHONE_REJECTED_PATTERNS)

        # OpenAI 侧流程状态已经不在 add-phone 上了（前一个号窗口耗尽、authorize 被
        # 重置等）。这类错和号码无关：接着换号只会一秒一个地重复同样的报错，把钱
        # 烧在租号上。交回上层重走 authorize 才有意义。
        _FLOW_STATE_PATTERNS = (
            "invalid authorization step",
            "invalid_authorization_step",
            "invalid state",
            "invalid_state",
        )

        def _is_flow_state_error(s: str) -> bool:
            sl = (s or "").lower()
            return any(p in sl for p in _FLOW_STATE_PATTERNS)

        last_err: Optional[Exception] = None
        # 同一句未识别错误连着来，说明问题不在号上（OpenAI 侧状态、风控、参数都可能），
        # 再换号也只是按秒烧租号额度。连续 3 次就收手。
        repeated_err = ""
        repeated_count = 0

        for phone_attempt in range(1, max_phone_attempts + 1):
            logger.info("[sms] 🔁 第 %d/%d 个号尝试...", phone_attempt, max_phone_attempts)

            # 阶段 1：租号（第 2+ 个号会自动租新号，SmsBower cache 已被前一次 cleanup 清掉）
            try:
                phone = ctrl.get_phone()
            except Exception as e:
                last_err = e
                logger.warning("[sms] 第 %d 个号租号失败: %s", phone_attempt, e)
                continue
            if not phone:
                last_err = RuntimeError("SMS 接码 controller 未返回手机号")
                continue

            # 阶段 2：通知 OpenAI 发码到这个号
            send_resp = None
            try:
                logger.info("[sms] 📤 准备 POST add-phone/send (phone=%s) ...", phone)
                send_resp = self._add_phone_send(phone)
                logger.info("[sms] ✅ POST add-phone/send 成功 (phone=%s)", phone)
            except Exception as e:
                err_text = str(e)
                if "too many phone verification" in err_text.lower() \
                        or "phone_verification_rate_limit" in err_text.lower():
                    logger.warning(
                        "⚠️ OpenAI 频控: 这个 outlook 号/IP 已累积太多 add-phone 请求，"
                        "建议换 outlook 号或换代理 IP 后重试。本次放弃 add-phone（session_token 仍可保留）"
                    )
                    ctrl.mark_send_failed(err_text)
                    last_err = e
                    break
                if _is_phone_rejected(err_text):
                    logger.warning("[sms] 号 %s 被 OpenAI 拒（已用过/不允许）: %s",
                                   phone, err_text[:200])
                    ctrl.mark_send_failed(err_text)
                    last_err = e
                    continue
                if _is_flow_state_error(err_text):
                    logger.warning(
                        "[sms] add-phone 步骤已失效（%s）：这不是号码问题，"
                        "继续换号只会一秒一个地重复同样的错，本轮到此为止，"
                        "交回上层重走 authorize",
                        err_text[:200],
                    )
                    ctrl.mark_send_failed(err_text)
                    last_err = e
                    break
                # 其它未识别错误 → 也打详细日志但不视为"号码问题"
                logger.warning("[sms] 号 %s POST add-phone/send 失败（未识别错误）: %s",
                               phone, err_text[:300])
                ctrl.mark_send_failed(err_text)
                last_err = e
                if err_text[:300] == repeated_err:
                    repeated_count += 1
                else:
                    repeated_err = err_text[:300]
                    repeated_count = 1
                if repeated_count >= 3:
                    logger.warning(
                        "[sms] 同一个错误连续 %d 个号了（%s），判定与号码无关，本轮停止换号",
                        repeated_count, err_text[:200],
                    )
                    break
                continue

            send_page_type = self._extract_page_type(send_resp)
            send_continue = self._normalize_continue_url(self._extract_continue_url_from_step(send_resp))
            if send_page_type not in ("phone_otp_verification", "external_url") \
                    and "phone-verification" not in (send_continue or ""):
                logger.warning(
                    "add-phone/send 未进入手机验证码页: page=%s continue=%s",
                    send_page_type or "(empty)",
                    (send_continue or "")[:180],
                )
                ctrl.mark_send_failed("did not enter phone-verification page")
                last_err = RuntimeError(f"add-phone/send 未进入 phone-verification: page={send_page_type}")
                continue

            ctrl.mark_send_succeeded()
            repeated_err = ""
            repeated_count = 0

            # 阶段 3：等 SMS code（窗口内只轮询接码平台，不去催发）
            phone_start = time.time()
            seen_codes: set[str] = set()
            code_attempt = 0
            phone_used = False

            while time.time() - phone_start < per_phone_timeout and code_attempt < max_code_retries_per_phone:
                remaining = per_phone_timeout - (time.time() - phone_start)
                if remaining < 10:
                    break
                code_attempt += 1
                logger.info(
                    "[sms] 号 %s 第 %d/%d 次等 SMS (剩余 %ds)",
                    phone, code_attempt, max_code_retries_per_phone, int(remaining),
                )
                code = ctrl.get_code(timeout=int(remaining))
                if not code:
                    break  # 超时换号
                if code in seen_codes:
                    logger.warning("[sms] 收到重复 code=%s，跳过", code)
                    continue
                seen_codes.add(code)
                phone_used = True

                try:
                    validate_resp = self._phone_otp_validate(code)
                    next_url = self._normalize_continue_url(
                        self._extract_continue_url_from_step(validate_resp)
                    )
                    logger.info("[sms] ✅ phone-otp/validate 通过 (phone=%s code=%s) next=%s",
                                phone, code, (next_url or "")[:160])
                    ctrl.report_success()
                    return next_url or continue_url or ""
                except Exception as e:
                    last_err = e
                    err_text = str(e)
                    logger.warning("[sms] validate 失败 (phone=%s code=%s): %s",
                                   phone, code, err_text[:200])
                    ctrl.mark_code_failed(err_text)
                    # 继续 while 循环等下一条 code（同号）

            # 单号窗口结束：cancel 这个号
            logger.warning("[sms] 号 %s 已用尽 %ds 窗口", phone, per_phone_timeout)
            try:
                ctrl.cleanup()
            except Exception:
                pass
            # cleanup 清掉 controller.activation，下一轮 get_phone 会租新号

        # 所有号都失败
        if last_err:
            raise last_err
        raise RuntimeError(f"SMS 接码 {max_phone_attempts} 个号均失败")


    def _handle_add_phone_via_env(self, continue_url: str = "") -> str:
        """
        处理 add-phone 验证分支（环境变量路径，旧用法）：
        - 需要通过环境变量提供号码与验证码来源：
          - OPENAI_PHONE_NUMBER=+1...
          - OPENAI_PHONE_OTP_CMD='...返回短信内容...' 或 OPENAI_PHONE_OTP=123456
        """
        phone_raw = self._get_env("OPENAI_PHONE_NUMBER", "").strip()
        phone_candidates = [x.strip() for x in phone_raw.split(",") if x.strip()]
        if not phone_candidates:
            if self._sms_callback is not None:
                # 走到这里说明接码是配了的，只是刚失败回落过来。再说一遍"未配置"会
                # 把人引到设置页去找一个根本没问题的开关。
                logger.warning(
                    "命中 add-phone：SMS 接码本轮没绑成号，也没配 OPENAI_PHONE_NUMBER 兜底，"
                    "本次无法继续推进"
                )
            else:
                logger.warning("命中 add-phone，但未配置 SMS 接码 / OPENAI_PHONE_NUMBER，无法继续推进")
            return continue_url or ""

        try:
            otp_timeout = max(30, int(self._get_env("OPENAI_PHONE_OTP_TIMEOUT", "180")))
        except Exception:
            otp_timeout = 180

        last_err = ""
        for idx, phone in enumerate(phone_candidates, 1):
            try:
                logger.info("add-phone 尝试号码 %s/%s: %s", idx, len(phone_candidates), phone)
                send_resp = self._add_phone_send(phone)
                send_page_type = self._extract_page_type(send_resp)
                send_continue = self._normalize_continue_url(self._extract_continue_url_from_step(send_resp))
                if send_page_type not in ("phone_otp_verification", "external_url") and "phone-verification" not in (send_continue or ""):
                    logger.warning(
                        "add-phone/send 未进入手机验证码页: page=%s continue=%s",
                        send_page_type or "(empty)",
                        (send_continue or "")[:180],
                    )
                    continue

                phone_code = self._wait_phone_otp(timeout=otp_timeout)
                validate_resp = self._phone_otp_validate(phone_code)
                next_url = self._normalize_continue_url(self._extract_continue_url_from_step(validate_resp))
                logger.info("add-phone 验证通过，next=%s", (next_url or "")[:180])
                return next_url or continue_url or ""
            except Exception as e:
                last_err = str(e)
                logger.warning("add-phone 号码 %s 失败: %s", phone, e)

        if last_err:
            logger.warning("add-phone 阶段未成功: %s", last_err)
        return continue_url or ""
