"""Kiro 平台插件。

**Kiro 的账号就是 AWS Builder ID**：注册走 `platforms.aws.client` 同一条链路
（OIDC → device → portal → workflow → OTP → 密码 → SSO），拿到 SSO 会话后再换
Kiro IDE 的 AT/RT（`authorize.authorize_kiro`）。

数据口径（用户要求「kiro 需要保存来源（aws）、aws（账号密码）、凭据」）：

| 位置 | 存什么 |
| --- | --- |
| `password` 列 | **AWS 的密码**（Kiro 登录就是它） |
| `extra.source` | `aws` —— 来源平台，可据此追到 `platforms.aws` 的同邮箱账号 |
| `extra.source_email` | 来源 AWS 账号的邮箱（= 本账号邮箱） |
| `extra.access_token` / `refresh_token` / `client_id` / `client_secret` / `profile_arn` | **Kiro 的凭据**（`kiro.json` 的 `pluginSecret` 同款） |
| `extra.kiro_aws_*` | 来源账号的 Builder ID 凭据快照（追溯/复核用） |
| `token` 列 | 镜像 `refresh_token`（长期凭据） |

**为什么不复用 `AwsPlatform` 实例**：注册任务按平台挑选插件，`kiro` 的任务要用
`kiro` 插件（否则账号会落到 `aws` 平台库里）。这里把 AWS 链路以**组合**方式
取用（`_register_aws_identity`），两个平台的注册逻辑只有一份实现。
"""
from __future__ import annotations

import logging
import time
from typing import Any

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.registry import register

from .authorize import KiroAuthorizeError, authorize_kiro
from .constants import (
    AUTH_METHOD_IDC,
    DEFAULT_PLAN,
    KIRO_REDIRECT_URI,
    SOURCE_AWS,
)
from .credentials import build_extra
from .probe import probe_kiro, refresh_kiro

logger = logging.getLogger(__name__)

#: 来源账号的 Builder ID 凭据在 `extra` 里的前缀。
SOURCE_PREFIX = "kiro_aws_"


def _source_snapshot(identity: dict[str, Any]) -> dict[str, str]:
    """来源 AWS 账号的凭据快照（重塑前缀，避免与 Kiro 凭据的键撞车）。"""
    snapshot: dict[str, str] = {}
    for key in ("access_token", "refresh_token", "client_id", "client_secret", "sso", "region"):
        value = str(identity.get(key) or "").strip()
        if value:
            snapshot[f"{SOURCE_PREFIX}{key}"] = value
    return snapshot


@register
class KiroPlatform(BasePlatform):
    name = "kiro"
    display_name = "Kiro"
    version = "1.0.0"
    #: 纯协议：注册与授权都是 HTTP 状态机。
    supported_executors = ["protocol"]
    executor_labels = {"protocol": "纯协议（AWS Builder ID + Kiro OAuth）"}
    uses_mailbox = True
    registration_modes = [
        {
            "key": "kiro_plan",
            "label": "账号套餐标注",
            "desc": "只影响本地记录的套餐名（注册出来的都是 FREE）",
            "default": DEFAULT_PLAN,
            "options": [
                {"value": "Kiro FREE", "label": "Kiro FREE"},
                {"value": "Kiro Pro", "label": "Kiro Pro"},
            ],
        },
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox
        self.last_registration_meta: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        log_fn = getattr(self, "_log_fn", None)
        if callable(log_fn):
            log_fn(message)
        else:
            print(message)

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def _register_aws_identity(
        self, *, mailbox, email: str, password: str, extra: dict[str, Any]
    ) -> dict[str, Any]:
        """走 AWS Builder ID 注册链路拿 SSO 会话（复用 `AwsPlatform`）。"""
        from platforms.aws.plugin import AwsPlatform

        aws_platform = AwsPlatform(config=self.config, mailbox=mailbox)
        aws_platform._log_fn = self._log
        aws_platform._task_control = getattr(self, "_task_control", None)
        account = aws_platform.register(email=email, password=password)
        identity = dict(account.extra or {})
        identity["email"] = account.email
        identity["password"] = account.password

        # 来源账号**立即落库**：Kiro 授权失败时 AWS 号已经真实存在（密码也设过），
        # 丢掉它就等于白烧一个号 + 一个邮箱。落进 `aws` 平台库，Kiro 记录用
        # `source/source_email` 指回来。
        try:
            from core.db import account_repository
            from core.models.account import AccountRecord

            source_record = AccountRecord(
                platform="aws",
                email=account.email,
                password=account.password,
                region=str(identity.get("region") or ""),
                token=str(identity.get("refresh_token") or ""),
                status=AccountStatus.REGISTERED,
                extra=dict(identity),
            )
            account_repository.upsert(source_record)
            self._log(f"[Kiro] 来源 AWS 账号已保存: {account.email}")
        except Exception as exc:  # noqa: BLE001 - 落库失败不该抹掉已完成的注册
            logger.debug("来源 AWS 账号落库失败（继续）: %s", exc)
        return identity

    def register(self, email: str = None, password: str = None) -> Account:
        extra = dict((self.config.extra or {}) if self.config else {})
        proxy = (self.config.proxy if self.config else None) or None
        requested_email = str(email or "").strip()

        # 判重：同邮箱在 kiro 上已注册就不再跑（会白烧一个 AWS 号）
        if requested_email and self.is_email_registered(requested_email):
            raise RuntimeError(f"该邮箱已注册 Kiro，跳过: {requested_email}")

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
        mailbox._platform_name = SOURCE_AWS  # AWS 号先被消耗一次

        identity = self._register_aws_identity(
            mailbox=mailbox,
            email=requested_email,
            password=str(password or "").strip(),
            extra=extra,
        )
        account_email = str(identity.get("email") or requested_email or "").strip()
        if not account_email:
            raise RuntimeError("AWS 注册未返回邮箱")
        aws_password = str(identity.get("password") or password or "")

        sso_token = str(identity.get("sso") or "")
        if not sso_token:
            raise RuntimeError("AWS 注册未拿到 SSO 会话，无法换取 Kiro 凭据")

        try:
            tokens = authorize_kiro(
                sso_token=sso_token,
                proxy=proxy or "",
                redirect_port=None,
                log=self._log,
            )
        except KiroAuthorizeError as exc:
            # AWS 号已经建好了 —— 抛出时带上「号还在」，让调用方知道别重复注册
            self._log(f"[Kiro] 授权失败（AWS 账号已创建）: {exc}")
            raise RuntimeError(
                "Kiro 授权失败（AWS 账号已创建并保存到 aws 平台，密码见该账号记录）: "
                f"{exc}"
            ) from exc

        plan = str(extra.get("kiro_plan") or DEFAULT_PLAN).strip() or DEFAULT_PLAN
        kiro_extra = build_extra(
            access_token=tokens.get("access_token", ""),
            refresh_token=tokens.get("refresh_token", ""),
            client_id=tokens.get("client_id", ""),
            client_secret=tokens.get("client_secret", ""),
            expires_in=tokens.get("expires_in", 0),
            profile_arn=tokens.get("profile_arn", ""),
            region=tokens.get("region", ""),
            auth_method=AUTH_METHOD_IDC,
            login_provider="BuilderId",
            plan=plan,
            account_id=account_email,
            uid="builderid",
            email=account_email,
            name=f"{account_email} (BuilderId)",
            source=SOURCE_AWS,
            source_email=account_email,
        )
        kiro_extra.update(_source_snapshot(identity))
        kiro_extra["register_mode"] = "protocol"
        if identity.get("user_code"):
            kiro_extra["kiro_aws_user_code"] = str(identity["user_code"])

        self.last_registration_meta = dict(kiro_extra)
        return Account(
            platform=self.name,
            email=account_email,
            password=aws_password,
            user_id=str(kiro_extra.get("account_id") or ""),
            region=str(kiro_extra.get("region") or ""),
            token=str(kiro_extra.get("refresh_token") or ""),
            status=AccountStatus.REGISTERED,
            extra=kiro_extra,
        )

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
            # 只有导入的 social 账号可能没有 client_secret（GitHub/Google 登录），
            # 这类号无法用 IdC 刷新 —— 如实报告，别假装探测过。
            return {
                "code": None,
                "usable": False,
                "summary": "缺少 Kiro refresh_token 或 IdC 客户端凭据（social 账号无法刷新）",
            }
        proxy = (self.config.proxy if self.config else None) or ""
        detail = probe_kiro(proxy=proxy, **credentials)
        detail["refreshed_at"] = int(time.time())
        return detail

    def check_valid(self, account: Account) -> bool:
        detail = self.probe_account_detail(account)
        self.last_probe_detail = detail
        return bool(detail.get("usable"))

    def apply_probe_status_policy(self, account, *, detail: dict) -> str:
        detail = detail or {}
        if detail.get("usable"):
            if str(getattr(account, "status", "") or "").lower() != AccountStatus.REGISTERED.value:
                account.status = AccountStatus.REGISTERED
                return "探测通过，恢复为正常"
            return ""
        if detail.get("banned") or detail.get("code") == 403:
            account.status = AccountStatus.BANNED
            return "上游返回 403（账号被停用）"
        account.status = AccountStatus.INVALID
        return f"Kiro 刷新失败: {str(detail.get('summary') or '')[:120]}"

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
                "data": {"status": detail.get("code"), "probe": detail},
                "error": "" if detail.get("usable") else str(detail.get("summary") or ""),
            }

        if action_id == "refresh_token":
            result = refresh_kiro(proxy=proxy, **credentials)
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
            "plan": extra.get("plan"),
            "has_access_token": bool(extra.get("access_token")),
            "has_refresh_token": bool(extra.get("refresh_token")),
            "source": extra.get("source"),
            "probe_status": extra.get("probe_status"),
        }

    # ------------------------------------------------------------------
    # 静态能力
    # ------------------------------------------------------------------
    @classmethod
    def check_environment(cls):
        """环境预检：`cryptography` 与 `curl_cffi` 是硬依赖（JWE + TLS 指纹）。

        缺依赖时按**环境错误**报（不可重试）—— 每轮重开还是同一个 ImportError。
        """
        from core.environment import EnvironmentVerdict

        try:
            import importlib

            importlib.import_module("curl_cffi")
            importlib.import_module("cryptography.hazmat.primitives.ciphers.aead")
        except Exception as exc:  # noqa: BLE001
            return EnvironmentVerdict(
                ok=False,
                is_environment=True,
                non_retryable=True,
                message=f"Kiro 注册缺少依赖: {type(exc).__name__}: {exc}（pip install -r requirements.txt）",
                hint="pip install cryptography curl-cffi",
            )
        return EnvironmentVerdict(ok=True)


def kiro_credentials_from_account(account: Any) -> dict[str, str]:
    """从账号里读出 Kiro 的 IdC 四件套（导出/刷新共用一条读法）。"""
    extra = getattr(account, "extra", {}) or {}
    if not isinstance(extra, dict):
        extra = {}
    return {
        "client_id": str(extra.get("client_id") or ""),
        "client_secret": str(extra.get("client_secret") or ""),
        "refresh_token": str(extra.get("refresh_token") or getattr(account, "refresh_token", "") or ""),
        "access_token": str(extra.get("access_token") or getattr(account, "access_token", "") or ""),
        "profile_arn": str(extra.get("profile_arn") or ""),
        "source": str(extra.get("source") or ""),
    }


__all__ = [
    "KiroPlatform",
    "KIRO_REDIRECT_URI",
    "SOURCE_PREFIX",
    "kiro_credentials_from_account",
]