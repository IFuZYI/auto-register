"""验证「cookie 弹窗挡住了提交按钮」这个假设。

线索：页面正文里一直有 `Close`（OneTrust 的 cookie 弹窗），而点「Sign up」
**一个请求都不发**。如果点击被遮罩吃掉，这就是全部原因。

做法：打开页面 → 先关掉 cookie 弹窗 → 再填邮箱 → 再点 Sign up →
抓请求。对比「关弹窗前后」有没有请求发出去。
"""
import json
import sys

sys.path.insert(0, "/data/workspace/Register")

from camoufox.sync_api import Camoufox  # noqa: E402
from platforms.grok import signup_pages as sp  # noqa: E402

PROXY = sys.argv[1] if len(sys.argv) > 1 else ""
EMAIL = sys.argv[2] if len(sys.argv) > 2 else "probe.banner.3377@icloud.com"

kw = {"headless": True, "locale": ["en-US", "en"], "humanize": True, "os": ["windows"]}
if PROXY:
    from core.proxy_utils import build_playwright_proxy_config

    cfg = build_playwright_proxy_config(PROXY) or {}
    if cfg:
        kw["proxy"] = cfg

reqs = []


def on_request(req):
    if req.method.upper() == "POST":
        reqs.append((req.url.split("?")[0][:90], (req.post_data or "")[:120]))


def on_response(resp):
    if "/api/auth/" in resp.url and resp.request.method.upper() == "POST":
        try:
            reqs.append(("RESP " + str(resp.status), resp.text()[:150]))
        except Exception:
            pass


CLICK_CLOSE = """
() => {
  const cands = Array.from(document.querySelectorAll('button,a,[role="button"]'))
    .filter(n => /^(close|accept all|allow all|got it|ok)$/i.test((n.innerText||'').trim()));
  for (const n of cands) { try { n.click(); return (n.innerText||'').trim(); } catch (e) {} }
  return '';
}
"""

with Camoufox(**kw) as browser:
    page = browser.new_page()
    page.on("request", on_request)
    page.on("response", on_response)
    page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    print("closed banner:", page.evaluate(CLICK_CLOSE))
    page.wait_for_timeout(1500)
    print("open_email_form:", sp.open_email_form(page, log=print))
    page.wait_for_timeout(1200)
    field = page.locator(sp.EMAIL_INPUT).first
    field.click()
    page.keyboard.type(EMAIL, delay=55)
    page.keyboard.press("Tab")
    page.wait_for_timeout(2500)
    print("button:", sp.submit_button_state(page))
    # 用**真实鼠标坐标**点，而不是 el.click()：遮罩层挡得住合成点击，挡不住
    # 坐标点击（Playwright 会做命中测试）。
    box = page.evaluate("""
    () => {
      const b = Array.from(document.querySelectorAll('button'))
        .find(n => /sign up/i.test(n.innerText||''));
      if (!b) return null;
      const r = b.getBoundingClientRect();
      return {x: r.x + r.width/2, y: r.y + r.height/2, w: r.width, h: r.height};
    }""")
    print("box:", box)
    if box:
        page.mouse.click(box["x"], box["y"])
    page.wait_for_timeout(9000)
    print("page text:", sp.page_text(page)[:160].replace("\n", " | "))
    print("--- requests ---")
    for r in reqs:
        print("  ", json.dumps(r, ensure_ascii=False)[:260])
