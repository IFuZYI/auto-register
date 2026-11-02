"""抓真页面点「Sign up」时**实际发出去的请求**，并对比直接 fetch。

上一支探针证明：同一个会话里直接 `fetch('/api/auth/send-verification-code')`
四个域名全回 `{"ok":true}`。也就是说服务端**不拒**这个出口、不拒这些域名。
那么 `account:email-signup-unavailable` 只可能来自**页面自己发的那一发**——
body / header / 端点里有东西不一样。

这支探针把那一发原样抓下来（URL、body、关键 header、响应），不做任何猜测。
"""
import json
import sys

sys.path.insert(0, "/data/workspace/Register")

from camoufox.sync_api import Camoufox  # noqa: E402
from platforms.grok import signup_pages as sp  # noqa: E402

PROXY = sys.argv[1] if len(sys.argv) > 1 else ""
EMAIL = sys.argv[2] if len(sys.argv) > 2 else "probe.capture.9911@icloud.com"

kw = {"headless": True, "locale": ["en-US", "en"], "humanize": True, "os": ["windows"]}
if PROXY:
    from core.proxy_utils import build_playwright_proxy_config

    cfg = build_playwright_proxy_config(PROXY) or {}
    if cfg:
        kw["proxy"] = cfg

seen = []


def on_request(req):
    try:
        if "/api/auth/" not in req.url:
            return
        if req.method.upper() != "POST":
            return
        hdrs = dict(req.headers or {})
        keep = {k: v for k, v in hdrs.items()
                if k.lower() in ("content-type", "origin", "referer", "x-castle-request-token",
                                 "x-requested-with", "user-agent", "accept", "x-xai-request-id",
                                 "x-client-version", "authorization")}
        seen.append({"url": req.url.split("?")[0], "body": (req.post_data or "")[:400], "headers": keep})
    except Exception:
        pass


def on_response(resp):
    try:
        if "/api/auth/" not in resp.url:
            return
        if resp.request.method.upper() != "POST":
            return
        try:
            body = resp.text()[:300]
        except Exception:
            body = "<no body>"
        seen.append({"RESPONSE": resp.url.split("?")[0], "status": resp.status, "body": body})
    except Exception:
        pass


with Camoufox(**kw) as browser:
    page = browser.new_page()
    page.on("request", on_request)
    page.on("response", on_response)
    page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    print("open_email_form:", sp.open_email_form(page, log=print))
    page.wait_for_timeout(1200)
    field = page.locator(sp.EMAIL_INPUT).first
    field.click()
    page.keyboard.type(EMAIL, delay=55)
    page.keyboard.press("Tab")
    page.wait_for_timeout(2500)
    st = sp.submit_button_state(page)
    print("button:", st)
    label, state = sp.submit_email_code(page)
    print("submit:", state.get("how"), state.get("label"))
    page.wait_for_timeout(9000)
    print("page text:", sp.page_text(page)[:200].replace("\n", " | "))
    print("--- captured ---")
    for item in seen:
        print(json.dumps(item, ensure_ascii=False)[:600])
