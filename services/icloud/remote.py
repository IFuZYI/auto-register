"""远程 iCloud 仓库业务：接入远端服务、同步别名、生成/停用/删除、收件。

与本地链路（`accounts.py` / `aliases.py` / `messages.py`）的分工：
- 本地行（`source=local`）：凭据在本机，直连 Apple；
- 远程行（`source=remote`）：凭据在独立的 icloud-hme 服务里，本机只存
  「服务地址 + 管理员密码密文 + 远端账号 id」，经 `remote_client` 调它的 API。

**本地优先去重**（用户要求 2026-10-09）：同步远端别名时，若地址在本地已有
行（`source=local`），保留本地行、不建远程行 —— 同一个地址不允许两行，
本地那行是「自己生成/管理」的那个，远程镜像不该把它顶掉。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import select

from core.db import (
    ICloudAccountModel,
    ICloudAliasModel,
    platform_session,
)
from platforms.icloud import (
    ALIAS_STATUS_ACTIVE,
    ALIAS_STATUS_DISABLED,
    POOL_STATUS_UNPOOLED,
    ICloudError,
    MailAddress,
    MailMessage,
)
from platforms.icloud.remote_client import RemoteHMEClient
from services.icloud._locks import _utcnow
from services.icloud._facade import _facade
from services.icloud.accounts import get_account
from services.icloud.aliases import _alias_to_dict, _record_sync_error, _upsert_alias

SOURCE_LOCAL = "local"
SOURCE_REMOTE = "remote"


# ------------------------------------------------------------------ 客户端


def remote_password(row: ICloudAccountModel) -> str:
    """解密远程账号的管理员密码（调用方永远拿不到密文本身）。"""
    cipher = str(getattr(row, "remote_password_cipher", "") or "")
    if not cipher:
        raise ICloudError("invalid_config", f"远程账号 {row.email} 未保存管理员密码")
    try:
        payload = _facade.secret_box.decrypt(cipher)
    except Exception as exc:  # noqa: BLE001 - 解密失败是配置问题，给出可行动的提示
        raise ICloudError(
            "invalid_config",
            f"远程账号 {row.email} 的管理员密码无法解密（加密密钥已变更），请重新接入该账号",
        ) from exc
    return payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)


def remote_client(
    row: ICloudAccountModel, *, proxy: str | None = None, timeout: int = 30
) -> RemoteHMEClient:
    """按账号行构建远程客户端（地址 + 解密后的密码 + 可选代理）。"""
    if str(getattr(row, "source", "") or "") != SOURCE_REMOTE:
        raise ICloudError("invalid_config", f"账号 {row.email} 不是远程账号")
    return RemoteHMEClient(
        str(getattr(row, "base_url", "") or ""),
        remote_password(row),
        proxy=proxy or str(getattr(row, "proxy", "") or ""),
        timeout=timeout,
    )


# ------------------------------------------------------------------ 接入


def discover_remote_accounts(*, base_url: str, admin_password: str, proxy: str = "") -> list[dict[str, Any]]:
    """只读探针：登录远端并列出它的账号（不落库）。

    给「添加远程服务」弹窗用 —— 先让用户看到远端有哪些账号、再选一个接入，
    比「先接入再发现选错了」少一轮删除。
    """
    url = str(base_url or "").strip().rstrip("/")
    password = str(admin_password or "")
    if not url:
        raise ICloudError("invalid_config", "请填写远程服务地址")
    if not password:
        raise ICloudError("invalid_config", "请填写远程服务管理员密码")
    client = RemoteHMEClient(url, password, proxy=str(proxy or "").strip())
    accounts = client.list_accounts()
    return [
        {
            "id": str(item.get("id") or ""),
            "name": str(item.get("name") or ""),
            "icloud_email": str(item.get("icloud_email") or item.get("real_email") or ""),
            "status": str(item.get("status") or ""),
            "alias_total": int(item.get("alias_total") or 0),
            "has_cookies": bool(item.get("has_cookies")),
            "has_app_password": bool(item.get("has_app_password")),
        }
        for item in accounts
    ]


def add_remote_account(
    *,
    base_url: str,
    admin_password: str,
    remote_account_id: str,
    name: str = "",
    proxy: str = "",
) -> dict[str, Any]:
    """把远程服务里的一个账号接入本地（一行一个远端账号）。

    流程：登录远端验证凭据 → 确认远端账号存在（拿它的 icloud_email）→ 落库。
    重复接入（同 base_url + remote_account_id）直接拒绝 —— 同一远端账号
    接两次会产生两行、两个池子，取号时会互相顶掉。
    """
    url = str(base_url or "").strip().rstrip("/")
    password = str(admin_password or "")
    target = str(remote_account_id or "").strip()
    if not url:
        raise ICloudError("invalid_config", "请填写远程服务地址")
    if not password:
        raise ICloudError("invalid_config", "请填写远程服务管理员密码")
    if not target:
        raise ICloudError("invalid_config", "请选择要接入的远端账号")

    with platform_session("icloud") as session:
        existing = session.exec(
            select(ICloudAccountModel)
            .where(ICloudAccountModel.source == SOURCE_REMOTE)
            .where(ICloudAccountModel.base_url == url)
            .where(ICloudAccountModel.remote_account_id == target)
        ).first()
        if existing is not None:
            raise ICloudError(
                "invalid_config",
                f"该远端账号已接入（{existing.email}），同一服务里的同一个账号只能接入一次",
            )

    client = RemoteHMEClient(url, password, proxy=str(proxy or "").strip())
    summary = client.account_summary(target)  # 登录 + 校验账号存在；失败抛错

    email = str(
        summary.get("icloud_email") or summary.get("real_email") or ""
    ).strip().lower()
    if not email:
        raise ICloudError("invalid_config", "远端账号没有可用的邮箱地址，无法接入")

    with platform_session("icloud") as session:
        row = ICloudAccountModel(
            email=email,
            display_name=str(name or "").strip() or str(summary.get("name") or ""),
            region="global",
            status="active",
            enabled=True,
            source=SOURCE_REMOTE,
            base_url=url,
            remote_account_id=target,
            proxy=str(proxy or "").strip(),
        )
        row.remote_password_cipher = _facade.secret_box.encrypt(password.encode("utf-8"))
        session.add(row)
        session.commit()
        session.refresh(row)
        return _account_public_dict(row)


def _account_public_dict(row: ICloudAccountModel) -> dict[str, Any]:
    """远程账号的脱敏 DTO（绝不回密码/密文）。"""
    return {
        "id": row.id,
        "email": row.email,
        "display_name": row.display_name,
        "region": row.region,
        "status": row.status,
        "enabled": row.enabled,
        "source": SOURCE_REMOTE,
        "base_url": row.base_url,
        "remote_account_id": row.remote_account_id,
        "proxy": bool(row.proxy),
        "alias_count": 0,
        "sync_error": row.sync_error,
        "last_sync_at": row.last_sync_at.isoformat() if row.last_sync_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "credential_state": {"remote": True, "has_remote_password": bool(row.remote_password_cipher)},
        "quota": {"limit": 0, "used": 0, "remaining": 0, "reset_at": None, "managed_by": "remote"},
    }


def list_remote_accounts() -> list[dict[str, Any]]:
    with platform_session("icloud") as session:
        rows = session.exec(
            select(ICloudAccountModel)
            .where(ICloudAccountModel.source == SOURCE_REMOTE)
            .order_by(ICloudAccountModel.id)
        ).all()
    return [_account_public_dict(row) for row in rows]


def resolve_remote_account(account_id: str = "") -> ICloudAccountModel:
    """按本地 id / 远端账号 id 定位远程账号；留空时取第一个启用的。"""
    raw = str(account_id or "").strip()
    with platform_session("icloud") as session:
        if raw:
            row: Optional[ICloudAccountModel] = None
            if raw.isdigit():
                row = session.get(ICloudAccountModel, int(raw))
                if row is not None and row.source != SOURCE_REMOTE:
                    row = None
            if row is None:
                row = session.exec(
                    select(ICloudAccountModel)
                    .where(ICloudAccountModel.source == SOURCE_REMOTE)
                    .where(ICloudAccountModel.remote_account_id == raw)
                ).first()
            if row is None:
                raise ICloudError("account_not_found", f"远程账号不存在: {raw}")
            if not row.enabled:
                raise ICloudError("account_disabled", f"远程账号已停用: {row.email}")
            return row

        row = session.exec(
            select(ICloudAccountModel)
            .where(ICloudAccountModel.source == SOURCE_REMOTE)
            .where(ICloudAccountModel.enabled == True)  # noqa: E712 - SQLModel 需要值比较
            .order_by(ICloudAccountModel.id)
        ).first()
    if row is None:
        raise ICloudError(
            "account_not_found", "还没有可用的远程 iCloud 账号，请先在「邮箱服务」里接入"
        )
    return row


# ------------------------------------------------------------------ 同步


def _remote_status(item: dict[str, Any]) -> str:
    return ALIAS_STATUS_ACTIVE if item.get("active", True) else ALIAS_STATUS_DISABLED


def _parse_remote_datetime(value: Any) -> Optional[datetime]:
    """解析远端时间串，**naive 值按 UTC 归一**。

    远端（icloud-hme）的时间字段有两种形态，实测都出现过：

    1. ISO 串（spec 里带 `Z`，但真实上游透传 Apple 原始串时可能不带时区）；
    2. **epoch 毫秒/秒的纯数字字符串** —— 生产账号 227 条里有 3 条是
       `"1791090760837"` 这种。`fromisoformat` 会把纯数字串按「日期+时间」
       硬解成 1791 年（既不报错也不抛异常），写库后时间全错。

    纯数字先按时间戳解析；其余按 ISO 解析。naive 结果一律按 UTC 归一 ——
    原样写进 aware 时间列会让 SQLModel 抛
    `ValueError: Datetime values must have timezone information`，整个同步
    接口 500（实测：`POST /api/icloud/accounts/{id}/sync`）。
    """
    text = str(value or "").strip()
    if not text:
        return None

    # 纯数字（可带小数）＝ epoch 时间戳。按量级分档而不是单一阈值：
    # 单一阈值 `> 1e12` 在**恰好 1e12** 处有缝隙 —— `"1000000000000"`
    # （1e12 毫秒 = 2001-09-09）会掉进「按秒」分支，fromtimestamp 因年份越界
    # 抛 ValueError 被吞成 None，created_at 静默回落到「同步时刻」（实测）。
    # 分档口径对齐 icloud-hme 前端：秒 / 毫秒 / 微秒。
    if text.replace(".", "", 1).isdigit():
        try:
            number = float(text)
        except ValueError:
            return None
        if number < 0:
            return None
        if number >= 1e14:  # 微秒
            number /= 1_000_000
        elif number >= 1e11:  # 毫秒
            number /= 1_000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def sync_remote_account(account_id: int, *, proxy: str | None = None) -> dict[str, Any]:
    """从远端拉取全部别名，按地址去重合并到本地（本地优先）。

    与本地 `sync_aliases` 的区别在去重规则：本地同步的 merge 目标是「本地行」，
    而远程同步遇到**本地行**时跳过（本地优先）—— 见模块 docstring。
    """
    row = get_account(account_id)
    if str(getattr(row, "source", "") or "") != SOURCE_REMOTE:
        raise ICloudError("invalid_config", "只有远程账号支持从远端同步")
    client = remote_client(row, proxy=proxy)
    try:
        aliases = client.list_aliases(row.remote_account_id)
    except ICloudError as exc:
        _record_sync_error(account_id, str(exc))
        raise

    created = updated = skipped_local = 0
    for item in aliases:
        address = str(item.get("email") or "").strip().lower()
        if not address:
            continue
        provider_id = str(item.get("anonymousId") or "").strip()
        label = str(item.get("label") or "").strip()
        status = _remote_status(item)
        created_at = _parse_remote_datetime(item.get("createdAt"))

        with platform_session("icloud") as session:
            existing = session.exec(
                select(ICloudAliasModel).where(ICloudAliasModel.address == address)
            ).first()
            if existing is None:
                fresh = ICloudAliasModel(
                    account_id=int(account_id),
                    address=address,
                    label=label,
                    status=status,
                    provider_id=provider_id,
                    source=SOURCE_REMOTE,
                )
                # 与本地语义一致：同步下来的别名默认「未入池」，
                # 必须在池页面勾选「导入邮箱池」才可领。
                fresh.pool_status = POOL_STATUS_UNPOOLED
                if created_at is not None:
                    fresh.created_at = created_at
                session.add(fresh)
                session.commit()
                created += 1
                continue
            if str(getattr(existing, "source", "") or "") == SOURCE_LOCAL:
                # 本地优先：同地址本地已有，远程镜像不建行、不改本地行。
                skipped_local += 1
                continue
            if existing.account_id == int(account_id):
                # 本账号的远程行：更新镜像字段（不动池记账 —— 用户可能已标 used/in_use）。
                existing.label = label or existing.label
                existing.status = status
                if provider_id:
                    existing.provider_id = provider_id
                existing.updated_at = _utcnow()
                session.add(existing)
                session.commit()
                updated += 1
            # 其它远程账号持有的同地址：保留现有行（地址全局唯一，不重复建）。

    _record_sync_error(account_id, "")
    return {
        "account_id": account_id,
        "fetched": len(aliases),
        "created": created,
        "updated": updated,
        "skipped_local": skipped_local,
        "synced_at": _utcnow().isoformat(),
    }


# ------------------------------------------------------------------ 生成


def generate_remote_alias(
    account_id: int, *, label: str = "", note: str = "", proxy: str | None = None
) -> dict[str, Any]:
    """在远端创建一个别名，并把镜像落进本地库（`source=remote`）。"""
    row = get_account(account_id)
    client = remote_client(row, proxy=proxy)
    created = client.create_alias(row.remote_account_id, label)
    address = str(created.get("email") or "").strip().lower()
    if not address:
        raise ICloudError("upstream_error", "远程服务未返回别名地址")

    # /api/create 的响应不含 anonymousId —— 从别名列表解析（删除/停用要用）。
    provider_id = ""
    try:
        for item in client.list_aliases(row.remote_account_id):
            if str(item.get("email") or "").strip().lower() == address:
                provider_id = str(item.get("anonymousId") or "").strip()
                break
    except ICloudError:
        # 列表拉不到不阻断创建结果；provider_id 留空，下次同步会补上。
        provider_id = ""

    with platform_session("icloud") as session:
        existing = session.exec(
            select(ICloudAliasModel).where(ICloudAliasModel.address == address)
        ).first()
        if existing is None:
            fresh = ICloudAliasModel(
                account_id=int(account_id),
                address=address,
                label=str(created.get("label") or label),
                status=ALIAS_STATUS_ACTIVE,
                provider_id=provider_id,
                source=SOURCE_REMOTE,
            )
            fresh.pool_status = POOL_STATUS_UNPOOLED
            session.add(fresh)
            session.commit()
            session.refresh(fresh)
            return _alias_to_dict(fresh, row.email)
        # 地址已存在（本地优先）：只补 provider_id，不动来源与标签。
        if provider_id and not str(existing.provider_id or "").strip():
            existing.provider_id = provider_id
            existing.updated_at = _utcnow()
            session.add(existing)
            session.commit()
            session.refresh(existing)
        return _alias_to_dict(existing, row.email)


# ------------------------------------------------------------------ 收件


def _parse_mail_address(value: str) -> MailAddress:
    """解析 "名称 <a@b.com>" / "a@b.com" 形态。"""
    text = str(value or "").strip()
    if "<" in text and text.endswith(">"):
        name, _, rest = text.partition("<")
        return MailAddress(email=rest[:-1].strip(), name=name.strip().strip('"'))
    return MailAddress(email=text)


def _to_mail_message(item: dict[str, Any]) -> MailMessage:
    """远端收件项 → 本项目 MailMessage（字段名与本地链路一致）。"""
    received = _parse_remote_datetime(item.get("date")) or _utcnow()
    to_text = str(item.get("to") or "").strip()
    return MailMessage(
        provider_message_id=str(item.get("id") or ""),
        mailbox=str(item.get("folder") or "INBOX"),
        subject=str(item.get("subject") or ""),
        snippet=str(item.get("preview") or ""),
        sender=_parse_mail_address(str(item.get("from") or "")),
        to=[_parse_mail_address(part) for part in to_text.split(",") if part.strip()],
        received_at=received,
        alias_address=to_text,
    )


def fetch_remote_messages(
    row: ICloudAccountModel,
    *,
    limit: int = 50,
    recipient: str = "",
    proxy: str | None = None,
) -> tuple[list[MailMessage], str, str]:
    """读远端账号收件箱，返回 `(messages, method, warning)`。"""
    client = remote_client(row, proxy=proxy)
    data = client.fetch_inbox(
        row.remote_account_id,
        alias=recipient,
        limit=limit,
        days=3650,  # 远端默认 7 天窗口会漏掉旧验证码；页面按时间筛，这里放宽
    )
    messages = [_to_mail_message(item) for item in (data.get("messages") or [])]
    return messages, "remote", ""


def check_remote_account(account_id: int, *, proxy: str | None = None) -> dict[str, Any]:
    """让远端服务对账号做一次低风险探活，并把结果映射回本地 DTO。"""
    row = get_account(account_id)
    client = remote_client(row, proxy=proxy)
    summary = client.check_account(row.remote_account_id)
    status = str(summary.get("status") or "").strip()
    with platform_session("icloud") as session:
        fresh = session.get(ICloudAccountModel, int(account_id))
        if fresh is not None:
            fresh.status = status or "active"
            fresh.sync_error = ""
            fresh.last_sync_at = _utcnow()
            fresh.updated_at = _utcnow()
            session.add(fresh)
            session.commit()
            session.refresh(fresh)
            return _account_public_dict(fresh)
    raise ICloudError("account_not_found", "远程账号不存在")


__all__ = [
    "SOURCE_LOCAL",
    "SOURCE_REMOTE",
    "add_remote_account",
    "check_remote_account",
    "discover_remote_accounts",
    "fetch_remote_messages",
    "generate_remote_alias",
    "list_remote_accounts",
    "remote_client",
    "remote_password",
    "resolve_remote_account",
    "sync_remote_account",
]
