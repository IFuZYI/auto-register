"""iCloud 隐私邮箱（本地）渠道：主号在本地，别名由本项目自己生成。

**这是本项目现在唯一的 iCloud 邮箱链路。**

它曾经有一个远程版本（`modules/mail/icloud_hme.py` + `services/icloud_hme_client.py`，
凭据放在独立的 icloud-hme 服务里，本机只填服务地址与管理员密码）。那条链路
已随「只留本地 iCloud 与 Outlook」的整体删除一起移除 —— 下面提到它的地方都是
历史对照，不是还在跑的代码。

凭据存在本机 `data/platforms/icloud.db`，主号由「邮箱服务 > iCloud 隐私邮箱
（本地）」那个控制台维护。这正是它与 Outlook 本地号池的共同点：都是
「本地维护的一份号，注册时从中取」。

地址从号池里复用，池空时才新建
--------------------------------
`get_email()` 走 `claim_alias`：优先复用池里没用过的别名，池空时才向 Apple
新生成（生成消耗上游额度：同账号每小时 5 个，撞上就抛 `provider_rate_limited`，
上层据此换渠道）。

**别名默认不进池**：新生成/同步下来的别名落在 `unpooled`（未入池）状态，
`claim_alias` 不会碰它。只有用户在 iCloud 页面显式勾选「导入邮箱池」之后才转
`available`。这条是用户明确要求的 —— 主号下可能已有历史别名，自动全部入池会
让注册任务悄悄消耗掉那些用户本来留着自己用的地址。

依赖方向
--------
`modules → services` 必须延迟导入（见 docs/EXTENDING.md）：这里在方法体内
import `services.icloud_service`，避免模块导入期拉起 requests/IMAP 栈。
"""

from __future__ import annotations

from typing import Optional

from core.mailboxes.base import BaseMailbox, MailboxAccount
from core.mailboxes.registry import register_mailbox_provider


class ICloudLocalMailbox(BaseMailbox):
    """把本地 iCloud 主号生成的隐私邮箱当作注册用邮箱。

    用法::

        mb = ICloudLocalMailbox()
        acct = mb.get_email()          # 用本地主号开一个隐私邮箱
        code = mb.wait_for_code(acct)
    """

    def __init__(
        self,
        proxy: str = None,
        account_id: str = "",
        label: str = "",
        note: str = "",
    ):
        self._proxy = (proxy or "").strip() or None
        # 指定用哪个主号（邮箱地址或主号 id）；留空时取第一个可用主号。
        self._account_id = str(account_id or "").strip()
        self._label = str(label or "").strip()
        self._note = str(note or "").strip()

    # ---------------------------------------------------------------- 内部

    @staticmethod
    def _service():
        """延迟导入本地 iCloud 服务（依赖 services 层）。"""
        from services import icloud_service

        return icloud_service

    def _resolve_account_row(self):
        """定位主号：显式指定优先，否则取第一个可用主号。"""
        service = self._service()
        if not self._account_id:
            return service.resolve_account("")

        raw = self._account_id
        # 允许用主号 id（纯数字）或主号邮箱指定
        if raw.isdigit():
            row = service.get_account(int(raw))
        else:
            row = service.find_account_by_email(raw)
            if row is None:
                raise RuntimeError(f"iCloud 主号不存在: {raw}")
        if not row.enabled:
            raise RuntimeError(f"iCloud 主号已停用: {row.email}")
        return row

    # ---------------------------------------------------------------- 接口

    def get_email(self) -> MailboxAccount:
        """从号池领一个隐私邮箱地址。

        走 `claim_alias` 而不是 `generate_alias`：先复用池里没用过的别名，
        没有再向 Apple 新生成。Apple 每个主号每小时只让生成 5 个，生成额度
        是稀缺资源，而池里常有从没用过的闲号 —— 每次都新生成等于把额度当
        无限用，跑到 5 个就报限流。

        额度耗尽、主号停用这类问题由 `icloud_service` 抛 `ICloudError`，
        这里不吞——上层需要据此判断是「换个渠道」还是「整个任务该停」。
        """
        service = self._service()
        row = self._resolve_account_row()

        # 领号前先放回「领了但一直没落定」的号（任务中途崩掉会留下这种）。
        # 放在这里而不是后台定时器：注册本来就串行跑，取号点是最自然的
        # 兜底时机，且不用给服务层引入线程。
        try:
            service.release_stale_claims()
        except Exception as exc:
            # 兜底失败不该挡住取号 —— 池子里的可用号还能正常领
            self._log(f"[iCloud 本地] 号池兜底清理失败（忽略）: {exc}")

        alias = service.claim_alias(
            int(row.id),
            label=self._label,
            note=self._note,
            proxy=self._proxy,
            platform=self._platform_name,
        )

        address = str(alias.get("address") or "").strip()
        if not address:
            raise RuntimeError("iCloud 主号未返回隐私邮箱地址")

        self._log(
            f"[iCloud 本地] 主号 {row.email} 取号: {address}"
            f"（池状态 {alias.get('pool_status', '')}）"
        )
        return MailboxAccount(
            email=address,
            # account_id 存「主号 id : 别名地址」：收件要按主号取、按别名过滤，
            # 与远程渠道保持同一格式，上层换渠道时不用区分。
            account_id=f"{row.id}:{address}",
            extra={
                "icloud_account_id": row.id,
                "icloud_alias_id": alias.get("id"),
                "icloud_alias_email": address,
                "icloud_alias_label": alias.get("label", ""),
                "icloud_pool_status": alias.get("pool_status", ""),
            },
        )

    def _resolve(self, account: MailboxAccount) -> tuple[int, str]:
        """解析出主号 id 与别名地址。"""
        extra = account.extra if isinstance(account.extra, dict) else {}
        account_id = extra.get("icloud_account_id")
        address = str(account.email or "").strip()

        if not account_id and account.account_id:
            head, _, _ = str(account.account_id).partition(":")
            account_id = head.strip()

        try:
            account_id_int = int(account_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("iCloud 本地邮箱账号缺少主号 id") from exc

        if not address:
            raise RuntimeError("iCloud 本地邮箱账号缺少地址信息")
        return account_id_int, address

    def set_account_status(self, account: MailboxAccount, status: str) -> None:
        """把注册结果记回号池（复用既有生命周期钩子）。

        上层（`platforms/chatgpt/registration_engine.py`）通过
        `getattr(mailbox, "set_account_status", None)` 回调渠道，Outlook 号池
        就是在这里更新自己的记账。本地 iCloud 之前没实现它，于是
        `mark_alias_used` / `release_alias_claim` 全仓没有一个生产调用点：
        领过的别名永远停在 `in_use`，池子只出不进，最后把每个任务都逼去
        消耗 Apple 每小时 5 个的生成额度 —— 正好是 `claim_alias` 想避免的事。

        状态映射沿用 Outlook 那套（available / in_use / used / failed）：
        `used` 表示这一轮跑完了，`failed` 表示这轮废了但地址还能再用，
        所以放回 `available` 而不是标死。
        """
        alias_id = (account.extra or {}).get("icloud_alias_id")
        try:
            alias_id_int = int(alias_id)
        except (TypeError, ValueError):
            # 不是从本渠道领来的账号（人工导入/换渠道重试），没有记账对象
            return

        normalized = str(status or "").strip().lower()
        service = self._service()
        try:
            if normalized == "used":
                # 记下「被哪个平台用掉的」—— 邮箱只对用掉它的那个平台一次性，
                # 别的平台还能再领它（`used_platforms` 见 core/db/base.py）。
                service.mark_alias_used(
                    alias_id_int, platform=self._platform_name
                )
            elif normalized == "failed":
                service.release_alias_claim(alias_id_int)
            elif normalized == "available":
                service.set_alias_pool_status(alias_id_int, "available")
            # 其余状态（in_use 等）不是收尾语义，忽略
        except Exception as exc:  # noqa: BLE001 - 记账失败不该影响注册结果
            self._log(f"[iCloud 本地] 号池记账失败（忽略）: {exc}")

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
            self._log(f"[iCloud 本地] 读取初始邮件失败（忽略）: {exc}")
            return set()

        ids = set()
        for message in messages:
            message_id = self._message_id(message)
            if message_id:
                ids.add(message_id)
        return ids

    @staticmethod
    def _message_id(message) -> str:
        """邮件标识。

        `MailMessage` 的字段是 `provider_message_id`（不是 `id`），这里按
        真实字段取值，再兜底字典形态，避免以后服务层换了返回类型就静默
        拿不到 id（拿不到 = 每轮都当新邮件，验证码会重复命中）。
        """
        for key in ("provider_message_id", "id", "uid", "message_id"):
            value = message.get(key) if isinstance(message, dict) else getattr(message, key, None)
            text = str(value or "").strip()
            if text:
                return text
        return ""

    @staticmethod
    def _message_text(message) -> str:
        """把邮件拼成可搜索文本。

        字段名取自 `platforms/icloud/models.MailMessage`（`snippet` /
        `text_body` / `html_body` / `sender`），不是通用的 `preview` / `body`
        —— 按通用名取会全部落空，只剩 subject，验证码正则就找不到。
        """
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
            # MailAddress dataclass（有 email/name），也可能是 dict
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
        # 发码时间窗：补发后必须只认**这一轮**的邮件。不看这个的话，补发后
        # 立刻又把上一封读出来（`seen` 只在单次调用内有效，跨调用不记得），
        # 于是拿同一个错码再验一次 —— 实测日志里「OTP 已重发」4 秒后读到的
        # 还是同一个码，第二次直接撞上 `max_check_attempts`。
        # Apple 转发有延迟，窗口放宽 30 秒（与 `chatgpt_otp_mailbox` 同口径）。
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
                    self._log(f"[iCloud 本地] 收到验证码: {code}")
                    return code
            return None

        # 本地收件走 IMAP（未配专用密码时回退 Web API），每次都要打上游，
        # 轮询间隔给大一点。
        return self._run_polling_wait(
            timeout=timeout, poll_interval=5, poll_once=poll_once
        )


@register_mailbox_provider("icloud_local", display_name="iCloud 隐私邮箱（本地）")
def build(*, extra: dict, proxy: str = None) -> BaseMailbox:
    """构建本地 iCloud 隐私邮箱渠道（主号在本地库）。"""
    return ICloudLocalMailbox(
        proxy=proxy,
        account_id=extra.get("icloud_local_account_id", ""),
        label=extra.get("icloud_local_label", ""),
        note=extra.get("icloud_local_note", ""),
    )


__all__ = ["ICloudLocalMailbox", "build"]
