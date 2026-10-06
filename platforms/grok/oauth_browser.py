"""浏览器内完成 OAuth Device Flow 授权（拿 access/refresh token）。

为什么需要
----------
`device/verify` 与 `device/approve` 都被 CF 保护：

- 纯 HTTP（curl_cffi）：device/verify → 403、device/approve → 403（实测）
- 浏览器 ctx.request：device/code 200 ✅，但 verify/approve 仍需真实页面上下文

只有**真实浏览器导航到授权页并点击授权按钮**才能完成：
  导航 https://accounts.x.ai/oauth2/device?user_code=XXX
  → 点 "Continue" / "Allow"
  → 落地 /oauth2/device/done
  → 纯 HTTP 轮询 /oauth2/token 拿 token

出处：grok-reg/device_mint.py:5-8
  「授权: 有头 Chrome 注入 SSO cookie → 打开授权页自动点"继续/允许" → 轮询 token」
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Any, Callable, Optional

LogFn = Optional[Callable[[str], None]]


def _log(fn: LogFn, msg: str) -> None:
    if fn:
        try:
            fn(msg)
        except Exception:
            pass


# 授权按钮文案（多语言 + 多种变体）
_APPROVE_SELECTORS = (
    "button:has-text('Continue')",
    "button:has-text('Allow')",
    "button:has-text('Authorize')",
    "button:has-text('Approve')",
    "button:has-text('同意')",
    "button:has-text('允许')",
    "button:has-text('继续')",
    "button[type='submit']",
)


def oauth_device_via_browser(
    sso_cookie: str,
    *,
    proxy: str = "",
    timeout: int = 180,
    headless: bool = True,
    log: LogFn = None,
) -> Optional[dict]:
    """用浏览器完成 Device Flow 授权，返回 token dict 或 None。

    步骤：
      1. 浏览器注入 sso cookie
      2. ctx.request 申请 device code（200，浏览器网络栈）
      3. 导航授权页 → 点 Continue/Allow → /device/done
      4. 纯 HTTP 轮询 /oauth2/token
    """
    sso_cookie = str(sso_cookie or "").strip()
    if not sso_cookie:
        return None

    from .constants import (
        CLIENT_ID, DEFAULT_UA, DEVICE_CODE_URL, DEVICE_GRANT_TYPE, SCOPES,
        TOKEN_URL,
    )

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        _log(log, f"[Grok] Playwright 不可用: {exc}")
        return None

    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
    ]
    exe = ""
    try:
        from .turnstile_mint import find_chrome

        exe = find_chrome() or ""
    except Exception:
        pass

    launch_kwargs: dict[str, Any] = {"headless": headless, "args": launch_args}
    if exe:
        launch_kwargs["executable_path"] = exe
    if proxy:
        # 走统一转换：带认证的 socks5 直接塞进 {"server": ...} 会被 Chromium
        # 拒连（net::ERR_NO_SUPPORTED_PROXIES / does not support socks5 proxy
        # authentication），见 core/proxy_utils.py 的实测记录。
        from core.proxy_utils import build_playwright_proxy_config

        proxy_cfg = build_playwright_proxy_config(proxy)
        if proxy_cfg:
            launch_kwargs["proxy"] = proxy_cfg

    deadline = time.time() + max(60, timeout)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            try:
                ctx = browser.new_context(
                    user_agent=DEFAULT_UA,
                    viewport={"width": 1280, "height": 900},
                    locale="en-US",
                )
                # 注入 sso（.x.ai 覆盖全部子域）
                try:
                    ctx.add_cookies([
                        {"name": "sso", "value": sso_cookie, "domain": ".x.ai", "path": "/"},
                        {"name": "sso-rw", "value": sso_cookie, "domain": ".x.ai", "path": "/"},
                    ])
                except Exception:
                    pass

                page = ctx.new_page()

                # ---- ① device/code（ctx.request，浏览器网络栈）----
                device_code = ""
                user_code = ""
                vcomplete = ""
                try:
                    resp = ctx.request.post(
                        DEVICE_CODE_URL,
                        form={"client_id": CLIENT_ID, "scope": SCOPES},
                        timeout=25000,
                    )
                    if resp.status == 200:
                        doc = resp.json()
                        device_code = str(doc.get("device_code") or "")
                        user_code = str(doc.get("user_code") or "")
                        vcomplete = str(doc.get("verification_uri_complete") or "")
                    else:
                        _log(log, f"[Grok] device/code HTTP {resp.status}")
                except Exception as exc:
                    _log(log, f"[Grok] device/code 异常: {type(exc).__name__}")
                if not device_code:
                    return None
                _log(log, f"[Grok] device user_code={user_code}")

                # ---- ② 导航授权页 + 点授权 ----
                auth_url = vcomplete or (
                    f"https://accounts.x.ai/oauth2/device?user_code="
                    f"{urllib.parse.quote(user_code)}"
                )
                try:
                    page.goto(auth_url, wait_until="domcontentloaded", timeout=45000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=15000)
                    except Exception:
                        pass
                except Exception as exc:
                    _log(log, f"[Grok] 授权页导航异常: {type(exc).__name__}")

                approved = False
                sso_rejected = False
                for i in range(24):
                    if time.time() > deadline:
                        break
                    try:
                        cur = str(page.url)
                    except Exception:
                        cur = ""
                    if "/device/done" in cur:
                        approved = True
                        break
                    # SSO 被拒的明确信号：授权页把未登录访客重定向到
                    # sign-in —— 对齐 grok2api 的「SSO credential rejected」
                    # （该账号需要重新登录，标失效而不是普通失败）。
                    if "sign-in" in cur or "sign-up" in cur:
                        sso_rejected = True
                        _log(log, "[Grok] SSO 被上游拒绝（重定向到登录页）")
                        break
                    # 点一次授权（点错后续按钮会命中拒绝路径，故只点可见的）
                    for sel in _APPROVE_SELECTORS:
                        try:
                            btn = page.query_selector(sel)
                            if btn and btn.is_visible():
                                btn.click()
                                _log(log, f"[Grok] 点击授权按钮 {sel[:32]}")
                                time.sleep(1.5)
                                break
                        except Exception:
                            continue
                    time.sleep(1.2)
                if not approved:
                    try:
                        if "/device/done" in str(page.url):
                            approved = True
                    except Exception:
                        pass
                if sso_rejected:
                    # 明确拒绝 → 由调用方把账号标失效（需要重新登录）
                    return {"sso_rejected": True}
                _log(log, f"[Grok] 授权{'完成' if approved else '未确认，仍尝试取 token'}")
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as exc:
        _log(log, f"[Grok] 浏览器授权异常: {type(exc).__name__}: {str(exc)[:120]}")
        return None

    # ---- ③ 轮询 token（纯 HTTP）----
    try:
        from curl_cffi import requests as curl_requests

        sess = curl_requests.Session()
        sess.impersonate = "chrome"
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
    except Exception:
        sess = None

    if sess is None:
        return None

    poll_interval = 5.0
    last_err = ""
    try:
        while time.time() < deadline:
            try:
                tr = sess.post(
                    TOKEN_URL,
                    data=urllib.parse.urlencode({
                        "client_id": CLIENT_ID,
                        "device_code": device_code,
                        "grant_type": DEVICE_GRANT_TYPE,
                    }),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": DEFAULT_UA,
                        "Accept": "application/json",
                    },
                    timeout=25,
                )
                doc = tr.json()
            except Exception as exc:
                last_err = f"{type(exc).__name__}"
                time.sleep(poll_interval)
                continue

            if doc.get("access_token"):
                doc.setdefault("expires_in", 21600)
                doc.setdefault("token_type", "Bearer")
                _log(log, "[Grok] OAuth token 已获取")
                return doc

            err = str(doc.get("error") or "")
            if err == "authorization_pending":
                time.sleep(poll_interval)
                continue
            if err == "slow_down":
                poll_interval = min(poll_interval + 2, 15)
                time.sleep(poll_interval)
                continue
            last_err = f"{err}: {str(doc.get('error_description') or '')[:80]}"
            if err in ("access_denied", "expired_token"):
                break
            time.sleep(poll_interval)
    finally:
        try:
            sess.close()
        except Exception:
            pass

    _log(log, f"[Grok] token 轮询失败: {last_err}")
    return None


__all__ = ["oauth_device_via_browser"]
