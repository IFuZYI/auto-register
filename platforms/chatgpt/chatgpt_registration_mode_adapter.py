"""ChatGPT 注册模式适配器。

这里有两个互相独立的维度：

``mode`` 决定要不要 refresh_token —— ``refresh_token`` 模式会额外走一次独立
authorize 链换 refresh_token，``access_token_only`` 模式直接跳过（省约 10 秒
且不产生必然失败的告警）。

``register_flow`` 决定拿什么当注册身份 —— 邮箱、接码平台的手机号，或者手机号
注册完再把邮箱池里的地址绑上去。两个维度可以任意组合。

``bind_2fa`` 是个独立开关：注册成功后顺手给账号绑一个 TOTP 双因素，密钥随号
落库。默认关，因为绑上之后每次登录都要动态码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, AccountStatus
from platforms.chatgpt.registration_engine import (
    REGISTER_FLOW_EMAIL,
    REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    REGISTRATION_MODE_REFRESH_TOKEN,
    ChatGPTRegistrationEngine,
    RegistrationResult,
)

CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN = REGISTRATION_MODE_REFRESH_TOKEN
CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY = REGISTRATION_MODE_ACCESS_TOKEN_ONLY
DEFAULT_CHATGPT_REGISTRATION_MODE = CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN

CHATGPT_REGISTER_FLOW_EMAIL = REGISTER_FLOW_EMAIL
DEFAULT_CHATGPT_REGISTER_FLOW = CHATGPT_REGISTER_FLOW_EMAIL

# 邮箱链路失效的特征串，命中就换下一个邮箱重试而不是把整轮判死
_MAILBOX_ERROR_MARKERS = ("service_abuse_mode", "oauth_token_failed", "imap")
_MAX_ATTEMPTS = 3


#: 合并后的「Token 方案」三取值 —— 一个值同时决定「要不要 RT」与「怎么换 RT」。
#:
#: 为什么合并：设置页原先渲染成两个独立下拉（有/无 RT + Codex/密码登录），
#: 而任务页早已合并成三选一（`frontend/src/lib/chatgptTokenPlan.ts`）。同一件事
#: 两处呈现不同，用户会以为设置页少了东西（用户 2026-10-26 要求合并）。
#:
#: 三个值都写进 `chatgpt_registration_mode`；`chatgpt_rt_route` 保留为**兼容输入**
#: （老配置里存过它），但不再单独渲染。
TOKEN_PLAN_REFRESH_LOGIN = "refresh_token_login"
TOKEN_PLAN_REFRESH_CODEX = "refresh_token_codex"

#: 设置页「Token 方案」的声明默认值 —— 必须与运行时行为一致。
#:
#: 选密码登录作为默认：2026-10-26 实测 Codex 路线在新号上卡在
#: choose-an-account / session.select（拿不到 RT），而密码登录路线在新号与
#: 老号上都能拿到。`DEFAULT_CHATGPT_REGISTRATION_MODE` 仍是 `refresh_token`
#: （合并值会归一成它），两者不冲突：前者是**声明层的默认选项**，后者是
#: **归一后的运行时模式**。
DEFAULT_CHATGPT_TOKEN_PLAN = TOKEN_PLAN_REFRESH_LOGIN


#: 旧值 → 合并方案的**单一来源**。设置页的 `legacy_aliases` 与
#: `normalize_chatgpt_registration_mode` 都从这里取 —— 各写一份的话，
#: 下拉里显示的与运行时解析的会漂移（代码审查 2026-10-27 I2）。
LEGACY_TOKEN_PLAN_ALIASES: dict[str, str] = {
    # 无 RT 的各种旧写法
    "access_token": CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    "at_only": CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    "without_rt": CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    "without_refresh_token": CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    "no_rt": CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    "0": CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    "false": CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
    # 有 RT 的各种旧写法（具体路线由 `chatgpt_rt_route` 或默认值决定，
    # 显示层映射到默认方案 —— 与 `resolve_chatgpt_token_plan` 同口径）
    "rt": TOKEN_PLAN_REFRESH_LOGIN,
    "with_rt": TOKEN_PLAN_REFRESH_LOGIN,
    "has_rt": TOKEN_PLAN_REFRESH_LOGIN,
    "1": TOKEN_PLAN_REFRESH_LOGIN,
    "true": TOKEN_PLAN_REFRESH_LOGIN,
    CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN: TOKEN_PLAN_REFRESH_LOGIN,
}


def normalize_chatgpt_registration_mode(value) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    # 合并值：先判它们，再走两态别名（`refresh_token_login` 里也含
    # "refresh_token"，顺序反了会被下面的集合先吃掉）。
    if normalized in {TOKEN_PLAN_REFRESH_LOGIN, TOKEN_PLAN_REFRESH_CODEX}:
        return CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
    # 运行时归一：无 RT 的旧写法 → `access_token_only`；其余有 RT 的写法
    # → `refresh_token`。这里**不复用** `LEGACY_TOKEN_PLAN_ALIASES` ——
    # 那张表是给显示层用的（旧值 → 合并方案三选一），两者的目标值域不同：
    # 显示层要 `refresh_token_login`，运行时归一要 `refresh_token`。
    # 硬把两者合并会让 `normalize` 返回显示层的值（实测踩过）。
    if normalized in {
        CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY,
        "access_token",
        "at_only",
        "without_rt",
        "without_refresh_token",
        "no_rt",
        "0",
        "false",
    }:
        return CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
    if normalized in {
        CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN,
        "rt",
        "with_rt",
        "has_rt",
        "1",
        "true",
    }:
        return CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
    return DEFAULT_CHATGPT_REGISTRATION_MODE


#: 换取 RT 的路线。
#:
#: - `codex`：现有行为，走 Codex CLI 的 client_id 做 OAuth 授权；撞 add-phone
#:   时要靠接码平台才能过。
#: - `login`：走 platform 的 client_id 做**密码登录**（password/verify →
#:   mfa/verify → code → token）；实测不需要手机号，但要求账号有密码与
#:   TOTP 密钥。
#:
#: 2026-10-26 实测：`codex` 在新号上卡在 choose-an-account / session.select，
#: 拿不到 RT；`login` 在同样的号上拿到。默认保持 `codex`（不改既有行为），
#: 由用户在设置里显式切换。
RT_ROUTE_CODEX = "codex"
RT_ROUTE_LOGIN = "login"
#: 路线默认值 —— 必须与「Token 方案」的声明默认（`DEFAULT_CHATGPT_TOKEN_PLAN`
#: = 密码登录）一致，否则会出现「设置页默认显示密码登录、运行时却按 Codex 走」
#: 的静默错位（实测 2026-10-27：补 RT 空配置时仍先跑 Codex）。
DEFAULT_CHATGPT_RT_ROUTE = RT_ROUTE_LOGIN


def normalize_chatgpt_rt_route(value) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"login", "password", "password_login", "pwd", "1", "true", "yes", "on"}:
        return RT_ROUTE_LOGIN
    return RT_ROUTE_CODEX


def resolve_chatgpt_token_plan(extra: Optional[dict]) -> str:
    """把任意历史配置归一成**合并后的**「Token 方案」三取值之一。

    设置页的下拉要显示当前选中项。已保存的旧值（`refresh_token` /
    `access_token_only`，或更早的 `chatgpt_rt_route` 独立键）都不是新选项，
    antd `Select` 只能把原始串显示出来 —— 用户看到 `refresh_token` 这种内部
    标识符，不知道实际选了哪个方案（实测 2026-10-26）。

    这个函数是前端的显示依据：任何旧配置都能还原成三个方案之一。
    """
    data = extra or {}
    mode = str(data.get("chatgpt_registration_mode") or "").strip().lower().replace("-", "_")

    if mode == TOKEN_PLAN_REFRESH_LOGIN:
        return TOKEN_PLAN_REFRESH_LOGIN
    if mode == TOKEN_PLAN_REFRESH_CODEX:
        return TOKEN_PLAN_REFRESH_CODEX
    if normalize_chatgpt_registration_mode(mode) == CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY:
        return CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
    # 旧的两态值 `refresh_token`（或空）：看有没有存过独立的路线键。
    # 没存过就跟**合并后的默认值**走（密码登录）—— 否则「默认改成密码登录了，
    # 但老用户的下拉还显示 Codex」这种不一致。
    route_key = str(data.get("chatgpt_rt_route") or "").strip()
    if not route_key:
        return DEFAULT_CHATGPT_TOKEN_PLAN
    return (
        TOKEN_PLAN_REFRESH_LOGIN
        if resolve_chatgpt_rt_route(data) == RT_ROUTE_LOGIN
        else TOKEN_PLAN_REFRESH_CODEX
    )


def resolve_chatgpt_rt_route(extra: Optional[dict]) -> str:
    """解析「怎么换 RT」。

    优先看合并值 `chatgpt_registration_mode`（`refresh_token_login` /
    `refresh_token_codex`）—— 那是设置页现在唯一暴露的入口；没有合并值时
    回落到旧的独立键 `chatgpt_rt_route`（老配置兼容）。
    """
    data = extra or {}
    mode = str(data.get("chatgpt_registration_mode") or "").strip().lower().replace("-", "_")
    if mode == TOKEN_PLAN_REFRESH_LOGIN:
        return RT_ROUTE_LOGIN
    if mode == TOKEN_PLAN_REFRESH_CODEX:
        return RT_ROUTE_CODEX

    # 旧的独立键：**存过**才用它，没存过就跟随默认值。
    # 不能直接把 None 丢给 `normalize_chatgpt_rt_route` —— 它把「没值」也
    # 归一成 codex，于是「设置页默认显示密码登录、运行时按 Codex 走」
    # （实测 2026-10-27）。
    route_key = str(data.get("chatgpt_rt_route") or "").strip()
    if not route_key:
        return DEFAULT_CHATGPT_RT_ROUTE
    return normalize_chatgpt_rt_route(route_key)


def resolve_chatgpt_registration_mode(extra: Optional[dict]) -> str:
    extra = extra or {}
    if "chatgpt_registration_mode" in extra:
        return normalize_chatgpt_registration_mode(extra.get("chatgpt_registration_mode"))
    if "chatgpt_has_refresh_token_solution" in extra:
        return (
            CHATGPT_REGISTRATION_MODE_REFRESH_TOKEN
            if bool(extra.get("chatgpt_has_refresh_token_solution"))
            else CHATGPT_REGISTRATION_MODE_ACCESS_TOKEN_ONLY
        )
    return DEFAULT_CHATGPT_REGISTRATION_MODE


def normalize_chatgpt_register_flow(value) -> str:
    """注册身份只剩邮箱（手机号注册已删除，见 ADR-0007）。

    旧值 `phone` / `phone_with_email` 一律归一到 `email` —— 它们只可能来自
    存量任务的配置。存量账号的 `extra.chatgpt_register_flow` 是**观测**值，
    不经过这里，原样保留。
    """
    return CHATGPT_REGISTER_FLOW_EMAIL


def resolve_chatgpt_register_flow(extra: Optional[dict]) -> str:
    extra = extra or {}
    return normalize_chatgpt_register_flow(extra.get("chatgpt_register_flow"))


def resolve_chatgpt_bind_2fa(extra: Optional[dict]) -> bool:
    """要不要在注册成功后顺手绑 TOTP 2FA。

    默认关：绑上之后这个号的每次登录都要动态码，密钥又只下发一次，丢了就再也
    登不进去。愿意承担这个代价的人自己在任务里勾。
    """
    value = (extra or {}).get("chatgpt_bind_2fa")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


@dataclass(frozen=True)
class ChatGPTRegistrationContext:
    mailbox: BaseMailbox
    proxy_url: Optional[str]
    callback_logger: Callable[[str], None]
    email: Optional[str]
    password: Optional[str]
    extra_config: dict
    mailbox_kind: str = "mailbox"


def _default_client_id() -> str:
    """兜底的 OAuth client_id。

    **只在账号自己的 client_id 拿不到时才用**（老号、或服务端没给 dump）。
    刷新要用「签发这个 RT 的那个 client」，不是任意一个常量 —— 见
    `resolve_client_id_for_patch` 的 docstring。
    """
    from platforms.chatgpt.protocol.mixins.platform_rt import PLATFORM_CLIENT_ID

    return PLATFORM_CLIENT_ID


def resolve_client_id_for_patch(flow_client_id: str, *, default: str = "") -> str:
    """决定**落库**用哪个 client_id。

    优先 `flow_client_id` —— 它是服务端 `client_auth_session` dump 里的
    `openai_client_id`，也就是「这个 RT 到底是谁签的」的权威答案
    （`protocol/mixins/trace.py:301` 把它同步到 `flow._oauth_client_id`）。
    拿不到才回落默认值。

    为什么不能写死一个常量：本仓有**两条**产出 RT 的路线，client 不同 ——

    | 路线 | client_id |
    | --- | --- |
    | Codex OAuth | `app_EMoamEEZ73f0CkXaXp7hrann` |
    | platform 密码登录 | `app_2SKx67EdpoN0G6j64rFvigXD` |

    写死任一个，另一条路线的号刷新时都会 `401 invalid_client`
    （实测 2026-10-28）—— 而错误信息看起来像「令牌失效」，极易误判。
    """
    got = str(flow_client_id or "").strip()
    if got:
        return got
    return str(default or "").strip() or _default_client_id()


class ChatGPTRegistrationModeAdapter:
    """按模式跑注册引擎，并把结果转成平台层的 ``Account``。"""

    def __init__(
        self,
        mode: str,
        register_flow: str = DEFAULT_CHATGPT_REGISTER_FLOW,
        bind_2fa: bool = False,
    ):
        self.mode = mode
        self.register_flow = register_flow
        self.bind_2fa = bind_2fa

    def run(self, context: ChatGPTRegistrationContext) -> RegistrationResult:
        result: Optional[RegistrationResult] = None
        for attempt in range(_MAX_ATTEMPTS):
            engine = ChatGPTRegistrationEngine(
                mailbox=context.mailbox,
                mode=self.mode,
                proxy=context.proxy_url,
                email=context.email or "",
                password=context.password or "",
                extra_config=context.extra_config,
                log_fn=context.callback_logger,
                mailbox_kind=context.mailbox_kind,
                register_flow=self.register_flow,
                bind_2fa=self.bind_2fa,
            )
            result = engine.run()
            if result.success:
                return result

            error = str(result.error_message or "").lower()
            marker = next((m for m in _MAILBOX_ERROR_MARKERS if m in error), None)
            if marker is None or attempt >= _MAX_ATTEMPTS - 1:
                break
            context.callback_logger(
                f"邮箱链路失效（{marker}），换用下一个邮箱重试 "
                f"({attempt + 1}/{_MAX_ATTEMPTS - 1})…"
            )
        return result

    def build_account(self, result: RegistrationResult, fallback_password: str) -> Account:
        return Account(
            platform="chatgpt",
            email=result.email,
            password=result.password or fallback_password,
            user_id=result.account_id,
            token=result.access_token,
            status=AccountStatus.REGISTERED,
            extra=self._build_account_extra(result),
        )

    def _build_account_extra(self, result: RegistrationResult) -> dict:
        return {
            "access_token": result.access_token,
            "refresh_token": result.refresh_token,
            "id_token": result.id_token,
            "session_token": result.session_token,
            "cookies": result.cookie_header,
            "workspace_id": result.workspace_id,
            "chatgpt_registration_mode": self.mode,
            # 按**实际拿到的凭证**写，而不是按模式写。
            #
            # 原来写的是 `self.mode == REFRESH_TOKEN`（意图），于是「选了 RT 模式
            # 但 Codex 交换撞上 add-phone 墙、RT 为空」的号也被标成 True ——
            # 下游据此以为它有 RT 方案，实际 `refresh_token` 是空串（实测踩过）。
            # 这个键是**读取方**用的（`resolve_chatgpt_registration_mode` 靠它
            # 反推模式），写意图会让「补 RT」挑不到该补的号。
            "chatgpt_has_refresh_token_solution": bool(result.refresh_token),
            "chatgpt_register_flow": self.register_flow,
            "chatgpt_bind_2fa": self.bind_2fa,
            "chatgpt_token_source": result.source,
            # **client_id 必须落库**：OAuth 刷新要它。空值换来的是
            # `invalid_client`（HTTP 401），而不是能自查的「参数缺失」——
            # 排查时极易误判成「RT 失效」。
            #
            # 实测（2026-10-28）：663 个 chatgpt 账号 **0 个** 有 client_id，
            # OAuth 刷新全部 401 → 回落 session → AT 未到期时原样返回旧值
            # （`refreshed=False`）→ 用户看到「刷不出新的」。
            "client_id": resolve_client_id_for_patch(
                str(getattr(result, "client_id", "") or ""),
                default=str(result.metadata.get("client_id") or ""),
            ),
            **{k: v for k, v in (result.metadata or {}).items() if v not in (None, "", False)},
        }


def build_chatgpt_registration_mode_adapter(
    extra: Optional[dict],
) -> ChatGPTRegistrationModeAdapter:
    return ChatGPTRegistrationModeAdapter(
        resolve_chatgpt_registration_mode(extra),
        resolve_chatgpt_register_flow(extra),
        resolve_chatgpt_bind_2fa(extra),
    )


def rt_route_env_overrides(config: Optional[dict]) -> dict[str, str]:
    """把路线设置翻译成协议层 `env_overrides` 里的两个键。

    `AuthFlow._speculative_rt_attempt` 从 `env_overrides` 读路线决定要不要抢跑
    Codex。三处构造 flow 的地方都要带（注册引擎、补 RT、登录刷新）——
    此前只有注册引擎带了，另外两处读空后按默认判定，抢跑行为与设置不符
    （代码审查 2026-10-27 I4）。

    抽成一个函数而不是各写一遍：这份映射是**单一来源**，漏一处就是一处
    静默不一致。
    """
    data = config or {}
    out: dict[str, str] = {}
    for key in ("chatgpt_registration_mode", "chatgpt_rt_route"):
        value = str(data.get(key) or "").strip()
        if value:
            out[key] = value
    return out
