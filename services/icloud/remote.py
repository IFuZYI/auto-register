"""远程 iCloud 仓库业务：接入远端服务、同步别名、生成/停用/删除、收件。

与本地链路（`accounts.py` / `aliases.py` / `messages.py`）的分工：
- 本地行（`source=local`）：凭据在本机，直连 Apple；
- 远程行（`source=remote`）：凭据在独立的 icloud-hme 服务里，本机只存
  「服务地址 + API 秘钥密文 + 远端账号 id」，经 `remote_client` 调它的 API。

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
from services.icloud.accounts import (
    _record_to_model,
    _registered_platforms_for,
    get_account,
)
from services.icloud.aliases import _alias_to_dict, _record_sync_error, _upsert_alias

SOURCE_LOCAL = "local"
SOURCE_REMOTE = "remote"


# ------------------------------------------------------------------ 客户端


def remote_password(row: ICloudAccountModel) -> str:
    """解密远程账号的 API 秘钥（调用方永远拿不到密文本身）。

    列名 `remote_password_cipher` 是历史命名 —— 2026-11-02 服务端鉴权升级为
    API 秘钥后，这里存的是 `sk-` 开头的秘钥（旧部署可能仍是管理员密码）。
    """
    cipher = str(getattr(row, "remote_password_cipher", "") or "")
    if not cipher:
        raise ICloudError("invalid_config", f"远程账号 {row.email} 未保存 API 秘钥")
    try:
        payload = _facade.secret_box.decrypt(cipher)
    except Exception as exc:  # noqa: BLE001 - 解密失败是配置问题，给出可行动的提示
        raise ICloudError(
            "invalid_config",
            f"远程账号 {row.email} 的 API 秘钥无法解密（加密密钥已变更），请重新接入该账号",
        ) from exc
    return payload.decode("utf-8") if isinstance(payload, bytes) else str(payload)


def remote_client(
    row: ICloudAccountModel, *, proxy: str | None = None, timeout: int = 30
) -> RemoteHMEClient:
    """按账号行构建远程客户端（地址 + 解密后的 API 秘钥 + 可选代理）。"""
    if str(getattr(row, "source", "") or "") != SOURCE_REMOTE:
        raise ICloudError("invalid_config", f"账号 {row.email} 不是远程账号")
    return RemoteHMEClient(
        str(getattr(row, "base_url", "") or ""),
        remote_password(row),
        proxy=proxy or str(getattr(row, "proxy", "") or ""),
        timeout=timeout,
    )


# ------------------------------------------------------------------ 接入


def discover_remote_accounts(
    *,
    base_url: str,
    admin_password: str = "",
    proxy: str = "",
    api_key: str = "",
) -> list[dict[str, Any]]:
    """只读探针：登录远端并列出它的账号（不落库）。

    给「添加远程服务」弹窗用 —— 先让用户看到远端有哪些账号、再选一个接入，
    比「先接入再发现选错了」少一轮删除。

    `api_key` 优先；`admin_password` 仅兼容旧客户端 / 旧服务。
    """
    url = str(base_url or "").strip().rstrip("/")
    credential = str(api_key or admin_password or "").strip()
    if not url:
        raise ICloudError("invalid_config", "请填写远程服务地址")
    if not credential:
        raise ICloudError("invalid_config", "请填写远程服务 API 秘钥")
    client = RemoteHMEClient(url, credential, proxy=str(proxy or "").strip())
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
    admin_password: str = "",
    remote_account_id: str,
    name: str = "",
    proxy: str = "",
    api_key: str = "",
) -> dict[str, Any]:
    """把远程服务里的一个账号接入本地（一行一个远端账号）。

    流程：验证凭据 → 确认远端账号存在（拿它的 icloud_email）→ 落库。
    重复接入（同 base_url + remote_account_id）直接拒绝 —— 同一远端账号
    接两次会产生两行、两个池子，取号时会互相顶掉。

    `api_key` 优先；`admin_password` 仅兼容旧客户端 / 旧服务。
    """
    url = str(base_url or "").strip().rstrip("/")
    credential = str(api_key or admin_password or "").strip()
    target = str(remote_account_id or "").strip()
    if not url:
        raise ICloudError("invalid_config", "请填写远程服务地址")
    if not credential:
        raise ICloudError("invalid_config", "请填写远程服务 API 秘钥")
    if not target:
        raise ICloudError("invalid_config", "请选择要接入的远端账号")

    from core.db.icloud_account_repository import icloud_account_repository

    # 查重走仓储（同一服务里的同一个远端账号只能接入一次）
    existing = icloud_account_repository.find_remote(base_url=url, remote_account_id=target)
    if existing is not None:
        raise ICloudError(
            "invalid_config",
            f"该远端账号已接入（{existing.get('email')}），同一服务里的同一个账号只能接入一次",
        )

    client = RemoteHMEClient(url, credential, proxy=str(proxy or "").strip())
    summary = client.account_summary(target)  # 鉴权 + 校验账号存在；失败抛错

    email = str(
        summary.get("icloud_email") or summary.get("real_email") or ""
    ).strip().lower()
    if not email:
        raise ICloudError("invalid_config", "远端账号没有可用的邮箱地址，无法接入")

    icloud_account_repository.add(
        email=email,
        display_name=str(name or "").strip() or str(summary.get("name") or ""),
        region="global",
        status="active",
        enabled=True,
        source=SOURCE_REMOTE,
        base_url=url,
        remote_account_id=target,
        proxy=str(proxy or "").strip(),
        # 密文原样存（加解密留在本模块，仓储只搬运；列名保留历史命名）
        remote_password_cipher=_facade.secret_box.encrypt(credential.encode("utf-8")),
    )
    # 重新读一次拿到完整行（仓储返回的就是落库后的形状）
    created = icloud_account_repository.find_remote(
        base_url=url, remote_account_id=target
    )
    return _account_public_dict(
        _record_to_model(created) if created else ICloudAccountModel()
    )


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
    from core.db.icloud_account_repository import icloud_account_repository

    rows = [
        r for r in icloud_account_repository.list_all()
        if str(r.get("source") or "") == SOURCE_REMOTE
    ]
    rows.sort(key=lambda r: int(r.get("id") or 0))
    return [_account_public_dict(_record_to_model(r)) for r in rows]


def _candidate_is_claimable(
    candidate: dict[str, Any], platform: str, registered: dict[str, set[str]]
) -> bool:
    """这条别名对目标平台是否还能领（与 `claim_alias` 的跳过规则同源）。

    关键：`accounts` 表里的注册证据比 `used_platforms` 记账更权威 ——
    账号可能注册过但池里没来得及记账，只信 `used_platforms` 会挑中
    「池里有几百个 available、实际全都注册过」的主号，取号时全被跳过，
    最后回落到远程不支持的「生成」，用户看到的就是「池里明明有邮箱，
    却报没有邮箱」。
    """
    address = str(candidate.get("address") or "").strip().lower()
    status = str(candidate.get("pool_status") or "").strip().lower() or "available"
    if status not in {"available", "used"}:
        return False
    wanted = str(platform or "").strip().lower()
    if wanted and wanted in registered.get(address, set()):
        return False
    used_platforms = str(candidate.get("used_platforms") or "")
    if wanted and f",{wanted}," in used_platforms:
        return False
    if status == "used":
        if not wanted:
            return False
    return True


def _claimable_account_counts(
    candidates_by_account: dict[int, list[dict[str, Any]]], platform: str
) -> dict[int, int]:
    """每个主号有多少条**真能领**的别名（0 = 不能选）。"""
    addresses = [
        str(c.get("address") or "")
        for rows in candidates_by_account.values()
        for c in rows
    ]
    registered = _registered_platforms_for(addresses)
    counts: dict[int, int] = {}
    for account_id, rows in candidates_by_account.items():
        counts[int(account_id)] = sum(
            1 for c in rows if _candidate_is_claimable(c, platform, registered)
        )
    return counts


def _first_remote_account_with_aliases(
    session, platform: str = ""
) -> Optional[ICloudAccountModel]:
    """挑一个**有可用别名**的远程主号；都没有时回落到第一个启用的。

    为什么不能只取 `order_by(id).first()`（用户 2026-10-27 报的注册失败）：

    库里 789 个可用别名全在 account 4/5 名下，而按 id 取到的是 account 3
    （`fuzy202610`）—— 它名下**一个可用别名都没有**。于是取号流程回落到
    「生成新别名」，而远程渠道明确不支持生成（只允许本地主号生成）→
    注册直接失败「远程账号不支持生成隐私邮箱」。

    2026-11-02 补：只看 `used_platforms` 还不够 —— 生产库里 account 3 的
    426 个 `available` 别名在 `accounts` 表里**全部注册过 ChatGPT**，
    但池里没记账。选主号必须同时看权威注册证据，否则仍会挑到它。

    兜底仍然返回第一个启用的：全都没别名时至少让取号走到「生成」那一步，
    报错信息比「没有可用远程账号」准确。
    """
    accounts = session.exec(
        select(ICloudAccountModel)
        .where(ICloudAccountModel.source == SOURCE_REMOTE)
        .where(ICloudAccountModel.enabled == True)  # noqa: E712 - SQLModel 需要值比较
        .order_by(ICloudAccountModel.id)
    ).all()
    if not accounts:
        return None

    wanted = [int(a.id) for a in accounts if a.id is not None]
    if not wanted:
        return accounts[0]

    # 候选与 `claim_alias` 的 `find_candidates` 同口径：active + available/used。
    from core.storage.query import any_of, eq, to_sql

    clauses = [
        any_of(*(eq("account_id", a) for a in wanted)),
        eq("status", "active"),
        any_of(
            eq("pool_status", "available"),
            eq("pool_status", "used"),
            eq("pool_status", None),
            eq("pool_status", ""),
        ),
    ]
    stmt = select(ICloudAliasModel).where(to_sql(ICloudAliasModel, clauses[0]))
    stmt = stmt.where(to_sql(ICloudAliasModel, clauses[1]))
    stmt = stmt.where(to_sql(ICloudAliasModel, clauses[2]))

    candidates_by_account: dict[int, list[dict[str, Any]]] = {}
    for row in session.exec(stmt).all():
        candidates_by_account.setdefault(int(row.account_id or 0), []).append(
            {
                "address": row.address,
                "pool_status": row.pool_status,
                "used_platforms": row.used_platforms,
            }
        )

    counts = _claimable_account_counts(candidates_by_account, platform)
    # 不只挑「有一个能领」的 —— 生产库 account 3 只有 1 条干净别名，
    # account 4/5 各几百条；挑 3 会很快再次耗尽并报「没有邮箱」。
    # 选「可领数量最多」的主号，取号才不会走两步就断粮。
    if any(count > 0 for count in counts.values()):
        return max(
            accounts,
            key=lambda account: (counts.get(int(account.id or 0), 0), -int(account.id or 0)),
        )
    return accounts[0]


def resolve_remote_account(
    account_id: str = "", platform: str = ""
) -> ICloudAccountModel:
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

        # **按后端分派**：JSON 后端下没有可用的 ORM 会话（`platform_session`
        # 拿到的是空的 SQLite），所以走仓储；SQLite 后端保持原路径 ——
        # 它同时是测试的注入点（mock `platform_session` 喂内存库）。
        from core.db.icloud_account_repository import icloud_account_repository

        if icloud_account_repository._store() is not None:
            candidates = [
                r for r in icloud_account_repository.list_all()
                if str(r.get("source") or "") == SOURCE_REMOTE and bool(r.get("enabled"))
            ]
            if not candidates:
                row = None
            else:
                # 与 SQLite 分支同源：每个主号的候选别名 + 权威注册证据一起判，
                # 不能只看 `used_platforms`（生产库 account 3 的 available 别名
                # 全部注册过 ChatGPT，但池里没记账 —— 只看记账会挑错主号）。
                from core.db.icloud_alias_repository import icloud_alias_repository

                candidates_by_account = {
                    int(c["id"]): icloud_alias_repository.find_candidates(
                        account_id=int(c["id"]), active_status="active"
                    )
                    for c in candidates
                    if c.get("id")
                }
                counts = _claimable_account_counts(candidates_by_account, platform)
                if any(count > 0 for count in counts.values()):
                    picked = max(
                        candidates,
                        key=lambda c: (
                            counts.get(int(c["id"] or 0), 0),
                            -int(c["id"] or 0),
                        ),
                    )
                else:
                    picked = candidates[0]
                row = _record_to_model(picked)
        else:
            row = _first_remote_account_with_aliases(session, platform)
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

        # 四条分支（新建 / 本地优先跳过 / 本账号更新 / 其它账号保留）都在
        # 仓储里，两种后端共用一份规则。
        from core.db.icloud_alias_repository import icloud_alias_repository

        outcome = icloud_alias_repository.mirror_remote(
            account_id=int(account_id),
            address=address,
            label=label,
            status=status,
            provider_id=provider_id,
            source=SOURCE_REMOTE,
            created_at=created_at,
        )
        if outcome == "created":
            created += 1
        elif outcome == "skipped_local":
            skipped_local += 1
        elif outcome == "updated":
            updated += 1
        # foreign / noop：保留现有行，不计数

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

    from core.db.icloud_alias_repository import icloud_alias_repository

    icloud_alias_repository.mirror_remote(
        account_id=int(account_id),
        address=address,
        label=str(created.get("label") or label),
        status=ALIAS_STATUS_ACTIVE,
        provider_id=provider_id,
        source=SOURCE_REMOTE,
        # 地址已存在时只补空的 provider_id，不动来源与标签（本地优先）
        backfill_provider_only=True,
    )
    fresh = icloud_alias_repository.get_by_address(address)
    return _alias_to_dict(fresh, row.email)


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
    raw_messages = data.get("messages") or []
    # 新接口（2026-11-02）列表里的 `preview` 可能是空串，验证码正文要靠
    # `/api/inbox/previews` 批量补 —— 不补的话 wait_for_code 只能看到标题，
    # 明明邮件到了却一直等不到码。
    missing_ids = [
        str(item.get("id") or "")
        for item in raw_messages
        if isinstance(item, dict) and not str(item.get("preview") or "").strip()
    ]
    missing_ids = [item for item in missing_ids if item]
    if missing_ids:
        try:
            previews = client.fetch_previews(row.remote_account_id, missing_ids)
        except ICloudError:
            previews = {}
        for item in raw_messages:
            if isinstance(item, dict):
                item["preview"] = previews.get(str(item.get("id") or ""), item.get("preview") or "")
    messages = [_to_mail_message(item) for item in raw_messages]
    return messages, "remote", ""


def check_remote_account(account_id: int, *, proxy: str | None = None) -> dict[str, Any]:
    """让远端服务对账号做一次低风险探活，并把结果映射回本地 DTO。"""
    row = get_account(account_id)
    client = remote_client(row, proxy=proxy)
    summary = client.check_account(row.remote_account_id)
    status = str(summary.get("status") or "").strip()
    from core.db.icloud_account_repository import icloud_account_repository

    fresh = icloud_account_repository.set_fields(
        account_id, status=status or "active", sync_error="", last_sync_at=_utcnow()
    )
    if fresh is None:
        raise ICloudError("account_not_found", "远程账号不存在")
    return _account_public_dict(_record_to_model(fresh))


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
