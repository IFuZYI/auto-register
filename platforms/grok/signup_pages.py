"""新版 Grok 注册流程（accounts.x.ai Next.js 前端）的页面适配层。

旧实现（2026-09）基于老版 SPA：`Sign up with email` → 邮箱 → 按 Enter 提交 →
`input[name=code]` 逐字输入 → givenName/familyName/password → 提交 → SSO。
2026-10 起线上换成了 Next.js 前端（turbopack，`data-dpl-id=…`），流程与判定全部变了：

  * 发码：`POST /api/auth/send-verification-code`，body 带 Cloudflare Castle 令牌
    `{"email": …, "castleRequestToken": "cNz8rPYH|JABF…"}`；三种结局
    `200 {"ok":true}` / `code=account:email-signup-unavailable` /
    `code=email:send-validation-code-rate-limited`。
  * 验证：`POST /api/auth/sign-up/verify-email`，`{"email": …, "code": "764275"}`
    （页面 maxlength=6，满 6 位自动提交，码不带连字符）。
  * 资料页：`input#givenName` / `input#familyName` / `input#password`，多了一个
    **Cloudflare Turnstile widget**（sitekey 0x4AAAAAAAhr9JGVDZbrZOo0）。令牌
    由 widget 的 `render({callback})` 回调进 React state；没有令牌就提交，页面
    报 "Complete the verification challenge, then try again."（埋点
    `error_type=turnstile_failed, step=credentials`）。
  * 建号：`POST /api/auth/sign-up/create-account`（body 带 `turnstileToken`、
    `castleRequestToken`），随后才落 `sso` cookie。

两条实测出来的关键结论（决定了地址回收策略）：

  1. `account:email-signup-unavailable` 跟**出口 IP 声誉**走，不跟邮箱走 ——
     同一个地址换住宅代理就能成功发码。**不能**把它当成地址已消耗。
  2. `email:send-validation-code-rate-limited` 是**按邮箱**限流，失败次数也计入，
     一个地址大约只有 4–6 次发码机会。所以重试要省着用地址、优先换 IP。
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────

SIGNUP_URL = "https://accounts.x.ai/sign-up?redirect=grok-com"
#: Turnstile widget 的 sitekey（从 challenge-platform 请求 URL 里读出来的）
TURNSTILE_SITEKEY = "0x4AAAAAAAhr9JGVDZbrZOo0"

#: 资料页 Turnstile 令牌所在页面的用途（铸币时作为 page_url 上报）
PROFILE_PAGE_URL = SIGNUP_URL

EMAIL_INPUT = "input#email"
CODE_INPUT = "input[name=code]"
#: 验证码框的**兜底选择器**。线上实测（2026-11-01）：新版是**六个
#: `maxlength=1` 的无名输入框**（参考实现的原话：「OTP widget = six unnamed
#: `maxlength=1` boxes: click the **first** one and `keyboard.type(code)`」），
#: 只认 `input[name=code]` 会报「验证码输入框未出现」。
CODE_INPUT_CANDIDATES = (
    "input[name=code]",
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"][maxlength="1"]',
    'input[maxlength="1"]',
    'input[data-testid="code"]',
)
GIVEN_NAME_INPUT = "input#givenName"
FAMILY_NAME_INPUT = "input#familyName"
PASSWORD_INPUT = "input#password"

# ── 发码结果分类 ──────────────────────────────────────────────────────

SEND_RESULT_SENT = "sent"
SEND_RESULT_RATE_LIMITED = "rate_limited"
SEND_RESULT_EMAIL_UNAVAILABLE = "email_unavailable"
SEND_RESULT_ALREADY_REGISTERED = "already_registered"
SEND_RESULT_TRANSIENT = "transient_error"
SEND_RESULT_CF_CHALLENGE = "cloudflare_challenge"
SEND_RESULT_NO_CONFIRMATION = "no_confirmation"
SEND_RESULT_DOMAIN_REJECTED = "email_domain_rejected"

#: 新版 API 的机器可读错误码 → 分类
_API_CODE_KINDS = {
    "account:email-signup-unavailable": SEND_RESULT_EMAIL_UNAVAILABLE,
    "email:send-validation-code-rate-limited": SEND_RESULT_RATE_LIMITED,
    "account:existing-email": SEND_RESULT_ALREADY_REGISTERED,
    "email:in-use": SEND_RESULT_ALREADY_REGISTERED,
    "account:email-in-use": SEND_RESULT_ALREADY_REGISTERED,
    "castle:token-failed": SEND_RESULT_TRANSIENT,
    "castle-token-failed": SEND_RESULT_TRANSIENT,
}

#: 只管「失败态」的文案表
_RATE_LIMIT_MARKERS = (
    "too many code requests",
    "wait a few minutes before requesting another code",
    "too many requests",
)
_EMAIL_UNAVAILABLE_MARKERS = (
    "email sign-up isn't available",
    "signup isn't available right now",
    "sign up another way",
)
_TRANSIENT_MARKERS = (
    "something went wrong",
    "refresh the page and try again",
    "please try again later",
)
_CF_CHALLENGE_MARKERS = (
    "just a moment",
    "verifying you are human",
    "enable javascript and cookies to continue",
    "cf-chl-widget",
    "checking your browser",
    "attention required! | cloudflare",
)
_ALREADY_REGISTERED_MARKERS = (
    "already exists",
    "already registered",
    "already been used",
    "an account with this email",
    "email already in use",
    "existing account found",
    "已有账号",
    "已注册",
)
_DOMAIN_REJECTED_MARKERS = (
    "your email address is invalid",
    "please use a different email address",
    "email domain is not",
    "domain is not allowed",
    "domain is blocked",
    "disposable email",
    "临时邮箱",
)

EDITORIAL_RETRY_AFTER_SECONDS = 180


def _norm(text: Any) -> str:
    """小写化 + 把排版撇号折成 ASCII，否则线上文案的 `’` 会漏匹配。"""
    return str(text or "").lower().replace("\u2019", "'").replace("\u2018", "'")


def _verdict(
    kind: str,
    *,
    retryable: bool,
    consumes_address: bool,
    already_registered: bool = False,
    retry_after_hint: int = 0,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "kind": kind,
        "retryable": retryable,
        "consumes_address": consumes_address,
        "already_registered": already_registered,
        "retry_after_hint": retry_after_hint,
        "detail": detail,
    }


def _verdict_for_kind(kind: str, *, detail: str = "") -> dict[str, Any]:
    if kind == SEND_RESULT_SENT:
        return _verdict(kind, retryable=False, consumes_address=False, detail=detail)
    if kind == SEND_RESULT_RATE_LIMITED:
        return _verdict(
            kind,
            retryable=True,
            consumes_address=False,
            retry_after_hint=EDITORIAL_RETRY_AFTER_SECONDS,
            detail=detail,
        )
    if kind == SEND_RESULT_EMAIL_UNAVAILABLE:
        # 域名配额的问题（见 `send_code_error_text` 里的实测记录）：地址没被消耗，
        # 但**同一个域名**再试也一样，所以「重试」必须换域名才有意义。
        return _verdict(kind, retryable=True, consumes_address=False, detail=detail)
    if kind == SEND_RESULT_ALREADY_REGISTERED:
        return _verdict(
            kind,
            retryable=False,
            consumes_address=True,
            already_registered=True,
            detail=detail,
        )
    if kind in (SEND_RESULT_TRANSIENT, SEND_RESULT_CF_CHALLENGE, SEND_RESULT_NO_CONFIRMATION):
        return _verdict(kind, retryable=True, consumes_address=False, detail=detail)
    if kind == SEND_RESULT_DOMAIN_REJECTED:
        # 域名级拒绝：这个地址本身不能用来注册，重试同一地址没意义
        return _verdict(kind, retryable=False, consumes_address=False, detail=detail)
    return _verdict(
        SEND_RESULT_NO_CONFIRMATION, retryable=True, consumes_address=False, detail=detail
    )


def _match_markers(text: str, markers: tuple[str, ...]) -> str:
    for marker in markers:
        if marker in text:
            return marker
    return ""


def classify_send_code_page(page_text: str) -> dict[str, Any]:
    """只看页面文案的判定（API 响应拿不到时的兜底）。"""
    text = _norm(page_text)
    for markers, kind in (
        (_RATE_LIMIT_MARKERS, SEND_RESULT_RATE_LIMITED),
        (_DOMAIN_REJECTED_MARKERS, SEND_RESULT_DOMAIN_REJECTED),
        (_EMAIL_UNAVAILABLE_MARKERS, SEND_RESULT_EMAIL_UNAVAILABLE),
        (_ALREADY_REGISTERED_MARKERS, SEND_RESULT_ALREADY_REGISTERED),
        (_CF_CHALLENGE_MARKERS, SEND_RESULT_CF_CHALLENGE),
        (_TRANSIENT_MARKERS, SEND_RESULT_TRANSIENT),
    ):
        hit = _match_markers(text, markers)
        if hit:
            return _verdict_for_kind(kind, detail=hit)
    # 兜底：没认出来 ≠ 地址被用掉（审计 R-22）
    return _verdict_for_kind(SEND_RESULT_NO_CONFIRMATION, detail=_norm(page_text)[:200])


def detect_blocking_state(page_text: str) -> dict[str, Any] | None:
    """加载页面后立刻要放弃的状态；良性页面（入口/邮箱页/资料页）返回 None。"""
    verdict = classify_send_code_page(page_text)
    if verdict["kind"] == SEND_RESULT_NO_CONFIRMATION:
        return None
    return verdict


def _loads(body: Any) -> dict[str, Any]:
    if isinstance(body, dict):
        return body
    text = str(body or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except Exception as exc:
        logger.debug("发码响应不是 JSON: %s", exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def classify_send_code_result(
    status: int | None = None, body: Any = "", page_text: str = ""
) -> dict[str, Any]:
    """优先按 HTTP 状态 + JSON 错误码判定，退化时再看页面文案。"""
    payload = _loads(body)
    code = str(payload.get("code") or "").strip().lower()
    if code:
        kind = _API_CODE_KINDS.get(code) or _API_CODE_KINDS.get(code.replace("_", ":"))
        if kind:
            return _verdict_for_kind(kind, detail=code)
    if status == 200 and payload.get("ok"):
        return _verdict_for_kind(SEND_RESULT_SENT, detail="ok")
    # JSON 里没有可识别的码：把 error 文案 + 原始 body + 页面文案一起喂给文案判定
    haystack = " ".join(
        part for part in (str(payload.get("error") or ""), str(body or ""), str(page_text or "")) if part
    )
    verdict = classify_send_code_page(haystack)
    if verdict["kind"] != SEND_RESULT_NO_CONFIRMATION:
        return verdict
    return _verdict_for_kind(
        SEND_RESULT_NO_CONFIRMATION, detail=f"http={status} {str(body or '')[:200]}".strip()
    )


def send_code_error_text(verdict: dict[str, Any], email: str) -> str:
    """把判定翻成一句人话，指明「该换 IP / 该等一会 / 这个号废了」。"""
    kind = (verdict or {}).get("kind")
    detail = str((verdict or {}).get("detail") or "")
    if kind == SEND_RESULT_EMAIL_UNAVAILABLE:
        # 2026-11-01 更正：这个码主要跟**邮箱域名配额**走，不是出口 IP。
        # 实测记录（Grok-farm 项目在同一 sticky 出口、同一 Gmail 账号上量的）：
        #   13:29 接受 / 13:32 接受 / 13:42 接受 / 13:47 拒 / 13:50 拒
        # 也就是同一域名一个窗口只能注册 3~4 个，之后**整个域名**在几小时内
        # 一律回这个码 —— 换出口没用，换域名才有用。
        # 我们的 iCloud 隐藏邮箱全都落在 `icloud.com` 这一个域名上，所以一旦
        # 撞到配额，池子里**每一个别名**都会被拒（这正是 2026-11-01 两轮
        # 不同出口、不同别名、同一个码的原因）。
        domain = str(email or "").rsplit("@", 1)[-1] or "该域名"
        return (
            f"{email}：x.ai 拒绝邮箱注册（account:email-signup-unavailable）—— "
            "这是**通用拒绝**，实测有三个触发点，按代价从低到高排查："
            "① **带空 Turnstile 令牌提交**（日志里「令牌 0 字符」就是它；无票时"
            "现在一律不提交，先看有没有这条）；"
            f"② **{domain} 域名**这一窗口的注册配额（同域名约 3~4 个/窗口，几小时后"
            "恢复，要换**邮箱域名**）；"
            "③ 该**出口**被判定不可注册（换住宅出口可解）。"
            "地址本身没被消耗。"
        )
    if kind == SEND_RESULT_RATE_LIMITED:
        hint = (verdict or {}).get("retry_after_hint") or EDITORIAL_RETRY_AFTER_SECONDS
        return f"{email}：发码被限流（email:send-validation-code-rate-limited），约 {hint}s 后可重试"
    if kind == SEND_RESULT_ALREADY_REGISTERED:
        return f"{email}：该地址已注册过账号"
    if kind == SEND_RESULT_DOMAIN_REJECTED:
        return f"域名被 x.ai 拒收: {str(email).split('@')[-1]}"
    if kind == SEND_RESULT_CF_CHALLENGE:
        return f"{email}：遇到 Cloudflare 挑战页（环境问题，不是账号问题），可重试"
    if kind == SEND_RESULT_TRANSIENT:
        return f"{email}：页面报错未发出验证码（{detail or 'transient'}），可重试"
    return f"{email}：未确认发信（{detail or 'no-confirmation'}），地址未消耗"


# ── 页面文案 / 表单辅助 ────────────────────────────────────────────────

_TURNSTILE_GATE_MARKERS = ("complete the verification challenge", "verification challenge")


def looks_turnstile_gated(page_text: str) -> bool:
    return _match_markers(_norm(page_text), _TURNSTILE_GATE_MARKERS) != ""


_PROFILE_FORM_MARKERS = ("complete your sign up", "first name", "last name")


def has_profile_form(page_text: str) -> bool:
    return _match_markers(_norm(page_text), _PROFILE_FORM_MARKERS) != ""


def page_text(page: Any) -> str:
    try:
        return str(page.evaluate("() => (document.body ? document.body.innerText : '')") or "")
    except Exception as exc:
        logger.debug("读页面文本失败: %s", exc)
        return ""


_SET_VALUE_JS = """
() => {
  const sel = %s;
  const val = %s;
  const el = document.querySelector(sel);
  if (!el) return false;
  const proto = Object.getPrototypeOf(el);
  const desc = Object.getOwnPropertyDescriptor(proto, 'value');
  if (desc && desc.set) { desc.set.call(el, val); } else { el.value = val; }
  if (el._valueTracker) { try { el._valueTracker.setValue(''); } catch (e) {} }
  el.dispatchEvent(new Event('beforeinput', {bubbles: true}));
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.dispatchEvent(new Event('blur', {bubbles: true}));
  return String(el.value) === String(val);
}
"""


def set_field_value(page: Any, selector: str, value: str) -> bool:
    """用原生 setter + 事件把值写进受控输入框，并读回校验。"""
    js = _SET_VALUE_JS % (json.dumps(selector), json.dumps(value))
    try:
        return bool(page.evaluate(js))
    except Exception as exc:
        logger.debug("写 %s 失败: %s", selector, exc)
        return False


def fill_profile(
    page: Any, *, given_name: str, family_name: str, password: str, log: Callable | None = None
) -> list[str]:
    """填资料页三件套，返回没填成功的字段名。"""
    missing: list[str] = []
    for selector, value, name in (
        (GIVEN_NAME_INPUT, given_name, "givenName"),
        (FAMILY_NAME_INPUT, family_name, "familyName"),
        (PASSWORD_INPUT, password, "password"),
    ):
        if not set_field_value(page, selector, value):
            missing.append(name)
    if log:
        log(f"资料页填写：{'全部成功' if not missing else '缺 ' + ','.join(missing)}")
    return missing


_CLICK_BY_LABELS_JS = """
(args) => {
  const wanted = args.labels.map(s => s.toLowerCase());
  const ban = (args.ban || []).map(s => s.toLowerCase());
  const nodes = Array.from(document.querySelectorAll(
    'button, [role="button"], input[type="submit"], a'));
  const score = (t) => {
    const s = t.toLowerCase();
    if (ban.some(b => s.includes(b))) return -1;
    if (wanted.includes(s)) return 100;
    if (wanted.some(w => s.includes(w))) return 50;
    return -1;
  };
  let best = null, bestScore = 0;
  for (const el of nodes) {
    const t = (el.innerText || el.value || el.textContent || '').replace(/\\s+/g, ' ').trim();
    if (!t) continue;
    const sc = score(t);
    if (sc > bestScore) { best = el; bestScore = sc; }
  }
  if (!best) return '';
  const label = (best.innerText || best.value || best.textContent || '').replace(/\\s+/g, ' ').trim();
  best.scrollIntoView({block: 'center'});
  best.click();
  return label;
}
"""


def click_button(page: Any, labels: list[str], *, ban: list[str] | None = None) -> str:
    """按文案打分点按钮（返回点到的文案；没找到返回空串）。"""
    args = {"labels": list(labels), "ban": list(ban or [])}
    try:
        return str(page.evaluate(_CLICK_BY_LABELS_JS, args) or "")
    except Exception as exc:
        logger.debug("按文案点按钮失败: %s", exc)
        return ""


# 「用邮箱注册」按钮的文案表（多语言兜底，理由见 `_ENTRY_MARKERS`）。
# 打分逻辑（`_CLICK_BY_LABELS_JS`）里完全命中给 100、子串命中给 50，
# 所以各语言只要有一条全长文案能命中，就一定会压过社交登录按钮。
_EMAIL_SIGNUP_LABELS = [
    "sign up with email",
    "sign up with your email",
    "signupwithemail",
    "continue with email",
    "使用邮箱注册",
    "用邮箱注册",
    "メールで登録",
    "メールアドレスで登録",
    "mit e-mail registrieren",
    "mit e-mail anmelden",
    "registrarse con correo electrónico",
    "registrarse con email",
    "registrarme con correo",
    "s'inscrire avec un e-mail",
    "s'inscrire par e-mail",
    "registrati con l'e-mail",
    "registrati con email",
    "inscrever-se com e-mail",
    "continuar com e-mail",
    "이메일로 가입",
    "이메일로 계속",
]


def click_email_signup(page: Any) -> str:
    """入口页点 “Sign up with email”（必须排除 Sign up with X/Apple/Google/GitHub）。"""
    return click_button(
        page,
        list(_EMAIL_SIGNUP_LABELS),
        ban=["with x", "with apple", "with google", "with github", " sign in",
             "で登録", "でサインイン", "でログイン"],
    )


#: 提交发码的脚本。**照 `reference/register/grok-register/registration_browser.py`
#: 那份线上跑通的实现移植**（`fill_email_and_submit` 的降级梯子），三档依次降级：
#:
#:   1. 找一个**可见、未禁用**、文案匹配的提交按钮 → `click()`
#:   2. 没有可用按钮 → 拿输入框所在的 `<form>` 走 `form.requestSubmit()`
#:      （没有 `requestSubmit` 就派发 `submit` 事件）
#:   3. 连 form 都没有 → 在输入框上派发 Enter 的 `keydown`/`keyup`
#:
#: 以及两个前置校验（同样来自那份实现）：输入框**可见/未禁用/非只读**且
#: **有值**，`type=email` 时还要过 `checkValidity()`。`disabled` 的按钮直接
#: **不进候选**（不是点了再祈祷）—— 线上实测就是这个坑：按钮当时禁用，
#: `el.click()` 什么都不发生，旧代码照样报「已提交」。
#:
#: 返回的 `how` 是**触发方式**，不是「服务端接受了」—— 那由响应/页面判定。
_SUBMIT_EMAIL_JS = """
(args) => {
  const labels = (args.labels || []).map(s => s.toLowerCase());
  const ban = (args.ban || []).map(s => s.toLowerCase());
  const isVisible = (node) => {
    if (!node) return false;
    const st = window.getComputedStyle(node);
    if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
    const r = node.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const textOf = (node) => [
    node.innerText, node.textContent, node.getAttribute('aria-label'), node.getAttribute('title'),
    node.getAttribute('placeholder'), node.getAttribute('data-testid'),
    node.getAttribute('name'), node.getAttribute('id'),
  ].filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
  const enabled = (n) => !n.disabled && n.getAttribute('aria-disabled') !== 'true';

  const diag = {
    url: location.href,
    buttons: Array.from(document.querySelectorAll('button,[role="button"],input[type="submit"]'))
      .filter(isVisible).map((n) => textOf(n).slice(0, 40)).filter(Boolean).slice(0, 8),
    inputs: Array.from(document.querySelectorAll('input,textarea')).filter(isVisible)
      .map((n) => [n.getAttribute('type') || '', n.getAttribute('id') || '', n.getAttribute('name') || ''].join('/'))
      .slice(0, 8),
  };
  const bail = (reason, found, hasInput) => ({
    how: '', label: '', reason, found, disabled: true, hasInput, inputOk: false, diag,
  });

  // 输入框：先按调用方给的选择器，再按属性兜底 —— xAI 换过 id/name，
  // 只认 `input#email` 会在改版当天整条链停摆（而报错会指向「提交没生效」）。
  let input = null;
  for (const sel of (args.inputSelectors || [])) {
    input = Array.from(document.querySelectorAll(sel)).find(
      (n) => isVisible(n) && enabled(n) && !n.readOnly);
    if (input) break;
  }
  if (!input) {
    input = Array.from(document.querySelectorAll('input, textarea')).find((n) => {
      const type = (n.getAttribute('type') || '').toLowerCase();
      if (['hidden','submit','button','checkbox','radio','file','search'].includes(type)) return false;
      if (!isVisible(n) || !enabled(n) || n.readOnly) return false;
      const meta = textOf(n).toLowerCase();
      return meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件');
    }) || null;
  }
  if (!input) return bail('no-input', false, false);
  if (!String(input.value || '').trim()) return bail('input-empty', true, true);
  if ((input.getAttribute('type') || '').toLowerCase() === 'email' && !input.checkValidity())
    return bail('input-invalid', true, true);

  const label = textOf(input);
  if (args.dryRun) {
    // 只看状态、不提交：给「按钮可用了没」这类轮询用。
    let best = null, bestScore = 0, anyDisabled = false;
    for (const el of document.querySelectorAll('button,[role="button"],input[type="submit"]')) {
      if (!isVisible(el)) continue;
      const s = textOf(el).toLowerCase();
      if (ban.some((b) => s.includes(b))) continue;
      const sc = labels.includes(s) ? 100 : (labels.some((w) => s.includes(w)) ? 50 : -1);
      if (sc <= 0) continue;
      if (!enabled(el)) { anyDisabled = true; continue; }
      if (sc > bestScore) { best = el; bestScore = sc; }
    }
    return {
      how: '', label: best ? textOf(best) : '', reason: best ? '' : (anyDisabled ? 'button-disabled' : 'no-button'),
      found: Boolean(best) || anyDisabled, disabled: !best, hasInput: true, inputOk: true, diag,
    };
  }

  // —— 第一档：按钮 ——
  let best = null, bestScore = 0, anyDisabled = false;
  for (const el of document.querySelectorAll('button,[role="button"],input[type="submit"]')) {
    if (!isVisible(el)) continue;
    const s = textOf(el).toLowerCase();
    if (ban.some((b) => s.includes(b))) continue;
    const sc = labels.includes(s) ? 100 : (labels.some((w) => s.includes(w)) ? 50 : -1);
    if (sc <= 0) continue;
    if (!enabled(el)) { anyDisabled = true; continue; }
    if (sc > bestScore) { best = el; bestScore = sc; }
  }
  if (best) {
    const btnLabel = textOf(best);
    try { best.scrollIntoView({ block: 'center' }); } catch (e) {}
    best.click();
    return { how: 'button', label: btnLabel, reason: '', found: true, disabled: false, hasInput: true, inputOk: true, diag };
  }

  // —— 第二档：form.requestSubmit() ——
  const form = input.closest('form');
  if (form) {
    try {
      if (form.requestSubmit) form.requestSubmit();
      else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
      return { how: 'form', label: '', reason: '', found: true, disabled: anyDisabled, hasInput: true, inputOk: true, diag };
    } catch (e) {}
  }

  // —— 第三档：在输入框上派发 Enter ——
  try { input.focus(); } catch (e) {}
  for (const kind of ['keydown', 'keyup']) {
    input.dispatchEvent(new KeyboardEvent(kind, {
      key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true, cancelable: true,
    }));
  }
  return { how: 'enter', label: '', reason: '', found: true, disabled: anyDisabled, hasInput: true, inputOk: true, diag };
}
"""

#: 提交按钮的文案表。
_EMAIL_SUBMIT_LABELS = ["sign up", "continue", "続行", "次へ", "weiter", "continuar", "continuer", "계속"]
_EMAIL_SUBMIT_BAN = ["with ", "go back", "sign in", "戻る", "zurück"]

#: 找输入框时先试这几个（真值在最前面），都命中不了才按属性兜底。
_EMAIL_INPUT_SELECTORS = (
    EMAIL_INPUT,
    'input[data-testid="email"]',
    'input[name="email"]',
    'input[type="email"]',
    'input[autocomplete="email"]',
)


def _run_submit_js(page: Any, *, dry_run: bool = False) -> dict[str, Any]:
    args = {
        "labels": list(_EMAIL_SUBMIT_LABELS),
        "ban": list(_EMAIL_SUBMIT_BAN),
        "inputSelectors": list(_EMAIL_INPUT_SELECTORS),
        "dryRun": bool(dry_run),
    }
    try:
        out = page.evaluate(_SUBMIT_EMAIL_JS, args)
    except Exception as exc:
        logger.debug("提交发码脚本失败: %s", exc)
        return {"how": "", "label": "", "reason": "js-error", "found": False,
                "disabled": True, "hasInput": False, "inputOk": False,
                "diag": {"error": str(exc)[:160]}}
    return out if isinstance(out, dict) else {}


#: 反机器人令牌的痕迹（Castle / Turnstile / Cloudflare）。
#: Castle 把令牌放在 `window`、`sessionStorage` 或请求头里，名字不固定，
#: 所以按**子串**扫，宁可多认几个也不要漏。
_ANTIBOT_JS = """
() => {
  const hits = [];
  try {
    for (const k of Object.keys(window)) {
      if (/castle|turnstile|cf_chl|__cf/i.test(k)) hits.push('win:' + k);
    }
  } catch (e) {}
  try {
    for (const store of [window.sessionStorage, window.localStorage]) {
      for (let i = 0; i < (store.length || 0); i++) {
        const k = store.key(i) || '';
        if (/castle|turnstile|cf_chl/i.test(k)) hits.push('store:' + k);
      }
    }
  } catch (e) {}
  try {
    // **必须看 value**：隐藏 input 一直都在，空着等于没解出来。
    // 2026-11-02 用户日志：按钮一直是禁用态、我们还硬发 `form.requestSubmit()`，
    // 服务端一律回 `email-signup-unavailable` —— 根子就是这里只判「元素在不在」。
    for (const el of document.querySelectorAll('input[name="cf-turnstile-response"]')) {
      if ((el.value || '').length > 16) hits.push('turnstile:' + el.value.length);
    }
    for (const el of document.querySelectorAll('[name="castleRequestToken"], [data-castle-token]')) {
      const v = el.value || el.getAttribute('data-castle-token') || '';
      if (v.length > 8) hits.push('castle:' + v.length);
    }
  } catch (e) {}
  return hits;
}
"""


def wait_for_antibot_token(page: Any, *, timeout: float = 45.0, poll: float = 0.5) -> bool:
    """等 Castle / Turnstile 的反机器人令牌落地，返回是否等到。

    **为什么必须等**（2026-11-01，用户实测对照）：同一个 `@icloud.com` 地址，
    手动操作能过、自动化过不去。差别是节奏 —— 我们的日志里「已进入邮箱注册视图」
    到「提交发码」只隔 **1 秒**，而参考实现明写：

    > Turnstile/Castle needs 30-45 s to settle — do not shorten it before the
    > submit path.

    令牌没到手就提交，服务端回的是 `account:email-signup-unavailable` —— 一个
    通用拒绝，看着像「域名配额」或「出口被标记」，实际是「这次请求不可信」。

    拿不到也不挡：超时就返回 False，让调用方照常提交（fail-open）—— 有些出口
    上令牌是懒加载的，等太久反而把整轮拖死。
    """
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            hits = page.evaluate(_ANTIBOT_JS)
        except Exception as exc:
            logger.debug("读反机器人令牌失败: %s", exc)
            hits = []
        if hits:
            logger.debug("反机器人令牌: %s", hits)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def antibot_state(page: Any) -> dict[str, Any]:
    """反机器人令牌 + 提交按钮的当前状态（**不提交**）。

    为什么要一起看：按钮禁用通常就是「Turnstile 还没给令牌」。只看令牌会漏掉
    「令牌有了但按钮还禁用」；只看按钮会漏掉「按钮能点但请求不带令牌」。
    """
    out: dict[str, Any] = {"token_len": 0, "input_exists": False,
                           "button_found": False, "button_disabled": True}
    try:
        info = page.evaluate("""
        () => {
          const inputs = Array.from(document.querySelectorAll('input[name="cf-turnstile-response"]'));
          const tokenLen = inputs.reduce((m, el) => Math.max(m, (el.value || '').length), 0);
          return {tokenLen, inputExists: inputs.length > 0};
        }
        """)
        if isinstance(info, dict):
            out["token_len"] = int(info.get("tokenLen") or 0)
            out["input_exists"] = bool(info.get("inputExists"))
    except Exception as exc:
        logger.debug("读 turnstile 状态失败: %s", exc)
    try:
        st = submit_button_state(page)
        out["button_found"] = bool(st.get("found"))
        out["button_disabled"] = bool(st.get("disabled"))
    except Exception as exc:
        logger.debug("读提交按钮状态失败: %s", exc)
    return out


def wait_until_submittable(page: Any, *, timeout: float = 60.0, poll: float = 0.5) -> dict[str, Any]:
    """等「页面真的可以提交」：拿到 Turnstile 令牌**或**按钮不是禁用态。

    （2026-11-04 独立审查指出：这句摘要原本写的是「**且**」，与下面的
    `ready = token_len > 16 or not saw_disabled_button` 正好相反 —— 而下面几段
    解释的也全是「令牌不是硬门槛」的道理。口径以代码为准：**按钮能点就算 ready**，
    令牌只是额外的加速信号。）

    2026-11-02 线上日志：`反机器人令牌未出现（继续尝试提交）` 紧接着
    `发码已提交（form.requestSubmit()）｜⚠ 按钮是禁用态` —— 我们**绕过页面
    自己的禁用态强行提交**，服务端一律回 `account:email-signup-unavailable`。
    按钮禁用就是页面在说「还没准备好」，硬提交等于拿一个必然被拒的请求去
    撞上游，既拿不到码、又白白消耗这个出口的信任分。

    2026-11-02 二次核对（真机 A/B）：**空票提交并不必然被拒**。同一天同一出口，
    `令牌 0 字符` 的提交拿到了验证码并注册成功；而此前失败的那些尝试里，
    同一出口换域名（outlook.es）同样被拒 —— 所以
    `account:email-signup-unavailable` 更像**出口声誉 / 域名窗口**的通用拒绝，
    不是「空票」本身的错。因此这里**不**把令牌当成硬门槛，只保留按钮口径。

    真正该补票的情形是**按钮禁用**（页面在说「还没准备好」）：调用方在
    `ready=False` 且页面上有令牌字段时用内建屏外铸币补一张，补不到就如实上报
    「页面未就绪」，不要硬提交。
    """
    # 用**轮次**而不是墙钟做上限：假页面（测试）里 `wait_for_timeout` 是空操作，
    # 按墙钟算会真的空转 60 秒，把整个测试套件拖死。
    budget = max(1, int(max(0.0, float(timeout)) / max(0.05, poll)))
    state: dict[str, Any] = {}
    for _ in range(budget):
        state = antibot_state(page)
        token_len = int(state.get("token_len") or 0)
        # 「没看到按钮」不等于「不能提交」—— 有些版本按钮在 shadow DOM 里，
        # 我们的探测看不见。只有**明确看到按钮且它是禁用态**才拦。
        saw_disabled_button = bool(state.get("button_found")) and bool(state.get("button_disabled"))
        # 口径：**按钮可点就算 ready**（保持原行为）。
        #
        # 2026-11-02 现场实测更正过一次：曾经把这里改成「有令牌字段就必须有票」，
        # 但真机跑下来，**空票提交也能成功发码**（17:40 那次 `令牌 0 字符` 直接
        # 拿到验证码并注册成功），所以「空票必被通用拒绝」不成立 —— 硬卡令牌
        # 反而会拦掉本来能过的提交。真正需要补票的是「按钮禁用」（页面自己在说
        # 还没准备好），那条路径由调用方在 ready=False 时用内建铸币补一张。
        ready = token_len > 16 or not saw_disabled_button
        state["ready"] = bool(ready)
        if ready:
            return state
        # 优先用页面的等待（假页面在测试里是空操作，不会真睡 60 秒）
        waiter = getattr(page, "wait_for_timeout", None)
        if callable(waiter):
            try:
                waiter(int(poll * 1000))
                continue
            except Exception as exc:
                logger.debug("wait_for_timeout 失败，退回 sleep: %s", exc)
        time.sleep(poll)
    return state


def submit_button_state(page: Any) -> dict[str, Any]:
    """当前「提交」按钮的文案与是否禁用（**不提交**）。

    走的是同一个脚本的 `dryRun` —— 两处各写一份选择逻辑的话，「看状态」和
    「点」迟早会落在不同元素上，而那种错只在线上才看得见。
    """
    state = _run_submit_js(page, dry_run=True)
    return {
        "label": state.get("label") or "",
        "disabled": bool(state.get("disabled", True)),
        "found": bool(state.get("found")),
        "reason": state.get("reason") or "",
    }


def email_input_present(page: Any) -> bool:
    """邮箱输入框还在不在 —— 用来判断「点了之后页面到底动没动」。"""
    return _count(page, EMAIL_INPUT) > 0


#: Castle 反机器人 SDK 的**兜底桩**。
#:
#: 2026-11-02 逆向结论（实测）：邮箱页的提交处理会先要一个 Castle 令牌
#: （`window.Castle.createRequestToken()`）。Castle 的脚本是从第三方 CDN 异步
#: 加载的，**在代理链路上经常没加载完**（抓包：castle.io 一个请求都没有）。
#: 拿不到 `window.Castle` 时它**静默 return** —— 不报错、不发请求、按钮还是
#: 可点的。表现就是「点了 Sign up，一个 POST 都没有」，然后我们误判成
#: 「上游拒绝」「域名配额」。
#:
#: 这个桩在页面脚本之前注入：真有 Castle 时会被它覆盖（不影响真实令牌），
#: 没有时至少让提交处理跑下去。
CASTLE_STUB_JS = """
(() => {
  try {
    if (window.Castle && typeof window.Castle.createRequestToken === 'function') return;
    const tok = 'grok-stub-' + Math.random().toString(36).slice(2) + Date.now();
    window.Castle = window.Castle || {};
    if (typeof window.Castle.createRequestToken !== 'function') {
      window.Castle.createRequestToken = function () { return Promise.resolve(tok); };
    }
    if (typeof window.Castle.getRequestToken !== 'function') {
      window.Castle.getRequestToken = function () { return Promise.resolve(tok); };
    }
  } catch (e) {}
})();
"""


def castle_ready(page: Any) -> bool:
    """页面上的 Castle 是否已经可用（真的或桩）。"""
    try:
        return bool(page.evaluate(
            "() => !!(window.Castle && typeof window.Castle.createRequestToken === 'function')"
        ))
    except Exception as exc:
        logger.debug("探测 Castle 失败: %s", exc)
        return False


def wait_for_castle(page: Any, *, timeout: float = 20.0, poll: float = 0.5) -> bool:
    """等 Castle 可用（桩注入后通常立刻为真；没注入时等真 SDK 加载）。"""
    budget = max(1, int(max(0.0, float(timeout)) / max(0.05, poll)))
    for _ in range(budget):
        if castle_ready(page):
            return True
        waiter = getattr(page, "wait_for_timeout", None)
        if callable(waiter):
            try:
                waiter(int(poll * 1000))
                continue
            except Exception as exc:
                logger.debug("等待 Castle 就绪时 wait_for_timeout 失败: %s", exc)
        time.sleep(poll)
    return False


#: 直接调发码接口（绕过页面自己的提交处理）。
#:
#: 2026-11-02 实测：点「Sign up」**一个 POST 都不发**（不是发失败，是根本没发），
#: 而同一会话里直接 fetch 这个接口回 `{"ok":true}` —— 四个域名（icloud /
#: outlook / gmail / proton）全过。也就是说：
#:   * 服务端不拒这个出口、不拒这个域名；
#:   * 坏的是页面自己的 React 提交处理（它等一个永远不来的反机器人令牌）。
#: 发完之后 `location.reload()`，服务端已经记下「这个邮箱待验证」，页面会自己
#: 进到下一步（实测：直接出现 `Create your account`）。
_SEND_CODE_FETCH_JS = """
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


def send_code_via_fetch(page: Any, email: str) -> dict[str, Any]:
    """直接调发码接口。返回 `{status, body, ok}`。"""
    try:
        out = page.evaluate(_SEND_CODE_FETCH_JS, email)
    except Exception as exc:
        logger.debug("直接调发码接口失败: %s", exc)
        return {"status": 0, "body": str(exc)[:200], "ok": False}
    if not isinstance(out, dict):
        return {"status": 0, "body": str(out)[:200], "ok": False}
    body = str(out.get("body") or "")
    ok = bool(out.get("status")) and "\"ok\":true" in body.replace(" ", "")
    return {"status": out.get("status"), "body": body, "ok": ok}


def email_value_matches(page: Any, email: str) -> bool:
    """邮箱框里现在是不是这个地址（空/不一致都算不匹配）。"""
    try:
        val = page.evaluate("""
        () => {
          const el = document.querySelector('input#email, input[name="email"], input[type="email"]');
          return el ? String(el.value || '') : '';
        }
        """)
        return str(val or "").strip() == str(email or "").strip()
    except Exception as exc:
        logger.debug("读邮箱框失败: %s", exc)
        return False


def retype_email(page: Any, email: str) -> bool:
    """把邮箱重新打进框里（点一下 → 全选 → 删除 → 逐字打）。"""
    try:
        field = page.locator(EMAIL_INPUT).first
        field.click(timeout=6000)
        page.keyboard.press("ControlOrMeta+a")
        page.keyboard.press("Delete")
        page.keyboard.type(email, delay=55)
        page.keyboard.press("Tab")
        page.wait_for_timeout(500)
        return email_value_matches(page, email)
    except Exception as exc:
        logger.debug("重输邮箱失败: %s", exc)
        return False


def submit_email_code(page: Any, *, wait_timeout: float = 8.0, poll: float = 0.3) -> tuple[str, dict]:
    """等输入框可提交再提交；三档降级（按钮 → form → Enter）。返回 `(文案, 状态)`。

    为什么要等 + 为什么要降级（线上实测 2026-11-01）：

    日志写着「发码已提交（Sign up）」，实际一个请求都没发出去 —— 邮箱视图刚出现
    时 React 还没挂上事件处理，或者按钮因为校验没过而 `disabled`，两种情况下
    `el.click()` **什么都不会发生**，而旧代码照样把按钮文案返回。接着等 9 秒超时，
    结论是 `no_confirmation: 未确认发信（页面未给出理由）`：把「点了个空」说成了
    「上游没确认」。

    所以：每次都跑一遍完整脚本 —— 输入框没就绪（缺失 / 空 / 校验不过）就等下一轮；
    就绪就直接提交，且按钮不可用时自动改用 `form.requestSubmit()` 或 Enter。
    """
    deadline = time.monotonic() + max(0.0, float(wait_timeout))
    state = _run_submit_js(page)
    while state.get("reason") in ("no-input", "input-empty", "input-invalid") and time.monotonic() < deadline:
        time.sleep(poll)
        state = _run_submit_js(page)
    return str(state.get("label") or ""), state


def click_profile_submit(page: Any) -> str:
    return click_button(
        page,
        ["complete sign up", "create account", "sign up", "完成注册", "创建账户",
         "登録を完了", "アカウントを作成", "konto erstellen", "crear cuenta",
         "créer un compte", "crea un account", "계정 만들기"],
        ban=["with ", "go back", "show password", "戻る", "zurück"],
    )


def wait_for_selector(page: Any, selector: str, *, timeout: float = 10.0, poll: float = 0.5,
                      sleep: Callable | None = None) -> bool:
    _sleep = sleep or time.sleep
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if _count(page, selector) > 0:
            return True
        if time.monotonic() >= deadline:
            return False
        _sleep(poll)


#: 入口页（`/sign-up`）渲染完成的标志：出现任意一条就算就绪。
#: 只认按钮文案不行 —— 白屏 / CF 挑战页里没有这些词。
# 入口页就绪判定的文案表。
#
# 为什么要多语言（2026-11-01 线上实测）：批量跑 5 个号里 3 个报「入口页没有渲染出
# 注册按钮」，探针一开就发现是**页面被本地化**：JP 出口（183.77.203.114）返回日语
# 页 `メールで登録` / `アカウントを作成`（title「Grok アカウントを作成」），
# 而 GB 出口才是 `Sign up with email`。只认英文 → 好出口被判成坏出口，一轮白烧
# 5-6 个出口、5 分钟。这里按线上真实文案补齐主要语言兜底。
_ENTRY_MARKERS = (
    # 英文
    "sign up with email",
    "sign up with your email",
    "create your account",
    "sign in",
    # 日文（JP 出口实测文案）
    "メールで登録",
    "アカウントを作成",
    "ログイン",
    # 德文
    "mit e-mail registrieren",
    "konto erstellen",
    "anmelden",
    # 西文
    "registrarse con correo electrónico",
    "correo electrónico",
    "crear cuenta",
    "iniciar sesión",
    # 法文
    "s'inscrire avec un e-mail",
    "créer un compte",
    "se connecter",
    # 意文
    "registrati con l'e-mail",
    "crea un account",
    "accedi",
    # 葡文
    "inscrever-se com e-mail",
    "criar conta",
    "iniciar sessão",
    # 韩文
    "이메일로 가입",
    "계정 만들기",
    "로그인",
    # 中文
    "使用邮箱注册",
    "创建账户",
    "登录",
)


def wait_for_entry(
    page: Any,
    *,
    timeout: float = 30.0,
    poll: float = 2.0,
    sleep: Callable | None = None,
    log: Callable | None = None,
) -> bool:
    """等入口页把注册按钮渲染出来。

    为什么需要：线上实测（2026-11-01）`goto` 之后入口页偶尔还在跑 Cloudflare
    的 jsd 探针（白屏，或停在 `Just a moment...`），这时盲点 `Sign up with email`
    只会一路「未命中」，6 次约 72 秒白等，而同一出口下一次启动就正常了。
    与其盲点，不如先等页面就绪。
    """
    _sleep = sleep or time.sleep
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if _match_markers(_norm(page_text(page)), _ENTRY_MARKERS):
            return True
        if time.monotonic() >= deadline:
            if log:
                log("入口页没有渲染出注册按钮（可能被 Cloudflare 挑战挡住）")
            return False
        _sleep(poll)


def open_email_form(page: Any, *, timeout: float = 10.0, attempts: int = 6,
                    sleep: Callable | None = None, log: Callable | None = None) -> bool:
    """点 “Sign up with email” 并等邮箱输入框出现（点一次不一定成功）。"""
    _sleep = sleep or time.sleep
    for attempt in range(max(1, attempts)):
        if _count(page, EMAIL_INPUT) > 0:
            return True
        label = click_email_signup(page)
        if log:
            log(f"点 Sign up with email（第 {attempt + 1} 次）→ {label or '未命中'}")
        if wait_for_selector(page, EMAIL_INPUT, timeout=timeout, sleep=_sleep):
            return True
    return _count(page, EMAIL_INPUT) > 0


# ── Turnstile ────────────────────────────────────────────────────────

_READ_TOKEN_JS = """
() => {
  const el = document.querySelector('input[name="cf-turnstile-response"]');
  const fromInput = el && el.value ? String(el.value) : '';
  let fromApi = '';
  try {
    if (window.turnstile && typeof window.turnstile.getResponse === 'function') {
      fromApi = String(window.turnstile.getResponse() || '');
    }
  } catch (e) {}
  return (fromInput || fromApi || '').trim();
}
"""


def read_turnstile_token(page: Any) -> str:
    """读当前 Turnstile 令牌（隐藏域或 window.turnstile.getResponse()）。"""
    try:
        return str(page.evaluate(_READ_TOKEN_JS) or "").strip()
    except Exception as exc:
        logger.debug("读 Turnstile 令牌失败: %s", exc)
        return ""


_HOOK_TURNSTILE_JS = """
() => {
  if (window.__grok_ts_hook) { return true; }
  const hook = {callback: null, container: null, count: 0};
  window.__grok_ts_hook = hook;
  const grab = (ts) => {
    if (!ts || ts.__grok_hooked) { return; }
    const origRender = typeof ts.render === 'function' ? ts.render.bind(ts) : null;
    ts.render = function (container, options) {
      try {
        hook.container = container;
        hook.callback = (options || {}).callback || null;
        hook.count += 1;
      } catch (e) {}
      return origRender ? origRender(container, options) : undefined;
    };
    ts.__grok_hooked = true;
  };
  grab(window.turnstile);
  let ticks = 0;
  const timer = setInterval(() => {
    grab(window.turnstile);
    if ((window.turnstile && window.turnstile.__grok_hooked) || (ticks += 1) > 600) {
      clearInterval(timer);
    }
  }, 50);
  return true;
}
"""


def install_turnstile_hook(page: Any) -> bool:
    """在应用 render 之前劫持 window.turnstile.render，捕获它的 callback。

    资料页的令牌只进 React state（不落 DOM），所以外部铸的令牌必须从 callback 灌回去。
    """
    try:
        return bool(page.evaluate(_HOOK_TURNSTILE_JS))
    except Exception as exc:
        logger.debug("劫持 turnstile.render 失败: %s", exc)
        return False


def inject_turnstile_token(page: Any, token: str) -> bool:
    """把外部铸好的令牌经捕获到的 callback 灌进应用状态。"""
    if not token:
        return False
    js = """
() => {
  const tok = %s;
  const hook = window.__grok_ts_hook;
  let ok = false;
  if (hook && typeof hook.callback === 'function') {
    try { hook.callback(tok); ok = true; } catch (e) {}
  }
  const el = document.querySelector('input[name="cf-turnstile-response"]');
  if (el) { try { el.value = tok; ok = true; } catch (e) {} }
  return ok;
}
""" % json.dumps(token)
    try:
        return bool(page.evaluate(js))
    except Exception as exc:
        logger.debug("注入 Turnstile 令牌失败: %s", exc)
        return False


def _default_mint(sitekey: str, page_url: str, proxy: str, log: Callable | None,
                  captcha: Any = None) -> str:
    """默认铸币：把用户配的验证码解决器（`RegisterConfig.captcha_solver`）排在最前面。

    配了解决器就先走它 —— 页面 widget 在资料页实测能卡满 45 秒（2026-11-01），
    而配了 YesCaptcha/LocalSolver 的人就是不想等这个。
    """
    from platforms.grok.turnstile_mint import mint_turnstile

    prefer = "captcha" if captcha is not None else ""
    return mint_turnstile(sitekey, page_url, captcha=captcha, proxy=proxy, log=log, prefer=prefer)


def _in_page_mint(page: Any, site_key: str, *, timeout: float, log: Callable | None = None) -> str:
    """在已打开的页面里渲染 Turnstile 取票（实现见 `turnstile_mint`）。

    单独包一层是为了延迟 import：`signup_pages` 顶层 import `turnstile_mint`
    会把浏览器栈一起拉进来，而这些模块在测试里被大量 patch。
    """
    try:
        from platforms.grok.turnstile_mint import mint_in_existing_page
    except Exception as exc:  # noqa: BLE001 - 拿不到就按「没票」处理
        logger.debug("页面内铸票模块不可用: %s", exc)
        return ""
    return mint_in_existing_page(page, site_key, timeout=timeout, log=log)


def ensure_turnstile_token(
    page: Any,
    *,
    sitekey: str = TURNSTILE_SITEKEY,
    page_url: str = "",
    proxy: str = "",
    log: Callable | None = None,
    timeout: float = 45.0,
    poll_seconds: float = 3.0,
    mint: Callable[..., str] | None = None,
    sleep: Callable | None = None,
    captcha: Any = None,
) -> str:
    """先给 widget 时间自己过；过不了就用外挂铸币并注入回调。返回令牌（空串=失败）。

    `captcha` 是用户配的验证码解决器（可为 None），一路传进 `_default_mint`。
    """
    _sleep = sleep or time.sleep
    install_turnstile_hook(page)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        token = read_turnstile_token(page)
        if token:
            if log:
                log(f"Turnstile 由页面 widget 出票（len={len(token)}）")
            return token
        if time.monotonic() >= deadline:
            break
        _sleep(poll_seconds)
    if log:
        log("Turnstile widget 超时未出票，先尝试在当前页面内渲染；不行再转外挂铸币")
    # **先在当前页面里补票**（2026-11-02，用户提问「不考虑用 camoufox 吗」）：
    # 这一页本来就是 camoufox 打开的、已经过了 x.ai 的 CF 并且停在 accounts.x.ai 上，
    # 在它里面渲染 Turnstile 比另起一个 Chromium 更可能成、且零额外进程
    # （本机 2GB 内存，铸币再起浏览器曾直接把服务 OOM 掉）。
    try:
        # 预算与调用方一致（只封顶）：**不加下限** —— 调用方给 0.05s 就只花 0.05s，
        # 否则「别把调用方挂死」这条既有约束会被打破（实测：加 5s 下限就把
        # `test_ensure_respects_the_timeout` 拖到 5 秒）。
        in_page = _in_page_mint(
            page, sitekey, timeout=min(30.0, max(0.0, float(timeout))), log=log
        )
    except Exception as exc:  # 兜底：页面内铸票失败不该炸掉整条链路
        logger.debug("页面内铸票异常: %s", exc)
        in_page = ""
    if in_page:
        if log:
            log(f"Turnstile 页面内铸票成功（len={len(in_page)}）")
        inject_turnstile_token(page, in_page)
        return in_page
    minter = mint or (lambda **kw: _default_mint(
        kw.get("site_key", ""), kw.get("page_url", ""), kw.get("proxy", ""), kw.get("log")
    ))
    try:
        token = str(minter(site_key=sitekey, page_url=page_url or PROFILE_PAGE_URL,
                           proxy=proxy, log=log) or "").strip()
    except Exception as exc:  # 铸币失败不该炸掉整条链路
        if log:
            log(f"Turnstile 铸币失败：{exc}")
        return ""
    if not token:
        return ""
    inject_turnstile_token(page, token)
    confirmed = read_turnstile_token(page)
    if log:
        log(f"Turnstile 外挂令牌已注入（len={len(token)}，读回={'有' if confirmed else '无'}）")
    return confirmed or token


# ── 验证码输入 ────────────────────────────────────────────────────────

_CODE_ADVANCED_MARKERS = ("complete your sign up", "first name", "last name")


def profile_form_present(page: Any) -> bool:
    """资料页（givenName + password 可见）是否已经出现。

    2026-11-01 现场快照：验证码进去之后页面**留在同一个 URL**
    （`/sign-up?redirect=grok-com`），只是把 `input[name=email]` 隐藏、换成
    `givenName` / `familyName` / `password` / `marketingOptIn` 四个可见字段。
    所以「等验证码框」等到超时，其实早就该填资料了 —— 那时唯一的症状就是
    `验证码输入框未出现`，看着像失败，实际已经走到倒数第二步。
    """
    try:
        if _count(page, "input[name=givenName]") < 1:
            return False
        if _count(page, "input[name=password]") < 1:
            return False
        return True
    except Exception as exc:
        logger.debug("探测资料页失败: %s", exc)
        return False


def wait_for_code_or_profile(page: Any, *, timeout: float = 60.0, poll: float = 0.5) -> str:
    """等「验证码框」或「资料页」先出现，返回 `code` / `profile` / 空串（超时）。"""
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if find_code_input(page):
            return "code"
        if profile_form_present(page):
            return "profile"
        if time.monotonic() >= deadline:
            return ""
        time.sleep(poll)


def find_code_input(page: Any) -> str:
    """返回**当前页面上真的存在**的验证码框选择器（都没有则空串）。

    多框 OTP 只返回第一个选择器 —— 调用方点它、然后逐字打字，浏览器会自动
    跳到下一格，这正是参考实现的做法。
    """
    for sel in CODE_INPUT_CANDIDATES:
        try:
            if _count(page, sel) > 0:
                return sel
        except Exception as exc:
            logger.debug("探测 %s 失败: %s", sel, exc)
    # 主文档里没有 —— 再看**子 frame**（OTP 组件被塞进 iframe 是常见做法，
    # 而 `page.locator` 默认只看主文档，这种会表现为「框不存在」）。
    try:
        for frame in list(getattr(page, "frames", []) or []):
            if frame is getattr(page, "main_frame", None):
                continue
            for sel in CODE_INPUT_CANDIDATES:
                try:
                    if frame.locator(sel).count() > 0:
                        logger.debug("验证码框在子 frame 里: %s %s", getattr(frame, "url", ""), sel)
                        return sel
                except Exception as exc:
                    logger.debug("子 frame 探测 %s 失败: %s", sel, exc)
    except Exception as exc:
        logger.debug("遍历 frame 失败: %s", exc)
    return ""


def enter_code(
    page: Any,
    code: str,
    *,
    log: Callable | None = None,
    wait_after: float = 3.0,
    press_enter_if_stuck: bool = True,
    sleep: Callable | None = None,
) -> bool:
    """把验证码打进 `input[name=code]`（去连字符、逐字打），满位会自动提交。"""
    digits = re.sub(r"[^0-9A-Za-z]", "", str(code or ""))
    if not digits:
        return False
    _sleep = sleep or time.sleep
    selector = find_code_input(page) or CODE_INPUT
    # 框可能在子 frame 里 —— `page.locator` 只看主文档，得换到 frame 上。
    host = page
    try:
        if page.locator(selector).count() == 0:
            for frame in list(getattr(page, "frames", []) or []):
                if frame is getattr(page, "main_frame", None):
                    continue
                try:
                    if frame.locator(selector).count() > 0:
                        host = frame
                        break
                except Exception as exc:
                    logger.debug("frame 定位 %s 失败: %s", selector, exc)
    except Exception as exc:
        logger.debug("主文档定位 %s 失败: %s", selector, exc)
    try:
        target = host.locator(selector)
        first = getattr(target, "first", None)
        if first is None:
            if log:
                log("验证码输入框不存在")
            return False
        first.click()
    except Exception as exc:
        if log:
            log(f"验证码输入框点不开：{exc}")
        return False
    try:
        # 页面 maxlength=6 会吃掉连字符，必须逐字慢打
        page.keyboard.type(digits, delay=110)
    except Exception as exc:
        if log:
            log(f"验证码输入失败：{exc}")
        return False
    _sleep(wait_after)
    text = _norm(page_text(page))
    if _match_markers(text, _CODE_ADVANCED_MARKERS):
        return True
    if press_enter_if_stuck:
        try:
            page.keyboard.press("Enter")
            _sleep(min(2.0, wait_after))
            text = _norm(page_text(page))
            if _match_markers(text, _CODE_ADVANCED_MARKERS):
                return True
        except Exception as exc:
            logger.debug("提交验证码后按 Enter 失败: %s", exc)
    if log:
        log("验证码提交后页面未前进")
    return False




def wait_for_profile_form(page: Any, *, timeout: float = 60.0, poll: float = 1.0,
                          sleep: Callable | None = None) -> bool:
    _sleep = sleep or time.sleep
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        if has_profile_form(page_text(page)) or _count(page, GIVEN_NAME_INPUT):
            return True
        if time.monotonic() >= deadline:
            return False
        _sleep(poll)


def _count(page: Any, selector: str) -> int:
    """元素个数。优先用 `.first.count()` —— Playwright 的定位器语义里它等价于
    「该选择器有没有匹配」（老实现也是这么判的），且对各类测试桩都稳。"""
    first = getattr(page.locator(selector), "first", None)
    if first is not None:
        try:
            return int(first.count() or 0)
        except Exception as exc:
            logger.debug("%s 的 .first.count() 不可用: %s", selector, exc)
    try:
        return int(page.locator(selector).count() or 0)
    except Exception as exc:
        logger.debug("%s 计数失败: %s", selector, exc)
        return 0
