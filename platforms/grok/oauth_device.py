"""SSO → OAuth Device Authorization Flow。

移植自 reference/grok/grokRegister-cpa/sso_to_auth_json.py:287-535，
使用本项目 ProtocolExecutor 作为 HTTP 层。

关键约束（4 个参考项目一致）：
- CLIENT_ID = b1a00492-073a-47ea-816f-4c329264a828
- SCOPE 必须正好 6 项；加 conversations/workspaces 会 Access denied
- PKCE 被 CF 拦，Device Flow 纯 HTTP 稳定
"""
from __future__ import annotations

import json
import time
import urllib.parse
from typing import Any, Callable, Optional

from .constants import (
    CLIENT_ID,
    CPA_GROK_BASE_URL,
    CPA_GROK_HEADERS,
    CPA_TOKEN_ENDPOINT,
    DEFAULT_UA,
    DEVICE_APPROVE_URL,
    DEVICE_CODE_URL,
    DEVICE_GRANT_TYPE,
    DEVICE_VERIFY_URL,
    SCOPES,
    TOKEN_URL,
)

LogFn = Optional[Callable[[str], None]]


class _NoRedirectSession:
    """HTTP 会话：支持 allow_redirects=False。

    为什么不用项目 ProtocolExecutor：它的 post/get 不支持 allow_redirects，
    而 Device Flow 的 verify/approve 必须看 302 的 Location 才能判定授权结果
    （出处：sso_to_auth_json.py:398-461 全部用 allow_redirects=False）。
    """

    def __init__(self, proxy: str = "", impersonate: str = "chrome"):
        from curl_cffi import requests as curl_requests

        self.s = curl_requests.Session()
        self.s.impersonate = impersonate
        if proxy:
            from ..proxy_utils import build_requests_proxy_config

            self.s.proxies = build_requests_proxy_config(proxy)

    def request(self, method: str, url: str, *, headers=None, data=None, json=None,
                allow_redirects: bool = True):
        return self.s.request(
            method, url, headers=headers, data=data, json=json,
            allow_redirects=allow_redirects,
        )

    def get(self, url, *, headers=None, allow_redirects: bool = True):
        return self.s.get(url, headers=headers, allow_redirects=allow_redirects)

    def post(self, url, *, headers=None, data=None, json=None,
             allow_redirects: bool = True):
        return self.s.post(url, headers=headers, data=data, json=json,
                           allow_redirects=allow_redirects)

    def get_cookies(self) -> dict:
        return {c.name: c.value for c in self.s.cookies.jar}

    def set_cookies(self, cookies: dict) -> None:
        for k, v in (cookies or {}).items():
            try:
                self.s.cookies.set(k, v)
            except Exception:
                pass

    def set_cookie_for_domains(self, name: str, value: str, domains: tuple) -> None:
        """按域名写入 cookie。

        出处：sso_to_auth_json.py:317-319 —— 必须对 .x.ai / accounts.x.ai /
        auth.x.ai 分别 set，否则请求 auth.x.ai 时不带 sso cookie（→ 401）。
        """
        for domain in domains:
            try:
                self.s.cookies.set(name, value, domain=domain)
            except Exception:
                pass

    def close(self) -> None:
        try:
            self.s.close()
        except Exception:
            pass


def _log(fn: LogFn, msg: str) -> None:
    if fn:
        try:
            fn(msg)
        except Exception:
            pass


def _device_location_error(loc: str) -> Optional[str]:
    """从 Location 查询串里提取 error 参数。出处：sso_to_auth_json.py:255-262"""
    if not loc:
        return None
    try:
        err = urllib.parse.parse_qs(urllib.parse.urlparse(loc).query).get("error", [None])[0]
    except Exception:
        return None
    return err or None


def _decode_jwt_payload(token: str) -> dict:
    import base64

    try:
        parts = str(token or "").split(".")
        if len(parts) != 3:
            return {}
        pad = "=" * (-len(parts[1]) % 4)
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
    except Exception:
        return {}


def _form_headers(extra: Optional[dict] = None) -> dict:
    h = {
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": DEFAULT_UA,
        "Accept": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def sso_to_token(
    sso_cookie: str,
    executor: Any,
    log: LogFn = None,
    should_stop: Callable[[], bool] = None,
    proxy: str = "",
) -> Optional[dict]:
    """SSO cookie → OAuth token dict。

    executor: ProtocolExecutor 实例或 curl_cffi Session（内部会适配为
              支持 allow_redirects=False 的会话）。
    proxy: executor 未带代理时可在此指定。
    返回 {access_token, refresh_token, id_token, expires_in, token_type} 或 None。
    """
    sso_cookie = str(sso_cookie or "").strip()
    if not sso_cookie:
        return None

    def _cancelled() -> bool:
        return bool(should_stop and should_stop())

    # 注意：不使用传入的 executor 会话，因为它可能已被注册流程污染
    # （带 __cf_bm / session cookie 时访问 auth.x.ai 会 520）。
    # 下面 ③ 新建干净会话。出处：sso_to_auth_json.py:314-319
    del executor

    # 说明：不再做 "访问 accounts.x.ai 看是否跳登录页" 的 SSO 校验 ——
    # 该判定不可靠（全新会话即使带 sso cookie 也常被判未登录而跳 sign-in，
    # 会误杀有效 SSO）。直接进入 Device Flow，用实际结果判断。

    # ---- ③ 申请 device code（干净会话，只带 sso）----
    # 实测（2026-09-30 逐组合对照）：
    #   sso + sso-rw × 3 域名（6 个 cookie ≈ 18KB Cookie 头）→ CF 520
    #   sso @ .x.ai（单 cookie，父域覆盖所有子域）→ 200
    # 因此只写一个 sso cookie 到 .x.ai。
    executor = _NoRedirectSession(proxy=proxy)
    executor.set_cookie_for_domains("sso", sso_cookie, (".x.ai",))

    r = None
    for dc_try in range(1, 4):
        if _cancelled():
            return None
        try:
            r = executor.post(
                DEVICE_CODE_URL,
                data=urllib.parse.urlencode({"client_id": CLIENT_ID, "scope": SCOPES}),
                headers=_form_headers(),
            )
        except Exception as exc:
            _log(log, f"[Grok] device/code 异常: {exc}")
            if dc_try < 3:
                time.sleep(1.5 * dc_try)
                continue
            return None
        code_ = int(r.status_code or 0)
        if 200 <= code_ < 300:
            break
        # CF 520/521/522/524 等瞬时错误 → 重试
        if code_ >= 500 and dc_try < 3:
            _log(log, f"[Grok] device/code HTTP {code_}（瞬时），重试 {dc_try}/3")
            time.sleep(1.5 * dc_try)
            continue
        _log(log, f"[Grok] device/code HTTP {r.status_code}: {(r.text or '')[:200]}")
        return None
    if r is None or not (200 <= int(r.status_code or 0) < 300):
        _log(log, f"[Grok] device/code 重试耗尽 HTTP {getattr(r, 'status_code', '?')}")
        return None
    try:
        device_doc = r.json()
    except Exception:
        _log(log, f"[Grok] device/code 非 JSON: {(r.text or '')[:200]}")
        return None

    device_code = str(device_doc.get("device_code") or "").strip()
    user_code = str(device_doc.get("user_code") or "").strip()
    verification_uri = str(
        device_doc.get("verification_uri") or device_doc.get("verification_url") or ""
    ).strip()
    verification_complete = str(device_doc.get("verification_uri_complete") or "").strip()
    try:
        expires_in = int(device_doc.get("expires_in") or 600)
    except (TypeError, ValueError):
        expires_in = 600
    try:
        interval = float(device_doc.get("interval") or 5)
    except (TypeError, ValueError):
        interval = 5.0
    interval = max(interval, 1.0)

    if not device_code or not user_code:
        _log(log, f"[Grok] device/code 缺字段: {device_doc}")
        return None
    if not verification_complete:
        base = verification_uri or "https://accounts.x.ai/oauth2/device"
        sep = "&" if "?" in base else "?"
        verification_complete = f"{base}{sep}user_code={urllib.parse.quote(user_code)}"
    _log(log, f"[Grok] user_code={user_code} interval={interval:g}s")

    # ---- ④ verify（带 sso cookie）----
    verify_headers = _form_headers(
        {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://accounts.x.ai",
            "Referer": verification_complete,
            "Cookie": f"sso={sso_cookie}",
        }
    )
    try:
        r = executor.post(
            DEVICE_VERIFY_URL,
            data=urllib.parse.urlencode({"user_code": user_code}),
            headers=verify_headers,
            allow_redirects=False,
        )
    except Exception as exc:
        _log(log, f"[Grok] device/verify 异常: {exc}")
        return None
    if _cancelled():
        return None
    if int(r.status_code or 0) == 403:
        _log(log, "[Grok] device/verify 被挑战（403）")
        return None
    loc = str((r.headers or {}).get("Location") or "")
    loc_err = _device_location_error(loc)
    if loc_err:
        _log(log, f"[Grok] device/verify error={loc_err}")
        return None

    # ---- ⑤ 需要时 approve ----
    if "/oauth2/device/done" not in loc:
        consent_ref = loc
        if not consent_ref:
            consent_ref = (
                "https://accounts.x.ai/oauth2/device/consent?"
                f"user_code={urllib.parse.quote(user_code)}"
            )
        elif consent_ref.startswith("/"):
            consent_ref = "https://accounts.x.ai" + consent_ref
        approve_headers = dict(verify_headers)
        approve_headers["Referer"] = consent_ref
        try:
            r2 = executor.post(
                DEVICE_APPROVE_URL,
                data=urllib.parse.urlencode(
                    {
                        "user_code": user_code,
                        "action": "allow",
                        "principal_type": "User",
                        "principal_id": "",
                    }
                ),
                headers=approve_headers,
                allow_redirects=False,
            )
        except Exception as exc:
            _log(log, f"[Grok] device/approve 异常: {exc}")
            return None
        if _cancelled():
            return None
        a_status = int(r2.status_code or 0)
        a_loc = str((r2.headers or {}).get("Location") or "")
        aerr = _device_location_error(a_loc)
        body_l = str(r2.text or "").lower()
        ok = (
            "device authorized" in body_l
            or "设备已授权" in (r2.text or "")
            or a_status // 100 == 2
            or "device/done" in a_loc
            or (a_loc and not aerr)   # 出处：sso_to_auth_json.py:456-462
        )
        if not ok:
            _log(log, f"[Grok] device/approve 失败 status={a_status} loc={a_loc[:80]}")
            return None
    _log(log, "[Grok] 设备已授权，轮询 token")

    # ---- ⑥ 轮询 token ----
    deadline = time.time() + max(expires_in, 60)
    poll_interval = interval
    last_err = ""
    while time.time() < deadline:
        if _cancelled():
            return None
        try:
            tr = executor.post(
                TOKEN_URL,
                data=urllib.parse.urlencode(
                    {
                        "client_id": CLIENT_ID,
                        "device_code": device_code,
                        "grant_type": DEVICE_GRANT_TYPE,
                    }
                ),
                headers=_form_headers(),
            )
        except Exception as exc:
            last_err = f"token poll 异常: {exc}"
            time.sleep(poll_interval)
            continue

        try:
            doc = tr.json()
        except Exception:
            last_err = f"token 非 JSON status={tr.status_code}"
            time.sleep(poll_interval)
            continue

        if int(tr.status_code or 0) // 100 == 2 and doc.get("access_token"):
            token = dict(doc)
            token.setdefault("expires_in", 21600)
            token.setdefault("token_type", "Bearer")
            ap = _decode_jwt_payload(token["access_token"])
            _log(
                log,
                f"[Grok] access_token 就绪 expires_in={token.get('expires_in')}s "
                f"scope={ap.get('scope')!r} referrer={ap.get('referrer')!r} "
                f"bot={ap.get('bot_flag_source')!r}"
                + (" +refresh_token" if token.get("refresh_token") else ""),
            )
            return token

        err = str(doc.get("error") or "")
        if err == "authorization_pending":
            time.sleep(poll_interval)
            continue
        if err == "slow_down":
            poll_interval += 1
            time.sleep(poll_interval)
            continue
        if err in ("access_denied", "expired_token"):
            _log(log, f"[Grok] token 轮询终止: {err}")
            return None
        last_err = err or f"status={tr.status_code}"
        _log(log, f"[Grok] token 轮询失败: {last_err}")
        return None

    _log(log, f"[Grok] token 轮询超时: {last_err}")
    return None


def token_to_cpa_record(token: dict, email: str = "", sso: str = "") -> dict:
    """token dict → CLIProxyAPI 扁平 xai auth 记录。

    出处：sso_to_auth_json.py:590-636
    """
    from datetime import datetime, timezone

    from .constants import cpa_include_sso

    access = token.get("access_token") or token.get("key") or ""
    refresh = token.get("refresh_token") or ""
    id_token = token.get("id_token") or ""
    payload = _decode_jwt_payload(access)
    id_payload = _decode_jwt_payload(id_token) if id_token else {}

    if not email:
        email = id_payload.get("email") or payload.get("email") or ""
    sub = payload.get("sub") or id_payload.get("sub") or ""

    expired = ""
    if "exp" in payload:
        try:
            expired = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except Exception:
            expired = ""
    elif token.get("expires_in") is not None:
        try:
            expired = datetime.fromtimestamp(
                int(time.time()) + int(token["expires_in"]), tz=timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
        except Exception:
            expired = ""

    record = {
        "type": "xai",
        "auth_kind": "oauth",
        "email": email or "",
        "sub": sub,
        "access_token": access,
        "refresh_token": refresh,
        "id_token": id_token,
        "token_type": token.get("token_type", "Bearer"),
        "expires_in": token.get("expires_in"),
        "expired": expired,
        "last_refresh": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "token_endpoint": CPA_TOKEN_ENDPOINT,
        "base_url": CPA_GROK_BASE_URL,
        "headers": dict(CPA_GROK_HEADERS),
    }
    if str(sso or "").strip() and cpa_include_sso():
        record["sso"] = sso
    return record


def cpa_auth_filename(record: dict) -> str:
    """生成 CPA auth 文件名：xai-<email>.json。"""
    ident = str(record.get("email") or "").strip() or str(record.get("sub") or "").strip()
    safe = "".join(ch if ch.isalnum() or ch in "._-@" else "_" for ch in ident) or "unknown"
    return f"{safe if safe.lower().startswith('xai') else f'xai-{safe}'}.json"


def token_to_auth_entry(token: dict, email: str = "") -> tuple[str, dict]:
    """token → ~/.grok/auth.json 风格条目。出处：sso_to_auth_json.py:538-574"""
    from datetime import datetime, timezone

    from .constants import OIDC_ISSUER

    access = token.get("access_token") or token.get("key") or ""
    payload = _decode_jwt_payload(access)
    user_id = payload.get("sub") or payload.get("principal_id") or ""

    def _rfc3339(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    expires_in = int(token.get("expires_in") or 21600)
    expires_at = (
        _rfc3339(float(payload["exp"]))
        if "exp" in payload
        else _rfc3339(time.time() + expires_in)
    )
    iat = payload.get("iat")
    entry = {
        "key": access,
        "auth_mode": "oidc",
        "create_time": _rfc3339(float(iat) if iat else time.time()),
        "user_id": user_id,
        "email": email or "",
        "principal_type": payload.get("principal_type") or "User",
        "principal_id": payload.get("principal_id") or user_id,
        "refresh_token": token.get("refresh_token") or "",
        "expires_at": expires_at,
        "oidc_issuer": OIDC_ISSUER,
        "oidc_client_id": CLIENT_ID,
    }
    return f"{OIDC_ISSUER}::{CLIENT_ID}", entry


__all__ = [
    "sso_to_token",
    "token_to_cpa_record",
    "token_to_auth_entry",
    "cpa_auth_filename",
]
