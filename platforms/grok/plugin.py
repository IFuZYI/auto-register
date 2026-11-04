"""Grok (x.ai) 平台插件。

支持：
- 浏览器注册（camoufox 过 CF，唯一路径 —— 协议路径已删除）
- 多种邮箱渠道（复用本项目 core.base_mailbox 的各家 provider）
- 代理池（由任务层注入，插件只用 config.proxy）
- 多种 Turnstile 方案（captcha solver / 屏外 Chrome / 外部 Solver）
- 产物：Web SSO（grok2api 上传）。**注册默认不换 OAuth**（不产 build 凭据，
  见 `_register_via_browser` 里的说明）；需要时可用账号页的「刷新 Token」
  走登录协议补 AT/RT，或给 `grok_oauth_exchange=1` 让注册时就换
- CPA / Grok2API / Sub2API 产物与上传
- 账号池操作：测活、刷新 token、导出、上传

参考：reference/grok/ 下 5 个项目（详见 ~/.hermes/wikis/grok-registration/）
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

from core.base_mailbox import BaseMailbox
from services.panel_status_sync import ACTION_PANEL_KEYS
from core.models.account import AccountRecord
from core.base_platform import Account, AccountStatus, BasePlatform, RegisterConfig
from core.environment import EnvironmentNotReadyError
from core.registry import register
from core.task_runtime import TaskInterruption

from .constants import (
    CPA_PROBE_MODEL,
    DEFAULT_UA,
    SIGNUP_PAGE_URL,
    mail_retries as _mail_retries,
    signup_retries,
)
from .probe import probe_token
from .profile import generate_password, random_name


def _truthy(value: Any, default: bool = True) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off")


def _env_default(name: str, fallback: str = "") -> str:
    """读环境变量作为配置缺省值（面板配置优先，环境变量兜底）。

    面板存的是空串时不能直接把它当值用 —— 否则「用户没填」会被当成
    「显式选择空」，把有意义的默认值（如发码走 UI）顶掉。
    """
    return str(os.getenv(name, "") or "").strip() or fallback


def _to_float(value: Any, default: float) -> float:
    """宽容地把配置值转成 float（空串/脏值回落默认）。"""
    try:
        text = str(value if value is not None else "").strip()
        return float(text) if text else float(default)
    except (TypeError, ValueError):
        return float(default)


# x.ai 对同一 IP 的取码有频率限制，太密会「声称已发信但不真发」，继续打会把
# 出口打进长冷却（参考项目原文：建议并发 1-2 + 强制发码间隔，默认 240 秒）。
# 间隔在**进程内**全局共享：同一台机器上并发跑多个任务时，限制是按 IP 生效的，
# 按任务各算一份等于没限。
_SEND_CODE_LOCK = threading.Lock()
_SEND_CODE_LAST_TS = 0.0


def _throttle_send_code(min_interval: float, log) -> None:
    """强制两次发码之间的最小间隔（进程内全局）。"""
    global _SEND_CODE_LAST_TS
    if min_interval <= 0:
        return
    with _SEND_CODE_LOCK:
        now = time.monotonic()
        wait = min_interval - (now - _SEND_CODE_LAST_TS)
        if wait > 0:
            log(f"[Grok] 发码节流：等待 {wait:.0f}s（x.ai 取码限流保护）")
            time.sleep(wait)
            now = time.monotonic()
        _SEND_CODE_LAST_TS = now


#: 平台侧**瞬时**拒绝：地址没被 x.ai 消耗，只是这一轮撞上了出口 IP 限流 /
#: 域名窗口（2026-11-03 实测：同出口换域名无效、约 1 小时后自行恢复）。
#: 「地址没被消耗」的信号 —— 命中任一就把地址**放回池子**，而不是永久移出。
#:
#: `antibot_not_ready`（2026-11-04 独立审查补）：表单**根本没提交**，页面停在
#: 邮箱那一步。地址连碰都没碰过，可它此前落到 `failed`，在 Outlook 渠道里
#: `failed` = 永久移出池子 —— 与 2026-11-03 那次「一小时烧掉 6 个可用号」是
#: 同一类损失，只是触发条件换成了「反机器人令牌没就绪」。
#: 注册链路在 `_send_code_stage` 里给的就是 `consumes_address=False,
#: antibot_not_ready=True, retryable=True`，没有一条指向「地址坏了」。
_TRANSIENT_REJECTION_KEYS = ("email_unavailable", "rate_limited", "antibot_not_ready")


def _mailbox_status_for_failure(result: dict) -> str:
    """注册失败时该把这个邮箱地址记成什么状态（纯函数，便于钉住）。

    三种状态的含义（渠道侧解释见 `_record_mailbox_status`）：

    - `used`：地址**已被消耗**（如「该邮箱在 x.ai 已有账号」）→ 不能再用；
    - `available`：地址没被消耗，只是撞上**平台侧瞬时拒绝**（域名窗口用尽、
      限流）或**我们压根没提交**（`antibot_not_ready`）→ 放回池子，
      等窗口恢复 / 换出口后再用；
    - `failed`：其它失败，按渠道自身语义处理。

    2026-11-03 线上实测：一小时的出口限流窗口把微软号池从 6 个可用**烧到 0 个**
    —— Outlook 渠道里 `failed` 等于「永久移出池子」，而几分钟后同一个地址其实
    完全可用（那批 7 个 `failed` 全是这么来的）。地址是稀缺资源，不该因为一个
    跟它无关的瞬时拒绝就丢掉。
    """
    if result.get("already_registered") or result.get("consumes_address"):
        return "used"
    if any(result.get(key) for key in _TRANSIENT_REJECTION_KEYS):
        return "available"
    return "failed"


@register
class GrokPlatform(BasePlatform):
    name = "grok"
    display_name = "Grok"
    version = "1.0.0"
    #: Grok **只有浏览器路径**：x.ai 的 CF 只有 camoufox 能过，协议层的发码
    #: 是「假接受」（gRPC grpc-status:0 / REST ok:true 但零投递）、验码在无
    #: CF 通行证的独立客户端里会 grpc=3。协议注册路径已整体删除
    #: （2026-10-01，见 `register()` 的注释）。
    #:
    #: 单个元素 = 没有可选项，界面会渲染成只有一个值的下拉（明确告诉用户
    #: 「就这一条路」，而不是给一个选了没用的选项）。
    supported_executors = ["browser"]
    executor_labels = {
        "browser": "浏览器（camoufox，唯一路径）",
    }
    # 平台可配置项（界面在「注册设置 → 各平台设置 → Grok」卡片里渲染）。
    #
    # `grok_send_code_mode` / `grok_send_code_timeout` 已随协议路径删除 ——
    # 它们只被那条路读取，留着会变成「界面能调、实际没人读」的死旋钮。
    registration_modes = [
        # 发码节流：Grok 专属的数值旋钮。放在这里而不是界面上的独立卡片 ——
        # 它们只在「声称已发信但不真发」时才需要调，而且只有 Grok 用得上，
        # 摆在平台卡片里才找得到（此前是界面里手写的一张独立卡）。
        # 浏览器路径在页面发码前会走这个节流（见 `_register_via_browser`）。
        {
            "key": "grok_send_code_min_interval",
            "label": "发码最小间隔（秒）",
            "type": "number",
            "min": 0,
            "default": 0,
            "desc": "x.ai 对同一 IP 的取码有频率限制，太密会把出口打进长冷却",
        },
        # 换 build 凭据（OAuth device flow）：默认关。
        # 必须挂在这里而不是只写进文档 —— 这个键不在 `CONFIG_KEYS` 里时，
        # 面板保存会被 `PUT /api/config` 静默丢进 ignored，注册页也从不提交它，
        # 于是「想要 build 就打开这个开关」对面板用户等于不存在（独立评审发现）。
        {
            "key": "grok_oauth_exchange",
            "label": "换取 build 凭据（AT/RT）",
            "default": "0",
            "desc": (
                "默认关：注册只产 Web SSO（上传 grok2api 用）。打开后注册末尾会走"
                "浏览器 OAuth device flow 换 AT/RT（CPA / Build 凭据），实测每次要"
                "白等约 4 分钟且经常停在「授权未确认」。"
                "注意：账号页的「刷新 Token（登录协议）」走的是同一个 device flow"
                "（同一个失败面），换条路不会更容易成功 —— 要 build 凭据前先想清楚"
                "这条链目前基本不通"
            ),
            "options": [
                {"value": "0", "label": "不换（默认，只产 Web SSO）"},
                {"value": "1", "label": "换（产 AT/RT，供 CPA / Build）"},
            ],
        },
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    @classmethod
    def check_environment(cls):
        """任务级环境预检：camoufox 配对浏览器是否就绪。

        注册 runner 在分配邮箱/代理之前调用（见 `_run_register` 的预检段）。
        浏览器没装好时整个任务直接失败并带修复指引 —— 而不是让每个账号
        白跑一遍、重试轮全烧在同一个环境错误上。
        """
        from core.environment import check_camoufox_ready

        return check_camoufox_ready()

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self, email: str = None, password: str = None) -> Account:
        extra = dict((self.config.extra or {}) if self.config else {})
        proxy = (self.config.proxy if self.config else None) or None
        log = getattr(self, "_log_fn", print)

        # -1) 环境预检（fail-fast）：浏览器路径依赖 camoufox 配对浏览器。
        #     浏览器没装好时（包升级后没跑 `camoufox fetch` 是常见形态），
        #     在**分配邮箱/推代理之前**就带修复指引失败 —— 否则每条链路
        #     白跑一遍，重试轮还全烧在同一个环境错误上（实测事故：
        #     task_1791300512055 两轮 3 秒全挂在 CamoufoxNotInstalled）。
        from core.environment import check_camoufox_ready

        verdict = check_camoufox_ready()
        if not verdict.ok:
            log(f"[Grok] 环境预检失败：{verdict.message}")
            raise EnvironmentNotReadyError(verdict.message)

        # 0) 判重：邮箱是账号唯一业务键，已注册的不再重复注册。
        #    渠道池常常会重复发出同一个邮箱（如 Outlook 邮箱池轮转），
        #    这里拦住能省掉整轮注册与验证码，也避免上游报「已存在」。
        requested_email = str(email or "").strip()
        if requested_email and not _truthy(extra.get("grok_allow_duplicate"), default=False):
            try:
                if self.is_email_registered(requested_email):
                    raise RuntimeError(f"该邮箱已注册，跳过: {requested_email}")
            except RuntimeError:
                raise
            except Exception as exc:
                log(f"[Grok] 判重查询失败（继续注册）: {type(exc).__name__}")

        # 走浏览器路径（唯一路径）。
        #
        # **协议路径已删除**（2026-10-01）。删除依据：
        #   1. 协议发码被 x.ai 「假接受」——gRPC 回 grpc-status:0、REST 回
        #      ok:true，但**从不投递**。实测两个邮箱各等 180 秒，收件箱 0 封。
        #   2. 协议验码在独立 HTTP 客户端里做（无 CF 通行证）→ `http=200 grpc=3`。
        #   3. 参考项目 grok-hub-clean（最新）自己的注释：
        #      「grpc 直调取码被 x.ai 假接受（status=0 但静默不发信），改走真页面表单」，
        #      且它的发码/验码/建号**全部**通过 `page.evaluate` 在浏览器内执行 ——
        #      它没有独立的纯协议注册路径。
        #   4. 历史数据：grok 41 条记录 8 次成功，**全部来自浏览器路径**；
        #      协议路径 0 成功。
        #
        # 兼容：`grok_register_mode` 仍可能出现在老任务/脚本的 extra 里，
        # 这里只做提示，不再改变行为（路径只有一条）。
        legacy_mode = str(extra.get("grok_register_mode") or "").strip().lower()
        if legacy_mode and legacy_mode != "browser":
            log(
                f"[Grok] 忽略 grok_register_mode={legacy_mode!r}："
                f"协议注册路径已移除，现在只有浏览器路径"
            )
        return self._register_via_browser(
            extra, proxy, log, requested_email, password
        )

    # ------------------------------------------------------------------
    # 有效性检查
    # ------------------------------------------------------------------

    def _probe_verdict(self, code: Optional[int], summary: str) -> tuple[bool, str]:
        """把测活结果翻译成 (账号是否有效, 原因)。

        **200 不是唯一「有效」**：账号已注册成功但额度耗尽时，x.ai 返回
        402（`personal-team-blocked:spending-limit`）或 403（权限类）——
        凭证本身是好的，只是没额度。把这些判成「无效」会把刚注册好的账号
        误报为死号（实测：注册成功、SSO 与 OAuth 都正常，测活却报无效）。
        本仓已有同一约定：`platforms/chatgpt/status_probe.py` 与
        `services/cliproxyapi_sync.py` 都把 402/403 归为
        `payment_required`（账号有效，非凭证问题）。

        抽成方法是为了让**所有**调用点共用同一套判定：此前 `execute_action`
        里单独写着 `code == 200`，与 `check_valid` 的语义互相矛盾 —— 同一
        个账号在「测活」按钮下报失败、在有效性检查里却算通过。
        """
        if code == 200:
            return True, ""
        if code in (402, 403):
            # 403 也可能是「token 被吊销」——响应体里会带 invalid / revoked
            # 字样，此时仍应判无效。
            low = str(summary or "").lower()
            reason_map = {
                "invalid": "凭证无效",
                "revoked": "凭证已吊销",
                "expired": "凭证已过期",
                "unauthenticated": "未认证",
            }
            for keyword, reason in reason_map.items():
                if keyword in low:
                    return False, f"HTTP {code}（{reason}）"
            return True, f"HTTP {code}（有效但无额度）"
        return False, (f"HTTP {code}" if code is not None else "请求失败")

    def check_valid(self, account: Account) -> bool:
        """账号是否仍有效（判定语义见 `_probe_verdict`）。

        探测细节（探测码/摘要/SSO 状态）存在 `last_probe_detail` 上，
        供调用方（`_do_check` / 调度器）落状态时区分 过期 / 失效 / 禁用。
        """
        detail = self.probe_account_detail(account)
        self.last_probe_detail = detail
        return bool(detail.get("usable"))

    def apply_probe_status_policy(self, account, *, detail: dict) -> str:
        """grok 的探测结论 → 状态落库（`BasePlatform` 钩子的 grok 实现）。

        延迟 import services（platforms 允许依赖 services；core 不允许 ——
        `core/scheduler.py` 经这个钩子调用，就不再反向依赖）。
        """
        from services.grok_account_state import apply_grok_status_policy

        detail = detail or {}
        return apply_grok_status_policy(
            account,
            probe_code=detail.get("code"),
            probe_summary=str(detail.get("summary") or ""),
            sso_rejected=(detail.get("sso_status") == "rejected"),
        )

    def probe_account_detail(self, account: Account) -> dict:
        """探测账号并返回判定细节（不落库）。

        返回 `{"code", "summary", "usable", "sso_status"}`：

        - 有 AT：走 CLI Proxy 测活（`probe_token`），code/summary 是原始结果；
        - 无 AT：用 SSO 访问 accounts.x.ai 得 `sso_status`，但**不据此判定有效性**
          （`usable` 恒 False —— 该判定永远返回 alive，见 `_sso_status` 的
          docstring；用它判有效会让死号被复活），`sso_status` ∈
          `alive` / `rejected` / `unknown` —— `rejected` 表示 SSO 已被上游
          拒绝（对齐 grok2api 的 `markSSOCredentialRejected` → reauthRequired，
          即「失效，需要重新登录」）；`unknown`（网络异常）不据此改状态。
        """
        extra = account.extra or {}
        # 凭证读取走注册表：grok 的 token 列镜像 SSO（不是 AT）——
        # 整理前 `or account.token` 会把 SSO 当 AT 拿去 probe。
        from core.credential_fields import get_credential, token_column_credential

        access = get_credential(extra, "access_token")
        if not access:
            # 没有 OAuth token 时，用 SSO 探测账号是否存在
            sso = get_credential(extra, "sso") or token_column_credential(account, "grok", "sso")
            if not sso:
                return {"code": None, "summary": "缺少 AT 与 SSO", "usable": False, "sso_status": "unknown"}
            sso_status = self._sso_status(sso)
            # **不能**用 `sso_status == "alive"` 当「账号有效」：
            # `_sso_status` 永远返回 alive（`Response` 没有 url，`rejected` 不可达，
            # 见它的 docstring），于是 `check_valid` 会恒为 True；而两个调用方
            # （`core/scheduler.py` / `api/accounts.py`）见 valid=True 就把
            # `expired`/`invalid` 改回 `registered` —— **死号被复活**，重新进池
            # 被领取（正确性复审 C1，已执行验证）。
            #
            # 没有 AT 就是「判不了」：usable=False（不复活），sso_status 照常带出
            # 供弱信号使用。真正要判有效，走 `refresh_token` / `probe_refresh`
            # 拿实际结果。改动前新号总有 AT，这条只是罕见兜底；现在它是唯一分支。
            return {
                "code": None,
                "summary": f"SSO 探测：{sso_status}（无 AT，不据此判定有效性）",
                "usable": False,
                "sso_status": sso_status,
            }
        try:
            code, summary = probe_token(
                access,
                email=account.email,
                sub=str(account.user_id or ""),
                proxy=(self.config.proxy if self.config else None) or "",
                model=str(extra.get("grok_probe_model") or CPA_PROBE_MODEL),
                log=getattr(self, "_log_fn", None),
            )
        except Exception as exc:
            # **必须留痕**：这里的静默 False 正是当初掩盖 `probe_token(proxy=…)`
            # TypeError 的元凶 —— 那次所有账号都被判无效，而日志里一个字都没有
            # （见 probe.py 的说明）。调用方会把 False 直接落库成 invalid。
            log_fn = getattr(self, "_log_fn", None)
            if log_fn:
                log_fn(f"[Grok] 测活异常（判无效）: {type(exc).__name__}: {str(exc)[:120]}")
            return {"code": None, "summary": str(exc)[:200], "usable": False, "sso_status": ""}
        ok, _reason = self._probe_verdict(code, summary)
        return {"code": code, "summary": str(summary or ""), "usable": ok, "sso_status": ""}

    def _sso_status(self, sso: str) -> str:
        """用 SSO cookie 访问 accounts.x.ai 判断 SSO 状态。

        **本判定已知不可靠**（「测活」动作不用它，但 `check_valid` 仍用 —— 见文末）：

        - `alive`：响应看起来不是登录/注册页（受缺陷 1 影响，实际**总是**这个）；
        - `rejected`：落到 sign-in / sign-up 页（受缺陷 1 影响，实际**不可达**）；
        - `unknown`：网络/浏览器异常。

        三条已知缺陷（2026-10-23 评审实测，改这里之前先读）：

        1. `core.base_executor.Response` **没有 url 字段**（只有 status_code /
           text / headers / cookies），所以「是否被重定向到登录页」根本看不出来
           —— 下面的 `getattr(r, "url", "")` 恒为空串；
        2. 就算改成看响应体也不成立：accounts.x.ai 的每个页面都内嵌同一份
           i18n 文案表，里面有 `Unable to add sign-in method` / `Create your
           account`，健康的页面也会被判成登录页；
        3. 全新会话即使带有效 sso cookie 也常被判未登录而跳 sign-in ——
           `oauth_device.py:172-174` 早就因此**放弃**了这套校验（会误杀有效 SSO）。

        现状与约束（读到这里的人请注意，别把这两件事搞混）：

        - `probe_account_detail` / `check_valid` 仍用它（`sso_status == "rejected"`
          → `apply_probe_status_policy` 判「失效」），这是**改动前就有**的行为，
          本次没有改；
        - 「测活」动作（`execute_action("probe")`）**不**用它 —— 没有 AT 时直接
          报缺并指路「刷新 Token」，不给 SSO 结论。

        已知它会误判（缺陷 3），所以不要把这个结论扩散到更多路径。

        也没有可靠的替代判据：`refresh_token`（登录协议）失败时返回
        `kind="retryable"`，而 `apply_grok_status_policy` 对 retryable 不动状态
        —— 所以它只在**真的换到 token**（正向）或明确 `sso_rejected` 时才有信息量，
        不能拿来当「SSO 还能不能用」的判据（终审 Minor 4）。
        """
        try:
            from modules.execution import BrowserExecutorFactory

            proxy = (self.config.proxy if self.config else None) or None
            executor = BrowserExecutorFactory().create("protocol", proxy=proxy)
            try:
                cookies = executor.get_cookies() or {}
                cookies["sso"] = sso
                executor.set_cookies(cookies)
                r = executor.get("https://accounts.x.ai/", headers={"User-Agent": DEFAULT_UA})
                final = str(getattr(r, "url", "") or "")
                if "sign-in" in final or "sign-up" in final:
                    return "rejected"
                return "alive"
            finally:
                executor.close()
        except Exception:
            return "unknown"

    # ------------------------------------------------------------------
    # 平台操作（账号池能力）
    # ------------------------------------------------------------------

    def get_platform_actions(self) -> list:
        return [
            {"id": "probe", "label": "测活（CLI Proxy）", "params": []},
            {"id": "probe_refresh", "label": "检测有效性（刷新凭证）", "params": []},
            {"id": "refresh_token", "label": "刷新 Token（登录协议）", "params": []},
            # 已弃用：纯协议 device flow 被 CF 挡死（实测 403），从未成功过。
            # 保留 id 转发到 `refresh_token`（老任务/老脚本可能还引用）。
            {"id": "refresh_oauth", "label": "重换 OAuth（已弃用，等同「刷新 Token」）", "params": []},
            # 面板动作（`scope: "panel"`）：目标是外部面板，操作面在「面板管理」
            # 页。账号页的菜单按 scope 过滤掉它们（用户要求「平台管理主要管账号」）。
            # 声明留在这里是因为面板注册表与批量端点都按动作 id 找它们。
            {
                "id": "sync_grok2api_status",
                "label": "同步 grok2api 状态",
                "scope": "panel",
                "params": [],
            },
            {
                "id": "upload_grok2api",
                "label": "上传 grok2api",
                "scope": "panel",
                "params": [
                    {"key": "api_url", "label": "grok2api URL（留空用全局配置）", "type": "text"},
                    {"key": "username", "label": "管理员用户名（留空用全局配置）", "type": "text"},
                    {"key": "password", "label": "管理员密码（留空用全局配置）", "type": "text"},
                    {"key": "nsfw", "label": "开启 NSFW（默认开）", "type": "text"},
                ],
            },
        ]

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        extra = dict(account.extra or {})
        params = dict(params or {})
        proxy = (self.config.proxy if self.config else None) or ""

        if action_id == "probe":
            # grok 的 token 列镜像 SSO，不是 AT —— 只从 extra 读 AT
            from core.credential_fields import get_credential

            access = get_credential(extra, "access_token")
            if not access:
                # 没有 AT 是**新号的常态**（注册默认不换 OAuth，只产 Web SSO）。
                #
                # 这里**不**回落 `_sso_status`：那套「访问 accounts.x.ai 看是否
                # 跳登录页」的判定已知不可靠（见 `_sso_status` 的 docstring ——
                # 响应没有 url、i18n 文案表里就带 sign-in 字样、全新会话会把有效
                # SSO 判成未登录），拿它当测活结论会把好号标成「失效」。
                # 明确告诉用户缺什么、怎么补，比给一个假结论有用。
                return {
                    "ok": False,
                    "error": (
                        "账号没有 access_token（注册默认不产 build 凭据）—— "
                        "要测活需先拿到 AT/RT，但「刷新 Token（登录协议）」走的是"
                        "与注册末尾同一个 device flow（实测常停在「授权未确认」），"
                        "不一定能拿到"
                    ),
                }
            code, summary = probe_token(
                access,
                email=account.email,
                sub=str(account.user_id or ""),
                proxy=proxy,
                model=str(params.get("model") or extra.get("grok_probe_model") or CPA_PROBE_MODEL),
                log=getattr(self, "_log_fn", None),
            )
            # 判定语义与 check_valid 共用（402/403 无额度仍算有效），
            # 否则同一个账号会在「测活」按钮下报失败、在有效性检查里算通过。
            ok, reason = self._probe_verdict(code, summary)
            return {
                "ok": ok,
                "data": {"status": code, "summary": summary},
                "error": "" if ok else (reason or f"HTTP {code}"),
                "account_extra_patch": {
                    "probe_status": code,
                    "probe_summary": summary[:200],
                },
            }

        if action_id == "probe_refresh":
            # 检测有效性：RT 还能不能换新 token。
            #
            # 为什么用它当「有效性检测」（用户需求「参考 grok2api」）：
            # grok2api 把 OAuth 刷新错误的**分类**当作账号失效的判据 ——
            # `invalid_grant` 等永久错误 → 标 reauthRequired。这里同一口径：
            #   - 成功 → 账号有效，顺带把轮换后的新凭证存回去（关键！x.ai 每次
            #     刷新都会轮换 RT，不存回去下一次就 invalid_grant）；
            #   - permanent → 账号需要重新授权（提示走「刷新 Token」动作）；
            #   - configuration → 网关的问题，**不该标账号失效**；
            #   - retryable/unknown → 瞬时或未知，提示重试。
            from .token_refresh import refresh_via_grant

            refresh_token = str(extra.get("refresh_token") or "").strip()
            if not refresh_token:
                return {
                    "ok": False,
                    "error": "账号没有 refresh_token，无法刷新检测；请先执行「刷新 Token（登录协议）」获取",
                }
            result = refresh_via_grant(
                refresh_token, proxy=proxy, log=getattr(self, "_log_fn", None)
            )
            if result.ok:
                tokens = result.tokens
                patch = {
                    "access_token": tokens.get("access_token", ""),
                    "refresh_token": tokens.get("refresh_token", ""),
                    "id_token": tokens.get("id_token", ""),
                    "expires_in": tokens.get("expires_in", 0),
                    "token_type": tokens.get("token_type", "Bearer"),
                    "probe_status": "refresh_ok",
                    "probe_summary": "refresh grant 成功",
                }
                # CPA / Sub2API 产物已随「Grok 只保留 SSO + grok2api」
                # （2026-10-27）一并删除 —— 这里只落 AT/RT 本身，
                # 不再生成 `cpa_record` / 不再落盘到 `cpa_auth_dir`。
                return {
                    "ok": True,
                    "data": {
                        "message": "刷新检测通过（凭证有效，已保存轮换后的新值）",
                        "method": result.method,
                        "expires_in": tokens.get("expires_in"),
                    },
                    "account_extra_patch": patch,
                }
            # 失败：按分类决定是否标账号失效
            patch: dict[str, Any] = {
                "probe_status": (
                    "refresh_invalid" if result.kind == "permanent" else "refresh_error"
                ),
                "probe_summary": result.error[:200],
            }
            if result.kind == "permanent":
                hint = "凭证已失效，请执行「刷新 Token（登录协议）」重新授权"
            elif result.kind == "configuration":
                hint = "网关配置问题（非账号问题），稍后重试或检查 OAuth 配置"
            else:
                hint = "瞬时失败，可稍后重试"
            # 错误码拼进消息：排障时 `invalid_grant` 这个码比自然语言描述有用
            # （上游描述文案可能变化，错误码是稳定的）。
            detail = result.error
            if result.error_code and result.error_code not in detail:
                detail = f"{detail} [{result.error_code}]"
            return {
                "ok": False,
                "data": {
                    "kind": result.kind,
                    "error_code": result.error_code,
                    "status": result.status,
                },
                "error": f"{detail}；{hint}",
                "account_extra_patch": patch,
            }

        if action_id == "refresh_token":
            # 走登录协议重新获取 token（用户需求：刷新 token 走登录协议）。
            #
            # 协议 device flow 被 CF 挡（verify/approve 403），必须用浏览器
            # 完成授权 —— `refresh_via_device_flow` 封装的就是浏览器 flow。
            from .token_refresh import refresh_via_device_flow
            from core.credential_fields import token_column_credential

            sso = str(extra.get("sso") or "").strip() or token_column_credential(
                account, "grok", "sso"
            )
            if not sso:
                return {"ok": False, "error": "账号没有 SSO，无法走登录协议刷新"}
            result = refresh_via_device_flow(
                sso,
                proxy=proxy,
                log=getattr(self, "_log_fn", None),
            )
            if not result.ok:
                return {
                    "ok": False,
                    "data": {"method": result.method, "kind": result.kind},
                    "error": result.error or "登录协议刷新失败",
                }
            tokens = result.tokens or {}
            # `ok=True` 不等于「换到了东西」：上游偶发返回空 body 时，
            # `refresh_via_device_flow` 仍可能带着空 token 回来。不拦的话会
            # 写出空 AT、把 `probe_status` 标成 `refresh_ok`、还生成一个空凭证的
            # cpa_record —— 账号被记成「刚刷新过、凭证正常」（多面检测发现）。
            if not str(tokens.get("access_token") or "").strip():
                return {
                    "ok": False,
                    "data": {"method": result.method, "kind": result.kind},
                    "error": (
                        "登录协议返回里没有 access_token（上游可能只回了空 body）"
                        "—— 没有写入任何凭证，可稍后重试"
                    ),
                }
            patch = {
                "access_token": tokens.get("access_token", ""),
                "refresh_token": tokens.get("refresh_token", ""),
                "id_token": tokens.get("id_token", ""),
                "expires_in": tokens.get("expires_in", 0),
                "token_type": tokens.get("token_type", "Bearer"),
                "probe_status": "refresh_ok",
                "probe_summary": "登录协议刷新成功",
            }
            log_fn = getattr(self, "_log_fn", None)
            # 在 try **之前**算好：下面 `cpa_notes` 在 try 之外读它，
            # **CPA / Sub2API 产物已删**（2026-10-27，用户要求 Grok 只保留
            # SSO + grok2api）。这里只落 AT/RT 本身 —— 刷新链路仍需要它们
            # （`refresh_via_grant` 用 RT 换新 token），但不再生成
            # `cpa_record`、不再落盘到 `cpa_auth_dir`、不再上传到面板。
            return {
                "ok": True,
                "data": {
                    "message": "Token 已刷新（登录协议），新凭证已保存",
                    "method": result.method,
                    "expires_in": tokens.get("expires_in"),
                },
                "account_extra_patch": patch,
            }

        if action_id == "refresh_oauth":
            # 已弃用（2026-10-04）：这个动作走**纯协议** device flow，
            # 而 device/verify 与 device/approve 被 CF 保护（实测 403）——
            # 它从未成功过一次（且带代理调用还会因历史 import bug 直接崩）。
            # 现在由 `refresh_token` 完整取代（同一件事，走浏览器完成授权）。
            # 保留 id 转发到新实现：老任务/老脚本里可能还引用着它。
            return self.execute_action("refresh_token", account, params)

        if action_id == "sync_grok2api_status":
            # 单账号版（批量端点有专用分支，一次拉列表写回所有账号）。
            # 读 grok2api 列表里的 `authStatus` + `enabled` 写回本地。
            from services.panel_status_sync import sync_panel_status_batch

            # 主键是 UUID（ADR-0008）：`int(...)` 兜底会在拿到 UUID 时直接抛。
            account_id = str(getattr(account, "id", None) or extra.get("account_id") or "")
            probe = AccountRecord(platform="grok", email=account.email, id=account_id)
            updates = sync_panel_status_batch(ACTION_PANEL_KEYS[action_id], [probe])
            update = updates.get(account_id, {})
            ok = bool(update.get("ok"))
            message = str(update.get("message") or "同步完成")
            return {
                "ok": ok,
                "data": {"message": f"grok2api 状态同步完成：{message}"},
                "error": "" if ok else message,
                "account_extra_patch": update.get("patch") or {},
            }

        if action_id == "upload_grok2api":
            return self._action_upload_grok2api(account, extra, params, proxy)

        raise NotImplementedError(f"平台 {self.name} 不支持操作: {action_id}")

    def get_quota(self, account: Account) -> dict:
        extra = account.extra or {}
        return {
            "has_oauth": bool(extra.get("access_token")),
            "expires_in": extra.get("expires_in", 0),
            "probe_status": extra.get("probe_status"),
        }

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _register_via_browser(
        self, extra: dict, proxy, log, requested_email: str, password: str = ""
    ) -> Account:
        """浏览器主导注册（默认路径）。

        为什么默认走这条：x.ai 的 Cloudflare 只有 camoufox 能过（chromium 一律
        403），协议发码又是「假接受」不投递，Turnstile 必须由页面自己触发。
        实测这条路径能真正注册成功。

        邮箱来源：`mail_provider=icloud_local` 时用 iCloud 号池（能读码）；
        其它渠道走通用的 `mailbox.get_email()` + `wait_for_code`。
        """
        from .register_browser import configured_captcha, register_grok_via_browser

        # 邮箱：iCloud 号池要拿 alias_id 才能读码；其它渠道用 mailbox 对象
        alias_id = None
        provider = str(extra.get("mail_provider") or "").strip().lower()
        email = str(requested_email or "").strip()
        mailbox = self.mailbox
        mailbox_account = None

        if not email:
            if mailbox is None:
                from modules.mail import create_mailbox

                mailbox = create_mailbox(provider, extra=extra, proxy=proxy)
                mailbox._task_control = getattr(self, "_task_control", None)
                mailbox._log_fn = log
            # 告诉渠道「谁在用」—— 邮箱消耗按平台记账（同一个地址注册过
            # ChatGPT 之后还能注册 Grok）。不注入就退回全局锁。
            mailbox._platform_name = self.name
            mailbox_account = mailbox.get_email()
            email = str(getattr(mailbox_account, "email", "") or "").strip()
        else:
            # 已有邮箱时复用渠道凭证（Outlook 等渠道收码需要 account_id）
            if mailbox is not None:
                mailbox_account = self._mailbox_account_for(mailbox, email)

        if not email:
            raise RuntimeError("邮箱分配失败：未拿到邮箱地址")
        if mailbox_account is not None:
            alias_id = (mailbox_account.extra or {}).get("icloud_alias_id")
            try:
                alias_id = int(alias_id) if alias_id is not None else None
            except (TypeError, ValueError):
                alias_id = None
        if alias_id is None and provider == "icloud_local":
            # 只对 iCloud 渠道查这张表：其它渠道查了也永远没有结果，
            # 白搭一次跨库查询（评审发现）。
            alias_id = self._alias_id_for(email)
        log(f"[Grok] 已分配邮箱: {email}（浏览器模式）")

        # 显式传入的密码优先（`register(email, password)` 的 password 参数，
        # 由 api/tasks.py 从任务请求透传）。此前浏览器路径只读 extra，把
        # 调用方明确指定的密码静默丢掉了（评审发现）。
        password = str(password or "").strip() or str(extra.get("grok_password") or "").strip()

        # 发码节流：x.ai 按 IP 限流（见 `_throttle_send_code` 的说明），
        # 而这条是**默认**路径 —— 不在这里调用的话，并发任务会直接把
        # `grok_send_code_min_interval` 变成摆设，然后一起撞限流。
        try:
            _throttle_send_code(
                _to_float(extra.get("grok_send_code_min_interval"), 0.0), log
            )
        except TaskInterruption:
            raise
        except Exception as exc:  # noqa: BLE001 - 节流失败不该挡住注册
            log(f"[Grok] 发码节流异常（忽略）: {type(exc).__name__}")

        try:
            result = register_grok_via_browser(
                email,
                alias_id=alias_id,
                proxy=proxy or "",
                password=password,
                # 等码超时跟随全局 OTP 设置（协议路径也是这么取的），
                # 写死 240 秒会让运维调 `mailbox_otp_timeout_seconds` 时
                # 以为没生效（评审发现）。
                code_timeout=float(self.get_mailbox_otp_timeout(240)),
                headless=_truthy(extra.get("grok_browser_headless"), default=True),
                # 非 iCloud 渠道（Outlook/tempmail…）读码要走渠道自己的
                # wait_for_code —— 只给地址读不到，必须带上账号对象。
                mailbox=mailbox if alias_id is None else None,
                mailbox_account=mailbox_account if alias_id is None else None,
                # 等码最长 240 秒，把任务控制传下去让「停止/跳过」能打断它
                task_control=getattr(self, "_task_control", None),
                # 界面上配的验证码解决器（审计 R-21 的另一半）：配了就进铸币链；
                # 没配 / 没 key 时 `configured_captcha` 返回 None，行为与以前一致。
                captcha=configured_captcha(self.config, log=log),
                log=log,
            )
        except TaskInterruption:
            # 用户主动停止/跳过不是失败 —— 记 failed 会把还好的别名放回池子
            raise
        except Exception:
            # 注册抛异常也必须把号放回去，否则别名永远停在 in_use（池子只出不进）
            self._record_mailbox_status(mailbox, mailbox_account, "failed", log)
            raise
        if not result.get("ok"):
            # 「地址已消耗」→ 标 used 而不是 failed：
            # failed 在 iCloud 池里会 release 回 available，下次又被领出来
            # 撞同一堵墙（线上实测同一地址被反复领取十余次）。
            #
            # `consumes_address` 覆盖两种情形：
            #   1. `already_registered`：该邮箱在 x.ai 已有账号；
            #   2. 发码被拒（限流 / 邮箱注册不可用 / 静默无理由）——
            #      这个地址这一轮已经用掉了，且再试也是同样结局。
            consumed = bool(
                result.get("already_registered") or result.get("consumes_address")
            )
            # 地址状态见 `_mailbox_status_for_failure`：瞬时拒绝不能烧号。
            self._record_mailbox_status(
                mailbox, mailbox_account, _mailbox_status_for_failure(result), log
            )
            message = result.get("error") or "浏览器注册失败"
            # 默认按「不可重试」处理：`consumes_address` 为真说明地址已用掉，
            # 重开一轮拿到的是**同一个**地址（池子就那几个），必然同样结局。
            # 只有浏览器层明确说 `retryable=True` 时才放行重试。
            if consumed and result.get("retryable") is not True:
                # 明确告诉上层「重开一轮也是同样结局」，别白烧 4 分钟。
                from core.task_runtime import NonRetryableRegisterError

                raise NonRetryableRegisterError(message)
            raise RuntimeError(message)

        sso = str(result.get("sso") or "")
        final_password = str(result.get("password") or "")

        # 换 OAuth（AT/RT = CPA / Build 凭据）**默认关**（用户要求 2026-10-23）：
        # 「注册 grok 时不需要注册 build 了，只要 websso（上传 grok2api）那个。」
        #
        # 依据（线上日志 task_1792682142255 / task_1792679012174）：这段 device
        # flow 每次都是 `点击授权按钮 → 授权未确认，仍尝试取 token →
        # token 轮询失败: ` → `OAuth 未成功（SSO 仍可用）`，白烧 4 分钟/号；
        # 随后 grok2api 接入（Web SSO）成功。也就是说 build 侧一次都没成功过，
        # 真正有产物的是 Web SSO 那一段。
        #
        # 保留开关：显式 `grok_oauth_exchange=1` 仍可换取（手动要 build 的入口）。
        account_extra: dict[str, Any] = {
            "sso": sso,
            "register_mode": "browser",
        }
        if _truthy(extra.get("grok_oauth_exchange"), default=False):
            from .register_browser import exchange_oauth_via_browser

            tokens = exchange_oauth_via_browser(sso, proxy=proxy or "", log=log)
            if tokens and tokens.get("access_token"):
                account_extra.update({
                    "access_token": tokens.get("access_token", ""),
                    "refresh_token": tokens.get("refresh_token", ""),
                    "id_token": tokens.get("id_token", ""),
                    "expires_in": tokens.get("expires_in", 0),
                    "token_type": tokens.get("token_type", "Bearer"),
                })
                # CPA / Sub2API 产物已删（2026-10-27）：不再生成 `cpa_record`、
                # 不再落盘到 `cpa_auth_dir`。AT/RT 本身仍写入 extra ——
                # `refresh_via_grant` 需要 RT 才能续期。
            else:
                log("[Grok] OAuth 未成功（SSO 仍可用）")
        else:
            # 留痕：不换 build 凭据是**有意的默认**，不是失败。不写日志的话，
            # 用户看到账号没有 AT/RT 会以为链路坏了（与「OAuth 未成功」混为一谈）。
            log("[Grok] 跳过 build 凭据（OAuth 换取已默认关闭，只产出 Web SSO）")

        # grok2api 接入（开关：`grok2api_enabled`，留空按「已配置就开」处理）
        if _truthy(extra.get("grok2api_enabled"), default=True) and _truthy(
            extra.get("grok_grok2api_ingest"), default=True
        ):
            try:
                from .grok2api import Grok2ApiClient

                g2a = Grok2ApiClient.from_config(proxy=proxy or "")
                if g2a.configured:
                    from services.chatgpt_sync import upload_proxy_for

                    ok_ing, msg_ing = g2a.ingest_sso(
                        sso, email,
                        nsfw=_truthy(extra.get("grok2api_auto_nsfw"), default=True),
                        # 「上传代理」开关：把账号绑定的代理一并交给 grok2api
                        # （它没有账号级代理字段，走 egress 节点绑定）
                        proxy=upload_proxy_for("grok2api", account_extra),
                        log=log,
                    )
                    account_extra["grok2api_ingested"] = bool(ok_ing)
                    account_extra["grok2api_result"] = msg_ing
                else:
                    log("[Grok] 未配置 grok2api，跳过接入")
            except Exception as exc:
                log(f"[Grok] grok2api 接入异常: {type(exc).__name__}: {str(exc)[:120]}")

        # 注册成功 → 把号池记账落定（不记的话别名永远停在 in_use）
        self._record_mailbox_status(mailbox, mailbox_account, "used", log)

        # 可选：测活（与协议路径同一开关 —— 浏览器路径此前漏了这一步，
        # 于是浏览器注册的账号永远没有 probe_status/probe_summary）
        if _truthy(extra.get("grok_probe_after_register"), default=False):
            access = account_extra.get("access_token", "")
            if not access:
                # 默认路径不换 OAuth，所以这里**必然**没有 AT —— 不解释一句的话，
                # 用户打开开关后看不到任何 probe_status，会以为测活坏了。
                log("[Grok] 跳过注册后测活：没有 AT（注册默认不换 build 凭据）")
            else:
                try:
                    code_http, summary = probe_token(
                        access,
                        email=email,
                        sub=self._subject_from_access_token(access),
                        proxy=proxy or "",
                        model=str(extra.get("grok_probe_model") or CPA_PROBE_MODEL),
                        log=log,
                    )
                    account_extra["probe_status"] = code_http
                    account_extra["probe_summary"] = str(summary)[:200]
                except Exception as exc:
                    log(f"[Grok] 测活异常: {type(exc).__name__}")

        return Account(
            platform=self.name,
            email=email,
            password=final_password,
            user_id=self._subject_from_access_token(str(account_extra.get("access_token") or "")),
            region=str(extra.get("grok_region") or ""),
            token=sso,
            status=AccountStatus.REGISTERED,
            extra=account_extra,
        )

    def _record_mailbox_status(self, mailbox, account, status: str, log) -> None:
        """把注册结果记回邮箱渠道（iCloud/Outlook 号池的记账钩子）。

        为什么必须有：`set_account_status` 之前**只有 ChatGPT 引擎调用**，
        Grok 路径一个调用点都没有 —— 于是领过的别名永远停在 `in_use`，
        池子只出不进。实测：一次失败任务留下 2 个 `in_use` 死号，
        只能靠 2 小时后的 `release_stale_claims` 兜底放回。

        **`failed` 在两个渠道里含义不同，调用方要知道**：
          - iCloud 别名池：`failed` → `release_alias_claim` → 放回 `available`，
            地址还能再领（适合「这轮没跑成」）；
          - Outlook 池：`failed` → 状态置 `failed`，而领取不认 `failed`
            （`available`/`used`/空 才可取），等于**永久移出池子**（与 ChatGPT 引擎同一约定）。
        所以「地址已被消耗」这类失败必须记 `used` 而不是 `failed` ——
        记 failed 在 iCloud 上会导致反复重领同一个死号。
        """
        if mailbox is None or account is None:
            return
        setter = getattr(mailbox, "set_account_status", None)
        if not callable(setter):
            return
        try:
            setter(account, status)
        except Exception as exc:  # noqa: BLE001 - 记账失败不该影响注册结果
            if log:
                log(f"[Grok] 号池记账失败（忽略）: {type(exc).__name__}")

    def _alias_id_for(self, email: str) -> Optional[int]:
        """按地址找 iCloud 别名的数字 id（读码用）。找不到/出错返回 None。

        只在 iCloud 渠道上有意义（调用方按 provider 判断），异常也记一笔：
        静默返回 None 会让「DB 坏了」表现成「没有可用收件箱」，排查方向全错。
        """
        try:
            from core.db import ICloudAliasModel, platform_session
            from sqlmodel import select

            with platform_session("icloud") as session:
                row = session.exec(
                    select(ICloudAliasModel).where(ICloudAliasModel.address == email)
                ).first()
            return str(row.id) if row is not None else None
        except Exception as exc:  # noqa: BLE001
            log_fn = getattr(self, "_log_fn", None)
            if log_fn:
                log_fn(f"[Grok] 查询 iCloud 别名失败（忽略）: {type(exc).__name__}")
            return None

    def _mailbox_account_for(self, mailbox, email: str):
        """已知邮箱时复用渠道凭证（避免额外 get_email 消耗配额）。

        关键：多数渠道（如 tempmail.lol）收码需要 `account_id`（inbox token），
        仅有邮箱地址是收不到码的。所以优先复用 mailbox 上次返回的账号对象。
        """
        for attr in ("_last_account", "last_account", "_account"):
            cached = getattr(mailbox, attr, None)
            if cached is not None and str(getattr(cached, "email", "") or "") == email:
                return cached
        # 渠道自身缓存的邮箱（如 TempMailLolMailbox._email/_token）
        if str(getattr(mailbox, "_email", "") or "") == email:
            token = getattr(mailbox, "_token", "") or ""
            if token:
                try:
                    from core.base_mailbox import MailboxAccount

                    return MailboxAccount(email=email, account_id=token)
                except Exception:
                    logger.debug("忽略异常（保留原行为）", exc_info=True)
        # 无凭证：交给调用方重新建邮（只有邮箱地址收不到码）
        return None

    def _action_upload_grok2api(self, account: Account, extra: dict, params: dict, proxy: str) -> dict:
        """把账号上传到 grok2api（手动动作）。

        与注册流程末尾的自动接入（`register` 里的 `grok_grok2api_ingest`）
        是同一套调用，差别只在**手动触发**：已注册的老账号、注册时没配
        grok2api、或自动接入失败的账号，都能在这里补传。

        需要的凭据：优先 SSO（grok2api 的 web 导入吃 SSO），没有 SSO 时
        才考虑 access_token —— 后者 grok2api 不收（它的导入格式是
        SSO / 凭据 JSON），所以缺 SSO 直接明确报错，别让它变成一个
        语焉不详的 500。
        """
        # 与 `refresh_token` 分支同款兜底：extra 无 sso 时读 token 列镜像
        # （grok 的 token 列 = SSO 镜像；OAuth 形态脏值会被拒掉）。
        from core.credential_fields import token_column_credential

        sso = str(extra.get("sso") or "").strip() or token_column_credential(
            account, "grok", "sso"
        )
        if not sso:
            return {"ok": False, "error": "账号没有 SSO（grok2api 的 Web 导入需要 SSO）"}

        from .grok2api import Grok2ApiClient

        client = Grok2ApiClient.from_config(
            proxy=proxy,
            api_url=str(params.get("api_url") or ""),
            username=str(params.get("username") or ""),
            password=str(params.get("password") or ""),
        )
        if not client.configured:
            return {
                "ok": False,
                "error": "grok2api 未配置（「全局配置 → 面板配置 → grok2api」填地址 / 用户名 / 密码）",
            }

        nsfw = _truthy(params.get("nsfw"), default=True)
        from services.chatgpt_sync import upload_proxy_for

        try:
            ok, msg = client.ingest_sso(
                sso,
                account.email,
                nsfw=nsfw,
                proxy=upload_proxy_for("grok2api", account.extra or {}),
            )
        except Exception as exc:  # noqa: BLE001 - 动作失败要带原因回前端
            return {
                "ok": False,
                "error": f"grok2api 上传异常: {type(exc).__name__}: {str(exc)[:150]}",
            }

        # 同时写两处：`grok2api_*` 保持与自动接入的字段一致（老地方），
        # `sync_statuses.grok2api` 供账号列表的通用上传状态标签渲染。
        from services.chatgpt_sync import record_grok2api_sync_result

        sync_state = record_grok2api_sync_result(extra, ok, msg)
        return {
            "ok": ok,
            "data": {"message": msg, "sync": sync_state},
            "error": "" if ok else msg,
            "account_extra_patch": {
                "grok2api_ingested": bool(ok),
                "grok2api_result": msg,
                "sync_statuses": {"grok2api": sync_state},
            },
        }

    @staticmethod
    def _subject_from_access_token(access_token: str) -> str:
        """从 AT 的 JWT 里取 `sub`。

        以前这里读 `extra.cpa_record.sub` —— 而 `cpa_record` 已随 CPA 支持
        一并删除（2026-10-27）。`sub` 本身仍有用（测活与账号 user_id 要它），
        直接从 AT 解，不再依赖那个中间结构。
        """
        token = str(access_token or "").strip()
        if not token:
            return ""
        try:
            from .oauth_device import _decode_jwt_payload

            payload = _decode_jwt_payload(token)
        except Exception:  # noqa: BLE001 - 解不出就当没有（不影响主流程）
            return ""
        return str(payload.get("sub") or "")

__all__ = ["GrokPlatform"]
