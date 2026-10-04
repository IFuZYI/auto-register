"""ChatGPT 账号与 CPA 的同步辅助逻辑。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.db import AccountModel, platform_session
from services.chatgpt_account_state import apply_chatgpt_status_policy

CPA_SYNC_NAME = "cpa"
SUB2API_SYNC_NAME = "sub2api"
CLIPROXY_SYNC_NAME = "cliproxyapi"
GROK2API_SYNC_NAME = "grok2api"
CHATGPT2API_SYNC_NAME = "chatgpt2api"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _get_config_value(key: str, default: str = "") -> str:
    try:
        from core.config_store import config_store

        value = str(config_store.get(key, "") or "").strip()
        return value or default
    except Exception:
        return default


def _resolve_cliproxy_target(api_url: str | None = None, api_key: str | None = None) -> tuple[str | None, str | None]:
    resolved_url = (
        str(api_url or "").strip()
        or _get_config_value("cliproxyapi_base_url")
        or _get_config_value("cpa_api_url")
        or None
    )
    resolved_key = (
        str(api_key or "").strip()
        or _get_config_value("cliproxyapi_management_key")
        or _get_config_value("cpa_api_key")
        or None
    )
    return resolved_url, resolved_key


def _get_account_extra(account: Any) -> dict[str, Any]:
    if hasattr(account, "get_extra"):
        try:
            extra = account.get_extra()
            if isinstance(extra, dict):
                return extra
        except Exception:
            pass
    extra = getattr(account, "extra", {})
    return extra if isinstance(extra, dict) else {}


def get_cliproxy_sync_state(extra_or_account: Any) -> dict[str, Any]:
    extra = extra_or_account if isinstance(extra_or_account, dict) else _get_account_extra(extra_or_account)
    sync_statuses = extra.get("sync_statuses", {})
    if not isinstance(sync_statuses, dict):
        return {}
    state = sync_statuses.get(CLIPROXY_SYNC_NAME, {})
    return state if isinstance(state, dict) else {}


def _record_sync_result(extra: dict[str, Any], sync_name: str, ok: bool, msg: str) -> dict[str, Any]:
    sync_statuses = extra.get("sync_statuses")
    if not isinstance(sync_statuses, dict):
        sync_statuses = {}

    state = sync_statuses.get(sync_name)
    if not isinstance(state, dict):
        state = {}

    now = _utcnow_iso()
    state["last_attempt_ok"] = bool(ok)
    state["last_message"] = msg
    state["last_attempt_at"] = now
    state["uploaded"] = bool(state.get("uploaded")) or bool(ok)
    if ok:
        state["uploaded_at"] = now

    sync_statuses[sync_name] = state
    extra["sync_statuses"] = sync_statuses
    return state


def record_cpa_sync_result(extra: dict[str, Any], ok: bool, msg: str) -> dict[str, Any]:
    return _record_sync_result(extra, CPA_SYNC_NAME, ok, msg)


def record_sub2api_sync_result(extra: dict[str, Any], ok: bool, msg: str) -> dict[str, Any]:
    return _record_sync_result(extra, SUB2API_SYNC_NAME, ok, msg)


def record_grok2api_sync_result(extra: dict[str, Any], ok: bool, msg: str) -> dict[str, Any]:
    """记录一次 grok2api 上传结果（供前端 `sync_statuses.grok2api` 展示）。

    与 CPA / Sub2API 共用 `_record_sync_result` 的形状，前端
    `uploadSyncMeta()` 那套渲染不用改就能显示。
    """
    return _record_sync_result(extra, GROK2API_SYNC_NAME, ok, msg)


def record_chatgpt2api_sync_result(extra: dict[str, Any], ok: bool, msg: str) -> dict[str, Any]:
    """记录一次 chatgpt2api 上传结果（供前端 `sync_statuses.chatgpt2api` 展示）。

    与 CPA / Sub2API / grok2api 共用 `_record_sync_result` 的形状，前端
    `uploadSyncMeta()` 那套渲染不用改就能显示。
    """
    return _record_sync_result(extra, CHATGPT2API_SYNC_NAME, ok, msg)


def record_cliproxy_sync_result(extra: dict[str, Any], sync_result: dict[str, Any]) -> dict[str, Any]:
    sync_statuses = extra.get("sync_statuses")
    if not isinstance(sync_statuses, dict):
        sync_statuses = {}
    sync_statuses[CLIPROXY_SYNC_NAME] = dict(sync_result or {})
    extra["sync_statuses"] = sync_statuses
    return sync_statuses[CLIPROXY_SYNC_NAME]


def build_chatgpt_sync_account(account: Any):
    extra = _get_account_extra(account)

    class _SyncAccount:
        pass

    obj = _SyncAccount()
    obj.email = getattr(account, "email", "")
    obj.user_id = getattr(account, "user_id", "")
    obj.access_token = extra.get("access_token") or getattr(account, "token", "")
    obj.refresh_token = extra.get("refresh_token", "")
    obj.id_token = extra.get("id_token", "")
    obj.session_token = extra.get("session_token", "")
    obj.client_id = extra.get("client_id", "app_EMoamEEZ73f0CkXaXp7hrann")
    obj.cookies = extra.get("cookies", "")
    return obj


def upload_chatgpt_account_to_cpa(account: Any, api_url: str | None = None, api_key: str | None = None) -> tuple[bool, str]:
    try:
        sync_account = build_chatgpt_sync_account(account)
        if not getattr(sync_account, "access_token", ""):
            return False, "账号缺少 access_token"

        from platforms.chatgpt.cpa_upload import generate_token_json, upload_to_cpa

        token_data = generate_token_json(sync_account)
        return upload_to_cpa(
            token_data,
            api_url=api_url,
            api_key=api_key,
            proxy=upload_proxy_for("cpa", _get_account_extra(account)),
        )
    except Exception as exc:
        return False, f"上传异常: {exc}"


def update_account_model_cpa_sync(
    account: AccountModel,
    ok: bool,
    msg: str,
    session: Session | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    extra = account.get_extra()
    state = record_cpa_sync_result(extra, ok, msg)
    account.set_extra(extra)
    account.updated_at = _utcnow()
    if session is not None:
        session.add(account)
        if commit:
            session.commit()
            session.refresh(account)
    return state


def update_account_model_sub2api_sync(
    account: AccountModel,
    ok: bool,
    msg: str,
    session: Session | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    extra = account.get_extra()
    state = record_sub2api_sync_result(extra, ok, msg)
    account.set_extra(extra)
    account.updated_at = _utcnow()
    if session is not None:
        session.add(account)
        if commit:
            session.commit()
            session.refresh(account)
    return state


def update_account_model_chatgpt2api_sync(
    account: AccountModel,
    ok: bool,
    msg: str,
    session: Session | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """把一次 chatgpt2api 上传结果写回账号行（与 sub2api 版本同形）。"""
    extra = account.get_extra()
    state = record_chatgpt2api_sync_result(extra, ok, msg)
    account.set_extra(extra)
    account.updated_at = _utcnow()
    if session is not None:
        session.add(account)
        if commit:
            session.commit()
            session.refresh(account)
    return state


def update_account_model_cliproxy_sync(
    account: AccountModel,
    sync_result: dict[str, Any],
    session: Session | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    extra = account.get_extra()
    state = record_cliproxy_sync_result(extra, sync_result)
    account.set_extra(extra)
    apply_chatgpt_status_policy(account, remote_sync=sync_result)
    account.updated_at = _utcnow()
    if session is not None:
        session.add(account)
        if commit:
            session.commit()
            session.refresh(account)
    return state


def update_account_model_local_probe(
    account: AccountModel,
    probe: dict[str, Any],
    session: Session | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    extra = account.get_extra()
    extra["chatgpt_local"] = probe
    account.set_extra(extra)
    apply_chatgpt_status_policy(account, local_probe=probe)
    account.updated_at = _utcnow()
    if session is not None:
        session.add(account)
        if commit:
            session.commit()
            session.refresh(account)
    return probe


def persist_cpa_sync_result(account: Any, ok: bool, msg: str) -> None:
    if isinstance(account, AccountModel) and account.id is not None:
        with platform_session("chatgpt") as session:
            row = session.get(AccountModel, account.id)
            if row:
                update_account_model_cpa_sync(row, ok, msg, session=session, commit=True)
                return

    extra = getattr(account, "extra", None)
    if isinstance(extra, dict):
        record_cpa_sync_result(extra, ok, msg)


def persist_sub2api_sync_result(account: Any, ok: bool, msg: str) -> None:
    if isinstance(account, AccountModel) and account.id is not None:
        with platform_session("chatgpt") as session:
            row = session.get(AccountModel, account.id)
            if row:
                update_account_model_sub2api_sync(row, ok, msg, session=session, commit=True)
                return

    extra = getattr(account, "extra", None)
    if isinstance(extra, dict):
        record_sub2api_sync_result(extra, ok, msg)


def persist_chatgpt2api_sync_result(account: Any, ok: bool, msg: str) -> None:
    """记录一次 chatgpt2api 上传结果（自动上传路径调用）。

    与 `persist_sub2api_sync_result` 同形：账号是数据库行时写回该行，
    否则退化为写内存里的 `extra`。前端账号列表的通用上传状态标签读
    `sync_statuses.chatgpt2api`，不写这里就永远不显示上传状态。
    """
    if isinstance(account, AccountModel) and account.id is not None:
        with platform_session("chatgpt") as session:
            row = session.get(AccountModel, account.id)
            if row:
                update_account_model_chatgpt2api_sync(row, ok, msg, session=session, commit=True)
                return

    extra = getattr(account, "extra", None)
    if isinstance(extra, dict):
        record_chatgpt2api_sync_result(extra, ok, msg)


def upload_account_model_to_cpa(
    account: AccountModel,
    session: Session | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    commit: bool = True,
) -> tuple[bool, str]:
    ok, msg = upload_chatgpt_account_to_cpa(account, api_url=api_url, api_key=api_key)
    update_account_model_cpa_sync(account, ok, msg, session=session, commit=commit)
    return ok, msg


def _remote_auth_missing(sync_result: dict[str, Any]) -> bool:
    if not isinstance(sync_result, dict):
        return True
    remote_state = str(sync_result.get("remote_state") or "").strip().lower()
    if remote_state == "not_found":
        return True
    return not bool(sync_result.get("uploaded"))


def _local_probe_uploadable(probe: dict[str, Any]) -> bool:
    auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
    return str(auth.get("state") or "").strip() == "access_token_valid"


def _remote_state_label(sync_result: dict[str, Any]) -> str:
    value = str(sync_result.get("remote_state") or sync_result.get("status") or "").strip()
    return value or "unknown"


def backfill_chatgpt_account_to_cpa(
    account: AccountModel,
    *,
    session: Session | None = None,
    api_url: str | None = None,
    api_key: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    from platforms.chatgpt.status_probe import probe_local_chatgpt_status
    from services.cliproxyapi_sync import sync_chatgpt_cliproxyapi_status

    api_url, api_key = _resolve_cliproxy_target(api_url=api_url, api_key=api_key)
    results: list[dict[str, Any]] = []
    cached_sync = get_cliproxy_sync_state(account)
    initial_sync = cached_sync if cached_sync else {}
    used_cached_sync = bool(cached_sync) and str(cached_sync.get("remote_state") or "").strip().lower() != "unreachable"

    if not used_cached_sync:
        sync_account = build_chatgpt_sync_account(account)
        initial_sync = sync_chatgpt_cliproxyapi_status(sync_account, api_url=api_url, api_key=api_key)
        update_account_model_cliproxy_sync(account, initial_sync, session=session, commit=False)

    remote_state = str(initial_sync.get("remote_state") or "").strip().lower()
    if remote_state == "unreachable":
        msg = initial_sync.get("message") or "CLIProxyAPI 无法连接"
        results.append({"name": "CLIProxyAPI 同步", "ok": False, "msg": msg})
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": False, "uploaded": False, "skipped": False, "message": msg, "results": results}

    if not _remote_auth_missing(initial_sync):
        msg = f"远端已存在 ({_remote_state_label(initial_sync)})，跳过上传"
        results.append({"name": "CLIProxyAPI 同步", "ok": True, "msg": msg})
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": True, "uploaded": False, "skipped": True, "message": msg, "results": results}

    sync_account = build_chatgpt_sync_account(account)
    probe = probe_local_chatgpt_status(sync_account, proxy=None)
    update_account_model_local_probe(account, probe, session=session, commit=False)
    if not _local_probe_uploadable(probe):
        auth = probe.get("auth") if isinstance(probe.get("auth"), dict) else {}
        msg = auth.get("message") or f"本地状态不可上传: {auth.get('state') or 'unknown'}"
        results.append({"name": "本地状态探测", "ok": False, "msg": msg})
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": False, "uploaded": False, "skipped": False, "message": msg, "results": results}

    ok, msg = upload_account_model_to_cpa(account, session=session, api_url=api_url, api_key=api_key, commit=False)
    results.append({"name": "CLIProxyAPI 上传", "ok": ok, "msg": msg})
    if not ok:
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": False, "uploaded": False, "skipped": False, "message": msg, "results": results}

    verified_sync = sync_chatgpt_cliproxyapi_status(build_chatgpt_sync_account(account), api_url=api_url, api_key=api_key)
    update_account_model_cliproxy_sync(account, verified_sync, session=session, commit=False)
    if _remote_auth_missing(verified_sync):
        verify_msg = verified_sync.get("message") or "上传后远端仍未发现 auth-file"
        results.append({"name": "CLIProxyAPI 复核", "ok": False, "msg": verify_msg})
        if session is not None and commit:
            session.commit()
            session.refresh(account)
        return {"ok": False, "uploaded": False, "skipped": False, "message": verify_msg, "results": results}

    verify_msg = f"补传完成，远端状态={_remote_state_label(verified_sync)}"
    results.append({"name": "CLIProxyAPI 复核", "ok": True, "msg": verify_msg})
    if session is not None and commit:
        session.commit()
        session.refresh(account)
    return {"ok": True, "uploaded": True, "skipped": False, "message": verify_msg, "results": results}


#: 支持「上传代理」的面板 → 开关配置键。
#:
#: 只有导入格式本身带代理字段的面板才支持（读参考实现确认）：
#: - CPA：auth 文件 JSON 顶层 `proxy_url`（`sdk/auth/filestore.go`）
#: - chatgpt2api：导入对象 `proxy` 字段（`_add_account_payloads`）
#: sub2api 需要先在面板里建代理记录拿 proxy_id、grok2api 的导入格式不含代理
#: —— 都不做（避免半吊子实现，见 `docs/` 的面板接线说明）。
_UPLOAD_PROXY_SWITCHES: dict[str, str] = {
    "cpa": "cpa_upload_proxy_enabled",
    "chatgpt2api": "chatgpt2api_upload_proxy_enabled",
}


def upload_proxy_for(panel: str, extra: dict[str, Any]) -> str:
    """按「上传代理」开关返回要随凭据上传的代理（账号绑定的 `register_proxy`）。

    开关关闭 / 未设置 / 面板不支持 / 账号没有绑定 —— 都返回空串（不改行为）。
    开关按面板分开：CPA 与 chatgpt2api 的导入格式各自独立，合成一个会让
    用户开了一边连带另一边。
    """
    switch_key = _UPLOAD_PROXY_SWITCHES.get(str(panel or "").strip().lower())
    if not switch_key:
        return ""
    try:
        from core.config_store import config_store

        raw = str(config_store.get(switch_key, "") or "").strip().lower()
    except Exception:
        return ""
    if raw not in {"1", "true", "yes", "on", "enabled"}:
        return ""
    return str((extra or {}).get("register_proxy") or "").strip()
