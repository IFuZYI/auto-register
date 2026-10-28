"""隐私邮箱（Hide My Email）业务：号池领取/归还、生成、同步、删除、启停。

⚠️ `claim_alias` 经 `_facade.generate_alias(...)` 调用生成逻辑（而非直接名字调用）：
测试用 `monkeypatch.setattr(icloud_service, "generate_alias", …)` 打桩，
必须让「patch 门面 = patch 调用点」。见 `_facade.py` 与门面 docstring。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from sqlmodel import select

from core.db import (
    ICloudAccountModel,
    ICloudAliasModel,
    email_used_by_platform,
    mark_email_used_by,
    new_alias_share_token,
    platform_session,
)
from platforms.icloud import (
    ALIAS_STATUS_ACTIVE,
    ALIAS_STATUS_DISABLED,
    POOL_STATUSES,
    POOL_STATUS_AVAILABLE,
    POOL_STATUS_IN_USE,
    POOL_STATUS_UNPOOLED,
    POOL_STATUS_USED,
    ICloudError,
)
from services.icloud._locks import HOURLY_ALIAS_LIMIT, _as_utc, _utcnow
from services.icloud._facade import _facade
from services.icloud.accounts import (
    _registered_platforms_for,
    _registered_platforms_one,
    alias_quota,
    get_account,
    load_credentials,
)

logger = logging.getLogger(__name__)


def _alias_to_dict(
    row: Any, account_email: str, *, registered: Optional[set[str]] = None
) -> dict[str, Any]:
    """别名行 → 界面用的 dict。

    **接受模型或 dict**：JSON 后端下仓储返回 dict，SQLite 下是
    `ICloudAliasModel` —— 两种形状都要能读，否则接一种后端就炸另一种。
    """
    def _v(name: str, default: Any = None) -> Any:
        if isinstance(row, dict):
            got = row.get(name)
        else:
            got = getattr(row, name, None)
        return default if got is None else got

    return {
        "id": _v("id"),
        "account_id": _v("account_id"),
        "account_email": account_email,
        "address": _v("address"),
        "label": _v("label"),
        "note": _v("note"),
        "status": _v("status"),
        # 来源：local（本机直连 Apple）/ remote（远程 icloud-hme 仓库）。
        # 界面按它显示「本地 / 远程」标签并做来源筛选。
        "source": _v("source", "") or "local",
        # 老库（pool_status 列补上之前）没有这个字段，按「未入池」处理最保守：
        # 当作可用会把一批从没被用户确认过的地址直接送进注册取号池。
        "pool_status": _v("pool_status", "") or POOL_STATUS_UNPOOLED,
        # 被哪些平台消耗过（`,grok,chatgpt,`）。界面用它显示「谁用过」，
        # 也用来解释「为什么这个 used 号还能被某个平台领到」。
        "used_platforms": _v("used_platforms", "") or "",
        # `accounts` 表里的权威注册证据（跨库），与上面的记账是两回事：
        # 任务中途崩掉会让记账缺项，这里查出来的才是「真的注册过」。
        # 界面按它显示「已注册平台」列并做平台筛选。
        "registered_platforms": sorted(registered or ()),
        "provider_id": _v("provider_id"),
        "share_token": _v("share_token", "") or "",
        "created_at": _as_utc(_v("created_at")).isoformat() if _v("created_at") else None,
    }


def list_aliases(account_id: Optional[int] = None) -> list[dict[str, Any]]:
    # 别名行走**仓储**（按后端分派）；主号邮箱仍查本平台库（它不在别名表里）。
    from core.db.icloud_alias_repository import icloud_alias_repository

    rows = icloud_alias_repository.list_aliases(account_id)

    from core.db.icloud_account_repository import icloud_account_repository

    emails = {row["id"]: row["email"] for row in icloud_account_repository.list_all()}
    # 「这个地址注册过哪些平台」的权威证据在 `accounts` 表（跨库），不是池里的
    # `used_platforms` 记账。界面要按平台筛选、也要在取号前比对，所以列表接口
    # 一并把它算出来；一次批量查询，不是每行一次。
    registered = _registered_platforms_for([row["address"] for row in rows])
    return [
        _alias_to_dict(
            row, emails.get(row.get("account_id"), ""),
            registered=registered.get(str(row.get("address") or "").strip().lower()),
        )
        for row in rows
    ]


# ----------------------------------------------------------------- 号池取号


def claim_alias(
    account_id: int,
    *,
    label: str = "",
    note: str = "",
    proxy: str | None = None,
    platform: str = "",
) -> dict[str, Any]:
    """从号池里领一个隐私邮箱给注册任务用。

    与 `generate_alias` 的区别是「池语义」：先看有没有没用过的现成别名，
    有就标记 `in_use` 直接发出去，没有才向 Apple 新生成一个（生成出来的
    也直接是 `in_use`，不会再被下一个任务领走）。

    只领 `available` 的号。生成/同步下来但还没被用户勾选「导入邮箱池」的别名
    是 `unpooled`，**不算池里的号**，这里会跳过它们（见 POOL_STATUS_UNPOOLED）。

    **按平台消耗**：`platform` 给定时，还会跳过「已被这个平台用过」的别名，
    但**不跳过被别的平台用过的** —— 同一个地址注册过 ChatGPT 之后还能注册
    Grok（两个平台的账号体系互不相干）。不给 `platform` 时退回保守行为：
    `used` 一律不领。

    为什么要先捞现成的：Apple 每个主号每小时只让生成 5 个（
    `ICLOUD_HOURLY_ALIAS_LIMIT`），生成额度是稀缺资源；而导入进来的
    别名里常有一批从没用过的。旧行为是每次注册都新生成一个，等于把额度当
    无限用，跑到 5 个就报 `provider_rate_limited`，明明池里还有闲号。

    过期回滚：`in_use` 但一直没被标记完成的别名会在
    `release_stale_claims()` 里放回 `available`，避免任务中途崩掉把号永久占死。
    **活着的长任务由 `renew_alias_claim` 续约保活**（等码轮询定期调），
    所以回收只命中真正崩掉的那些 —— 使用中的号不会被第二个任务领走。
    """
    wanted_platform = str(platform or "").strip().lower()
    with _facade._account_lock(account_id):
        row = get_account(account_id)
        if not row.enabled:
            raise ICloudError("account_disabled", f"iCloud 主号已停用: {row.email}")

        # 1) 先捞池里没用过的（且没被停用、没出错的）
        # 候选查询走**仓储**（筛选条件用查询层写一份，两种后端各自执行）
        from core.db.icloud_alias_repository import icloud_alias_repository

        candidates = icloud_alias_repository.find_candidates(
            account_id=account_id, active_status=ALIAS_STATUS_ACTIVE
        )

        # 权威证据：`accounts` 表里这些地址注册过哪些平台（跨库）。池里的
        # `used_platforms` 只是记账，任务中途崩掉会缺项 —— 只信记账就会把
        # 一个其实已经注册过的地址再发出去，白跑一轮还撞「邮箱已被占用」。
        # 这一步就是「先判断，再和平台已注册账号里进行比对，确保没注册
        # 才能开始注册」。
        registered = _facade._registered_platforms_for([c["address"] for c in candidates])

        existing = None
        for candidate in candidates:
            status = str(candidate.get("pool_status") or "") or POOL_STATUS_AVAILABLE
            address_key = str(candidate.get("address") or "").strip().lower()
            hit_platforms = registered.get(address_key, set())
            if wanted_platform and wanted_platform in hit_platforms:
                # 本平台已经在 `accounts` 表里有这个地址的账号 —— 绝不能
                # 再发给本平台（重复注册必然失败）。顺手把记账补上，
                # 让它下次一眼可见。
                if not email_used_by_platform(
                    str(candidate.get("used_platforms") or ""), wanted_platform
                ):
                    icloud_alias_repository.set_fields(
                        candidate["id"],
                        used_platforms=mark_email_used_by(
                            str(candidate.get("used_platforms") or ""), wanted_platform
                        ),
                    )
                    logger.info(
                        "iCloud 号池：%s 已注册过 %s（accounts 表有记录），跳过",
                        candidate.get("address"),
                        wanted_platform,
                    )
                continue
            if status == POOL_STATUS_USED:
                # `used` = 至少有一个平台用掉了它。只有**本平台**没用过
                # 才允许复用；`platform` 未知时保守跳过（宁可去生成，
                # 也不要把一个已注册过的地址再发给同一个平台）。
                if not wanted_platform:
                    continue
                if email_used_by_platform(
                    str(candidate.get("used_platforms") or ""), wanted_platform
                ):
                    continue
            existing = candidate
            break

        if existing is not None:
            icloud_alias_repository.set_fields(
                existing["id"], pool_status=POOL_STATUS_IN_USE
            )
            logger.info("iCloud 号池：复用已入池的隐私邮箱 %s", existing.get("address"))
            refreshed = icloud_alias_repository.get(existing["id"]) or existing
            return _alias_to_dict(
                refreshed,
                str(row.email or ""),
                registered=registered.get(
                    str(refreshed.get("address") or "").strip().lower()
                ),
            )

        # 2) 池里没有可用的，才向 Apple 新生成
        created = _facade.generate_alias(account_id, label=label, note=note, proxy=proxy)
        icloud_alias_repository.set_fields(
            created["id"], pool_status=POOL_STATUS_IN_USE
        )
        refreshed = icloud_alias_repository.get(created["id"]) or created
        return _alias_to_dict(
            refreshed,
            str(row.email or ""),
            registered=_facade._registered_platforms_one(
                str(refreshed.get("address") or "")
            ),
        )


def mark_alias_used(alias_id: int, *, platform: str = "") -> None:
    """注册成功（地址已被消耗）落定后把别名从 `in_use` 推到 `used`；失败走 `release_alias_claim` 放回 `available`。

    别名本身不删除也不停用 —— Apple 那边还留着地址，能继续收信；这里只是
    号池记账，让**同一个平台**的下一个注册任务不再领它。`used` 对其它平台
    不生效：`platform` 会并进 `used_platforms`，别的平台取号时仍能复用它
    （邮箱只对用掉它的那个平台一次性）。

    传空 `platform` 时只标状态、不记平台 —— 那样对所有平台一起锁，
    是旧行为，只在拿不到平台名时才会发生。

    `used` 是**单向**的：想再用它得去池页面手动改回 `available`
    （`set_alias_pool_status`）。
    """
    from core.db.icloud_alias_repository import icloud_alias_repository

    alias = icloud_alias_repository.get(alias_id)
    if alias is None:
        return
    icloud_alias_repository.set_fields(
        alias_id,
        pool_status=POOL_STATUS_USED,
        used_platforms=mark_email_used_by(
            str(alias.get("used_platforms") or ""), platform
        ),
    )


def release_alias_claim(alias_id: int) -> None:
    """把没跑完就被放弃的 `in_use` 别名放回 `available`。"""
    from core.db.icloud_alias_repository import icloud_alias_repository

    alias = icloud_alias_repository.get(alias_id)
    if alias is None:
        return
    if (alias.get("pool_status") or "") != POOL_STATUS_IN_USE:
        return
    icloud_alias_repository.set_fields(alias_id, pool_status=POOL_STATUS_AVAILABLE)
    # **记一次失败**：只放回 available 而不记账，等于把「这个号刚失败过」
    # 丢掉 —— 它回到队首，下次任务还是它，每次都撞同一堵墙（实测 2026-10-28：
    # 两次冒烟都取到同一个地址、两次都 429）。
    failures = icloud_alias_repository.mark_failed(alias_id)
    if failures >= 3:
        logger.warning(
            "iCloud 号池：%s 连续失败 %d 次，取号将跳过它（疑似被 OpenAI 标记）",
            alias.get("address"), failures,
        )


def renew_alias_claim(alias_id: int) -> int:
    """给持有中的 `in_use` 租约续期（心跳），返回是否命中。

    用户要求（2026-10-10）：「使用中的账号不要再同时使用避免冲突」。
    回收（`release_stale_claims`）按 `updated_at` 超时判定，没有续约时
    **还活着的长任务**（等码重试等链路超过阈值）会被另一个取号点回收 ——
    号被重复领走，两个任务抢同一个收件箱、验证码互相顶掉。

    注册链在等码轮询里定期调它，把持有中的租约保持新鲜；回收只会命中
    真正崩掉（没续约）的那些。条件 UPDATE（`WHERE pool_status='in_use'`）
    保证不碰并发改过的行 —— 与回收同一套并发纪律。
    """
    from core.db.icloud_alias_repository import icloud_alias_repository

    # 条件 UPDATE：只碰「仍处于 in_use」的那一行 —— 与回收同一套并发纪律
    return icloud_alias_repository.update_where(
        match={"id": int(alias_id), "pool_status": POOL_STATUS_IN_USE},
        values={},
    )


def release_stale_claims(*, older_than_minutes: int = 120) -> int:
    """把「领了但一直没落定」的 `in_use` 别名放回 `available`，返回放回数量。

    没有它的话，任务中途崩掉（或进程被杀）会让别名永远停在 `in_use`：
    既没被标成 `used`，也没人把它放回去 —— 池子只出不进，慢慢掏空。
    `claim_alias` 的 docstring 承诺了这层兜底，这里把它补上。

    阈值默认 2 小时：正常注册一轮远小于它，所以超过这个时长还挂着的
    一定是被放弃的领取，而不是正在跑的活。

    **活着的长任务靠 `renew_alias_claim` 续约**（用户要求 2026-10-10
    「使用中的账号不要再同时使用避免冲突」）：等码轮询每 60 秒续一次，
    租约保持新鲜；这里只回收真正崩掉（没续约）的那些。没有续约机制时，
    等码链路超过阈值的任务会被另一个取号点回收 —— 号被重复领走，
    两个任务抢同一个收件箱、验证码互相顶掉。

    设计成**由调用方按需触发**（而不是起后台定时器）：这个模块是纯服务层，
    起线程会让它的生命周期变得难讲；需要兜底时在任务开始处调一次即可。
    """
    from core.db.icloud_alias_repository import icloud_alias_repository

    # 用条件 UPDATE 而不是「SELECT 出来改对象再 commit」：读-改-写之间若有人
    # 手动改了这个号的状态（池页面可点），整行写回会把对方的结果静默覆盖 ——
    # 包括把刚标好的 `used` 又打回 `available`。
    released = icloud_alias_repository.release_stale(
        older_than_minutes=max(int(older_than_minutes), 1)
    )
    if released:
        logger.info("iCloud 号池：%d 个超时未落定的别名已放回 available", released)
    return released


def set_alias_pool_status(alias_id: int, pool_status: str) -> dict[str, Any]:
    """手动改号池状态（池页面用），非法值直接拒绝。"""
    value = str(pool_status or "").strip().lower()
    if value not in POOL_STATUSES:
        raise ICloudError("invalid_pool_status", f"无效的号池状态: {pool_status}")
    from core.db.icloud_alias_repository import icloud_alias_repository

    alias = icloud_alias_repository.get(alias_id)
    if alias is None:
        raise ICloudError("alias_not_found", "隐私邮箱不存在")
    updated = icloud_alias_repository.set_fields(alias_id, pool_status=value)
    # 主号邮箱走主号仓储（不在别名表里）
    from core.db.icloud_account_repository import icloud_account_repository

    account = icloud_account_repository.get(alias.get("account_id") or 0)
    account_email = str((account or {}).get("email") or "")
    return _alias_to_dict(updated or alias, account_email)


def import_aliases_to_pool(alias_ids: list[int]) -> dict[str, Any]:
    """把选中的隐私邮箱导入号池（`unpooled` → `available`）。

    生成/同步下来的别名默认是 `unpooled`，注册取号会跳过；用户必须在池页面
    勾选之后调这个接口，它们才变成可领的。这是「选择导入才能导入邮箱池，不是
    全部导入邮箱池」的落地动作。

    只动 `unpooled` 的行：已经是 `used` 的地址重新入池等于让注册任务再领一次
    用过的号，而 `in_use` 的号正被某个任务持有 —— 两类都不该被这个批量操作
    顺手改掉。想回收它们用单条的 `set_alias_pool_status`，那是明确的意图。
    """
    return _set_pool_status_many(alias_ids, from_status=POOL_STATUS_UNPOOLED, to_status=POOL_STATUS_AVAILABLE)


def unpool_aliases(alias_ids: list[int]) -> dict[str, Any]:
    """把选中的隐私邮箱移出号池（`available` → `unpooled`）。

    导入的反向操作。只动还没被领过的 `available`：正在用的号移出去会让持有它的
    任务收不到码，用过的号移出去则丢失「已用过」的记账。
    """
    return _set_pool_status_many(alias_ids, from_status=POOL_STATUS_AVAILABLE, to_status=POOL_STATUS_UNPOOLED)


def _set_pool_status_many(
    alias_ids: list[int], *, from_status: str, to_status: str
) -> dict[str, Any]:
    """批量改号池状态，逐条按条件 UPDATE（不命中就是没改）。

    用条件 UPDATE 而不是「查出来改对象再写回」：读-改-写之间若有人并发改了
    这个号的状态，ORM 的整行写回会静默覆盖对方的结果。WHERE 带上原状态后，
    这类并发变更会被数据库层挡掉。

    返回 `changed` / `skipped`：跳过的是「不在原状态」的那些（比如用户勾了一
    个正在用的号想入池）。前端据此提示，而不是假装全都成功。
    """
    from core.db.icloud_alias_repository import icloud_alias_repository

    changed: list[int] = []
    skipped: list[int] = []
    seen: set[int] = set()
    for raw_id in alias_ids:
        alias_id = int(raw_id)
        if alias_id in seen:
            continue
        seen.add(alias_id)
        # 条件 UPDATE（不命中就是没改）—— 并发纪律靠数据库层
        hit = icloud_alias_repository.update_where(
            match={"id": alias_id, "pool_status": from_status},
            values={"pool_status": to_status},
        )
        if hit:
            changed.append(alias_id)
        else:
            skipped.append(alias_id)
    return {
        "changed": changed,
        "skipped": skipped,
        "pool_status": to_status,
        "pool": pool_summary(),
    }


def pool_summary(account_id: Optional[int] = None) -> dict[str, int]:
    """号池计数（未入池 / 可领取 / 使用中 / 已领用），池页面顶部的统计条用。

    这是**存储层四态计数**；界面展示层把 `available` 与 `used` 折叠为
    「已入池」（用户要求 2026-10-11：号池标签只有未入池 / 已入池 / 使用中，
    折叠规则在前端 `lib/poolStatus.ts` 的 `poolDisplaySummary`）。

    `unpooled` 单独计数：它不是「池子里的可用号」，而是「还没决定要不要进池」。
    前端把它和另两个分开显示，否则用户会把「未入池 168 个」误读成「有 168 个
    可用号」，然后奇怪为什么注册取不到号。
    """
    from core.db.icloud_alias_repository import icloud_alias_repository

    rows = icloud_alias_repository.list_aliases(account_id)

    summary = {status: 0 for status in POOL_STATUSES}
    summary[POOL_STATUS_UNPOOLED] = 0
    for row in rows:
        value = str(row.get("pool_status") or "") or POOL_STATUS_AVAILABLE
        # 未知值（脏数据/跨版本残留）并入 available：行级展示折叠（前端
        # `poolDisplayStatus`）把未知值归「已入池」，这里不兜底的话统计条
        # 会出现「共 N 个 ≠ 三个 Tag 之和」、筛选数与统计数对不上。
        if value not in summary:
            value = POOL_STATUS_AVAILABLE
        summary[value] += 1
    summary["total"] = len(rows)
    return summary


# ------------------------------------------------------------- 隐私邮箱管理


def generate_alias(account_id: int, *, label: str = "", note: str = "", proxy: str | None = None) -> dict[str, Any]:
    """生成并保留一个隐私邮箱地址。

    **只支持本地账号**（用户要求 2026-10-09：「本注册机不应该在远端创建隐私邮箱，
    只有本地账号才行」）。远程账号在这里明确拒绝，不再落到远端创建 ——
    远端仓库只作为**取号来源**（同步/复用已有别名），不在远端新增。

    远程账号的报错用 `invalid_config`（HTTP 400）：这是「这个账号不能这么用」
    的配置问题，不是上游故障，用 502 会让用户以为是 icloud-hme 挂了。
    """
    with _facade._account_lock(account_id):
        row = get_account(account_id)
        if not row.enabled:
            raise ICloudError("account_disabled", f"iCloud 主号已停用: {row.email}")
        if str(getattr(row, "source", "") or "") == "remote":
            raise ICloudError(
                "invalid_config",
                "远程账号不支持生成隐私邮箱 —— 本注册机只允许本地账号生成。"
                "请在 iCloud 隐私邮箱页选择本地主号生成，或先同步/导入远程已有别名。",
            )

        quota = alias_quota(account_id)
        if quota["remaining"] <= 0:
            raise ICloudError(
                "provider_rate_limited",
                f"主号 {row.email} 本小时的隐私邮箱生成额度已用完（{HOURLY_ALIAS_LIMIT} 个/小时）",
            )

        credentials = load_credentials(row)
        with _facade.web_client(proxy=proxy) as client:
            private_email = client.generate_private_email(credentials, label=label, note=note)
        return _upsert_alias(account_id, private_email.to_dict(), row.email)


def sync_aliases(account_id: int, *, proxy: str | None = None) -> dict[str, Any]:
    """从 iCloud 拉取主号名下全部历史隐私邮箱，按地址去重合并到本地。

    远程账号走远程分支（从 icloud-hme 服务拉，去重时本地优先）。
    """
    row = get_account(account_id)
    if str(getattr(row, "source", "") or "") == "remote":
        return _facade.sync_remote_account(account_id, proxy=proxy)

    credentials = load_credentials(row)
    try:
        with _facade.web_client(proxy=proxy) as client:
            private_emails = client.list_private_emails(credentials)
    except ICloudError as exc:
        _record_sync_error(account_id, str(exc))
        raise

    created = updated = 0
    for private_email in private_emails:
        _, is_new = _upsert_alias(account_id, private_email.to_dict(), row.email, return_flag=True)
        created += int(is_new)
        updated += int(not is_new)

    _record_sync_error(account_id, "")
    return {
        "account_id": account_id,
        "fetched": len(private_emails),
        "created": created,
        "updated": updated,
        "synced_at": _utcnow().isoformat(),
    }


def delete_alias(alias_id: int, *, remote: bool = True, proxy: str | None = None) -> None:
    from core.db.icloud_alias_repository import icloud_alias_repository

    alias = icloud_alias_repository.get(alias_id)
    if alias is None:
        raise ICloudError("alias_not_found", "隐私邮箱不存在")
    # 主号对象要拿去做远端操作（`load_credentials` / `remote_client` 要模型）。
    # 走 `get_account`（它已改走仓储），拿不到就按 None 处理。
    account = None
    try:
        account = get_account(int(alias.get("account_id") or 0))
    except ICloudError:
        account = None
    snapshot = (alias.get("address"), alias.get("provider_id"), alias.get("status"))

    if remote and account is not None:
        if str(getattr(account, "source", "") or "local") == "remote":
            # 远程行：让 icloud-hme 服务删（anonymousId 即 provider_id）。
            client = _facade.remote_client(account)
            client.delete_alias(account.remote_account_id, snapshot[1])
        else:
            credentials = load_credentials(account)
            with _facade.web_client(proxy=proxy) as client:
                client.delete_private_email(
                    credentials, address=snapshot[0], provider_id=snapshot[1], status=snapshot[2]
                )

    icloud_alias_repository.delete(alias_id)


def set_alias_active(
    alias_id: int, active: bool, *, proxy: str | None = None
) -> dict[str, Any]:
    """停用 / 重新激活一个隐私邮箱（不删除，可逆）。

    与删除的区别：停用后地址还在 Apple 那边保留，随时能恢复；删除不可逆。
    停用还顺带挡住转发，不想再收某个站点的信但又不舍得销毁地址时用它。

    远程行走远程分支：让 icloud-hme 服务停用/激活（它再对 Apple 操作）。
    """
    from core.db.icloud_alias_repository import icloud_alias_repository

    alias = icloud_alias_repository.get(alias_id)
    if alias is None:
        raise ICloudError("alias_not_found", "隐私邮箱不存在")
    account = None
    try:
        account = get_account(int(alias.get("account_id") or 0))
    except ICloudError:
        account = None
    address, provider_id = alias.get("address"), alias.get("provider_id")

    if account is None:
        raise ICloudError("account_not_found", "隐私邮箱所属主号不存在")

    if str(getattr(account, "source", "") or "local") == "remote":
        client = _facade.remote_client(account)
        client.set_alias_active(account.remote_account_id, provider_id, active)
    else:
        credentials = load_credentials(account)
        with _facade.web_client(proxy=proxy) as client:
            client.set_private_email_active(
                credentials, address=address, provider_id=provider_id, active=active
            )

    updated = icloud_alias_repository.set_fields(
        alias_id,
        status=ALIAS_STATUS_ACTIVE if active else ALIAS_STATUS_DISABLED,
    )
    if updated is None:
        raise ICloudError("alias_not_found", "隐私邮箱不存在")
    return _alias_to_dict(updated, str(account.email or ""))


def delete_aliases(
    alias_ids: list[int], *, remote: bool = True, proxy: str | None = None
) -> dict[str, Any]:
    """批量删除隐私邮箱。

    逐条走单条删除的老路，一条失败不拖累后面的：上游偶发拒绝、个别地址已经在
    Apple 那边不存在都很常见，整批回滚只会让用户反复重试。
    """
    deleted: list[int] = []
    failed: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw_id in alias_ids:
        alias_id = int(raw_id)
        if alias_id in seen:
            continue
        seen.add(alias_id)
        try:
            delete_alias(alias_id, remote=remote, proxy=proxy)
        except ICloudError as error:
            failed.append({"alias_id": alias_id, "code": error.code, "message": str(error)})
        except Exception as error:  # noqa: BLE001 - 批量里不能让单条异常吞掉剩下的
            logger.exception("批量删除隐私邮箱 %s 失败", alias_id)
            failed.append({"alias_id": alias_id, "code": "unknown", "message": str(error)})
        else:
            deleted.append(alias_id)
    return {"deleted": deleted, "failed": failed}


def _upsert_alias(
    account_id: int,
    payload: dict[str, Any],
    account_email: str,
    *,
    return_flag: bool = False,
):
    """同步下来的别名写入 —— 规则在仓储里（三条业务规则，见那个方法）。

    这里只做参数适配：既有调用方传 `(account_id, payload, account_email)`。
    """
    from core.db.icloud_alias_repository import icloud_alias_repository

    return icloud_alias_repository.upsert_from_sync(
        account_id=account_id,
        address=str(payload.get("address") or ""),
        payload=payload,
        account_email=account_email,
        return_flag=return_flag,
    )


def _record_sync_error(account_id: int, message: str) -> None:
    from core.db.icloud_account_repository import icloud_account_repository

    icloud_account_repository.set_fields(
        account_id,
        sync_error=message,
        status="error" if message else "active",
        last_sync_at=_utcnow(),
    )
