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
from datetime import datetime
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
        # 密码只在内存里传给 `_register_new_aws_and_kiro`（要落 Kiro 记录的
        # password 列）；**不进** extra —— extra_json 会整段下发。
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
                # 内存里的 password 不进 extra（列上已经加密存了一份）
                extra={key: value for key, value in identity.items() if key != "password"},
            )
            account_repository.upsert(source_record)
            self._log(f"[Kiro] 来源 AWS 账号已保存: {account.email}")
        except Exception as exc:  # noqa: BLE001 - 落库失败不该抹掉已完成的注册
            logger.debug("来源 AWS 账号落库失败（继续）: %s", exc)
        return identity

    # ------------------------------------------------------------------
    # 来源选择：复用已有 AWS / 自动注册 AWS+Kiro
    # ------------------------------------------------------------------
    def _pick_unused_aws(self):
        """挑一个「还没注册过 Kiro」的 AWS 账号（FIFO）。

        判定依据是 Kiro 账号的 `extra.source_email`：Kiro 记录里指回哪个 AWS
        邮箱，就说明那个 AWS 已经被用掉了。没有密码的（导入缺字段）跳过 ——
        复用需要密码登录。
        """
        from core.db import account_repository

        aws_rows = account_repository.list_all_accounts(platform="aws")
        kiro_rows = account_repository.list_all_accounts(platform="kiro")
        used = {
            str((row.get_extra() or {}).get("source_email") or "").strip().lower()
            for row in kiro_rows
        }
        candidates = [
            row
            for row in aws_rows
            if str(row.password or "").strip()
            and row.email.strip().lower() not in used
            and str(row.status or "").strip().lower() not in ("banned", "invalid")
        ]
        candidates.sort(key=lambda row: row.created_at or datetime.min)
        return candidates[0] if candidates else None

    def _mailbox_account_for_reuse(self, mailbox, email: str):
        """复用登录时读码用的邮箱账号对象。

        iCloud 别名需要主号 id 才能走远程收件（`_resolve` 读
        `extra.icloud_account_id`），所以先按地址查别名表；其它渠道退回裸地址
        （渠道自己按地址检索，读不到会超时而不是拿到别人的信）。
        """
        from core.mailboxes.base import MailboxAccount

        try:
            from services import icloud_service

            for item in icloud_service.list_aliases():
                if str(item.get("address") or "").strip().lower() == email.lower():
                    main_id = item.get("account_id")
                    alias_id = item.get("id")
                    return MailboxAccount(
                        email=email,
                        account_id=f"{main_id}:{alias_id}",
                        extra={"icloud_account_id": main_id, "icloud_alias_id": alias_id},
                    )
        except Exception as exc:  # noqa: BLE001 - 查不到就按裸地址继续
            logger.debug("iCloud 别名查找失败（按裸地址继续）: %s", exc)
        return MailboxAccount(email=email)

    def _build_kiro_extra(
        self,
        *,
        email: str,
        tokens: dict[str, Any],
        plan: str,
        snapshot: dict[str, Any],
    ) -> dict[str, Any]:
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
            account_id=email,
            uid="builderid",
            email=email,
            name=f"{email} (BuilderId)",
            source=SOURCE_AWS,
            source_email=email,
        )
        kiro_extra.update(snapshot)
        kiro_extra["register_mode"] = "protocol"
        return kiro_extra

    def _to_account(self, *, email: str, password: str, kiro_extra: dict[str, Any]) -> Account:
        self.last_registration_meta = dict(kiro_extra)
        return Account(
            platform=self.name,
            email=email,
            password=password,
            user_id=str(kiro_extra.get("account_id") or ""),
            region=str(kiro_extra.get("region") or ""),
            token=str(kiro_extra.get("refresh_token") or ""),
            status=AccountStatus.REGISTERED,
            extra=kiro_extra,
        )

    def _reuse_aws_account(self, row, *, extra: dict[str, Any], proxy, plan: str) -> Account:
        """用已有 AWS 账号登录，换 Kiro 凭据（不新建 AWS）。"""
        from core.captcha_providers import build_captcha_chain
        from platforms.aws.client import AwsBuilderIdClient

        email = str(row.email or "").strip()
        password = str(row.password or "")
        if not password:
            raise RuntimeError(f"AWS 账号没有密码，无法复用登录: {email}")

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
        mailbox._platform_name = SOURCE_AWS

        client = AwsBuilderIdClient(
            proxy=proxy or "",
            log=self._log,
            captcha=build_captcha_chain(),
        )

        def otp_provider() -> str:
            account = self._mailbox_account_for_reuse(mailbox, email)
            return mailbox.wait_for_code(
                account,
                keyword="aws",
                timeout=self.get_mailbox_otp_timeout(default=300),
                otp_sent_at=time.time(),
            )

        self._log(f"[Kiro] 复用 AWS 账号登录: {email}")
        sso = client.sso_workflow(email=email, password=password, otp_provider=otp_provider)
        sso_token = client.sso_token(sso["auth_code"], sso["state"])
        tokens = authorize_kiro(sso_token=sso_token, proxy=proxy or "", log=self._log)

        source_extra = dict(row.get_extra() or {})
        source_extra.update({"email": email, "password": password})
        snapshot = _source_snapshot(source_extra)
        kiro_extra = self._build_kiro_extra(
            email=email, tokens=tokens, plan=plan, snapshot=snapshot
        )
        self._log("[Kiro] 复用登录完成，Kiro 凭据已换取")
        return self._to_account(email=email, password=password, kiro_extra=kiro_extra)

    def register(self, email: str = None, password: str = None) -> Account:
        """注册 Kiro。

        来源三选一（`extra.kiro_source`）：

        * `reuse`（默认）—— 先挑「还没注册 Kiro」的 AWS 账号复用；
          没有可用号时，按 `kiro_auto_register`（默认开）决定是否自动新建
          AWS + Kiro；
        * `manual` —— 用 `extra.kiro_aws_email` 指定的 AWS 账号复用
          （缺这个键时明确报错）；
        * `auto` —— 直接新建 AWS + Kiro。
        """
        extra = dict((self.config.extra or {}) if self.config else {})
        proxy = (self.config.proxy if self.config else None) or None
        plan = str(extra.get("kiro_plan") or DEFAULT_PLAN).strip() or DEFAULT_PLAN
        source_mode = str(extra.get("kiro_source") or "reuse").strip().lower()
        requested_aws = str(extra.get("kiro_aws_email") or "").strip()
        auto_register = str(extra.get("kiro_auto_register", "1")).strip().lower() not in {
            "0", "false", "no", "off", ""
        }

        # 用户点名要注册某个邮箱 → 必须为这个邮箱新建 AWS，不能拿别的号复用。
        # （「补 Kiro」走的是 kiro_aws_email，不带 email；两者不会打架。）
        if str(email or "").strip() and not requested_aws:
            return self._register_new_aws_and_kiro(
                extra=extra, proxy=proxy, email=email, password=password, plan=plan
            )

        if requested_aws:
            from core.db import account_repository

            row = account_repository.find_by_email("aws", requested_aws)
            if row is None:
                raise RuntimeError(f"指定的 AWS 账号不存在: {requested_aws}")
            if self.is_email_registered(row.email):
                raise RuntimeError(f"该 AWS 账号已注册 Kiro: {row.email}")
            return self._reuse_aws_account(row, extra=extra, proxy=proxy, plan=plan)

        if source_mode == "manual":
            raise RuntimeError(
                "kiro_source=manual 需要同时指定 kiro_aws_email（要复用哪个 AWS 账号）"
            )

        if source_mode == "reuse":
            row = self._pick_unused_aws()
            if row is not None:
                return self._reuse_aws_account(row, extra=extra, proxy=proxy, plan=plan)
            if not auto_register:
                raise RuntimeError(
                    "没有未注册 Kiro 的 AWS 账号；如需自动新建 AWS + Kiro，"
                    "请勾选「没有可用 AWS 时自动注册」"
                )
            self._log("[Kiro] 没有未注册 Kiro 的 AWS 账号 → 自动注册 AWS + Kiro")
        elif source_mode != "auto":
            raise RuntimeError(f"未知的 kiro_source: {source_mode}")

        return self._register_new_aws_and_kiro(
            extra=extra, proxy=proxy, email=email, password=password, plan=plan
        )

    def _register_new_aws_and_kiro(
        self,
        *,
        extra: dict[str, Any],
        proxy,
        email: str = None,
        password: str = None,
        plan: str = DEFAULT_PLAN,
    ) -> Account:
        requested_email = str(email or "").strip()
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
        mailbox._platform_name = SOURCE_AWS

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
            self._log(f"[Kiro] 授权失败（AWS 账号已创建）: {exc}")
            raise RuntimeError(
                "Kiro 授权失败（AWS 账号已创建并保存到 aws 平台，密码见该账号记录）: "
                f"{exc}"
            ) from exc

        kiro_extra = self._build_kiro_extra(
            email=account_email,
            tokens=tokens,
            plan=plan,
            snapshot=_source_snapshot(identity),
        )
        if identity.get("user_code"):
            kiro_extra["kiro_aws_user_code"] = str(identity["user_code"])
        return self._to_account(
            email=account_email, password=aws_password, kiro_extra=kiro_extra
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
        if not credentials["refresh_token"] or not credentials["client_id"] or not credentials["client_secret"]:
            # 只有导入的 social 账号可能没有 client_secret（GitHub/Google 登录），
            # 这类号无法用 IdC 刷新。**usable=None 表示「测不了」**，不是「坏了」——
            # 返回 False 会让状态策略把好号打成 invalid（审计 2026-11-13 实证）。
            return {
                "code": None,
                "usable": None,
                "indeterminate": True,
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
        if detail.get("indeterminate") or detail.get("usable") is None:
            # 测不了 ≠ 失效：social 号 / 网络错误不该改状态
            return ""
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