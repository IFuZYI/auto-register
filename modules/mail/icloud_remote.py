"""iCloud 隐私邮箱（远程）渠道：主号在远程仓库，别名经远程服务生成。

与本地渠道（`modules/mail/icloud_local.py`）的关系
--------------------------------------------------
两者是**同一件事的两种部署形态**：从 iCloud 主号开隐私邮箱、按别名收验证码。
区别只在凭据与执行位置：

- 本地：主号凭据在本机 `data/platforms/icloud.db`，本机直连 Apple；
- 远程：主号凭据在独立的 icloud-hme 服务里，本机只存服务地址 + API 秘钥
  + 远端账号 id，生成/停用/删除/收件全部经它的 HTTP API。

取号语义与本地完全一致（`claim_alias`）：优先复用池里可领的别名，
池空时报错（`invalid_config`）—— 本注册机不在远端新建隐私邮箱，远端仓库只是
取号来源（用户要求 2026-10-09）。该轮尝试失败后由任务按重试轮数用同一渠道
重开；不会自动切渠道。

依赖方向
--------
`modules → services` 必须延迟导入（见 docs/EXTENDING.md）：这里在方法体内
import `services.icloud_service`，避免模块导入期拉起 requests 栈。
"""

from __future__ import annotations

import time
from typing import Optional

from core.mailboxes.base import BaseMailbox, MailboxAccount
from core.mailboxes.registry import register_mailbox_provider
from platforms.icloud.errors import ICloudError, is_recoverable
from services.icloud.upstream_gate import is_upstream_busy

#: 「使用中」租约的续约最小间隔（秒）—— 与本地渠道同一口径。
LEASE_RENEW_INTERVAL_SECONDS = 60

#: 等验证码时一次读多少封。
#:
#: **不能太小。** 上游 `forEachByRecipientRange` 用 `need = offset + limit`
#: 截候选（每个文件夹只取末尾 need 封再合并排序），limit 小于「该别名在
#: 两个文件夹里的信数 + 跨文件夹排序需要」时，信会被截掉。2026-11-14 实测
#: 同一别名同一时刻：`limit=5`→0 封、`limit=10`→0 封、`limit=15/20/25/30/50`
#: →2 封（稳定复现，与 days 无关）。所以 5 会**丢信**，这是比慢更严重的错。
#:
#: 也不能太大：上游信封预算是 `max(15s, limit × 0.3s)`，`limit=50` 时预算
#: 正好顶到 15s 悬崖，实测冷读中位 14.4s、峰值 34.5s，超过就降级 Web API
#: （按收件人过滤不可靠，读成 0 封）。而实测 `limit=20` 冷读 3.4s、
#: `limit=50` 3.8s —— 耗时主要由 SEARCH 与窗口决定，不是 limit。
#:
#: 20 = 实测能稳定读到信的最小档位，同时离 15s 悬崖还有余量。
OTP_READ_LIMIT = 20


class ICloudRemoteMailbox(BaseMailbox):
    """把远程 icloud-hme 服务里的主号当作注册用邮箱来源。

    用法::

        mb = ICloudRemoteMailbox(account_id="3")
        acct = mb.get_email()          # 从远程仓库领一个隐私邮箱
        code = mb.wait_for_code(acct)
    """

    def __init__(
        self,
        proxy: str = None,
        account_id: str = "",
        label: str = "",
        note: str = "",
        platform_name: str = "",
    ):
        self._proxy = (proxy or "").strip() or None
        # 指定用哪个远程账号（本地账号 id、远端账号 id 或主号邮箱）；留空取第一个可用的。
        self._account_id = str(account_id or "").strip()
        self._label = str(label or "").strip()
        self._note = str(note or "").strip()
        # **总是设置**（空串也要有）：下面 `_resolve_account_row` 读的是
        # `self._platform_name`，属性不存在时 `getattr(..., "")` 会静默拿到
        # 空串 —— 而空 platform 会让 `claim_alias` 跳过所有 `used` 状态的
        # 别名（保守策略），于是**一个现成号都领不到**、每次回落到新生成。
        self._platform_name = str(platform_name or "").strip().lower()
        #: 首读收件箱失败过 = 基线不可信，等码时改用时间窗兜底。
        self._baseline_degraded = False

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _service():
        """延迟导入本地 iCloud 服务（依赖 services 层）。"""
        from services import icloud_service

        return icloud_service

    def _resolve_account_row(self):
        """定位远程账号：显式指定优先，否则取第一个可用的远程账号。"""
        service = self._service()
        if not self._account_id:
            # 带上平台：挑主号时要排除「别名已被本平台用尽」的那些，
            # 否则会挑到领不到号的主号（代码审查 2026-10-27 I3）。
            # 属性名带下划线。写成 `platform_name` 会永远取到空串 ——
            # 于是挑主号时不排除「别名已被本平台用尽」的号，取号时也不按
            # 平台过滤 `used` 别名，**领不到现成号**（实测踩过）。
            return service.resolve_remote_account(
                "", self._platform_name or ""
            )

        raw = self._account_id
        if raw.isdigit() or raw.startswith("acc_"):
            row = service.resolve_remote_account(raw)
        else:
            # 允许用主号邮箱指定：在远程账号里按邮箱找
            row = None
            for item in service.list_remote_accounts():
                if str(item.get("email") or "").strip().lower() == raw.strip().lower():
                    row = service.resolve_remote_account(str(item["id"]))
                    break
            if row is None:
                raise RuntimeError(f"远程 iCloud 账号不存在: {raw}")
        if not row.enabled:
            raise RuntimeError(f"远程 iCloud 账号已停用: {row.email}")
        return row

    # ---------------------------------------------------------------- 接口

    def get_email(self) -> MailboxAccount:
        """从远程仓库领一个隐私邮箱地址。

        走 `claim_alias` 而不是直接让远端新建：先复用池里没用过的别名，
        没有再向远端创建 —— 与本地渠道同语义（远程创建额度同样稀缺：
        同账号 20 分钟一次、每天 50 个）。
        """
        service = self._service()
        row = self._resolve_account_row()

        # 领号前先放回「领了但一直没落定」的号（任务中途崩掉会留下这种）。
        try:
            service.release_stale_claims()
        except Exception as exc:  # noqa: BLE001 - 兜底失败不该挡住取号
            self._log(f"[iCloud 远程] 号池兜底清理失败（忽略）: {exc}")

        alias = service.claim_alias(
            int(row.id),
            label=self._label,
            note=self._note,
            proxy=self._proxy,
            platform=self._platform_name,
        )

        address = str(alias.get("address") or "").strip()
        if not address:
            raise RuntimeError("远程 iCloud 仓库未返回隐私邮箱地址")

        self._log(f"[iCloud 远程] 账号 {row.email} 取号: {address}（已标记为「使用中」）")
        return MailboxAccount(
            email=address,
            # account_id 存「本地账号 id : 别名地址」：收件要按账号取、按别名过滤，
            # 与本地渠道保持同一格式，上层换渠道时不用区分。
            account_id=f"{row.id}:{address}",
            extra={
                "icloud_account_id": row.id,
                "icloud_alias_id": alias.get("id"),
                "icloud_alias_email": address,
                "icloud_alias_label": alias.get("label", ""),
                "icloud_pool_status": alias.get("pool_status", ""),
                "icloud_source": "remote",
            },
        )

    def _resolve(self, account: MailboxAccount) -> tuple[int, str]:
        """解析出本地账号 id 与别名地址。"""
        extra = account.extra if isinstance(account.extra, dict) else {}
        account_id = extra.get("icloud_account_id")
        address = str(account.email or "").strip()

        if not account_id and account.account_id:
            head, _, _ = str(account.account_id).partition(":")
            account_id = head.strip()

        try:
            account_id_int = int(account_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("远程 iCloud 邮箱账号缺少主号 id") from exc

        if not address:
            raise RuntimeError("远程 iCloud 邮箱账号缺少地址信息")
        return account_id_int, address

    def set_account_status(
        self, account: MailboxAccount, status: str, *, error: BaseException | None = None
    ) -> None:
        """把注册结果记回号池（与本地渠道同一套生命周期钩子）。"""
        alias_id = (account.extra or {}).get("icloud_alias_id")
        try:
            alias_id_int = int(alias_id)
        except (TypeError, ValueError):
            return

        normalized = str(status or "").strip().lower()
        service = self._service()
        try:
            if normalized == "used":
                service.mark_alias_used(alias_id_int, platform=self._platform_name)
            elif normalized == "failed":
                # 把原因传下去：上游故障不该记成「这个地址失败」
                # （见 `release_alias_claim` 的 error 参数）。
                service.release_alias_claim(alias_id_int, error=error)
            elif normalized == "available":
                service.set_alias_pool_status(alias_id_int, "available")
        except Exception as exc:  # noqa: BLE001 - 记账失败不该影响注册结果
            self._log(f"[iCloud 远程] 号池记账失败（忽略）: {exc}")

    def _messages(self, account: MailboxAccount, limit: int = OTP_READ_LIMIT) -> list:
        account_id, address = self._resolve(account)
        return self._service().fetch_account_messages(
            account_id, limit=limit, recipient=address
        )

    def _messages_detailed(self, account: MailboxAccount, limit: int = OTP_READ_LIMIT):
        """读一次收件箱，带上「是不是降级了」。

        返回 `(messages, degraded, reason)`。判据只有 `warning`：降级信息由
        `services.icloud.remote.fetch_remote_messages` 归一后放进 warning
        （上游 `method != imap` 时它才非空）。**不要**直接拿上游的 `method`
        判定 —— 老版本服务端/测试双端不返回该字段，那会把正常读取误判成降级。
        """
        account_id, address = self._resolve(account)
        detailed = getattr(self._service(), "fetch_account_messages_detailed", None)
        if not callable(detailed):
            return self._messages(account, limit=limit), False, ""
        messages, _method, warning = detailed(
            account_id, limit=limit, recipient=address
        )
        reason = str(warning or "").strip()
        return messages, bool(reason), reason

    def get_current_ids(self, account: MailboxAccount) -> set:
        """取当前收件箱里发给该别名的邮件标识集合。

        读不到时**必须**把 `_baseline_degraded` 置起来：空集会被
        `wait_for_code` 解读成「收件箱本来就没信」，于是收件箱里原有的历史
        邮件会被当成本轮新到的信 —— 可能拿旧验证码去填，让失败原因变得
        完全看不懂。置标记后上层改用时间窗兜底。
        """
        try:
            messages, degraded, reason = self._messages_detailed(account)
        except Exception as exc:  # noqa: BLE001 - 首轮探测失败不该打断注册
            self._baseline_degraded = True
            self._log(
                f"[iCloud 远程] 读取初始邮件失败，基线降级：本轮改按发信时间窗"
                f"判断新邮件（收件箱里已有的信不会被当成本轮的码）: {exc}"
            )
            return set()

        if degraded:
            # 降级 = 按收件人过滤不可靠，这一读**可能漏信**。不能把它当成
            # 「收件箱本来就没信」（那会让历史邮件充当本轮新信），也不能
            # 打断注册（首读失败历来不致命）。标记基线不可信，让等码改用
            # 时间窗兜底；同时记一条能看懂的日志。
            self._baseline_degraded = True
            self._log(
                f"[iCloud 远程] 上游降级，本轮收到的 0 封不能当作「没有邮件」；"
                f"基线标为不可信: {reason}"
            )
            return set()

        self._baseline_degraded = False
        ids = set()
        for message in messages:
            message_id = self._message_id(message)
            if message_id:
                ids.add(message_id)
        return ids

    @staticmethod
    def _message_id(message) -> str:
        """邮件标识（字段名以 `MailMessage.provider_message_id` 为准）。"""
        for key in ("provider_message_id", "id", "uid", "message_id"):
            value = message.get(key) if isinstance(message, dict) else getattr(message, key, None)
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _message_text(message) -> str:
        """把邮件拼成可搜索文本（字段名取自 `platforms.icloud.models.MailMessage`）。"""
        def _get(key):
            value = message.get(key) if isinstance(message, dict) else getattr(message, key, None)
            return value

        parts = []
        for key in ("subject", "snippet", "text_body", "html_body"):
            value = _get(key)
            if value:
                parts.append(str(value))

        sender = _get("sender")
        if sender is not None:
            email = getattr(sender, "email", None) or (
                sender.get("email") if isinstance(sender, dict) else None
            )
            if email:
                parts.append(str(email))

        return "\n".join(parts)

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set = None,
        code_pattern: str = None,
        **kwargs,
    ) -> str:
        seen = {str(item) for item in (before_ids or set())}
        exclude_codes = {
            str(code).strip()
            for code in (kwargs.get("exclude_codes") or set())
            if str(code or "").strip()
        }
        # 发码时间窗：补发后必须只认**这一轮**的邮件（与本地渠道同口径）。
        otp_sent_at = kwargs.get("otp_sent_at")
        try:
            cutoff = (float(otp_sent_at) - 30) if otp_sent_at else None
        except (TypeError, ValueError):
            cutoff = None

        # 租约续约：等码期间给「使用中」的租约保持新鲜（用户要求：使用中
        # 不能同时使用）。回收按 updated_at 超时判定，不续约的话长任务会被
        # 另一个取号点回收、号被重复领走 —— 两个任务抢同一个收件箱。
        lease_alias_id = self._lease_alias_id(account)
        last_renew = 0.0

        def _renew_lease() -> None:
            nonlocal last_renew
            if lease_alias_id is None:
                return
            now = time.monotonic()
            if last_renew and now - last_renew < LEASE_RENEW_INTERVAL_SECONDS:
                return
            last_renew = now
            try:
                self._service().renew_alias_claim(lease_alias_id)
            except Exception as exc:  # noqa: BLE001 - 续约失败不该影响读码
                self._log(f"[iCloud 远程] 租约续约失败（忽略）: {exc}")

        # 基线不可信（首读收件箱失败）+ 没有发码时间窗 = 无法区分历史邮件与
        # 本轮新到的信。这时宁可空等到超时，也不要拿旧码去填 —— 那会让
        # 失败原因完全看不出是服务抖动。
        baseline_untrusted = bool(getattr(self, "_baseline_degraded", False))
        if baseline_untrusted and cutoff is None:
            self._log(
                "[iCloud 远程] 收件基线不可信且没有发码时间窗，本轮无法可靠判断新邮件；"
                "保守起见不猜码（避免把历史验证码当本轮结果）"
            )
            # 直接放弃：宁可让上层看到「等码超时」，也不要返回一个来路不明的码。
            raise TimeoutError(
                "[iCloud 远程] 收件基线不可信（首读失败）且没有发码时间窗，"
                "无法判断哪些信是本轮的；未猜码，请重试本轮"
            )

        def poll_once() -> Optional[str]:
            _renew_lease()
            messages, degraded, reason = self._messages_detailed(account)
            if degraded:
                # 降级路径按收件人过滤不可靠 —— 这一读的 0 封**不能**当作
                # 「没有新邮件」。抛可恢复错误让容错循环继续等（错误预算与
                # 熔断仍会兜住），日志里也能看出是上游降级而不是没收到码。
                raise ICloudError(
                    "upstream_error",
                    f"上游降级，按收件人过滤不可靠: {reason}",
                )
            for message in messages:
                message_id = self._message_id(message)
                if not message_id or message_id in seen:
                    continue
                seen.add(message_id)

                if cutoff is not None:
                    received_at = getattr(message, "received_at", None)
                    try:
                        if received_at is not None and received_at.timestamp() < cutoff:
                            continue
                    except (AttributeError, OSError, ValueError):
                        pass

                text = self._message_text(message)
                if keyword and keyword.lower() not in text.lower():
                    continue

                code = self._safe_extract(text, code_pattern)
                if code and code in exclude_codes:
                    continue
                if code:
                    self._log(f"[iCloud 远程] 收到验证码: {code}")
                    return code
            return None

        # 远程收件每次都要打服务端（它那边再打上游）。
        # 分段退避：验证码多在前 20 秒到，后面的空转可以放慢。
        return self._run_polling_wait(
            timeout=timeout,
            poll_interval=5,
            poll_once=poll_once,
            # 上游暂时不可达不该判整轮失败（实测 2026-11-13：一次 30s 超时 =
            # 丢一个号 + 一个邮箱 + 一轮重试，两批任务约 80% 的失败是这条）。
            is_transient=is_recoverable,
            # 但也不能无限陪跑：连续 4 次读不到就放弃，把原错误交回上层，
            # 让「服务挂了」和「没收到码」在日志里能分辨。
            max_consecutive_errors=4,
            interval_schedule=((0, 5.0), (20, 8.0), (60, 15.0)),
            # 熔断打开 = 服务现在不值得试，立刻放弃而不是空等冷却。
            is_fatal_busy=is_upstream_busy,
        )

    @staticmethod
    def _lease_alias_id(account: MailboxAccount) -> Optional[int]:
        """从账号对象取出租约对应的别名 id；不是从本渠道领来的返回 None。"""
        alias_id = (account.extra or {}).get("icloud_alias_id")
        try:
            return int(alias_id)
        except (TypeError, ValueError):
            return None


@register_mailbox_provider("icloud_remote", display_name="iCloud 隐私邮箱（远程）")
def build(*, extra: dict, proxy: str = None) -> BaseMailbox:
    """构建远程 iCloud 隐私邮箱渠道（凭据在远程 icloud-hme 服务里）。"""
    return ICloudRemoteMailbox(
        proxy=proxy,
        account_id=extra.get("icloud_remote_account_id", ""),
        label=extra.get("icloud_remote_label", ""),
        note=extra.get("icloud_remote_note", ""),
    )


__all__ = ["ICloudRemoteMailbox", "build"]
