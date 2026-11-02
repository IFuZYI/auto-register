"""点「Sign up」后**所有** POST 请求（不限路径），并等足够久。

之前只筛了 `/api/auth/`，如果应用走了别的路径（或先打一个前置接口），
就会看起来「一个请求都没发」。这支探针不筛，全打。
"""
import json
import sys

sys.path.insert(0, "/data/workspace/Register")

from camoufox.sync_api import Camoufox  # noqa: E402
from platforms.grok import signup_pages as sp  # noqa: E402

PROXY = sys.argv[1] if len(sys.argv) > 1 else ""
EMAIL = sys.argv[2] if len(sys.argv) > 2 else "probe.allpost.6655@icloud.com"

kw = {"headless": True, "locale": ["en-US", "en"], "humanize": True, "os": ["windows"]}
if PROXY:
    from core.proxy_utils import build_playwright_proxy_config

    cfg = build_playwright_proxy_config(PROXY) or {}
    if cfg:
        kw["proxy"] = cfg

posts = []


def on_request(req):
    if req.method.upper() != "POST":
        return
    posts.append({
        "url": req.url[:140],
        "body": (req.post_data or "")[:300],
    })


with Camoufox(**kw) as browser:
    page = browser.new_page()
    page.on("request", on_request)
    page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    sp.open_email_form(page, log=print)
    page.wait_for_timeout(1200)
    field = page.locator(sp.EMAIL_INPUT).first
    field.click()
    page.keyboard.type(EMAIL, delay=55)
    page.keyboard.press("Tab")
    page.wait_for_timeout(2500)
    posts.clear()
    label, state = sp.submit_email_code(page)
    print("submit:", state.get("how"), state.get("label"))
    for i in range(6):
        page.wait_for_timeout(5000)
        txt = sp.page_text(page)[:120].replace("\n", " | ")
        print(f"  +{(i+1)*5}s posts={len(posts)} text={txt}")
        if posts:
            break
    print("--- POSTs ---")
    for p in posts[:10]:
        print("  ", json.dumps(p, ensure_ascii=False)[:400])
