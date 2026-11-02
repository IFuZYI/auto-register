"""一次性现场探针：打开注册页 → 进邮箱视图 → 填邮箱 → 打印所有能解释
「按钮为什么禁用」的状态（表单校验 / Castle / Turnstile / 请求体）。

用完即弃，不进生产链路。
"""
import json
import sys

sys.path.insert(0, "/data/workspace/Register")

from camoufox.sync_api import Camoufox  # noqa: E402
from platforms.grok import signup_pages as sp  # noqa: E402

PROXY = sys.argv[1] if len(sys.argv) > 1 else ""
EMAIL = sys.argv[2] if len(sys.argv) > 2 else "probe.test.7788@icloud.com"

kw = {"headless": True, "locale": ["en-US", "en"], "humanize": True, "os": ["windows"]}
if PROXY:
    from core.proxy_utils import build_playwright_proxy_config

    cfg = build_playwright_proxy_config(PROXY) or {}
    if cfg:
        kw["proxy"] = cfg

JS = """
() => {
  const out = {};
  const inputs = Array.from(document.querySelectorAll('input')).map(n => ({
    type: n.type, name: n.name, id: n.id, value: (n.value||'').slice(0,40),
    disabled: n.disabled, readOnly: n.readOnly,
    valid: n.checkValidity ? n.checkValidity() : null,
    msg: n.validationMessage || '',
    visible: !!(n.offsetWidth || n.offsetHeight),
  }));
  out.inputs = inputs;
  out.buttons = Array.from(document.querySelectorAll('button,[role="button"],input[type="submit"]'))
    .map(n => ({text: (n.innerText||n.value||'').slice(0,30), disabled: !!n.disabled,
                aria: n.getAttribute('aria-disabled'), visible: !!(n.offsetWidth||n.offsetHeight)}));
  out.castle = Object.keys(window).filter(k => /castle/i.test(k));
  out.castleToken = (() => {
    try {
      if (window.Castle && typeof window.Castle.createRequestToken === 'function') return 'has-createRequestToken';
      if (window._castle) return 'has-_castle';
    } catch (e) {}
    return '';
  })();
  out.turnstile = {
    keys: Object.keys(window).filter(k => /turnstile/i.test(k)),
    input: Array.from(document.querySelectorAll('input[name="cf-turnstile-response"]')).map(n => (n.value||'').length),
  };
  out.forms = Array.from(document.querySelectorAll('form')).map(f => ({
    action: f.getAttribute('action') || '', method: f.getAttribute('method') || '',
    valid: f.checkValidity ? f.checkValidity() : null,
    inputs: f.querySelectorAll('input').length,
  }));
  out.text = (document.body ? document.body.innerText : '').slice(0, 300);
  return out;
}
"""

with Camoufox(**kw) as browser:
    page = browser.new_page()
    page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    print("open_email_form:", sp.open_email_form(page, log=print))
    page.wait_for_timeout(1500)
    field = page.locator(sp.EMAIL_INPUT).first
    field.click()
    page.keyboard.type(EMAIL, delay=55)
    page.keyboard.press("Tab")
    page.wait_for_timeout(3000)
    print("=== 生产脚本怎么看 ===")
    print("email_value_matches:", sp.email_value_matches(page, EMAIL))
    print("submit_button_state:", sp.submit_button_state(page))
    print("raw submit js:", json.dumps(sp._run_submit_js(page, dry_run=True), ensure_ascii=False)[:600])
    print("=== 页面全量 ===")
    print(json.dumps(page.evaluate(JS), ensure_ascii=False, indent=1)[:2500])
    # 再等 20 秒，看 Castle / Turnstile 会不会自己出票
    page.wait_for_timeout(20000)
    print("--- after 20s ---")
    st = page.evaluate(JS)
    print(json.dumps({"buttons": st["buttons"], "castle": st["castle"],
                      "turnstile": st["turnstile"], "forms": st["forms"]},
                     ensure_ascii=False)[:1500])
