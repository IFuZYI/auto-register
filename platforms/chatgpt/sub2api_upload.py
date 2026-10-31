"""
Sub2API 上传功能
"""

from __future__ import annotations
from core.panel_tls import verify_for

import base64
import json
import logging
import time
from typing import Any, Tuple

from curl_cffi import requests as cffi_requests

from platforms.chatgpt.cpa_upload import generate_token_json

logger = logging.getLogger(__name__)

DEFAULT_GROUP_IDS = [2]
DEFAULT_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def _get_config_value(key: str) -> str:
    try:
        from core.config_store import config_store

        return str(config_store.get(key, "") or "").strip()
    except Exception:
        return ""


def _parse_group_ids(raw: Any, fallback: list[int] | None = None) -> list[int]:
    candidates: list[Any]
    if isinstance(raw, str):
        candidates = [part.strip() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        candidates = list(raw)
    elif raw is None:
        candidates = []
    else:
        candidates = [raw]

    values: list[int] = []
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            values.append(int(text))
        except ValueError:
            continue

    return values or list(fallback or DEFAULT_GROUP_IDS)


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_auth(payload: dict[str, Any]) -> dict[str, Any]:
    auth_info = payload.get("https://api.openai.com/auth")
    return auth_info if isinstance(auth_info, dict) else {}


def _extract_organization_id(id_token_payload: dict[str, Any]) -> str:
    auth_info = _extract_auth(id_token_payload)
    organization_id = str(auth_info.get("organization_id") or "").strip()
    if organization_id:
        return organization_id

    organizations = auth_info.get("organizations") or []
    if isinstance(organizations, list):
        for item in organizations:
            if isinstance(item, dict):
                organization_id = str(item.get("id") or "").strip()
                if organization_id:
                    return organization_id
    return ""


def _build_sub2api_account_payload(
    account,
    group_ids: list[int] | None = None,
    proxy_id: int | None = None,
) -> dict[str, Any]:
    token_data = generate_token_json(account)
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    id_token = str(token_data.get("id_token") or "").strip()
    email = str(token_data.get("email") or getattr(account, "email", "") or "").strip()

    access_payload = _decode_jwt_payload(access_token)
    access_auth = _extract_auth(access_payload)
    expires_at = access_payload.get("exp")
    if not isinstance(expires_at, int) or expires_at <= 0:
        expires_at = int(time.time()) + 863999

    # 关键逻辑：Sub2API 依赖 OpenAI OAuth 结构化字段，这里尽量从现有 token 自动补齐。
    organization_id = _extract_organization_id(_decode_jwt_payload(id_token))

    payload: dict[str, Any] = {
        "name": email,
        "notes": "",
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 863999,
            "expires_at": expires_at,
            "chatgpt_account_id": str(
                access_auth.get("chatgpt_account_id") or token_data.get("account_id") or ""
            ).strip(),
            "chatgpt_user_id": str(access_auth.get("chatgpt_user_id") or "").strip(),
            "organization_id": organization_id,
            "client_id": str(getattr(account, "client_id", "") or DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID,
            "id_token": id_token,
        },
        "extra": {"email": email},
        "group_ids": _parse_group_ids(group_ids),
        "concurrency": 10,
        "priority": 1,
        "auto_pause_on_expired": True,
    }
    # `proxy_id` 只在**确实拿到 id** 时下发。不传等价于「不指定」；
    # 显式写 None 会让「上传代理」开关看起来生效了（键存在）实际什么也没绑，
    # 所以干脆不写 —— 键不存在就没有歧义。
    if proxy_id is not None:
        payload["proxy_id"] = int(proxy_id)
    return payload


def upload_to_sub2api(
    account,
    api_url: str | None = None,
    api_key: str | None = None,
    group_ids: list[int] | None = None,
    proxy: str = "",
) -> Tuple[bool, str]:
    """上传单个账号到 Sub2API 管理后台。

    `proxy` 非空时先确保面板里有一条对应代理的记录、拿 `proxy_id` 再随账号
    提交（Sub2API 的账号只认外键，不认 URL）。由「上传代理」开关控制
    （见 `services.chatgpt_sync.upload_proxy_for`）。

    **绑定代理失败不影响上传**：账号能不能用不取决于它走哪条出口，
    因为建不出代理记录就把账号丢掉是本末倒置。失败原因并进返回消息，
    让界面能显示「传上去了、代理没绑上」。
    """
    api_url = str(api_url or _get_config_value("sub2api_api_url")).strip()
    api_key = str(api_key or _get_config_value("sub2api_api_key")).strip()
    resolved_group_ids = _parse_group_ids(
        _get_config_value("sub2api_group_ids") if group_ids is None else group_ids
    )

    if not api_url:
        return False, "Sub2API API URL 未配置"
    if not api_key:
        return False, "Sub2API API Key 未配置"

    proxy_id: int | None = None
    proxy_note = ""
    proxy_url = str(proxy or "").strip()
    if proxy_url:
        from services.panel_proxy_binding import ensure_sub2api_proxy

        proxy_id, proxy_note = ensure_sub2api_proxy(api_url, api_key, proxy_url)

    payload = _build_sub2api_account_payload(
        account, group_ids=resolved_group_ids, proxy_id=proxy_id
    )
    url = f"{api_url.rstrip('/')}/api/v1/admin/accounts"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{api_url.rstrip('/')}/admin/accounts",
        "x-api-key": api_key,
    }

    try:
        response = cffi_requests.post(
            url,
            headers=headers,
            json=payload,
            proxies=None,
            verify=verify_for("sub2api"),
            timeout=30,
            impersonate="chrome110",
        )

        if response.status_code in (200, 201):
            return True, ("上传成功；代理：" + proxy_note) if proxy_url else "上传成功"

        error_msg = f"上传失败: HTTP {response.status_code}"
        try:
            detail = response.json()
            if isinstance(detail, dict):
                error_msg = str(
                    detail.get("message")
                    or detail.get("msg")
                    or detail.get("error")
                    or error_msg
                )
        except Exception:
            error_msg = f"{error_msg} - {response.text[:200]}"
        return False, error_msg
    except Exception as exc:
        logger.error("Sub2API 上传异常: %s", exc)
        return False, f"上传异常: {exc}"
