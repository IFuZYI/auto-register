"""AWS Builder ID 的 token 刷新与测活（纯协议，两步都是 OIDC 标准端点）。

刷新：`POST {OIDC}/token` + `grant_type=refresh_token`。
判读（与 `reference/register/kirox/internal/core/verify.go` 一致）：

* **200** —— 凭证有效；
* **400** —— 典型是 `invalid_grant`（RT 被吊销/过期）→ 账号失效，可重登；
* **403** —— 上游明确拒绝（封禁/停用）→ 不该进重试队列；
* 其它/网络异常 —— 判**不可用但不定性**（调用方不要据此落 banned）。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from .constants import HTTP_TIMEOUT, OIDC_BASE

logger = logging.getLogger(__name__)


def _json_of(response: Any) -> dict:
    try:
        value = response.json()
    except Exception as exc:  # noqa: BLE001 - 非 JSON 响应按空处理
        logger.debug("响应体不是 JSON（按空处理）: %s", exc)
        return {}
    return value if isinstance(value, dict) else {}


def _session(proxy: str = "", session: Any = None):
    if session is not None:
        return session, False
    from curl_cffi import requests as curl_requests

    client = curl_requests.Session(impersonate="chrome131", timeout=HTTP_TIMEOUT)
    if proxy:
        from core.proxy_utils import normalize_curl_proxy

        normalized = normalize_curl_proxy(proxy)
        client.proxies = {"http": normalized, "https": normalized}
    return client, True


def refresh_builder_id(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    proxy: str = "",
    session: Any = None,
) -> dict[str, Any]:
    """用 RT 换一对新的 AT/RT。返回 `{ok, access_token, refresh_token, expires_in, error}`。"""
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
                "grantType": "refresh_token",
            },
            headers={"Content-Type": "application/json"},
            timeout=HTTP_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 - 网络异常按刷新失败返回
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
    detail = str(data.get("error") or data.get("message") or response.text or "")[:200]
    return {"ok": False, "code": response.status_code, "error": detail}


def probe_builder_id(
    *,
    client_id: str,
    client_secret: str,
    refresh_token: str,
    proxy: str = "",
    session: Any = None,
) -> dict[str, Any]:
    """测活：刷新一次 token 即结论。返回 `{code, usable, summary, ...}`。"""
    result = refresh_builder_id(
        client_id=client_id,
        client_secret=client_secret,
        refresh_token=refresh_token,
        proxy=proxy,
        session=session,
    )
    if result.get("ok"):
        return {
            "code": 200,
            "usable": True,
            "summary": "",
            "access_token": result["access_token"],
            "refresh_token": result["refresh_token"],
            "expires_in": result["expires_in"],
        }
    code: Optional[int] = result.get("code")
    indeterminate = code is None
    return {
        "code": code,
        # 网络错误/超时 = 测不了（None），不是失效（False）—— 状态策略会跳过
        "usable": None if indeterminate else False,
        "indeterminate": indeterminate,
        "summary": str(result.get("error") or "刷新失败")[:200],
        "banned": code == 403,
    }


__all__ = ["probe_builder_id", "refresh_builder_id"]