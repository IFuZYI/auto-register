"""A/B 探针：在真实浏览器会话里直接打发码接口，比不同邮箱域名的返回。

目的：区分「icloud.com 这个域名被限」和「这个出口/会话被限」。
同一个页面、同一个出口、同一套 cookie，只有邮箱域名不同 —— 唯一变量。

只发**假地址**（不存在的本地部分），不发真别名：
  - 回 200 → 这个域名现在还能注册（说明是域名配额问题）
  - 回 account:email-signup-unavailable → 这个域名也被限
如果所有域名都一样被拒 → 是这个出口/会话被限，不是域名。
"""
import json
import sys

sys.path.insert(0, "/data/workspace/Register")

from camoufox.sync_api import Camoufox  # noqa: E402

PROXY = sys.argv[1] if len(sys.argv) > 1 else ""

kw = {"headless": True, "locale": ["en-US", "en"], "humanize": True, "os": ["windows"]}
if PROXY:
    from core.proxy_utils import build_playwright_proxy_config

    cfg = build_playwright_proxy_config(PROXY) or {}
    if cfg:
        kw["proxy"] = cfg

FETCH = """
async (email) => {
  try {
    const r = await fetch('/api/auth/send-verification-code', {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify({email}),
      credentials: 'include',
    });
    const text = await r.text();
    return {status: r.status, body: text.slice(0, 300)};
  } catch (e) {
    return {status: 0, body: String(e).slice(0, 200)};
  }
}
"""

DOMAINS = ["icloud.com", "outlook.com", "gmail.com", "proton.me"]

with Camoufox(**kw) as browser:
    page = browser.new_page()
    page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    for i, dom in enumerate(DOMAINS):
        addr = f"probe{7731 + i}xq@{dom}"
        out = page.evaluate(FETCH, addr)
        body = out.get("body") or ""
        code = ""
        try:
            code = (json.loads(body) or {}).get("code") or ""
        except Exception:
            pass
        print(f"{dom:14} HTTP {out.get('status')}  code={code or '-':42} {body[:100]}")
        page.wait_for_timeout(2500)
