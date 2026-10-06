"""ChatGPT / Codex CLI 平台插件"""

import logging
from datetime import datetime, timezone

from core.base_mailbox import BaseMailbox
from core.base_platform import Account, BasePlatform, RegisterConfig
from core.registry import register
from modules.platforms import create_application_platform_registration_service
from platforms.chatgpt.chatgpt_registration_mode_adapter import (
    ChatGPTRegistrationContext,
    build_chatgpt_registration_mode_adapter,
)
from platforms.chatgpt.registration_engine import generate_password

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@register
class ChatGPTPlatform(BasePlatform):
    name = "chatgpt"
    display_name = "ChatGPT"
    version = "1.0.0"
    #: ChatGPT **只有纯协议**：全树无 playwright/camoufox，走 `curl_cffi`
    #: 指纹伪装 + 协议层注册。此前没声明，继承了基类默认的三个执行器 ——
    #: 界面上给它选「无头/有头浏览器」，运行时会静默降级，用户以为选了浏览器。
    supported_executors = ["protocol"]
    executor_labels = {
        "protocol": "纯协议（curl_cffi 指纹伪装，唯一路径）",
    }
    # 注册方式（界面上显示在「全局配置 → 默认注册方式」下）。
    #
    # 这两个维度原先只在**任务页**（浏览器 localStorage）里选，全局没有默认值 ——
    # 换台机器/换个浏览器打开，选项就回到硬编码默认。这里补上全局默认：
    # 任务页仍可临时覆盖（它的值随任务提交，优先级更高，见
    # `frontend/src/lib/chatgptRegistrationRequestAdapter.ts`）。
    registration_modes = [
        {
            "key": "chatgpt_registration_mode",
            "label": "Token 方案",
            "desc": (
                "有 RT 方案走新链路，产出 Access Token + Refresh Token；"
                "无 RT 方案走旧链路，只产出 Access Token / Session，"
                "依赖 RT 的能力可能不可用。任务页可临时覆盖"
            ),
            "default": "refresh_token",
            "options": [
                {"value": "refresh_token", "label": "有 RT（默认推荐）"},
                {"value": "access_token_only", "label": "无 RT（兼容旧方案）"},
            ],
        },
        {
            "key": "chatgpt_register_flow",
            "label": "注册流程",
            "desc": "邮箱注册用邮箱池收码；手机注册进 add-phone 后自动租号、等短信。任务页可临时覆盖",
            "default": "email",
            "options": [
                {"value": "email", "label": "邮箱注册（默认）"},
                {"value": "phone", "label": "手机注册（需要接码）"},
                {"value": "phone_with_email", "label": "手机 + 邮箱（需要接码）"},
            ],
        },
    ]

    def __init__(self, config: RegisterConfig = None, mailbox: BaseMailbox = None):
        super().__init__(config)
        self.mailbox = mailbox

    def check_valid(self, account: Account) -> bool:
        """账号有效性 = access_token 能通过 /backend-api/me 认证。

        原实现走 `platforms.chatgpt.payment.check_subscription_status`（订阅计划
        查询），支付模块整体移除后改用同源的 `status_probe`：它同样打
        `/backend-api/me`，但只关心认证是否通过，不看订阅计划。
        """
        try:
            from platforms.chatgpt.status_probe import probe_local_chatgpt_status

            class _A:
                pass

            a = _A()
            extra = account.extra or {}
            from core.credential_fields import get_credential, token_column_credential

            a.access_token = get_credential(extra, "access_token") or token_column_credential(
                account, "chatgpt", "access_token"
            )
            a.cookies = extra.get("cookies", "")
            a.user_id = account.user_id
            probe = probe_local_chatgpt_status(a, proxy=self.config.proxy if self.config else None)
            # status_probe 认证通过时写 "access_token_valid"（见 status_probe.py:472）
            return str(probe.get("auth", {}).get("state") or "") == "access_token_valid"
        except Exception:
            return False

    def register(self, email: str = None, password: str = None) -> Account:
        proxy = self.config.proxy if self.config else None
        extra_config = (self.config.extra or {}) if self.config and getattr(self.config, "extra", None) else {}
        log_fn = getattr(self, "_log_fn", print)

        # 没有邮箱池就直接报错：曾经这里会悄悄兜底成 TempMail.lol（一次性临时
        # 邮箱），注册出来的账号在临时邮箱过期后就永远收不到验证码了 —— 用户看到
        # 的是「注册成功」，实际是一个救不回来的账号。临时邮箱渠道已整体删除，
        # 现在唯一正确的行为是明确失败。
        mailbox = self.mailbox
        if mailbox is None:
            raise RuntimeError(
                "没有可用的邮箱：任务启动前必须先选定邮箱服务"
                "（Outlook 号池或 iCloud 隐私邮箱）。"
            )
        mailbox_kind = "mailbox"

        try:
            service = create_application_platform_registration_service(
                adapter_builder=build_chatgpt_registration_mode_adapter,
                context_builder=ChatGPTRegistrationContext,
                password_generator=generate_password,
            )
            return service.register_chatgpt(
                mailbox=mailbox,
                proxy=proxy,
                email=email,
                password=password,
                settings=extra_config,
                mailbox_kind=mailbox_kind,
                log_fn=log_fn,
            )
        except RuntimeError as exc:
            if not bool(getattr(exc, "retryable", True)):
                from core.task_runtime import NonRetryableRegisterError

                raise NonRetryableRegisterError(str(exc)) from exc
            raise

    def get_platform_actions(self) -> list:
        return [
            {"id": "probe_local_status", "label": "探测本地状态", "params": []},
            {"id": "check_plus_trial", "label": "检测 Plus 试用", "params": []},
            # 下面三个是**面板动作**（`scope: "panel"`）：上传/同步的目标是外部
            # 面板，操作面在「面板管理」页。账号页的菜单按 scope 过滤掉它们 ——
            # 用户要求「平台管理主要管账号就行」。声明仍留在这里：面板注册表
            # 按动作 id 找到它们（`services/panel_registry.py` 的 upload_action /
            # sync_action），批量端点也要靠 `execute_action` 分发。
            {"id": "sync_cliproxyapi_status", "label": "同步 CLIProxyAPI 状态", "params": [], "scope": "panel"},
            # Sub2API / chatgpt2api 的「同步远端状态」：列表接口自带权威状态，
            # 读回来写回本地（批量端点有专用分支，一次拉远端列表）。
            {"id": "sync_sub2api_status", "label": "同步 Sub2API 状态", "params": [], "scope": "panel"},
            {"id": "sync_chatgpt2api_status", "label": "同步 chatgpt2api 状态", "params": [], "scope": "panel"},
            {"id": "refresh_token", "label": "刷新 Token", "params": []},
            {"id": "backfill_refresh_token", "label": "补 RT", "params": []},
            {"id": "bind_2fa", "label": "绑定 2FA", "params": []},
            {
                "id": "upload_cpa",
                "label": "上传 CPA",
                "scope": "panel",
                "params": [
                    {"key": "api_url", "label": "CPA API URL", "type": "text"},
                    {"key": "api_key", "label": "CPA API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_sub2api",
                "label": "上传 Sub2API",
                "scope": "panel",
                "params": [
                    {"key": "api_url", "label": "Sub2API API URL", "type": "text"},
                    {"key": "api_key", "label": "Sub2API API Key", "type": "text"},
                ],
            },
            {
                "id": "upload_chatgpt2api",
                "label": "上传 chatgpt2api",
                "scope": "panel",
                "params": [
                    {"key": "api_url", "label": "chatgpt2api 地址", "type": "text"},
                    {"key": "api_key", "label": "chatgpt2api 管理密钥", "type": "text"},
                ],
            },
        ]

    def _refresh_via_login(self, a, result):
        """RT / session token 都拿不到可用 AT 时的兜底：重走登录链。

        需要邮箱 + 密码。服务端要邮箱验证码时，先看这个地址能不能从号池里读到
        收件箱（读不到就直接失败并说清原因，不去撞一个必然超时的收件箱）；
        整条链上任何一处认出「账号已封禁」就把 `banned` 置真。

        **原地改写并返回同一个 `result`**（调用方写 `result = self._refresh_via_login(...)`
        也行，不写也行 —— 两条路等价）。刻意不做成"返回新对象"：调用方已经把
        `result` 拿在手上，多一份副本只会让"哪个才是最新的"变成隐患。
        """
        from platforms.chatgpt.login_refresh import LoginAccessTokenRefresher
        from services.chatgpt_otp_mailbox import resolve_otp_mail_provider

        email = str(getattr(a, "email", "") or "")
        password = str(getattr(a, "password", "") or "")
        extra = getattr(a, "extra", {}) or {}
        log = getattr(self, "_log_fn", None) or logger.info
        config = dict((self.config.extra or {}) if self.config else {})

        # 没密码这条链根本走不通（密码是登录链的第一道）。提前返回，别去解析
        # 邮箱 —— `resolve_otp_mail_provider` 会真连 IMAP/OAuth 做基线读取
        # （`_prime`），对注定失败的账号白花几秒网络 I/O。
        if not password:
            result.success = False
            result.banned = False
            result.error_message = "账号没有密码，无法走登录流程（需要密码 + 2FA）"
            result.strategy = ""
            return result

        # 邮箱是否入池 —— 没入池就没有验证码可读，提前失败好过等满一个超时
        mail_provider, mail_reason = resolve_otp_mail_provider(
            email,
            account_extra=extra,
            config=config,
            proxy=self.config.proxy if self.config else None,
            log_fn=log,
        )
        if mail_provider is None:
            log(f"[登录刷新] 邮箱 {email} 不在号池里，读不到收件箱（{mail_reason}）")

        login_result = LoginAccessTokenRefresher(
            email=email,
            password=password,
            totp_secret=str(extra.get("totp_secret") or ""),
            proxy=self.config.proxy if self.config else None,
            extra_config=config,
            # 默认值由 refresher 自己兜（它知道 mail_reason 的语义），这里不再重复
            mail_provider=mail_provider,
            mail_unavailable_reason=mail_reason,
            log_fn=log,
        ).run()

        if login_result.success:
            # 登录链拿到的 AT 也要**真校验**（打一次 /backend-api/me）——
            # 用户要求「GPT刷新token你要确认AT真的更新了」。此前这里
            # `verified = True` 是写死的：登录链刚跑完就假定可用，若服务端
            # 返回一个已失效的令牌，会被当成功写回库，下一个任务全线失败。
            from platforms.chatgpt.token_refresh import TokenRefreshManager

            verifier = TokenRefreshManager(proxy_url=self.config.proxy if self.config else None)
            verified, verify_message = verifier.verify_access_token(login_result.access_token)
            if not verified:
                result.success = False
                result.verified = False
                result.verify_message = verify_message
                result.error_message = (
                    f"登录流程拿到 AT 但未通过校验（{verify_message}）"
                )
                result.strategy = login_result.strategy
                log(f"[登录刷新] {result.error_message}")
                return result

            result.success = True
            result.access_token = login_result.access_token
            result.refresh_token = login_result.refresh_token or result.refresh_token
            result.verified = True
            result.verify_message = ""
            result.error_message = ""
            result.strategy = login_result.strategy
            # 登录链顺带换到的其它凭证也要能落库，否则白跑一趟
            result.session_token = login_result.session_token
            result.id_token = login_result.id_token
            result.cookie_header = login_result.cookie_header
            return result

        # 登录链认出封禁 → 把结论带回去（调用方据此标「已封禁」）
        result.success = False
        result.banned = bool(login_result.banned)
        result.error_message = login_result.error_message or "登录流程刷新失败"
        result.strategy = login_result.strategy
        return result

    def execute_action(self, action_id: str, account: Account, params: dict) -> dict:
        proxy = self.config.proxy if self.config else None
        extra = account.extra or {}

        class _A:
            """把 Account 摊平成 token_refresh / 登录链认识的字段形状。"""

            email = ""
            password = ""
            access_token = ""
            refresh_token = ""
            id_token = ""
            session_token = ""
            client_id = ""
            cookies = ""
            user_id = ""
            extra: dict = {}

        a = _A()
        a.email = account.email
        a.password = account.password
        from core.credential_fields import get_credential, token_column_credential

        a.access_token = get_credential(extra, "access_token") or token_column_credential(
            account, "chatgpt", "access_token"
        )
        a.refresh_token = get_credential(extra, "refresh_token")
        a.id_token = get_credential(extra, "id_token")
        a.session_token = get_credential(extra, "session_token")
        a.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
        a.cookies = extra.get("cookies", "")
        a.user_id = account.user_id
        a.extra = extra

        if action_id == "probe_local_status":
            from platforms.chatgpt.status_probe import probe_local_chatgpt_status

            probe_result = probe_local_chatgpt_status(a, proxy=proxy)
            summary = (
                f"认证={probe_result.get('auth', {}).get('state', 'unknown')}, "
                f"订阅={probe_result.get('subscription', {}).get('plan', 'unknown')}, "
                f"Codex={probe_result.get('codex', {}).get('state', 'unknown')}"
            )
            return {
                "ok": True,
                "data": {
                    "message": f"本地状态探测完成：{summary}",
                    "probe": probe_result,
                },
                "account_extra_patch": {
                    "chatgpt_local": probe_result,
                },
            }

        if action_id == "check_plus_trial":
            from platforms.chatgpt.status_probe import (
                PLUS_TRIAL_INCONCLUSIVE,
                probe_plus_trial_status,
            )

            trial = probe_plus_trial_status(a, proxy=proxy)
            conclusive = trial["status"] not in PLUS_TRIAL_INCONCLUSIVE
            return {
                "ok": conclusive,
                "data": {
                    "message": f"Plus 试用检测完成：{trial['label']}",
                    "plus_check": trial,
                },
                "error": "" if conclusive else trial.get("message") or trial["label"],
                # 没查出结论就不落库，免得账号从"未检测"里消失、看着像查过了
                "account_extra_patch": {"plus_check": trial} if conclusive else {},
            }

        if action_id == "sync_cliproxyapi_status":
            from services.cliproxyapi_sync import is_sync_ok, sync_chatgpt_cliproxyapi_status

            sync_result = sync_chatgpt_cliproxyapi_status(a)
            ok = is_sync_ok(sync_result)
            summary = (
                f"远端状态={sync_result.get('status') or 'not_found'}, "
                f"探测={sync_result.get('remote_state') or 'not_checked'}"
            )
            return {
                "ok": ok,
                "data": {
                    "message": f"CLIProxyAPI 状态同步完成：{summary}",
                    "sync": sync_result,
                },
                "error": sync_result.get("message") if not ok else "",
                "account_extra_patch": {
                    "sync_statuses": {
                        "cliproxyapi": sync_result,
                    },
                },
            }

        if action_id == "refresh_token":
            from platforms.chatgpt.token_refresh import TokenRefreshManager

            manager = TokenRefreshManager(proxy_url=proxy)
            previous_at = str(getattr(a, "access_token", "") or "").strip()
            result = manager.refresh_account(a)
            _log = getattr(self, "_log_fn", None) or logger.info

            # 兜底两条触发条件：
            # ① 刷新调用回了 200 但 AT 没通过校验（见 verify_access_token）；
            # ② 整链失败（session/OAuth 都拿不到可用 AT）—— 登录链是最后一条
            #    能拿回 AT 的路，也是「号没了」措辞唯一会出现的地方（用户
            #    要求：禁用靠登录流程发掘）。实测 10 个 chatgpt 账号全是
            #    session-only，会话死了刷新链自身无路可走。
            # 封禁是终局结论，不兜底（登录链只会被同样拒绝）。
            refresh_failed_reason = ""
            if result.success and not result.verified:
                _log(f"[刷新Token] 刷出的 AT 未通过校验（{result.verify_message}），改走登录流程")
                result = self._refresh_via_login(a, result)
            elif not result.success and not result.banned:
                refresh_failed_reason = (
                    result.error_message or result.verify_message or "刷新失败"
                )
                _log(f"[刷新Token] {refresh_failed_reason}，改走登录流程")
                result = self._refresh_via_login(a, result)
                if not result.success and not result.banned:
                    # 登录链也失败：两段原因都要留下（否则「为什么没刷上」
                    # 只剩后半句，用户看不到刷新链为什么先失败）。
                    login_reason = result.error_message or "登录流程刷新失败"
                    if refresh_failed_reason and refresh_failed_reason not in login_reason:
                        result.error_message = f"{refresh_failed_reason}；登录流程：{login_reason}"

            if result.success:
                # 登录链换回来的 AT 是新签发的 —— refreshed 要按它重算，
                # 不能沿用刷新链失败时的旧值（那是「没换发」的语义）。
                result.refreshed = bool(result.access_token) and result.access_token != previous_at

            stamp = {"ok": result.success, "verified": bool(result.verified),
                     "refreshed": bool(result.refreshed),
                     "strategy": str(result.strategy or ""),
                     "message": result.error_message or result.verify_message or "",
                     "at": _utcnow_iso()}

            if not result.success:
                return {
                    "ok": False,
                    "error": result.error_message,
                    # 批量界面读的是 data.message（`_result_message`），
                    # 只放 banned 的话那一行会显示成 JSON
                    "data": {
                        "banned": bool(result.banned),
                        "message": result.error_message or result.verify_message or "刷新失败",
                    },
                    "account_extra_patch": {"chatgpt_token_refresh": stamp},
                }

            # 如实区分「真的换发了」与「服务端认为无需换发」：AT 未到期时
            # session 端点原样返回旧 AT（实测 2026-10-06），此时显示「已换新」
            # 是误导 —— 用户看到的 AT 根本没变。
            if result.refreshed:
                message = f"Token 已刷新（{result.strategy or '未知方式'}，AT 已换发）"
            else:
                message = (
                    f"AT 仍有效、无需换发（{result.strategy or '未知方式'}）"
                    "—— 服务端在令牌未到期时返回原值"
                )

            # 只写非空字段：登录链可能只换到 AT，用空串覆盖库里的 session_token
            # 等于把号弄坏（同 build_extra_patch 的理由）。
            patch: dict = {"chatgpt_token_refresh": stamp}
            for key, value in (
                ("access_token", result.access_token),
                ("refresh_token", result.refresh_token),
                ("session_token", result.session_token),
                ("id_token", result.id_token),
                ("cookies", result.cookie_header),
            ):
                text = str(value or "").strip()
                if text:
                    patch[key] = text
            if patch.get("refresh_token"):
                patch["chatgpt_has_refresh_token_solution"] = True

            return {
                "ok": True,
                "data": {
                    "message": message,
                    "access_token": result.access_token,
                    "refresh_token": result.refresh_token,
                    "verified": bool(result.verified),
                    "refreshed": bool(result.refreshed),
                    "strategy": str(result.strategy or ""),
                },
                "account_extra_patch": patch,
            }

        if action_id == "backfill_refresh_token":
            from services.chatgpt_rt_backfill import backfill_account_data, build_extra_patch

            result = backfill_account_data(
                email=account.email,
                password=account.password,
                extra=extra,
                token=account.token,
                config=(self.config.extra or {}) if self.config else {},
                proxy=proxy,
                allow_login=str(params.get("allow_login", "1")).lower() not in ("0", "false", "no"),
                log_fn=getattr(self, "_log_fn", None),
            )
            return {
                "ok": result.success,
                # 登录链发掘的封禁结论要带出来：状态接线（api/actions.py）与
                # 批量界面都读 data.banned。
                "data": {"message": result.summary(), "strategy": result.strategy, "banned": bool(result.banned)},
                "error": "" if result.success else result.summary(),
                "account_extra_patch": build_extra_patch(result),
            }

        if action_id == "bind_2fa":
            from services.chatgpt_two_factor import (
                bind_account_two_factor,
                build_extra_patch,
                make_secret_persister,
            )

            result = bind_account_two_factor(
                email=account.email,
                password=account.password,
                extra=extra,
                token=account.token,
                config=(self.config.extra or {}) if self.config else {},
                proxy=proxy,
                allow_login=str(params.get("allow_login", "1")).lower() not in ("0", "false", "no"),
                log_fn=getattr(self, "_log_fn", None),
                # 密钥一到手就落库：拿到与「动作返回后落库」之间还隔着 activate
                # 和复核两次网络往返，中间进程退出/请求断开都会丢密钥（丢了锁死）
                persist_secret=make_secret_persister(
                    email=account.email, log=getattr(self, "_log_fn", None)
                ),
            )
            ok = result.ok or result.already_bound
            return {
                "ok": ok,
                # 密钥只下发这一次，返回给前端让用户当场导入验证器。
                # banned 是登录链发掘的封禁结论（用户要求：禁用靠登录流程发掘），
                # 状态接线（api/actions.py）与批量界面都读它。
                "data": {
                    "message": result.summary(),
                    "totp_secret": result.secret,
                    "banned": bool(result.banned),
                },
                "error": "" if ok else result.summary(),
                "account_extra_patch": build_extra_patch(result),
            }

        if action_id == "upload_cpa":
            from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa
            from services.chatgpt_sync import upload_proxy_for

            token_data = generate_token_json(a)
            ok, msg = upload_to_cpa(
                token_data,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
                proxy=upload_proxy_for("cpa", a.extra or {}),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_sub2api":
            from platforms.chatgpt.sub2api_upload import upload_to_sub2api

            ok, msg = upload_to_sub2api(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
            )
            return {"ok": ok, "data": msg}

        if action_id == "upload_chatgpt2api":
            from platforms.chatgpt.chatgpt2api_upload import upload_to_chatgpt2api
            from services.chatgpt_sync import upload_proxy_for

            ok, msg = upload_to_chatgpt2api(
                a,
                api_url=params.get("api_url"),
                api_key=params.get("api_key"),
                proxy=upload_proxy_for("chatgpt2api", a.extra or {}),
            )
            return {"ok": ok, "data": msg}

        if action_id in ("sync_sub2api_status", "sync_chatgpt2api_status"):
            # 单账号版（批量端点有专用分支，一次拉列表写回所有账号）。
            # 读面板列表里的状态字段写回本地（不做探活 —— 面板自己跑 token 刷新）。
            from services.panel_status_sync import sync_panel_status_batch

            panel_key = "sub2api" if action_id == "sync_sub2api_status" else "chatgpt2api"
            account_id = getattr(a, "id", None) or int((a.extra or {}).get("account_id") or 0) or 0
            probe = type("A", (), {
                "id": account_id, "email": a.email, "platform": "chatgpt",
            })()
            updates = sync_panel_status_batch(panel_key, [probe])
            update = updates.get(int(account_id), {})
            ok = bool(update.get("ok"))
            message = str(update.get("message") or "同步完成")
            return {
                "ok": ok,
                "data": {"message": f"{panel_key} 状态同步完成：{message}"},
                "error": "" if ok else message,
                "account_extra_patch": update.get("patch") or {},
            }

        raise NotImplementedError(f"未知操作: {action_id}")
