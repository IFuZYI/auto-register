"""补 RT 的库侧胶水：挑号、跑引擎、把结果写回账号表。

引擎（``platforms.chatgpt.rt_backfill``）刻意不认识数据库，这里负责把 ``accounts``
表的一行翻译成引擎要的入参，再把拿到的凭证塞回 ``extra_json``。每个号的补号
过程还会在 extra 里留一份 ``chatgpt_rt_backfill`` 留痕，方便事后查是哪条策略
成的、失败又是卡在哪。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional

from sqlmodel import Session

from core.db import AccountModel
from platforms.chatgpt.rt_backfill import BackfillResult, RefreshTokenBackfiller
from services.chatgpt_account_selection import select_chatgpt_accounts

logger = logging.getLogger(__name__)


def account_refresh_token(model: AccountModel) -> str:
    extra = model.get_extra()
    return str(extra.get("refresh_token") or extra.get("refreshToken") or "").strip()


def account_missing_rt(model: AccountModel) -> bool:
    return not account_refresh_token(model)


def select_backfill_targets(
    session: Session,
    *,
    account_ids: Optional[Iterable[int]] = None,
    all_filtered: bool = False,
    email: str = "",
    status: str = "",
    plus_status: str = "",
    only_missing_rt: bool = True,
) -> tuple[list[AccountModel], list[int]]:
    """挑出要补 RT 的号，返回 ``(账号列表, 找不到的 id)``。

    ``only_missing_rt`` 是默认行为：已经有 RT 的号再跑一遍纯属给 OpenAI 送风控
    素材。想强制重拿（比如怀疑旧 RT 失效）才关掉它。
    """
    return select_chatgpt_accounts(
        session,
        account_ids=account_ids,
        all_filtered=all_filtered,
        email=email,
        status=status,
        plus_status=plus_status,
        keep=account_missing_rt if only_missing_rt else None,
    )


def backfill_account_data(
    *,
    email: str,
    password: str = "",
    extra: Optional[dict] = None,
    token: str = "",
    config: Optional[dict] = None,
    proxy: Optional[str] = None,
    allow_login: bool = True,
    log_fn: Optional[Callable[[str], None]] = None,
    task_control=None,
    attempt_id=None,
) -> BackfillResult:
    """按账号字段补 RT，不落库（落库交给 ``apply_backfill_result``）。

    只收纯数据不收 ORM 对象：一个号要跑几十秒网络请求，调用方得以在这期间
    把数据库连接还回池子里。

    ``task_control`` 传的是后台任务的停止/跳过开关，会一路交到邮箱的等码循环
    里 —— 等验证码是整个补号流程里最长的一段，不接开关就停不下来。
    """
    from services.chatgpt_otp_mailbox import resolve_otp_mail_provider

    extra = dict(extra or {})
    config = dict(config or _load_config())
    log = log_fn or logger.info

    mail_provider, mail_reason = resolve_otp_mail_provider(
        email,
        account_extra=extra,
        config=config,
        proxy=proxy,
        log_fn=log,
        task_control=task_control,
        attempt_id=attempt_id,
    )
    if mail_provider is None:
        if allow_login:
            log(f"[补RT] {email} 暂时读不到收件箱（{mail_reason}），需要邮箱验证码时会失败")
    else:
        log(f"[补RT] 收件通道: {getattr(mail_provider, 'display_name', '邮箱')} → {email}")

    return RefreshTokenBackfiller(
        email=email,
        password=password,
        session_token=str(extra.get("session_token") or ""),
        access_token=str(extra.get("access_token") or token or ""),
        device_id=str(extra.get("device_id") or ""),
        totp_secret=str(extra.get("totp_secret") or ""),
        proxy=proxy,
        extra_config=config,
        mail_provider=mail_provider,
        mail_unavailable_reason=mail_reason,
        allow_login=allow_login,
        log_fn=log,
    ).run()


def build_extra_patch(result: BackfillResult) -> dict[str, Any]:
    """把补号结果整理成可以合并进 ``extra_json`` 的补丁。

    只写非空字段：会话复用那条路常常只刷新了 access_token，用空串覆盖掉库里
    原有的 session_token 等于把号弄坏。
    """
    patch: dict[str, Any] = {}
    for key in ("refresh_token", "access_token", "session_token", "id_token"):
        value = str(getattr(result, key, "") or "").strip()
        if value:
            patch[key] = value
    if result.cookie_header:
        patch["cookies"] = result.cookie_header
    if result.refresh_token:
        # 号已经有 RT 了，别再被当成 access_token_only 方案的产物
        patch["chatgpt_has_refresh_token_solution"] = True
    patch["chatgpt_rt_backfill"] = {
        "ok": result.success,
        "strategy": result.strategy,
        "message": result.summary(),
        "attempts": [
            {"strategy": item.strategy, "ok": item.ok, "message": item.message}
            for item in result.attempts
        ],
        "at": datetime.now(timezone.utc).isoformat(),
    }
    return patch


def apply_backfill_result(
    model: AccountModel,
    result: BackfillResult,
    *,
    session: Optional[Session] = None,
    commit: bool = False,
) -> dict[str, Any]:
    """把补号结果落到账号行上，返回实际写入的补丁。"""
    patch = build_extra_patch(result)
    extra = model.get_extra()
    extra.update(patch)
    model.set_extra(extra)
    if patch.get("access_token"):
        model.token = patch["access_token"]
    model.updated_at = datetime.now(timezone.utc)
    if session is not None:
        session.add(model)
        if commit:
            session.commit()
    return patch


def _load_config() -> dict:
    from core.config_store import config_store

    return config_store.get_all() or {}
