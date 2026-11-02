"""绕过坏掉的 UI：直接 fetch 发码，然后看页面能不能自己进到验证码步骤。

已知：
  - 点「Sign up」**一个请求都不发**（页面自己的提交处理是坏的）
  - 同一会话直接 `fetch('/api/auth/send-verification-code', {email})` 回 `{"ok":true}`

那么问题只剩：发出去之后，怎么让页面走到验证码输入。
试三条路：① 再点一次按钮；② 刷新页面；③ 看 localStorage/session 里有没有
「待验证邮箱」状态可以喂回去。
"""
import json
import sys

sys.path.insert(0, "/data/workspace/Register")

from camoufox.sync_api import Camoufox  # noqa: E402
from platforms.grok import signup_pages as sp  # noqa: E402

PROXY = sys.argv[1] if len(sys.argv) > 1 else ""
EMAIL = sys.argv[2] if len(sys.argv) > 2 else "probe.fetchsend.8899@icloud.com"

kw = {"headless": True, "locale": ["en-US", "en"], "humanize": True, "os": ["windows"]}
if PROXY:
    from core.proxy_utils import build_playwright_proxy_config

    cfg = build_playwright_proxy_config(PROXY) or {}
    if cfg:
        kw["proxy"] = cfg

FETCH = """
async (email) => {
  const r = await fetch('/api/auth/send-verification-code', {
    method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({email}), credentials: 'include',
  });
  return {status: r.status, body: (await r.text()).slice(0, 200)};
}
"""

STATE = """
() => ({
  ls: Object.keys(localStorage).slice(0, 20),
  ss: Object.keys(sessionStorage).slice(0, 20),
  cookies: document.cookie.split(';').map(c => c.split('=')[0].trim()).slice(0, 20),
  text: (document.body ? document.body.innerText : '').slice(0, 160),
  codeInputs: document.querySelectorAll('input[maxlength="1"], input[name="code"], input[autocomplete="one-time-code"]').length,
})
"""

with Camoufox(**kw) as browser:
    page = browser.new_page()
    page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    sp.open_email_form(page, log=print)
    page.wait_for_timeout(1200)
    field = page.locator(sp.EMAIL_INPUT).first
    field.click()
    page.keyboard.type(EMAIL, delay=55)
    page.keyboard.press("Tab")
    page.wait_for_timeout(2000)

    print("fetch:", page.evaluate(FETCH, EMAIL))
    page.wait_for_timeout(2500)
    print("① 再点按钮:", sp.submit_email_code(page)[0])
    page.wait_for_timeout(6000)
    print("   状态:", json.dumps(page.evaluate(STATE), ensure_ascii=False)[:300])

    print("② 刷新")
    page.reload(wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(6000)
    print("   状态:", json.dumps(page.evaluate(STATE), ensure_ascii=False)[:300])
