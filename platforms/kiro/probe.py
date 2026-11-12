"""Kiro token 的刷新与测活。

Kiro 的凭据是 IdC（Builder ID）模式：`client_id` + `client_secret` + `refresh_token`
换新 AT。判读与 `platforms/aws/probe.py` 同一套（同一组 OIDC 端点）：

* 200 → 有效；
* 400 → `invalid_grant`：RT 被吊销/过期（账号失效，可重登）；
* 403 → 上游明确拒绝（封禁/停用）。

测活额外查一次 `getUsageLimits`（Kiro 后端的账号状态判据，403 即封禁）。
"""
from __future__ import annotations

import logging
from typing import Any

from platforms.aws.constants import HTTP_TIMEOUT, OIDC_BASE

from .constants import KIRO_USER_AGENT, REFRESH_TOKEN_GRANT, USAGE_URL

logger = logging.getLogger(__name__)


def _json_of(response: Any) -> dict:
    try:
        value = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.debug("响应体不是 JSON（按空处理）: %s", exc)
        return {}
    return value if isinstance(value, dict) else {}


def _session(proxy: str = "", session: Any = None):
    if session is not None:
        return session, False
    from curl_cffi import requests as curl_requests

    client = curl_requests.Session(impersonate="chrome131", timeout=HTTP_TIMEOUT)
    if proxy:
        client.proxies = {"http": proxy, "https": proxy}
    return client, True


def refresh_kiro(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    proxy: str = "",
    session: Any = None,
) -> dict[str, Any]:
    """刷新 Kiro 的 AT/RT。返回 `{ok, access_token, refresh_token, expires_in, error}`。"""
    if not refresh_token or not client_id or not client_secret:
        return {"ok": False, "error": "缺少 refresh_token / client_id / client_secret"}
    client, owned = _session(proxy, session)
    try:
        response = client.post(
            f"{OIDC_BASE}/token",
            json={
                "clientId": client_id,
                "clientSecret": client_secret,
                "refreshToken": refresh_token,
                "grantType": REFRESH_TOKEN_GRANT,
            },
            headers={"Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        if owned:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭会话失败（忽略）: %s", exc)

    data = _json_of(response)
    if response.status_code == 200 and data.get("accessToken"):
        return {
            "ok": True,
            "access_token": str(data.get("accessToken") or ""),
            "refresh_token": str(data.get("refreshToken") or refresh_token),
            "expires_in": int(data.get("expiresIn") or 0),
        }
    detail = str(data.get("error") or data.get("message") or getattr(response, "text", "") or "")[:200]
    return {"ok": False, "code": response.status_code, "error": detail}


def probe_kiro(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    proxy: str = "",
    session: Any = None,
    check_usage: bool = True,
) -> dict[str, Any]:
    """测活：刷新 token，可选再查一次用量。返回 `{code, usable, summary, ...}`。"""
    refreshed = refresh_kiro(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        proxy=proxy,
        session=session,
    )
    if not refreshed.get("ok"):
        code = refreshed.get("code")
        return {
            "code": code,
            "usable": False,
            "summary": str(refreshed.get("error") or "刷新失败")[:200],
            "banned": code == 403,
        }

    detail: dict[str, Any] = {
        "code": 200,
        "usable": True,
        "summary": "",
        "access_token": refreshed["access_token"],
        "refresh_token": refreshed["refresh_token"],
        "expires_in": refreshed["expires_in"],
    }
    if not check_usage:
        return detail

    client, owned = _session(proxy, session)
    try:
        usage = client.get(
            USAGE_URL,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {detail['access_token']}",
                "User-Agent": KIRO_USER_AGENT,
            },
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - 用量查询失败不影响「凭证有效」的结论
        detail["usage_error"] = f"{type(exc).__name__}"[:80]
        return detail
    finally:
        if owned:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("关闭会话失败（忽略）: %s", exc)

    usage_data = _json_of(usage)
    detail["usage_status"] = usage.status_code
    if usage.status_code == 403:
        detail.update(
            {
                "usable": False,
                "banned": True,
                "code": 403,
                "summary": "上游返回 403（账号被停用）",
            }
        )
    elif usage.status_code == 200:
        subscription = (usage_data.get("subscriptionInfo") or {}).get("subscriptionTitle") or ""
        if subscription:
            detail["subscription"] = str(subscription)
        breakdown = usage_data.get("usageBreakdownList") or []
        if isinstance(breakdown, list) and breakdown:
            first = breakdown[0] if isinstance(breakdown[0], dict) else {}
            detail["credit_used"] = first.get("currentUsageWithPrecision", first.get("currentUsage", 0))
            detail["credit_limit"] = first.get("usageLimitWithPrecision", first.get("usageLimit", 0))
    else:
        detail["summary"] = f"用量查询 HTTP {usage.status_code}（凭证仍有效）"
    return detail


__all__ = ["probe_kiro", "refresh_kiro"]