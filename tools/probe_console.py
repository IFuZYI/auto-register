"""点「Sign up」时页面到底报了什么错 —— 抓 console + pageerror。

已知：同一会话直接 fetch 接口回 `{"ok":true}`，但点按钮一个请求都不发。
那就是页面的 JS 在提交前就挂了（异常被吞掉，界面上看不出）。
这支探针把 console.error / uncaught exception 全打出来。
"""
import json
import sys

sys.path.insert(0, "/data/workspace/Register")

from camoufox.sync_api import Camoufox  # noqa: E402
from platforms.grok import signup_pages as sp  # noqa: E402

PROXY = sys.argv[1] if len(sys.argv) > 1 else ""
EMAIL = sys.argv[2] if len(sys.argv) > 2 else "probe.console.4422@icloud.com"

kw = {"headless": True, "locale": ["en-US", "en"], "humanize": True, "os": ["windows"]}
if PROXY:
    from core.proxy_utils import build_playwright_proxy_config

    cfg = build_playwright_proxy_config(PROXY) or {}
    if cfg:
        kw["proxy"] = cfg

logs = []

with Camoufox(**kw) as browser:
    page = browser.new_page()
    page.on("console", lambda m: logs.append(("console." + m.type, m.text[:300])))
    page.on("pageerror", lambda e: logs.append(("pageerror", str(e)[:300])))
    page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    sp.open_email_form(page, log=print)
    page.wait_for_timeout(1200)
    field = page.locator(sp.EMAIL_INPUT).first
    field.click()
    page.keyboard.type(EMAIL, delay=55)
    page.keyboard.press("Tab")
    page.wait_for_timeout(2000)
    logs.clear()
    label, state = sp.submit_email_code(page)
    page.wait_for_timeout(6000)
    print("submit:", state.get("how"), state.get("label"))
    print("--- console during submit ---")
    for kind, text in logs[:30]:
        print(f"  [{kind}] {text}")
    print("--- react props of button ---")
    print(page.evaluate("""
    () => {
      const b = Array.from(document.querySelectorAll('button')).find(n => /sign up/i.test(n.innerText||''));
      if (!b) return 'no button';
      const key = Object.keys(b).find(k => k.startsWith('__reactProps'));
      if (!key) return 'no react props';
      const p = b[key] || {};
      return {props: Object.keys(p), onClickType: typeof p.onClick, disabled: b.disabled, type: b.type};
    }"""))
