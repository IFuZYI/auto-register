"""ChatGPT 本地真实状态探测。"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from curl_cffi import requests as cffi_requests
from services.chatgpt_account_state import (
    is_account_deactivated_message,
    looks_like_banned_response,
)

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CHATGPT_ME_URL = "https://chatgpt.com/backend-api/me"
CHATGPT_ACCOUNTS_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
CODEX_USER_AGENT = "codex_cli_rs/0.116.0 (Mac OS 26.0.1; arm64) Apple_Terminal/464"
# accounts/check 是网页端自己调的接口，用 Codex CLI 的 UA 去打属于明显的非浏览器特征
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

# 首月免费的促销活动 id。账号还能领时它会出现在 eligible_promo_campaigns.plus 里，
# 领过或不符合条件就没有——这是判断"这个号还能不能白嫖一个月 Plus"的唯一依据。
PLUS_TRIAL_PROMO_ID = "plus-1-month-free"

PLUS_TRIAL_LABELS = {
    "trial_eligible": "可领首月免费",
    "plus_active": "Plus 生效中",
    "free": "Free",
    "banned": "封号",
    "token_invalid": "凭证失效",
    "no_access_token": "无 AT",
    "error": "检测失败",
}

# 这几个不是"检测结论"而是"没检测成"，不该写进账号里冒充结果：
# 写了之后账号就从"未检测"里消失，看着像已经查过。
PLUS_TRIAL_INCONCLUSIVE = {"no_access_token", "error"}

# 有这些套餐就说明已经在付费/订阅里，不用再问能不能领试用
_SUBSCRIBED_PLANS = {"plus", "pro", "team", "enterprise"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_proxies(proxy: Optional[str]) -> Optional[dict[str, str]]:
    if proxy:
        return {"http": proxy, "https": proxy}
    return None


def _decode_jwt_payload(token: str) -> dict[str, Any]:
    try:
        parts = str(token or "").split(".")
        if len(parts) < 2:
            return {}
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload.encode("utf-8"))
        data = json.loads(decoded)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_auth_info(payload: dict[str, Any]) -> dict[str, Any]:
    nested = payload.get("https://api.openai.com/auth", {})
    if isinstance(nested, dict):
        return nested
    return {}


def extract_chatgpt_account_id(account: Any) -> str:
    user_id = str(getattr(account, "user_id", "") or "").strip()
    if user_id:
        return user_id

    extra = getattr(account, "extra", {}) or {}
    id_token = str(extra.get("id_token") or getattr(account, "id_token", "") or "").strip()
    access_token = str(
        extra.get("access_token")
        or getattr(account, "access_token", "")
        or getattr(account, "token", "")
        or ""
    ).strip()

    id_payload = _decode_jwt_payload(id_token)
    auth_info = _extract_auth_info(id_payload)
    account_id = str(auth_info.get("chatgpt_account_id") or auth_info.get("account_id") or "").strip()
    if account_id:
        return account_id

    access_payload = _decode_jwt_payload(access_token)
    auth_info = _extract_auth_info(access_payload)
    return str(auth_info.get("chatgpt_account_id") or auth_info.get("account_id") or "").strip()


def _parse_loose_json(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_header_error_json(headers: Any) -> dict[str, Any]:
    if not headers:
        return {}
    raw = headers.get("X-Error-Json") or headers.get("x-error-json") or ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    raw = str(raw or "").strip()
    if not raw:
        return {}
    try:
        decoded = base64.b64decode(raw).decode("utf-8", errors="ignore")
    except Exception:
        return {}
    return _parse_loose_json(decoded)


def _extract_error_code(headers: Any, body_json: dict[str, Any], header_error_json: dict[str, Any]) -> str:
    for key in ("X-Openai-Ide-Error-Code", "x-openai-ide-error-code"):
        value = headers.get(key) if headers else None
        if isinstance(value, list):
            value = value[0] if value else ""
        if str(value or "").strip():
            return str(value).strip()

    candidates = [
        ((body_json.get("error") or {}).get("code") if isinstance(body_json.get("error"), dict) else ""),
        ((header_error_json.get("error") or {}).get("code") if isinstance(header_error_json.get("error"), dict) else ""),
    ]
    for candidate in candidates:
        if str(candidate or "").strip():
            return str(candidate).strip()
    return ""


def _extract_error_message(body_json: dict[str, Any], header_error_json: dict[str, Any], body_text: str, status_code: int) -> str:
    candidates = [
        ((body_json.get("error") or {}).get("message") if isinstance(body_json.get("error"), dict) else ""),
        ((header_error_json.get("error") or {}).get("message") if isinstance(header_error_json.get("error"), dict) else ""),
        body_json.get("message", ""),
        body_text.strip(),
    ]
    for candidate in candidates:
        if str(candidate or "").strip():
            return str(candidate).strip()[:500]
    return f"HTTP {status_code}"


@dataclass
class ProbeHTTPResult:
    status_code: int
    headers: Any
    body_text: str
    body_json: dict[str, Any]
    error_code: str
    message: str


def _perform_get(url: str, headers: dict[str, str], proxy: Optional[str]) -> ProbeHTTPResult:
    response = cffi_requests.get(
        url,
        headers=headers,
        proxies=_build_proxies(proxy),
        timeout=20,
        impersonate="chrome110",
    )
    body_text = response.text or ""
    body_json = _parse_loose_json(body_text)
    header_error_json = _parse_header_error_json(response.headers)
    error_code = _extract_error_code(response.headers, body_json, header_error_json)
    message = _extract_error_message(body_json, header_error_json, body_text, response.status_code)
    return ProbeHTTPResult(
        status_code=response.status_code,
        headers=response.headers,
        body_text=body_text,
        body_json=body_json,
        error_code=error_code,
        message=message,
    )


def _normalize_plan_type(plan_type: str, workspace_plan_type: str) -> str:
    raw = f"{plan_type} {workspace_plan_type}".strip().lower()
    if not raw:
        return "unknown"
    if "enterprise" in raw:
        return "enterprise"
    if "team" in raw:
        return "team"
    if "plus" in raw:
        return "plus"
    if "pro" in raw:
        return "pro"
    if "free" in raw:
        return "free"
    return plan_type.strip().lower() or workspace_plan_type.strip().lower() or "unknown"


def _probe_backend_me(access_token: str, proxy: Optional[str]) -> ProbeHTTPResult:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": CODEX_USER_AGENT,
    }
    return _perform_get(CHATGPT_ME_URL, headers=headers, proxy=proxy)


def _probe_codex_usage(access_token: str, account_id: str, proxy: Optional[str]) -> ProbeHTTPResult:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "User-Agent": CODEX_USER_AGENT,
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    return _perform_get(CODEX_USAGE_URL, headers=headers, proxy=proxy)


def _extract_oai_device_id(account: Any) -> str:
    """优先用账号自己的 oai-did，没有就按邮箱派生一个固定值。

    同一个号每次检测都是同一个 device，比每次随机更像正常客户端。
    """
    extra = getattr(account, "extra", {}) or {}
    cookies = str(extra.get("cookies") or getattr(account, "cookies", "") or "")
    for part in cookies.split(";"):
        part = part.strip()
        if part.startswith("oai-did="):
            value = part[len("oai-did=") :].strip()
            if value:
                return value
    email = str(getattr(account, "email", "") or "").strip().lower()
    if not email:
        return ""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"chatgpt-plus-check:{email}"))


def _probe_accounts_check(
    access_token: str, account_id: str, device_id: str, proxy: Optional[str]
) -> ProbeHTTPResult:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": BROWSER_USER_AGENT,
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    if device_id:
        headers["OAI-Device-Id"] = device_id
    return _perform_get(CHATGPT_ACCOUNTS_CHECK_URL, headers=headers, proxy=proxy)


def _pick_account_entry(accounts: Any) -> dict[str, Any]:
    if not isinstance(accounts, dict) or not accounts:
        return {}
    entry = accounts.get("default")
    if isinstance(entry, dict):
        return entry
    for value in accounts.values():
        if isinstance(value, dict):
            return value
    return {}


def classify_plus_trial(result: ProbeHTTPResult) -> dict[str, Any]:
    """把 accounts/check 的响应翻译成试用资格结论。

    401/403 一定要读响应体：账号被封时 access_token 会一起被吊销，请求在这里就
    被拒了，走不到 200 分支里的 is_deactivated 判据。未过期却失效 = 被吊销，
    而 OpenAI 会在响应体里写明原因，所以按措辞区分"封号"和"单纯的凭证失效"。
    """
    verdict: dict[str, Any] = {
        "status": "error",
        "plan": "",
        "promo_id": "",
        "http_status": result.status_code,
        "message": result.message,
    }

    if result.status_code in (401, 403):
        if looks_like_banned_response(result.body_text) or is_account_deactivated_message(
            result.error_code, result.message
        ):
            verdict["status"] = "banned"
        elif result.status_code == 401:
            verdict["status"] = "token_invalid"
        return verdict

    if result.status_code != 200:
        return verdict

    entry = _pick_account_entry(result.body_json.get("accounts"))
    if not entry:
        verdict["message"] = "响应里没有账户数据"
        return verdict

    account_info = entry.get("account") if isinstance(entry.get("account"), dict) else {}
    entitlement = entry.get("entitlement") if isinstance(entry.get("entitlement"), dict) else {}
    promos = (
        entry.get("eligible_promo_campaigns")
        if isinstance(entry.get("eligible_promo_campaigns"), dict)
        else {}
    )

    plan = _normalize_plan_type(str(account_info.get("plan_type") or ""), "")
    verdict["plan"] = plan
    verdict["message"] = ""

    if account_info.get("is_deactivated"):
        verdict["status"] = "banned"
        return verdict

    plus_promo = promos.get("plus") if isinstance(promos.get("plus"), dict) else {}
    promo_id = str(plus_promo.get("id") or "").strip()
    verdict["promo_id"] = promo_id

    if plan in _SUBSCRIBED_PLANS or entitlement.get("has_active_subscription"):
        verdict["status"] = "plus_active"
    elif promo_id == PLUS_TRIAL_PROMO_ID:
        verdict["status"] = "trial_eligible"
    else:
        verdict["status"] = "free"
    return verdict


def probe_plus_trial_status(account: Any, proxy: Optional[str] = None) -> dict[str, Any]:
    """查这个号还能不能领首月免费的 Plus 试用。"""
    checked_at = _utcnow_iso()
    extra = getattr(account, "extra", {}) or {}
    access_token = str(
        extra.get("access_token")
        or getattr(account, "access_token", "")
        or getattr(account, "token", "")
        or ""
    ).strip()

    if not access_token:
        verdict = {
            "status": "no_access_token",
            "plan": "",
            "promo_id": "",
            "http_status": 0,
            "message": "账号缺少 access_token",
        }
    else:
        try:
            result = _probe_accounts_check(
                access_token,
                account_id=extract_chatgpt_account_id(account),
                device_id=_extract_oai_device_id(account),
                proxy=proxy,
            )
        except Exception as error:  # noqa: BLE001 - 网络问题不该冒充检测结论
            verdict = {
                "status": "error",
                "plan": "",
                "promo_id": "",
                "http_status": 0,
                "message": f"请求失败：{error}"[:500],
            }
        else:
            verdict = classify_plus_trial(result)

    verdict["label"] = PLUS_TRIAL_LABELS.get(verdict["status"], verdict["status"])
    verdict["checked_at"] = checked_at
    return verdict


def probe_local_chatgpt_status(account: Any, proxy: Optional[str] = None) -> dict[str, Any]:
    checked_at = _utcnow_iso()
    extra = getattr(account, "extra", {}) or {}
    access_token = str(
        extra.get("access_token")
        or getattr(account, "access_token", "")
        or getattr(account, "token", "")
        or ""
    ).strip()
    refresh_token = str(extra.get("refresh_token") or getattr(account, "refresh_token", "") or "").strip()
    session_token = str(extra.get("session_token") or getattr(account, "session_token", "") or "").strip()
    account_id = extract_chatgpt_account_id(account)

    result: dict[str, Any] = {
        "version": 1,
        "checked_at": checked_at,
        "auth": {
            "state": "unknown",
            "checked_at": checked_at,
            "source": "backend_me",
            "http_status": 0,
            "error_code": "",
            "message": "",
            "refresh_available": bool(refresh_token or session_token),
        },
        "subscription": {
            "plan": "unknown",
            "checked_at": checked_at,
            "source": "backend_me",
            "workspace_plan_type": "",
            "subscription_active_until": "",
            "chatgpt_account_id": account_id,
        },
        "codex": {
            "state": "not_checked",
            "checked_at": checked_at,
            "source": "wham_usage",
            "http_status": 0,
            "error_code": "",
            "message": "",
            "chatgpt_account_id": account_id,
        },
    }

    if not access_token:
        result["auth"].update(
            {
                "state": "missing_access_token",
                "message": "账号缺少 access_token",
            }
        )
        result["codex"].update(
            {
                "state": "skipped_auth_invalid",
                "message": "缺少 access_token，跳过 Codex 探测",
            }
        )
        return result

    me_result = _probe_backend_me(access_token, proxy=proxy)
    result["auth"].update(
        {
            "http_status": me_result.status_code,
            "error_code": me_result.error_code,
            "message": me_result.message,
        }
    )

    if me_result.status_code == 200 and me_result.body_json:
        body = me_result.body_json
        plan_type = str(body.get("plan_type") or "").strip()
        workspace_plan_type = ""
        orgs = ((body.get("orgs") or {}).get("data") if isinstance(body.get("orgs"), dict) else []) or []
        if isinstance(orgs, list):
            for org in orgs:
                if not isinstance(org, dict):
                    continue
                settings = org.get("settings") or {}
                if isinstance(settings, dict) and str(settings.get("workspace_plan_type") or "").strip():
                    workspace_plan_type = str(settings.get("workspace_plan_type") or "").strip()
                    break

        result["auth"]["state"] = "access_token_valid"
        result["subscription"].update(
            {
                "plan": _normalize_plan_type(plan_type, workspace_plan_type),
                "workspace_plan_type": workspace_plan_type,
                "subscription_active_until": str(
                    body.get("chatgpt_subscription_active_until")
                    or body.get("subscription_active_until")
                    or ""
                ).strip(),
            }
        )

        if not account_id:
            result["codex"].update(
                {
                    "state": "probe_failed",
                    "message": "缺少 Chatgpt-Account-Id，无法严格探测 Codex 状态",
                }
            )
            return result

        codex_result = _probe_codex_usage(access_token, account_id=account_id, proxy=proxy)
        result["codex"].update(
            {
                "http_status": codex_result.status_code,
                "error_code": codex_result.error_code,
                "message": codex_result.message,
            }
        )
        if codex_result.status_code == 200:
            result["codex"]["state"] = "usable"
        elif codex_result.status_code == 401:
            if codex_result.error_code == "token_invalidated":
                result["codex"]["state"] = "access_token_invalidated"
            else:
                result["codex"]["state"] = "unauthorized"
        elif is_account_deactivated_message(codex_result.error_code, codex_result.message):
            result["codex"]["state"] = "account_deactivated"
        elif codex_result.status_code in (402, 403):
            result["codex"]["state"] = "payment_required"
        elif codex_result.status_code == 429:
            result["codex"]["state"] = "quota_exhausted"
        else:
            result["codex"]["state"] = "probe_failed"
        return result

    if me_result.status_code == 401:
        result["auth"]["state"] = (
            "access_token_invalidated"
            if me_result.error_code == "token_invalidated"
            else "unauthorized"
        )
        result["codex"].update(
            {
                "state": "skipped_auth_invalid",
                "message": "本地 access_token 未通过 /backend-api/me 校验，跳过 Codex 探测",
            }
        )
        return result

    if me_result.status_code == 403:
        result["auth"]["state"] = (
            "account_deactivated"
            if is_account_deactivated_message(me_result.error_code, me_result.message)
            else "banned_like"
        )
        result["codex"].update(
            {
                "state": "skipped_auth_invalid",
                "message": "本地 access_token 被拒绝，跳过 Codex 探测",
            }
        )
        return result

    result["auth"]["state"] = "probe_failed"
    result["codex"].update(
        {
            "state": "not_checked",
            "message": "本地认证探测失败，未执行 Codex 探测",
        }
    )
    return result
