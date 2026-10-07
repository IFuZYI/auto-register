"""平台插件基类"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional
from enum import Enum
import os
import time


#: 已删除的状态值 → 归一目标（用户要求删除「试用中 / 已订阅」）。
#: 老库、旧导入文件里可能还带着这两个值 —— 统一归一到 registered
#: （它们都是「账号已注册、可用」的语义）。
_REMOVED_STATUS_ALIASES = {"trial": "registered", "subscribed": "registered"}


class AccountStatus(str, Enum):
    """账号状态：只保留注册 / 过期 / 失效 / 封禁四个值。

    「试用中（trial）」「已订阅（subscribed）」已按用户要求删除 ——
    历史数据由 `normalize` 与启动迁移归一成 registered。
    """

    REGISTERED   = "registered"
    EXPIRED      = "expired"
    INVALID      = "invalid"
    #: 账号被上游封禁/停用（OpenAI 的原话是 "deleted or deactivated"）。
    #:
    #: 与 `invalid` 的区别：`invalid` 是「凭证失效，重登一下可能还有救」，
    #: `banned` 是「号没了，怎么登都登不回来」—— 前者进重试队列，后者不该进。
    #: 判重语义上它与 registered 同侧：号虽然废了但**邮箱仍被这个平台占用**，
    #: 不该被当成"没注册过"再拿这个邮箱去注册一次。
    BANNED       = "banned"

    @classmethod
    def values(cls) -> set[str]:
        """全部合法状态值（用于校验外部传入的字符串）。"""
        return {member.value for member in cls}

    @classmethod
    def normalize(cls, value: Any) -> str:
        """状态字符串归一：已删除的值（trial / subscribed）→ registered。

        删除功能时的兜底（见 docs/MAINTENANCE.md §6）：老库、旧导入文件、
        旧客户端都可能还带着已删除的状态值 —— 不归一的话界面上会重新冒出
        「试用中/已订阅」。空值 → registered；其它未知值原样保留（历史遗留
        值由各自的读侧处理，不在这里猜测）。
        """
        text = str(getattr(value, "value", value) or "").strip().lower()
        if not text:
            return cls.REGISTERED.value
        return _REMOVED_STATUS_ALIASES.get(text, text)

    @classmethod
    def active_values(cls) -> set[str]:
        """「账号还能用」的状态集合（白名单）。

        注意：这与 `is_registered(include_invalid=False)` 的「可重试」判定**不是**
        同一套逻辑——后者是黑名单（只有明确失效的才放行重试），未知状态按
        「已注册」处理以保守地避免重复注册。需要保守判定时用 `retryable()`。
        """
        return {cls.REGISTERED.value}

    @classmethod
    def dead_values(cls) -> set[str]:
        """明确「已失效」的状态值（黑名单）。"""
        return {cls.INVALID.value, cls.EXPIRED.value}

    @classmethod
    def is_active(cls, value: Any) -> bool:
        """给定状态值是否表示账号仍可用（白名单判定，未知值按不可用处理）。

        适合「测活哪些账号」这类场景：宁可漏测，也不要把明确失效的再测一遍。
        """
        text = str(getattr(value, "value", value) or "").strip().lower()
        return text in cls.active_values()

    @classmethod
    def counts_as_registered(cls, value: Any) -> bool:
        """判重场景：给定状态是否应视为「该邮箱已注册」。

        黑名单判定——只有**明确失效**（invalid/expired）的才不算已注册，
        未知状态一律按「已注册」处理，保守地避免重复注册、重复占用邮箱。
        等价于旧写法 `status not in ("invalid", "expired")`。
        """
        text = str(getattr(value, "value", value) or "").strip().lower()
        return text not in cls.dead_values()

    @classmethod
    def coerce(cls, value: Any, default: "AccountStatus | None" = None) -> "AccountStatus":
        """把任意输入转成枚举成员；不认识的值回落到 default（默认 REGISTERED）。

        数据库里的历史行可能存着任意字符串（如 chatgpt 侧的 active/banned），
        直接 `AccountStatus(raw)` 会抛 ValueError 打断整条流程，所以这里不抛。
        """
        text = str(getattr(value, "value", value) or "").strip().lower()
        for member in cls:
            if member.value == text:
                return member
        return default if default is not None else cls.REGISTERED


def resolve_mailbox_otp_timeout(extra_config: Any, *, default: int = 180) -> int:
    """从配置里解析邮箱 OTP 等待秒数（三个键按优先级取第一个正整数）。

    单点定义，因为这段优先级规则以前抄了四份（`rt_backfill`、`registration_engine`、
    `chatgpt_two_factor` 各一份手写循环，外加 `BasePlatform.get_mailbox_otp_timeout`），
    而且默认值已经不一致了 —— 三份手写用 180、平台方法用 120。等待时长直接决定
    "验证码还没到就放弃"的频率，不能按调用路径取不同的值。

    默认 180：邮箱（尤其 iCloud 转发）有时要一两分钟才到，120 会误判成收不到。
    """
    candidates = (
        (extra_config or {}).get("mailbox_otp_timeout_seconds"),
        (extra_config or {}).get("email_otp_timeout_seconds"),
        (extra_config or {}).get("otp_timeout"),
    )
    for value in candidates:
        if value in (None, ""):
            continue
        try:
            resolved = int(value)
        except (TypeError, ValueError):
            continue
        if resolved > 0:
            return resolved
    return default


@dataclass
class Account:
    platform: str
    email: str
    password: str
    user_id: str = ""
    region: str = ""
    token: str = ""
    status: AccountStatus = AccountStatus.REGISTERED
    extra: dict = field(default_factory=dict)  # 平台自定义字段
    created_at: int = field(default_factory=lambda: int(time.time()))


@dataclass
class RegisterConfig:
    """注册任务配置"""
    #: 执行器 = 平台用什么方式访问目标站。**空串 = 让平台决定**（回落它声明的
    #: 第一个）。不能写死 "protocol"：各平台支持集合不同，写死一个值等于替
    #: 所有平台预选了同一项 —— Grok 的默认是 browser（协议路径在 x.ai 上会
    #: CF 403），写死 protocol 会把默认任务打进死路。
    executor_type: str = ""
    captcha_solver: str = "yescaptcha"  # yescaptcha | 2captcha | manual
    proxy: Optional[str] = None
    extra: dict = field(default_factory=dict)


class BasePlatform(ABC):
    # 子类必须定义
    name: str = ""
    display_name: str = ""
    version: str = "1.0.0"
    #: 子类声明**实际支持**的执行器类型，未列出的自动降级到第一个受支持项。
    #:
    #: **必须与实现一致**：界面只渲染这里列出的选项，运行时也按这里降级。
    #: 声明了但没实现（或实现但不声明）都会造成「界面说支持、运行时静默忽略」——
    #: 实测踩过：ChatGPT 是纯协议（`curl_cffi` 指纹伪装，全树无浏览器代码），
    #: 却因为继承默认值而列了三个执行器。
    #:
    #: 语义：执行器 = **访问目标站的方式**，由平台自己解释。
    #: 各平台取值可以不同（Grok 用 `browser` / `protocol`，ChatGPT 只有 `protocol`），
    #: 界面按平台的声明渲染，不假设全局统一。
    #:
    #: **第一个元素是平台默认**：未指定或值不受支持时回落到它。
    supported_executors: list = ["protocol", "headless", "headed"]
    #: 执行器的显示名（可选）。不声明时界面回落到通用标签表。
    #: 平台专属的执行器（如 Grok 的 `browser`）必须在这里给标签，否则界面只能显示原始值。
    executor_labels: dict = {}
    # 注册流程是否需要外部邮箱池。置 False 的平台（自带邮箱，如 iCloud 隐私邮箱）
    # 会被运行时跳过邮箱构建 —— 否则会去建一个全局默认渠道（`mail_provider`，
    # 现在默认留空）并报「未配置邮箱服务」，而该邮箱根本不会被用到。
    uses_mailbox: bool = True
    #: 该平台声明的可配置项，供界面在各平台卡片里按平台渲染 ——
    #: 既包括「注册方式」这类下拉，也包括平台专属的数值旋钮（如 Grok 发码节流）。
    #:
    #: 每项形如（下拉，默认）::
    #:
    #:     {"key": "grok_register_mode",   # 存哪个配置键
    #:      "label": "注册方式",
    #:      "desc": "…",                    # 可选的说明（界面上显示为 tooltip）
    #:      "default": "browser",           # 未设置时界面上显示的默认值
    #:      "options": [{"value": "browser", "label": "浏览器（推荐）"}, …]}
    #:
    #: 或（数值）::
    #:
    #:     {"key": "grok_send_code_timeout",
    #:      "label": "页面发码超时（秒）",
    #:      "type": "number",               # 不填 = 下拉
    #:      "min": 10,
    #:      "default": 90,
    #:      "desc": "…"}
    #:
    #: **必须由插件自己声明**：合法取值是插件内部实现的细节
    #: （Grok 只有 browser/protocol，ChatGPT 有 RT 方案与注册流程两个维度），
    #: 界面硬编码一份迟早与插件分叉 —— 分叉的后果是用户在界面上选了一个
    #: 「界面说支持」、运行时却被静默忽略的值。
    #:
    #: 为空表示该平台没有额外的可配置项（只有执行器）。
    registration_modes: list = []

    def __init__(self, config: RegisterConfig = None):
        self.config = config or RegisterConfig()
        self._task_control = None
        # 执行器归一：不受支持的值回落到**平台声明的第一个**（即平台默认）。
        # 空值（未指定）静默回落 —— 那是「让平台自己决定」，不是用户选错了。
        requested_executor = str(self.config.executor_type or "").strip()
        supported = list(self.supported_executors or [])
        # 记下「用户是否显式指定过」。归一后 `config.executor_type` 总是有值，
        # 平台分不清「用户选了 browser」和「我们替它默认成 browser」——
        # 而有些兼容逻辑（如 Grok 读遗留的 `grok_register_mode`）只在
        # **未指定**时才该生效。不记这个标记的话，那种兼容会被默认值挡掉。
        self._executor_explicit = bool(requested_executor)
        if requested_executor not in supported:
            fallback = supported[0] if supported else "protocol"
            if requested_executor:
                print(
                    f"[{self.display_name or self.name}] 执行器 '{requested_executor}' 不受支持，"
                    f"自动切换为 '{fallback}' (支持: {supported})"
                )
            self.config.executor_type = fallback
        else:
            self.config.executor_type = requested_executor

    @abstractmethod
    def register(self, email: str, password: str = None) -> Account:
        """执行注册流程，返回 Account"""
        ...

    @abstractmethod
    def check_valid(self, account: Account) -> bool:
        """检测账号是否有效"""
        ...

    def apply_probe_status_policy(self, account, *, detail: dict) -> str:
        """按探测细节落账号状态（平台可覆写；返回判定理由，空串 = 没判定）。

        默认 no-op。`core/scheduler.py` 的批量测活通过这个钩子让平台自己
        决定「探测结论 → 状态」的映射 —— core 不能反向 import services
        （docs/EXTENDING.md 的依赖方向），而各平台的落状态策略在
        services/ 里（如 grok 的 `apply_grok_status_policy`）。
        平台侧在方法体内延迟 import services 即可（platforms 允许依赖）。
        """
        return ""

    def get_platform_actions(self) -> list:
        """
        返回平台支持的额外操作列表，每项格式:
        {"id": str, "label": str, "params": [{"key": str, "label": str, "type": str}]}
        """
        return []

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        """
        执行平台特定操作，返回 {"ok": bool, "data": any, "error": str}
        """
        raise NotImplementedError(f"平台 {self.name} 不支持操作: {action_id}")

    def get_quota(self, account: Account) -> dict:
        """查询账号配额（可选实现）"""
        return {}

    def bind_task_control(self, task_control) -> None:
        """绑定协作式任务控制器，供邮箱等待/人工跳过等场景复用。"""
        self._task_control = task_control
        mailbox = getattr(self, "mailbox", None)
        if mailbox is not None:
            mailbox._task_control = task_control

    # ------------------------------------------------------------------
    # 账号池：邮箱唯一键 + 分库
    # ------------------------------------------------------------------

    def db_session(self):
        """打开本平台的账号库会话（未分库时即默认库）。

        平台库由 `DATABASE_URL_<PLATFORM>` 或 `PLATFORM_DATABASE_URLS` 配置；
        没配就透明用默认库，所以插件不需要关心库在哪。
        """
        from core.db import platform_session

        return platform_session(self.name)

    def is_email_registered(self, email: str, *, include_invalid: bool = True) -> bool:
        """该邮箱在本平台是否已注册过。

        邮箱是账号的唯一业务键；注册前调用可以避免重复注册同一邮箱。
        `include_invalid=False` 时失效账号不算已注册（用于「失效重试」场景）。
        """
        from core.db import account_repository

        return account_repository.is_registered(
            self.name, email, include_invalid=include_invalid
        )

    def save_account(self, account: "Account"):
        """把账号存入本平台库（同平台同邮箱则更新）。

        等价于 `core.db.save_account`，但明确走本平台的分库；新插件建议用它。
        """
        from core.db import account_repository

        return account_repository.upsert(account)

    def get_mailbox_otp_timeout(self, default: int = 180) -> int:
        """统一解析邮箱 OTP 等待秒数，避免平台内散落魔法值。

        默认 180 与 `resolve_mailbox_otp_timeout` 保持一致 —— 以前这里默认 120、
        各平台内的手写循环默认 180，同一个配置项按调用路径给出不同等待时长。
        """
        extra = getattr(self.config, "extra", {}) or {}
        return resolve_mailbox_otp_timeout(extra, default=default)

    def _make_executor(self):
        """根据 config 创建执行器"""
        from .executors.factory import BrowserExecutorFactory

        return BrowserExecutorFactory().create(
            self.config.executor_type,
            proxy=self.config.proxy,
        )

    def _make_captcha(self, **kwargs):
        """根据 config 创建验证码解决器"""
        from .base_captcha import YesCaptcha, ManualCaptcha, LocalSolverCaptcha
        t = self.config.captcha_solver
        if t == "yescaptcha":
            key = kwargs.get("key") or self.config.extra.get("yescaptcha_key", "")
            return YesCaptcha(key)
        elif t == "manual":
            return ManualCaptcha()
        elif t == "local_solver":
            url = (
                self.config.extra.get("solver_url")
                or os.getenv("LOCAL_SOLVER_URL")
                or f"http://127.0.0.1:{os.getenv('SOLVER_PORT', '8889')}"
            )
            return LocalSolverCaptcha(url)
        raise ValueError(f"未知验证码解决器: {t}")
