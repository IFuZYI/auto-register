"""CLIProxyAPI 只读状态同步。"""

from __future__ import annotations

import base64
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from platforms.chatgpt.status_probe import CODEX_USER_AGENT, extract_chatgpt_account_id
from services.chatgpt_account_state import is_account_deactivated_message

DEFAULT_CLIPROXYAPI_BASE_URL = "http://127.0.0.1:8317"
SYNC_RETRY_ATTEMPTS = 3
SYNC_RETRY_DELAY_SECONDS = 0.4
BATCH_PROBE_DELAY_SECONDS = 0.12

logger = logging.getLogger(__name__)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


#: 本模块读配置时的别名映射：规范键 → 备选键。
#:
#: `config_store.get_all()` 会把重复服务的别名折叠进规范键
#: （`cliproxyapi_base_url` → `cpa_api_url`），但**单键 `get()` 不做折叠** ——
#: 本模块历史上读的是别名键，于是用户在「面板配置」里填的 `cpa_api_url`
#: 读不到，回落到 `http://127.0.0.1:8317` 默认值。实测后果：面板对比能连通
#: （它读的是规范键），而「同步 CLIProxyAPI 状态」报「无法连接」——
#: 同一个服务两个结果，排查方向完全被带偏。
#:
#: 所以这里自己兜一层：规范键优先，读不到再试别名键。
_CONFIG_ALIASES: dict[str, tuple[str, ...]] = {
    "cpa_api_url": ("cliproxyapi_base_url",),
    "cpa_api_key": ("cliproxyapi_management_key",),
}


def _get_config_value(key: str, default: str = "") -> str:
    """读配置，带别名兜底（见 `_CONFIG_ALIASES` 的说明）。"""
    try:
        from core.config_store import config_store

        for candidate in (key, *_CONFIG_ALIASES.get(key, ())):
            value = str(config_store.get(candidate, "") or "").strip()
            if value:
                return value
        return default
    except Exception:
        return default


def _base_url(api_url: str | None = None) -> str:
    return str(api_url or _get_config_value("cpa_api_url", DEFAULT_CLIPROXYAPI_BASE_URL) or DEFAULT_CLIPROXYAPI_BASE_URL).rstrip("/")


def _api_key(api_key: str | None = None) -> str:
    return str(api_key or _get_config_value("cpa_api_key", "cliproxyapi") or "cliproxyapi").strip()


def _headers(api_key: str | None = None) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {_api_key(api_key)}",
        "Content-Type": "application/json",
    }


def _parse_json_text(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_header_error_json(headers: dict[str, Any]) -> dict[str, Any]:
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
    return _parse_json_text(decoded)


def _extract_error_code(headers: dict[str, Any], body_json: dict[str, Any], header_error_json: dict[str, Any]) -> str:
    for key in ("X-Openai-Ide-Error-Code", "x-openai-ide-error-code"):
        value = headers.get(key)
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


def _request_json(
    method: str,
    path: str,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    json_body: dict | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    import requests
    import urllib3

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    target = f"{_base_url(api_url)}{path}"
    try:
        response = requests.request(
            method,
            target,
            headers=_headers(api_key),
            json=json_body,
            params=params,
            timeout=30,
            verify=False,
        )
    except requests.exceptions.ConnectionError as exc:
        raise RuntimeError(f"CLIProxyAPI 无法连接，请确认服务已启动或 API URL 是否正确：{_base_url(api_url)}") from exc
    except requests.exceptions.Timeout as exc:
        raise RuntimeError(f"CLIProxyAPI 请求超时：{_base_url(api_url)}") from exc
    response.raise_for_status()
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return response.text


def _is_retryable_sync_error(exc: Exception) -> bool:
    text = str(exc or "").strip().lower()
    if not text:
        return False
    markers = (
        "无法连接",
        "请求超时",
        "connection",
        "timeout",
        "timed out",
    )
    return any(marker in text for marker in markers)


def _retry_sync_call(func, *, attempts: int = SYNC_RETRY_ATTEMPTS):
    last_error = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return func()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts or not _is_retryable_sync_error(exc):
                raise
            time.sleep(SYNC_RETRY_DELAY_SECONDS)
    if last_error is not None:
        raise last_error
    raise RuntimeError("sync retry failed without captured error")


def list_auth_files(*, api_url: str | None = None, api_key: str | None = None) -> list[dict[str, Any]]:
    data = _request_json("GET", "/v0/management/auth-files", api_url=api_url, api_key=api_key)
    files = data.get("files", []) if isinstance(data, dict) else []
    return [item for item in files if isinstance(item, dict)]


def get_auth_file_credentials(
    name: str,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """按文件名取**完整**凭证（含 access_token / refresh_token / id_token）。

    列表接口（`/v0/management/auth-files`）**不返回凭证** —— 实测只有
    id_token 的摘要（plan_type 等）与时间戳，AT/RT 都不在里面。要按凭证比对
    "本地和远端是否同步"，必须走这个 download 端点。

    `name` 取自列表项的 `name` 字段（形如 `xxx@icloud.com.json`）。
    实测单次 ~94ms，按账号数线性增长 —— 调用方要控制条数（见
    `panel_comparison.fetch_cpa_remote_accounts` 的凭证抓取上限）。
    """
    data = _request_json(
        "GET",
        "/v0/management/auth-files/download",
        api_url=api_url,
        api_key=api_key,
        params={"name": str(name or "").strip()},
    )
    return data if isinstance(data, dict) else {}


def _status_rank(status: str) -> int:
    order = {
        "active": 0,
        "refreshing": 1,
        "pending": 2,
        "error": 3,
        "disabled": 4,
    }
    return order.get(str(status or "").strip().lower(), 9)


def _match_auth_file(
    account: Any,
    files: list[dict[str, Any]],
    *,
    provider: str = "codex",
) -> dict[str, Any] | None:
    """在 auth-files 里找这个账号对应的那条记录。

    `provider` 指定号池类型：ChatGPT 是 `codex`、Grok 是 `xai`。CPA 面板同时
    托管两类凭据，不指定的话一个 Grok 账号可能匹配到同邮箱的 codex 记录
    （邮箱相同、平台不同），同步结果就全错了。
    """
    email = str(getattr(account, "email", "") or "").strip().lower()
    if not email:
        return None
    want = str(provider or "").strip().lower()
    candidates = []
    for item in files:
        item_provider = str(item.get("provider") or item.get("type") or "").strip().lower()
        item_email = str(item.get("email") or "").strip().lower()
        item_name = str(item.get("name") or "").strip().lower()
        if want and item_provider != want:
            continue
        if item_email == email or item_name == f"{email}.json":
            candidates.append(item)
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            _status_rank(item.get("status", "")),
            str(item.get("updated_at") or item.get("modtime") or item.get("created_at") or ""),
        ),
        reverse=False,
    )
    return candidates[0]


def _credential_rejection_result(exc: Exception, checked_at: str) -> Optional[dict[str, Any]]:
    """CPA 管理接口 4xx（如 `auth token refresh failed`）→ 探活结果。

    这是「账号凭证被 CPA 拒绝」而不是「CPA 连不上」：HTTP 层是通的，CPA
    明确回了错误体。实测（线上 32 个 xai 账号里 21 个）之前统一标成
    `unreachable`，批量结果全部显示「无法连接」，排查方向被带偏 ——
    真实原因是这些账号的 RT 已死（CPA 侧 `status: error / invalid grant`）。

    返回 None 表示不是凭证类拒绝（非 HTTP 错误 / 非 4xx / 错误体不含凭证
    措辞），调用方按原逻辑抛给上层。识别的是 CPA 的三种文案：
    `auth token refresh failed` / `auth token not found` /
    `auth credential not found for auth_index`（见 CLIProxyAPI
    `internal/api/handlers/management/api_tools.go`）。
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    status_code = int(getattr(response, "status_code", 0) or 0)
    if not (400 <= status_code < 500):
        return None

    message = ""
    try:
        body = response.json()
        if isinstance(body, dict):
            message = str(body.get("error") or "").strip()
    except Exception:
        pass
    if not message:
        message = str(getattr(response, "text", "") or "").strip()
    if "auth token" not in message and "auth credential" not in message:
        return None

    return {
        "last_probe_at": checked_at,
        "last_probe_status_code": status_code,
        "last_probe_error_code": "",
        "last_probe_message": message,
        "remote_state": "credential_error",
    }


#: 同步结果里视为「需要人工处理」的远端状态 —— 这些状态下账号不计同步成功。
#: 三个调用点（chatgpt 插件 / grok 插件 / 批量动作）共用 `is_sync_ok`，
#: 口径只在这里维护一次。
SYNC_FAILED_STATES = frozenset({"unreachable", "not_found", "credential_error"})


def is_sync_ok(sync_result: dict[str, Any]) -> bool:
    """同步的一行是否算成功（批量结果的成功/失败计数用）。

    失败口径：远端没有该账号（`not_found`）、CPA 连不上（`unreachable`）、
    账号凭证被拒（`credential_error`）—— 三者都要用户去处理。
    """
    if not isinstance(sync_result, dict):
        return False
    if not bool(sync_result.get("uploaded")):
        return False
    state = str(sync_result.get("remote_state") or "").strip().lower()
    return state not in SYNC_FAILED_STATES


def _probe_remote_auth(auth_index: str, account_id: str, *, api_url: str | None = None, api_key: str | None = None) -> dict[str, Any]:
    checked_at = _utcnow_iso()
    if not auth_index:
        return {
            "last_probe_at": checked_at,
            "last_probe_status_code": 0,
            "last_probe_error_code": "",
            "last_probe_message": "缺少 auth_index，无法探测远端额度状态",
            "remote_state": "probe_skipped",
        }
    if not account_id:
        return {
            "last_probe_at": checked_at,
            "last_probe_status_code": 0,
            "last_probe_error_code": "",
            "last_probe_message": "缺少 Chatgpt-Account-Id，无法严格探测远端额度状态",
            "remote_state": "probe_skipped",
        }

    data = _request_json(
        "POST",
        "/v0/management/api-call",
        api_url=api_url,
        api_key=api_key,
        json_body={
            "authIndex": auth_index,
            "method": "GET",
            "url": "https://chatgpt.com/backend-api/wham/usage",
            "header": {
                "Authorization": "Bearer $TOKEN$",
                "Content-Type": "application/json",
                "User-Agent": CODEX_USER_AGENT,
                "Chatgpt-Account-Id": account_id,
            },
        },
    )

    upstream_status = int((data or {}).get("status_code") or 0)
    headers = (data or {}).get("header") or {}
    body_text = str((data or {}).get("body") or "")
    body_json = _parse_json_text(body_text)
    header_error_json = _parse_header_error_json(headers)
    error_code = _extract_error_code(headers, body_json, header_error_json)
    message = _extract_error_message(body_json, header_error_json, body_text, upstream_status)

    remote_state = "probe_failed"
    if upstream_status == 200:
        remote_state = "usable"
    elif upstream_status == 401:
        remote_state = "access_token_invalidated" if error_code == "token_invalidated" else "unauthorized"
    elif is_account_deactivated_message(error_code, message):
        remote_state = "account_deactivated"
    elif upstream_status in (402, 403):
        remote_state = "payment_required"
    elif upstream_status == 429:
        remote_state = "quota_exhausted"

    return {
        "last_probe_at": checked_at,
        "last_probe_status_code": upstream_status,
        "last_probe_error_code": error_code,
        "last_probe_message": message,
        "remote_state": remote_state,
    }


def _build_remote_sync_result(
    account: Any,
    matched: dict[str, Any] | None,
    synced_at: str,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    if not matched:
        return {
            "uploaded": False,
            "last_synced_at": synced_at,
            "message": "未在 CLIProxyAPI 找到匹配的 Codex auth-file",
            "remote_state": "not_found",
            "base_url": _base_url(api_url),
        }

    account_id = extract_chatgpt_account_id(account)
    remote = {
        "uploaded": True,
        "last_synced_at": synced_at,
        "message": "",
        "base_url": _base_url(api_url),
        "auth_index": str(matched.get("auth_index") or "").strip(),
        "name": str(matched.get("name") or "").strip(),
        "provider": str(matched.get("provider") or matched.get("type") or "").strip(),
        "status": str(matched.get("status") or "").strip(),
        "status_message": str(matched.get("status_message") or "").strip(),
        "unavailable": bool(matched.get("unavailable")),
        "disabled": bool(matched.get("disabled")),
        "last_refresh": str(matched.get("last_refresh") or "").strip(),
        "next_retry_after": str(matched.get("next_retry_after") or "").strip(),
        "remote_plan_type": str(((matched.get("id_token") or {}).get("plan_type") if isinstance(matched.get("id_token"), dict) else "") or "").strip(),
        "chatgpt_subscription_active_until": str(((matched.get("id_token") or {}).get("chatgpt_subscription_active_until") if isinstance(matched.get("id_token"), dict) else "") or "").strip(),
    }
    try:
        remote.update(
            _retry_sync_call(
                lambda: _probe_remote_auth(remote["auth_index"], account_id, api_url=api_url, api_key=api_key)
            )
        )
    except Exception as exc:
        # CPA 可达但拒绝账号凭证（如 RT 已死）→ 标 credential_error，
        # 不跟「连不上 CPA」混为一谈（见 `_credential_rejection_result`）。
        rejection = _credential_rejection_result(exc, synced_at)
        if rejection is not None:
            remote.update(rejection)
        else:
            remote.update(
                {
                    "last_probe_at": synced_at,
                    "last_probe_status_code": 0,
                    "last_probe_error_code": "",
                    "last_probe_message": str(exc),
                    "remote_state": "unreachable",
                    "message": str(exc),
                }
            )
        if remote.get("status") == "error" and remote.get("status_message"):
            remote["message"] = remote["status_message"]
        return remote
    if remote["status"] == "error" and remote["status_message"]:
        remote["message"] = remote["status_message"]
    elif remote["last_probe_message"]:
        remote["message"] = remote["last_probe_message"]
    return remote


def sync_chatgpt_cliproxyapi_status(
    account: Any,
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    provider: str = "codex",
) -> dict[str, Any]:
    synced_at = _utcnow_iso()
    try:
        files = _retry_sync_call(lambda: list_auth_files(api_url=api_url, api_key=api_key))
    except Exception as exc:
        return {
            "uploaded": False,
            "last_synced_at": synced_at,
            "message": str(exc),
            "remote_state": "unreachable",
            "base_url": _base_url(api_url),
        }
    matched = _match_auth_file(account, files, provider=provider)
    return _build_remote_sync_result(account, matched, synced_at, api_url=api_url, api_key=api_key)


#: Grok（xai）账号探活用的上游端点。
#:
#: 与 ChatGPT 侧走 `chatgpt.com/backend-api/wham/usage` 同理：经 CPA 的
#: `api-call` 用该账号自己的 token 打一次上游。选 `/v1/models` 而不是
#: chat 端点 —— 它便宜（不消耗额度）、只要 token 有效就 200，实测
#: 新号返回 200 且带模型列表。
_GROK_PROBE_URL = "https://cli-chat-proxy.grok.com/v1/models"
_GROK_PROBE_UA = "grok-shell/1.0.40 (linux; x86_64)"


def _probe_grok_remote_auth(auth_index: str, *, api_url: str | None = None, api_key: str | None = None) -> dict[str, Any]:
    """经 CPA 的 `api-call` 探测 Grok 账号在上游的状态。"""
    checked_at = _utcnow_iso()
    if not auth_index:
        return {
            "last_probe_at": checked_at,
            "last_probe_status_code": 0,
            "last_probe_error_code": "",
            "last_probe_message": "缺少 auth_index，无法探测远端状态",
            "remote_state": "probe_skipped",
        }
    data = None
    try:
        data = _request_json(
            "POST",
            "/v0/management/api-call",
            api_url=api_url,
            api_key=api_key,
            json_body={
                "authIndex": auth_index,
                "method": "GET",
                "url": _GROK_PROBE_URL,
                "header": {
                    "Authorization": "Bearer $TOKEN$",
                    "User-Agent": _GROK_PROBE_UA,
                    "x-grok-client-version": "1.0.40",
                    "x-grok-client-identifier": "grok-shell",
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - 转成探活结果，由调用方决定标签
        rejection = _credential_rejection_result(exc, checked_at)
        if rejection is not None:
            return rejection
        raise
    upstream_status = int((data or {}).get("status_code") or 0)
    body_text = str((data or {}).get("body") or "")
    body_json = _parse_json_text(body_text)
    error_code = str((body_json or {}).get("code") or "")
    message = str((body_json or {}).get("error") or body_text or "").strip()[:500]

    remote_state = "probe_failed"
    if upstream_status == 200:
        remote_state = "usable"
    elif upstream_status == 401:
        remote_state = "access_token_invalidated"
    elif upstream_status in (402, 403):
        # 与 ChatGPT 侧同口径：没额度仍算「账号有效」，不是失效。
        remote_state = "payment_required"
    elif upstream_status == 429:
        remote_state = "quota_exhausted"

    return {
        "last_probe_at": checked_at,
        "last_probe_status_code": upstream_status,
        "last_probe_error_code": error_code,
        "last_probe_message": message,
        "remote_state": remote_state,
    }


def sync_grok_cliproxyapi_status_batch(
    accounts: list[Any],
    *,
    api_url: str | None = None,
    api_key: str | None = None,
) -> dict[int, dict[str, Any]]:
    """Grok 账号的 CPA 状态同步（按 `xai` 记录匹配 + 探活）。

    与 ChatGPT 版的差别：匹配 provider 固定 `xai`，探活走
    `_probe_grok_remote_auth`（Grok 的额度端点与 ChatGPT 完全不同）。
    """
    synced_at = _utcnow_iso()
    results: dict[int, dict[str, Any]] = {}
    if not accounts:
        return results

    try:
        files = _retry_sync_call(lambda: list_auth_files(api_url=api_url, api_key=api_key))
    except Exception as exc:
        fallback = {
            "uploaded": False,
            "last_synced_at": synced_at,
            "message": str(exc),
            "remote_state": "unreachable",
            "base_url": _base_url(api_url),
        }
        for account in accounts:
            account_id = getattr(account, "id", None)
            if account_id is not None:
                results[int(account_id)] = dict(fallback)
        logger.warning("CLIProxyAPI Grok 批量同步失败：无法获取 auth-files, accounts=%s, error=%s", len(accounts), exc)
        return results

    for index, account in enumerate(accounts):
        account_id = getattr(account, "id", None)
        if account_id is None:
            continue
        matched = _match_auth_file(account, files, provider="xai")
        if not matched:
            results[int(account_id)] = {
                "uploaded": False,
                "last_synced_at": synced_at,
                "message": "未在 CLIProxyAPI 找到匹配的 xai auth-file",
                "remote_state": "not_found",
                "base_url": _base_url(api_url),
            }
            continue
        remote = {
            "uploaded": True,
            "last_synced_at": synced_at,
            "message": "",
            "base_url": _base_url(api_url),
            "auth_index": str(matched.get("auth_index") or "").strip(),
            "name": str(matched.get("name") or "").strip(),
            "provider": str(matched.get("provider") or matched.get("type") or "").strip(),
            "status": str(matched.get("status") or "").strip(),
            "status_message": str(matched.get("status_message") or "").strip(),
            "unavailable": bool(matched.get("unavailable")),
            "disabled": bool(matched.get("disabled")),
            "last_refresh": str(matched.get("last_refresh") or "").strip(),
        }
        try:
            remote.update(
                _retry_sync_call(
                    lambda: _probe_grok_remote_auth(remote["auth_index"], api_url=api_url, api_key=api_key)
                )
            )
        except Exception as exc:  # noqa: BLE001 - 探活失败不该毁整次同步
            remote.update(
                {
                    "last_probe_at": synced_at,
                    "last_probe_status_code": 0,
                    "last_probe_error_code": "",
                    "last_probe_message": str(exc),
                    "remote_state": "unreachable",
                    "message": str(exc),
                }
            )
        if remote.get("status") == "error" and remote.get("status_message"):
            remote["message"] = remote["status_message"]
        elif remote.get("last_probe_message"):
            remote["message"] = remote["last_probe_message"]
        results[int(account_id)] = remote
        if index < len(accounts) - 1:
            time.sleep(BATCH_PROBE_DELAY_SECONDS)

    logger.info(
        "CLIProxyAPI Grok 批量同步完成：accounts=%s, base_url=%s",
        len(results),
        _base_url(api_url),
    )
    return results


def sync_chatgpt_cliproxyapi_status_batch(
    accounts: list[Any],
    *,
    api_url: str | None = None,
    api_key: str | None = None,
    provider: str = "codex",
) -> dict[int, dict[str, Any]]:
    synced_at = _utcnow_iso()
    results: dict[int, dict[str, Any]] = {}
    if not accounts:
        return results

    try:
        files = _retry_sync_call(lambda: list_auth_files(api_url=api_url, api_key=api_key))
    except Exception as exc:
        fallback = {
            "uploaded": False,
            "last_synced_at": synced_at,
            "message": str(exc),
            "remote_state": "unreachable",
            "base_url": _base_url(api_url),
        }
        for account in accounts:
            account_id = getattr(account, "id", None)
            if account_id is not None:
                results[int(account_id)] = dict(fallback)
        logger.warning("CLIProxyAPI 批量同步失败：无法获取 auth-files, accounts=%s, error=%s", len(accounts), exc)
        return results

    for index, account in enumerate(accounts):
        account_id = getattr(account, "id", None)
        if account_id is None:
            continue
        matched = _match_auth_file(account, files, provider=provider)
        results[int(account_id)] = _build_remote_sync_result(account, matched, synced_at, api_url=api_url, api_key=api_key)
        if index < len(accounts) - 1 and matched:
            time.sleep(BATCH_PROBE_DELAY_SECONDS)

    unreachable = sum(1 for item in results.values() if str(item.get("remote_state") or "").strip().lower() == "unreachable")
    not_found = sum(1 for item in results.values() if str(item.get("remote_state") or "").strip().lower() == "not_found")
    logger.info(
        "CLIProxyAPI 批量同步完成：accounts=%s, unreachable=%s, not_found=%s, base_url=%s",
        len(results),
        unreachable,
        not_found,
        _base_url(api_url),
    )
    return results
