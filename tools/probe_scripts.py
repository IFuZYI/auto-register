"""看真页面到底加载了哪些第三方脚本 —— 重点是 Castle（castle.io）。

现象：点「Sign up」**一个请求都不发**（上一支探针 `--- captured ---` 是空的），
而同一个会话里直接 `fetch('/api/auth/send-verification-code')` 回 `{"ok":true}`。
说明：服务端没问题，是**页面的提交处理没跑**。最常见的原因是它在等一个
反机器人 SDK 的令牌（Castle），而那个 SDK 没加载成功 —— 没令牌就静默返回。

这支探针列出所有外部请求的域名，以及 castle 相关请求的成败。
"""
import sys
from collections import Counter

sys.path.insert(0, "/data/workspace/Register")

from camoufox.sync_api import Camoufox  # noqa: E402

PROXY = sys.argv[1] if len(sys.argv) > 1 else ""
kw = {"headless": True, "locale": ["en-US", "en"], "humanize": True, "os": ["windows"]}
if PROXY:
    from core.proxy_utils import build_playwright_proxy_config

    cfg = build_playwright_proxy_config(PROXY) or {}
    if cfg:
        kw["proxy"] = cfg

hosts = Counter()
castle = []
failed = []


def on_request(req):
    try:
        from urllib.parse import urlparse

        h = urlparse(req.url).hostname or ""
        hosts[h] += 1
        if "castle" in h.lower() or "castle" in req.url.lower():
            castle.append(("REQ", req.url[:120]))
    except Exception:
        pass


def on_response(resp):
    try:
        if "castle" in resp.url.lower():
            castle.append(("RESP", resp.status, resp.url[:120]))
    except Exception:
        pass


def on_failed(req):
    try:
        failed.append((req.url[:110], req.failure or ""))
    except Exception:
        pass


with Camoufox(**kw) as browser:
    page = browser.new_page()
    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", on_failed)
    page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(12000)
    print("--- hosts ---")
    for h, n in hosts.most_common(25):
        print(f"  {h:45} {n}")
    print("--- castle ---")
    for c in castle[:20]:
        print("  ", c)
    print("--- failed ---")
    for f in failed[:15]:
        print("  ", f)
    print("--- iframes / turnstile ---")
    print(page.evaluate("""() => ({
      iframes: Array.from(document.querySelectorAll('iframe')).map(f => (f.src||'').slice(0,90)),
      turnstileKeys: Object.keys(window).filter(k => /turnstile/i.test(k)),
      hasTurnstile: typeof window.turnstile !== 'undefined',
      cfInputs: Array.from(document.querySelectorAll('input[name=\"cf-turnstile-response\"]')).map(n => (n.value||'').length),
    })"""))
    print("--- window.castle ---")
    print(page.evaluate("() => ({keys: Object.keys(window).filter(k=>/castle/i.test(k)), hasCastle: typeof window.Castle !== 'undefined', hasCreate: !!(window.Castle && window.Castle.createRequestToken)})"))
