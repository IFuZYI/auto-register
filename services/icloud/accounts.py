"""iCloud 主号业务：读取、登录会话落库、启停、删除、注册证据查询。

以及主号字典化（`_account_to_dict`）与滚动小时额度（`alias_quota`）。
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Optional

from sqlmodel import select

from core.db import ICloudAccountModel, ICloudAliasModel, platform_session
from platforms.icloud import (
    ICloudCredentials,
    ICloudError,
    LoginRequest,
    LoginState,
    SessionImportRequest,
    login_manager,
    normalize_region,
)
from services.icloud._locks import HOURLY_ALIAS_LIMIT, _as_utc, _utcnow
from services.icloud._facade import _facade

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------- 读取


def load_credentials(row: ICloudAccountModel) -> ICloudCredentials:
    try:
        payload = _facade.secret_box.decrypt_json(row.credentials_cipher)
    except Exception as exc:
        # 密文本身没坏，是解密密钥换了（最常见的原因是密钥没放在挂载卷里，
        # 重建容器时被重新生成）。抛原始的 InvalidTag 只会得到一个 500，
        # 用户看不出该怎么办——只能重新登录主号。
        raise ICloudError(
            "credentials_unreadable",
            f"iCloud 主号 {row.email} 的凭据无法解密（加密密钥已变更），请重新登录该主号",
        ) from exc
    return ICloudCredentials.from_dict(payload)


def get_account(account_id: int) -> ICloudAccountModel:
    with platform_session("icloud") as session:
        row = session.get(ICloudAccountModel, int(account_id))
        if row is None:
            raise ICloudError("account_not_found", "iCloud 主号不存在")
        return row


def find_account_by_email(email: str) -> Optional[ICloudAccountModel]:
    normalized = str(email or "").strip().lower()
    if not normalized:
        return None
    with platform_session("icloud") as session:
        return session.exec(
            select(ICloudAccountModel).where(ICloudAccountModel.email == normalized)
        ).first()


def resolve_account(email: str = "") -> ICloudAccountModel:
    """按邮箱定位主号；未指定时取第一个可用主号。"""
    if str(email or "").strip():
        row = find_account_by_email(email)
        if row is None:
            raise ICloudError("account_not_found", f"iCloud 主号不存在: {email}")
        if not row.enabled:
            raise ICloudError("account_disabled", f"iCloud 主号已停用: {row.email}")
        return row

    with platform_session("icloud") as session:
        row = session.exec(
            select(ICloudAccountModel)
            .where(ICloudAccountModel.enabled == True)  # noqa: E712 - SQLModel 需要值比较
            .order_by(ICloudAccountModel.id)
        ).first()
    if row is None:
        raise ICloudError("account_not_found", "还没有可用的 iCloud 主号，请先完成 Apple ID 登录")
    return row


def list_accounts() -> list[dict[str, Any]]:
    with platform_session("icloud") as session:
        rows = session.exec(select(ICloudAccountModel).order_by(ICloudAccountModel.id)).all()
        aliases = session.exec(select(ICloudAliasModel)).all()
    counts: dict[int, int] = {}
    for alias in aliases:
        counts[alias.account_id] = counts.get(alias.account_id, 0) + 1
    return [_account_to_dict(row, alias_count=counts.get(row.id or 0, 0)) for row in rows]


def _account_to_dict(row: ICloudAccountModel, *, alias_count: int = 0) -> dict[str, Any]:
    quota = alias_quota(row.id or 0)
    try:
        credential_state = load_credentials(row).public_state()
    except Exception:
        credential_state = {"credentials_unreadable": True}
    return {
        "id": row.id,
        "email": row.email,
        "display_name": row.display_name,
        "region": row.region,
        "status": row.status,
        "enabled": row.enabled,
        "alias_count": alias_count,
        "sync_error": row.sync_error,
        "last_sync_at": _as_utc(row.last_sync_at).isoformat() if row.last_sync_at else None,
        "created_at": _as_utc(row.created_at).isoformat() if row.created_at else None,
        "credential_state": credential_state,
        "quota": quota,
    }


def alias_quota(account_id: int) -> dict[str, Any]:
    """按滚动一小时窗口统计剩余生成额度。"""
    since = _utcnow() - timedelta(hours=1)
    with platform_session("icloud") as session:
        recent = session.exec(
            select(ICloudAliasModel)
            .where(ICloudAliasModel.account_id == int(account_id))
            .where(ICloudAliasModel.created_at >= since)
        ).all()
    used = len(recent)
    earliest = min((_as_utc(item.created_at) for item in recent), default=None)
    return {
        "limit": HOURLY_ALIAS_LIMIT,
        "used": used,
        "remaining": max(HOURLY_ALIAS_LIMIT - used, 0),
        "reset_at": (earliest + timedelta(hours=1)).isoformat() if earliest else None,
    }


def _registered_platforms_for(addresses: list[str]) -> dict[str, set[str]]:
    """地址 → 已注册平台集合（跨库查 `accounts`）。查不到时返回空表。

    判重是「确保没注册才能开始注册」的最后一道闸，但它**不能把取号整个
    卡死**：库暂时读不出来时宁可少判重（多注册一次）也不要让所有任务失败。
    """
    try:
        from core.db import account_repository

        return account_repository.platforms_by_email(addresses)
    except Exception as exc:  # noqa: BLE001 - 证据查询失败不阻断主流程
        logger.warning("读取邮箱的已注册平台失败（按无记录处理）: %s", exc)
        return {}


def _registered_platforms_one(address: str) -> set[str]:
    return _registered_platforms_for([address]).get(str(address or "").strip().lower(), set())


# ----------------------------------------------------------------- 主号登录


def start_login(request: LoginRequest, *, proxy: str | None = None) -> LoginState:
    return login_manager().start(request, proxy=proxy)


def login_state(login_id: str) -> LoginState:
    return login_manager().state(login_id)


def verify_login(login_id: str, code: str) -> LoginState:
    return login_manager().verify(login_id, code)


def resend_login_code(login_id: str) -> LoginState:
    return login_manager().resend(login_id)


def send_login_sms(login_id: str, phone_id: int, mode: str = "") -> LoginState:
    return login_manager().send_sms(login_id, phone_id, mode)


def cancel_login(login_id: str) -> None:
    login_manager().cancel(login_id)


def complete_login(state: LoginState, *, proxy: str | None = None) -> dict[str, Any]:
    """把已完成的登录会话导入为主号，随后销毁内存中的会话。"""
    if state.session is None:
        raise ICloudError("login_incomplete", "登录尚未完成，无法保存主号")
    try:
        account = import_session(
            state.session,
            email=state.email,
            display_name=state.display_name,
            proxy=proxy,
        )
    finally:
        cancel_login(state.login_id)
    return account


def import_session(
    request: SessionImportRequest,
    *,
    email: str = "",
    display_name: str = "",
    proxy: str | None = None,
) -> dict[str, Any]:
    """校验并保存一份 iCloud Web Session（应用内登录与手工 Cookie 导入共用）。"""
    with _facade.web_client(proxy=proxy) as client:
        imported = client.import_session(request)

    resolved_email = str(email or "").strip().lower() or imported.account_email
    if not resolved_email:
        raise ICloudError("invalid_config", "无法确定主号邮箱，请手工填写 Apple ID")

    with platform_session("icloud") as session:
        row = session.exec(
            select(ICloudAccountModel).where(ICloudAccountModel.email == resolved_email)
        ).first()
        credentials = imported.credentials
        if row is None:
            row = ICloudAccountModel(email=resolved_email)
        else:
            # 重新登录时保留已有 IMAP 凭据，除非本次显式提交了新的配置。
            credentials = load_credentials(row).merged_with(credentials)

        row.display_name = str(display_name or "").strip() or row.display_name
        row.region = normalize_region(credentials.region)
        row.status = "active"
        row.enabled = True
        row.sync_error = ""
        row.credentials_cipher = _facade.secret_box.encrypt_json(credentials.to_dict())
        row.updated_at = _utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _account_to_dict(row)


def set_account_enabled(account_id: int, enabled: bool) -> dict[str, Any]:
    with platform_session("icloud") as session:
        row = session.get(ICloudAccountModel, int(account_id))
        if row is None:
            raise ICloudError("account_not_found", "iCloud 主号不存在")
        row.enabled = bool(enabled)
        row.updated_at = _utcnow()
        session.add(row)
        session.commit()
        session.refresh(row)
        return _account_to_dict(row)


def delete_account(account_id: int) -> None:
    with platform_session("icloud") as session:
        row = session.get(ICloudAccountModel, int(account_id))
        if row is None:
            raise ICloudError("account_not_found", "iCloud 主号不存在")
        for alias in session.exec(
            select(ICloudAliasModel).where(ICloudAliasModel.account_id == row.id)
        ).all():
            session.delete(alias)
        session.delete(row)
        session.commit()
