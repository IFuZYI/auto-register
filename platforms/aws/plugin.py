"""AWS Builder ID 平台插件。

注册的是 **AWS Builder ID**（Amazon Q Developer / Kiro 的身份后端）：
一条纯协议链路，无浏览器 —— TLS 指纹由 `curl_cffi` 伪装，设备指纹走
`crypto.FingerprintBuilder` 的 XXTEA 信封，密码用服务端下发的公钥做 JWE。

产物：`{access_token, refresh_token, client_id, client_secret, sso, region}`，
`token` 列镜像 `refresh_token`（刷新一次就能拿回 AT，比镜像 1 小时过期的 AT 更耐用）。

**与 kiro 平台的关系**：Kiro 的账号就是 Builder ID，`platforms/kiro` 复用本平台
的注册链路并把 `source` 记成 `aws` —— 这样「账号从哪来」在数据里可查。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.models.account import AccountRecord
from core.registry import register

from core.captcha_providers import build_captcha_chain

from .client import AwsBuilderIdClient, AwsEmailAlreadyRegistered, AwsRegistrationError
from .credentials import build_extra
from .probe import probe_builder_id, refresh_builder_id

logger = logging.getLogger(__name__)

#: 默认全名（AWS 建身份时的 `fullName`；参考实现同值，注册时不校验真实性）。
DEFAULT_FULL_NAME = "Test User"

#: 密码字符集：AWS 要求大小写 + 数字 + 符号，长度 ≥ 8。
_PASSWORD_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_PASSWORD_LOWER = "abcdefghijkmnopqrstuvwxyz"
_PASSWORD_DIGITS = "23456789"
_PASSWORD_SYMBOLS = "!@#$%^&*"


def generate_password(length: int = 14) -> str:
    """生成满足 AWS 复杂度要求的随机密码（字母数字符号各至少一个）。"""
    import random
    import secrets

    if length < 8:
        length = 8
    chars = [
        secrets.choice(_PASSWORD_UPPER),
        secrets.choice(_PASSWORD_LOWER),
        secrets.choice(_PASSWORD_DIGITS),
        secrets.choice(_PASSWORD_SYMBOLS),
    ]
    pool = _PASSWORD_UPPER + _PASSWORD_LOWER + _PASSWORD_DIGITS + _PASSWORD_SYMBOLS
    chars.extend(secrets.choice(pool) for _ in range(length - len(chars)))
    random.shuffle(chars)  # noqa: S311 - 只打乱已生成的密码
    return "".join(chars)


@register
class AwsPlatform(BasePlatform):
    name = "aws"
    display_name = "AWS"
    version = "1.0.0"
    #: 只有纯协议：注册链路是 HTTP 状态机，不需要浏览器。
    supported_executors = ["protocol"]
    executor_labels = {"protocol": "纯协议（curl_cffi 指纹伪装）"}
    uses_mailbox = True
    registration_modes = [
        {
            "key": "aws_full_name",
            "label": "账号全名",
            "desc": "AWS 建身份时提交的姓名，服务端不做真实性校验",
            "default": DEFAULT_FULL_NAME,
            "options": [
                {"value": DEFAULT_FULL_NAME, "label": "Test User"},
                {"value": "Alex Chen", "label": "Alex Chen"},
                {"value": "Jordan Lee", "label": "Jordan Lee"},
            ],
        },
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox
        self.last_registration_meta: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 构造点（测试替换：patch `_make_client`）
    # ------------------------------------------------------------------
    def _make_client(self, proxy: Optional[str] = None) -> AwsBuilderIdClient:
        # 打码链按「全局配置 → 验证码」里的 key 现场拼：没配任何 provider 时是
        # 空链，遇到 WAF 会以 `AwsCaptchaRequired` 明确失败（而不是无限重试）。
        return AwsBuilderIdClient(
            proxy=proxy or "",
            log=self._log,
            captcha=build_captcha_chain(),
        )

    def _log(self, message: str) -> None:
        log_fn = getattr(self, "_log_fn", None)
        if callable(log_fn):
            log_fn(message)
        else:
            print(message)

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, email: str = None, password: str = None) -> Account:
        extra = dict((self.config.extra or {}) if self.config else {})
        proxy = (self.config.proxy if self.config else None) or None
        full_name = str(extra.get("aws_full_name") or DEFAULT_FULL_NAME).strip() or DEFAULT_FULL_NAME
        final_password = str(password or "").strip() or generate_password()

        mailbox = self.mailbox
        if mailbox is None:
            from modules.mail import create_mailbox

            mailbox = create_mailbox(
                str(extra.get("mail_provider") or ""),
                extra=extra,
                proxy=proxy,
            )
            mailbox._task_control = getattr(self, "_task_control", None)
            mailbox._log_fn = self._log
        mailbox._platform_name = self.name

        email = str(email or "").strip()
        mailbox_account = None
        if not email:
            mailbox_account = mailbox.get_email()
            email = str(getattr(mailbox_account, "email", "") or "").strip()
        elif mailbox is not None:
            # 指定邮箱时优先复用渠道**上一次返回的账号对象**：多数渠道收码要
            # `account_id`（inbox token），只给地址读不到信。
            # 缓存里没有就退回裸地址 —— 与 grok 路径同一约定（渠道自己按地址
            # 检索；检索不到会超时，而不是静默拿到别人的信）。
            cached = getattr(mailbox, "_last_account", None)
            if cached is not None and str(getattr(cached, "email", "") or "") == email:
                mailbox_account = cached
        if not email:
            raise RuntimeError("邮箱分配失败：未拿到邮箱地址")

        client = self._make_client(proxy)

        def _otp() -> str:
            timeout = self.get_mailbox_otp_timeout()
            # 关键词必须是 `aws`：AWS 的邮件来自 `no-reply@signin.aws`（iCloud 转发后
            # 变成 `no-reply_at_signin_aws_…`），正文写「AWS Builder ID」——
            # 写 `amazon` 时邮件到了也会被渠道的关键词过滤跳过（2026-11-13 实测）。
            kwargs: dict[str, Any] = {"keyword": "aws", "timeout": timeout}
            if mailbox_account is not None:
                try:
                    return mailbox.wait_for_code(mailbox_account, **kwargs)
                except TypeError:
                    return mailbox.wait_for_code(mailbox_account)
            # 只有地址时渠道也能查（Outlook 等按地址检索）
            from core.mailboxes.base import MailboxAccount

            return mailbox.wait_for_code(
                MailboxAccount(email=email, account_id=""), **kwargs
            )

        try:
            result = client.register(
                email=email,
                password=final_password,
                full_name=full_name,
                otp_provider=_otp,
            )
        except AwsEmailAlreadyRegistered as exc:
            self._record_mailbox_status(mailbox, mailbox_account, "used")
            raise RuntimeError(f"该邮箱已有 AWS Builder ID，跳过: {email}") from exc
        except AwsRegistrationError as exc:
            if getattr(client, "password_set", False):
                # 号已经真实创建（密码设过），只是 SSO/换 token 没走完。
                # 不落库就白烧一个号 + 一个邮箱；落成「待补凭据」，之后可以用
                # Kiro 的「指定 AWS 账号」或重试补回。
                self._save_partial_account(client, email, final_password)
                self._record_mailbox_status(mailbox, mailbox_account, "used")
                raise RuntimeError(
                    f"AWS 账号已创建（密码已设置），但拿凭据失败：{exc}。"
                    "账号已保存到「授权账号」，可稍后重试补凭据。"
                ) from exc
            self._record_mailbox_status(mailbox, mailbox_account, "failed")
            raise
        except Exception as exc:
            logger.debug("AWS 注册失败: %s", exc)
            self._record_mailbox_status(mailbox, mailbox_account, "failed")
            raise

        self._record_mailbox_status(mailbox, mailbox_account, "used")
        account_extra = build_extra(result)
        account_extra["register_mode"] = "protocol"
        account_extra["full_name"] = full_name

        self.last_registration_meta = dict(account_extra)
        return Account(
            platform=self.name,
            email=email,
            password=final_password,
            user_id="",
            region=str(account_extra.get("region") or ""),
            token=str(account_extra.get("refresh_token") or ""),
            status=AccountStatus.REGISTERED,
            extra=account_extra,
        )

    def _save_partial_account(self, client, email: str, password: str) -> None:
        """把「密码已设、凭据没拿到」的 AWS 号落库（带 `needs_sso` 标记）。"""
        try:
            from core.db import account_repository
            from core.models.account import AccountRecord

            extra = {
                "client_id": str(getattr(client, "client_id", "") or ""),
                "client_secret": str(getattr(client, "client_secret", "") or ""),
                "region": str(getattr(client, "region", "") or "us-east-1"),
                "provider": "BuilderId",
                "needs_sso": True,
                "register_mode": "protocol",
            }
            account_repository.upsert(
                AccountRecord(
                    platform="aws",
                    email=email,
                    password=password,
                    region=extra["region"],
                    token="",
                    status=AccountStatus.REGISTERED,
                    extra=extra,
                )
            )
            self._log(f"[AWS] 半成品账号已保存（待补凭据）: {email}")
        except Exception as exc:  # noqa: BLE001 - 落库失败不该掩盖原始错误
            logger.debug("半成品账号落库失败（继续抛原始错误）: %s", exc)

    def get_mailbox_otp_timeout(self, default: int = 300) -> int:
        """AWS 的 OTP 等待默认 300s（覆盖基类的 180s）。

        实测（2026-11-13）：AWS 验证码邮件约 3 分钟后才到，180s 会卡在边界上 ——
        超时后工作流状态丢失，只能整轮重来（白等一次发码）。
        配置里显式给了 `mailbox_otp_timeout_seconds` 时以配置为准。
        """
        return super().get_mailbox_otp_timeout(default=default)

    def _record_mailbox_status(self, mailbox, account, status: str) -> None:
        """把注册结果记回邮箱渠道（号池记账钩子；渠道不支持时静默跳过）。"""
        recorder = getattr(mailbox, "set_account_status", None)
        if not callable(recorder) or account is None:
            return
        try:
            recorder(account, status)
        except TypeError:
            try:
                recorder(account, status, "")
            except Exception as exc:  # noqa: BLE001
                logger.debug("邮箱记账失败（忽略）: %s", exc)
        except Exception as exc:  # noqa: BLE001 - 记账失败不影响注册结论
            logger.debug("邮箱记账失败（忽略）: %s", exc)

    # ------------------------------------------------------------------
    # 有效性 / 测活
    # ------------------------------------------------------------------
    def probe_account_detail(self, account: Account) -> dict[str, Any]:
        extra = account.extra or {}
        credentials = {
            "client_id": str(extra.get("client_id") or ""),
            "client_secret": str(extra.get("client_secret") or ""),
            "refresh_token": str(extra.get("refresh_token") or account.refresh_token or ""),
        }
        if not credentials["refresh_token"]:
            return {"code": None, "usable": False, "summary": "缺少 refresh_token"}
        proxy = (self.config.proxy if self.config else None) or ""
        detail = probe_builder_id(proxy=proxy, **credentials)
        detail["refreshed_at"] = int(time.time())
        return detail

    def check_valid(self, account: Account) -> bool:
        detail = self.probe_account_detail(account)
        self.last_probe_detail = detail
        return bool(detail.get("usable"))

    def apply_probe_status_policy(self, account, *, detail: dict) -> str:
        """探测结论 → 状态：200 恢复正常，403 落 banned，其余落 invalid。"""
        detail = detail or {}
        if detail.get("usable"):
            if str(getattr(account, "status", "") or "").lower() != AccountStatus.REGISTERED.value:
                account.status = AccountStatus.REGISTERED
                return "探测通过，恢复为正常"
            return ""
        if detail.get("banned"):
            account.status = AccountStatus.BANNED
            return "上游返回 403（封禁）"
        account.status = AccountStatus.INVALID
        return f"刷新失败: {str(detail.get('summary') or '')[:120]}"

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def get_platform_actions(self) -> list:
        return [
            {"id": "probe", "label": "测活", "params": []},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        extra = account.extra or {}
        credentials = {
            "client_id": str(extra.get("client_id") or ""),
            "client_secret": str(extra.get("client_secret") or ""),
            "refresh_token": str(extra.get("refresh_token") or account.refresh_token or ""),
        }
        proxy = (self.config.proxy if self.config else None) or ""

        if action_id == "probe":
            detail = self.probe_account_detail(account)
            return {
                "ok": bool(detail.get("usable")),
                "data": {
                    "status": detail.get("code"),
                    "probe": detail,
                    "summary": detail.get("summary") or "",
                },
                "error": "" if detail.get("usable") else str(detail.get("summary") or ""),
            }

        if action_id == "refresh_token":
            result = refresh_builder_id(proxy=proxy, **credentials)
            if not result.get("ok"):
                return {
                    "ok": False,
                    "error": str(result.get("error") or "刷新失败"),
                    "data": {"status": result.get("code")},
                }
            return {
                "ok": True,
                "data": {
                    "access_token": result["access_token"],
                    "refresh_token": result["refresh_token"],
                    "expires_in": result.get("expires_in", 0),
                    "message": "刷新成功",
                },
            }

        raise NotImplementedError(f"平台 {self.name} 不支持操作: {action_id}")

    # ------------------------------------------------------------------
    # 配额
    # ------------------------------------------------------------------
    def get_quota(self, account: Account) -> dict:
        extra = account.extra or {}
        return {
            "has_access_token": bool(extra.get("access_token")),
            "has_refresh_token": bool(extra.get("refresh_token")),
            "expires_in": extra.get("expires_in", 0),
            "probe_status": extra.get("probe_status"),
        }


def builder_id_from_account(account: AccountRecord | Account) -> dict[str, str]:
    """从账号记录里读出 Builder ID 的四件套（kiro 平台复用这条读法）。"""
    extra = getattr(account, "extra", {}) or {}
    if not isinstance(extra, dict):
        extra = {}
    return {
        "client_id": str(extra.get("client_id") or ""),
        "client_secret": str(extra.get("client_secret") or ""),
        "refresh_token": str(extra.get("refresh_token") or getattr(account, "refresh_token", "") or ""),
        "access_token": str(extra.get("access_token") or getattr(account, "access_token", "") or ""),
        "sso": str(extra.get("sso") or ""),
        "region": str(extra.get("region") or ""),
    }


__all__ = ["AwsPlatform", "DEFAULT_FULL_NAME", "builder_id_from_account", "generate_password"]