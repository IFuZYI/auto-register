"""决定性实验：同一个会话里，只改 body 的字段，看服务端怎么回。

已知：直接 `fetch('/api/auth/send-verification-code', {body: {email}})` → `{"ok":true}`
（四个域名全过）。而页面点按钮的结果是 `account:email-signup-unavailable`。

差别只可能在 body 里多出来的字段。最可疑的是 Castle 的反机器人令牌字段：
页面会带 `castleRequestToken`（**空串**，因为 castle.io 脚本根本没加载），
而直接 fetch 压根不带这个字段。

所以：分别发「不带字段」「带空串」「带假串」三种，看哪种会触发
`account:email-signup-unavailable`。一次就能定论。
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
async (payload) => {
  const r = await fetch('/api/auth/send-verification-code', {
    method: 'POST',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify(payload),
    credentials: 'include',
  });
  return {status: r.status, body: (await r.text()).slice(0, 300)};
}
"""

CASES = [
    ("只有 email", {"email": "probe.c1.5512@icloud.com"}),
    ("email + castleRequestToken=''", {"email": "probe.c2.5512@icloud.com", "castleRequestToken": ""}),
    ("email + castleRequestToken=假串", {"email": "probe.c3.5512@icloud.com", "castleRequestToken": "not-a-real-token"}),
    ("email + turnstileToken=''", {"email": "probe.c4.5512@icloud.com", "turnstileToken": ""}),
]

with Camoufox(**kw) as browser:
    page = browser.new_page()
    page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    for name, payload in CASES:
        out = page.evaluate(FETCH, payload)
        body = out.get("body") or ""
        code = ""
        try:
            code = (json.loads(body) or {}).get("code") or ""
        except Exception:
            pass
        print(f"{name:36} HTTP {out.get('status')} code={code or '-':40} {body[:90]}")
        page.wait_for_timeout(2000)
