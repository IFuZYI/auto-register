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
        # 出口 IP 的问题：换 IP 后同一地址还能用，绝不能烧地址
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
        return (
            f"{email}：x.ai 拒绝邮箱注册（account:email-signup-unavailable）—— "
            "出口 IP 被判定不可注册，换住宅代理后同一地址仍然可用"
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


#: 提交按钮的「文案 + 是否可用」。和 `_CLICK_BY_LABELS_JS` 用同一套打分，
#: 保证「看状态」和「点」落在同一个元素上。
_SUBMIT_STATE_JS = """
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
  if (!best) return {label: '', disabled: true, found: false};
  const label = (best.innerText || best.value || best.textContent || '').replace(/\\s+/g, ' ').trim();
  const disabled = Boolean(best.disabled)
    || best.getAttribute('aria-disabled') === 'true'
    || best.getAttribute('data-disabled') === 'true';
  return {label, disabled, found: true};
}
"""

#: 提交按钮的文案表（与 `click_email_submit` 完全一致）。
_EMAIL_SUBMIT_LABELS = ["sign up", "continue", "続行", "次へ", "weiter", "continuar", "continuer", "계속"]
_EMAIL_SUBMIT_BAN = ["with ", "go back", "sign in", "戻る", "zurück"]


def submit_button_state(page: Any) -> dict[str, Any]:
    """当前「提交」按钮的文案与是否禁用（找不到时 found=False）。"""
    args = {"labels": list(_EMAIL_SUBMIT_LABELS), "ban": list(_EMAIL_SUBMIT_BAN)}
    try:
        out = page.evaluate(_SUBMIT_STATE_JS, args)
    except Exception as exc:
        logger.debug("读提交按钮状态失败: %s", exc)
        return {"label": "", "disabled": True, "found": False}
    return out if isinstance(out, dict) else {"label": "", "disabled": True, "found": False}


def email_input_present(page: Any) -> bool:
    """邮箱输入框还在不在 —— 用来判断「点了之后页面到底动没动」。"""
    return _count(page, EMAIL_INPUT) > 0


def submit_email_code(page: Any, *, wait_timeout: float = 8.0, poll: float = 0.3) -> tuple[str, dict]:
    """等提交按钮**可用**再点，返回 `(点到文案, 点击前状态)`。

    为什么不能直接 `click_button`（线上实测 2026-11-01）：

    邮箱视图刚出现时 React 还没挂上事件处理，或者按钮因为邮箱校验没过而
    `disabled` —— 这两种情况下 `el.click()` **什么都不会发生**，但
    `click_button` 照样把按钮文案返回，日志于是写着「发码已提交（Sign up）」，
    实际上一个请求都没发出去。接着等 9 秒超时，结论是
    `no_confirmation: 未确认发信（页面未给出理由）` —— 提示把「点了个空」
    说成了「上游没确认」，排查方向全错。

    所以这里先轮询到按钮存在且未禁用（最多 `wait_timeout` 秒），再点，
    并把点击前的状态返回给调用方记日志。
    """
    deadline = time.monotonic() + max(0.0, float(wait_timeout))
    state: dict[str, Any] = {"label": "", "disabled": True, "found": False}
    while True:
        state = submit_button_state(page)
        if state.get("found") and not state.get("disabled"):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(poll)
    label = click_email_submit(page)
    return label, state


def click_email_submit(page: Any) -> str:
    return click_button(page, list(_EMAIL_SUBMIT_LABELS), ban=list(_EMAIL_SUBMIT_BAN))


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
        log("Turnstile widget 超时未出票，转外挂铸币")
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
    try:
        target = page.locator(CODE_INPUT)
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


def wait_for_code_input(page: Any, *, timeout: float = 90.0, poll: float = 1.0,
                        sleep: Callable | None = None) -> bool:
    return wait_for_selector(page, CODE_INPUT, timeout=timeout, poll=poll, sleep=sleep)


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
