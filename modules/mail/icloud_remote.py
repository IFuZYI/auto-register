"""iCloud 隐私邮箱（远程）渠道：主号在远程仓库，别名经远程服务生成。

与本地渠道（`modules/mail/icloud_local.py`）的关系
--------------------------------------------------
两者是**同一件事的两种部署形态**：从 iCloud 主号开隐私邮箱、按别名收验证码。
区别只在凭据与执行位置：

- 本地：主号凭据在本机 `data/platforms/icloud.db`，本机直连 Apple；
- 远程：主号凭据在独立的 icloud-hme 服务里，本机只存服务地址 + 管理员密码
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

from typing import Optional

from core.mailboxes.base import BaseMailbox, MailboxAccount
from core.mailboxes.registry import register_mailbox_provider


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
        if platform_name:
            self._platform_name = str(platform_name).strip().lower()

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
            return service.resolve_remote_account("")

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

        self._log(
            f"[iCloud 远程] 账号 {row.email} 取号: {address}"
            f"（池状态 {alias.get('pool_status', '')}）"
        )
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

    def set_account_status(self, account: MailboxAccount, status: str) -> None:
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
                service.release_alias_claim(alias_id_int)
            elif normalized == "available":
                service.set_alias_pool_status(alias_id_int, "available")
        except Exception as exc:  # noqa: BLE001 - 记账失败不该影响注册结果
            self._log(f"[iCloud 远程] 号池记账失败（忽略）: {exc}")

    def _messages(self, account: MailboxAccount, limit: int = 50) -> list:
        account_id, address = self._resolve(account)
        return self._service().fetch_account_messages(
            account_id, limit=limit, recipient=address
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        """取当前收件箱里发给该别名的邮件标识集合。"""
        try:
            messages = self._messages(account, limit=50)
        except Exception as exc:  # noqa: BLE001 - 首轮探测失败不该打断注册
            self._log(f"[iCloud 远程] 读取初始邮件失败（忽略）: {exc}")
            return set()

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

        def poll_once() -> Optional[str]:
            messages = self._messages(account, limit=50)
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

        # 远程收件每次都要打服务端（它那边再打上游），轮询间隔给大一点。
        return self._run_polling_wait(
            timeout=timeout, poll_interval=5, poll_once=poll_once
        )


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
