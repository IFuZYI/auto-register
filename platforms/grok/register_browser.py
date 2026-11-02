"""Grok 全流程注册（浏览器主导）—— 本项目实测跑通的路径。

## 为什么是「浏览器主导」
本机逐项实测（2026-09-30）把每个环节的可行路径逼出来了：

| 环节 | 结论 |
|---|---|
| 浏览器引擎 | **camoufox 是唯一能过 x.ai CF 的**。chromium（playwright）无论直连还是住宅代理，提交发码都被 CF 403；camoufox 通过并拿到 x.ai 真实答复 |
| 发码 | 必须走真实页面（协议 gRPC/REST 都「假接受」：grpc=0 / ok:true 但零投递） |
| 域名 | x.ai 接受主流域（@icloud.com 实测通过）；拒绝小域名（YYDS 8/8 被拒） |
| 验证码 | 页面会自动格式化（`955-995` → `955995`），用键盘逐字输入才稳 |
| OAuth | 协议 device flow 的 approve 被 CF 403，**必须走浏览器兜底** |

## 2026-11-01：x.ai 换新版前端（Next.js / SpaceXSI 品牌），旧流程失效
真机 + 前端 bundle 反查（`accounts.x.ai/_next/static/**`）确认的**新四步**：

1. 入口 `https://accounts.x.ai/sign-up?redirect=grok-com`：只有 6 个按钮
   （X / email / Apple / Google / GitHub / Sign in），**没有邮箱输入框**。
   点文案精确等于 `Sign up with email` 的按钮才进下一步（按 `signup` 子串匹配会
   点到 `Sign up with GitHub`，跳到 github.com 的 OAuth 页）。
2. 邮箱页：`input#email` → 提交 → `POST /api/auth/send-verification-code`
   `{email, castleRequestToken}`（Castle 令牌由页面自动生成）。
3. 验证码页：`input[name=code]`（`maxlength=6`、**不含连字符**、满 6 位自动提交）
   → `POST /api/auth/sign-up/verify-email {email, code}`。
4. 资料页：`input#givenName` / `input#familyName` / `input#password` + **Turnstile**
   → `POST /api/auth/sign-up/create-account`（带 `turnstileToken`）→ SSO cookie。

**发码被拒有两种，处置完全不同（A/B 实测）**：

| 响应 | 归属 | 处置 |
|---|---|---|
| `400 {"code":"email:send-validation-code-rate-limited"}` | 跟**邮箱**走（约 4–6 次/地址，含失败） | 可重试，地址不消耗 |
| `400 {"code":"account:email-signup-unavailable"}` | 跟**出口 IP** 声誉走（换住宅出口同一地址立刻成功） | 可重试，地址不消耗 |
| 页面 `Something went wrong…`（客户端没发出请求） | 环境问题 | 可重试，地址不消耗 |
| 页面「这个邮箱已有账号」 | 该地址确实用掉了 | **消耗地址，不可重试** |

旧实现把前三种都当「地址已消耗」→ 每次失败都烧一个别名，且被判死不再重试
（审计 R-22）。新版分类逻辑在 `signup_pages.py`，这里是兼容转发。

资料页的 Turnstile 令牌由**本页 widget** 经 `window.turnstile.render` 的
callback 交给 React state，**写 DOM 隐藏域无效**；sitekey 见
`signup_pages.TURNSTILE_SITEKEY`，铸币兜底走 `turnstile_mint.py`。

## 与 plugin.py 的关系
`plugin.py` 是任务框架调用的入口（邮箱由号池注入）。本模块把上面这套
「浏览器主导」的实现独立出来，供需要真投递的场景直接调用；两者共用
`grok2api` 等基础件（Turnstile 取 token 走 `turnstile_mint.py`）。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import re
import time
import os
import random
from typing import Any, Callable, Optional

from core.proxy_utils import build_playwright_proxy_config, build_requests_proxy_config
from core.task_runtime import TaskInterruption

# SIGNUP_URL / CODE_RE 复用 constants 里的定义：各自已有单一来源，
# 在这里再写一份意味着改一处要同步 N 处（评审发现）。
from . import signup_pages
from .constants import CODE_RE_ANY as CODE_RE
from .constants import FAMILY_NAMES, GIVEN_NAMES, SIGNUP_URL
from .profile import generate_password

#: iCloud 租约的续约间隔（秒）—— 与渠道层同一口径（modules/mail/icloud_*.py）。
LEASE_RENEW_INTERVAL_SECONDS = 60

#: 提交发码后等「确认已发信」的时长（秒）。真实链路里 API 响应是几百毫秒级、
#: 验证码页 1–3 秒内出现；超时还没有任何确认就是「客户端没发出请求」那一类
#: （实测出现的 "Something went wrong. Refresh the page and try again."）。
SEND_CODE_SETTLE_TIMEOUT = 8.0

LogFn = Optional[Callable[[str], None]]

# 「该邮箱已注册」的判定模式。取值对齐 reference/grok/grok-register 的
# `signup_flow._ALREADY_REGISTERED_PATTERNS`（最新参考实现，实测维护过 11 条），
# 而不是只匹配两个字面量 —— x.ai 的文案会变、还可能返回中文，窄匹配会漏判，
# 漏判的后果是把这个别名当成「本轮失败」放回 available，下次再领出来撞同一堵墙。
_ALREADY_REGISTERED_PATTERNS = (
    re.compile(r"existing account found", re.I),
    re.compile(r"email.{0,80}already.{0,40}(?:registered|exists|in use|used|taken)", re.I),
    re.compile(r"account.{0,80}already.{0,40}(?:registered|exists)", re.I),
    re.compile(r"already.{0,40}registered.{0,80}email", re.I),
    re.compile(r"user.{0,60}already.{0,40}exists", re.I),
    re.compile(r"email.{0,40}(?:is|address is).{0,20}(?:unavailable|not available)", re.I),
    re.compile(r"邮箱.{0,40}(?:已|已经).{0,40}(?:注册|存在|使用|占用)"),
    re.compile(r"账号.{0,40}(?:已|已经).{0,40}(?:注册|存在)"),
    re.compile(r"找到现有(?:账号|账户)"),
    re.compile(r"(?:账号|账户).{0,20}(?:已|已经).{0,40}(?:注册|存在)"),
    re.compile(r"已存在与此邮箱地址关联的(?:账号|账户)"),
)


#: 发码页面的判定结果。线上实测（2026-10-23，抓包确认）：
#: 发码请求确实发出（`POST /api/auth/send-verification-code`），x.ai 会用
#: 三种不同文案拒绝 / 接受，含义完全不同，必须分开处置。
#:
#: 2026-11-01 修正：分类逻辑搬到 `signup_pages`（新版前端的可机器读错误码 +
#: 文案表），这里保留常量与函数名做兼容。**旧版「三类拒绝都算地址已消耗」的
#: 结论是错的** —— 实测同一地址换住宅代理即可成功发码（`email-signup-unavailable`
#: 跟出口 IP 声誉走），限流也只是一段时间。烧地址会导致号池被无谓消耗。
SEND_CODE_SENT = "sent"
SEND_CODE_RATE_LIMITED = "rate_limited"
SEND_CODE_EMAIL_UNAVAILABLE = "email_unavailable"
SEND_CODE_NO_CONFIRMATION = "no_confirmation"
SEND_CODE_ALREADY_REGISTERED = "already_registered"
SEND_CODE_TRANSIENT = "transient_error"
SEND_CODE_CF_CHALLENGE = "cloudflare_challenge"
SEND_CODE_DOMAIN_REJECTED = "email_domain_rejected"

#: 限流 / 不可用 / 已发出的文案表保留在 `signup_pages`（唯一事实源）
_RATE_LIMIT_MARKERS = signup_pages._RATE_LIMIT_MARKERS
_EMAIL_UNAVAILABLE_MARKERS = signup_pages._EMAIL_UNAVAILABLE_MARKERS
_SENT_MARKERS = (
    "we've emailed",
    "we’ve emailed",
    "verify your email",
    "check your email",
)


def classify_send_code_page(text: str) -> dict:
    """判定发码提交后的页面处于哪种状态（转发到 `signup_pages`）。

    返回 `{"kind", "retryable", "consumes_address", "retry_after_hint", "detail"}`。

    历史上这里把「任何没认出来的文本」都当作地址已消耗（`consumes_address=True`），
    后果是 CF 挑战页 / 网络半截 / 未渲染完都在白烧别名（审计 R-22）。
    2026-11-01 起：**只有「该邮箱已有账号」与「域名被拒」才消耗地址**。
    """
    blob = str(text or "")
    low = blob.lower()
    if any(m in low for m in _SENT_MARKERS):
        return {
            "kind": SEND_CODE_SENT,
            "retryable": False,
            "consumes_address": False,
            "retry_after_hint": "",
            "already_registered": False,
            "detail": "",
        }
    return signup_pages.classify_send_code_page(blob)



def _looks_already_registered(text: str) -> bool:
    """页面文本是否表明「这个邮箱已经有账号」。"""
    blob = str(text or "")
    if not blob:
        return False
    return any(p.search(blob) for p in _ALREADY_REGISTERED_PATTERNS)


def _resolve_exit_ip(proxy: str, *, timeout: float = 12.0) -> str:
    """尽力解析代理的出口 IP，用于让 camoufox 对齐时区/语言。

    为什么不等 camoufox 自己来：它的 `geoip=True` 会用 6 个公有 IP 探测服务
    逐个反查、每个 5s 超时，住宅代理上实测直接抛
    `camoufox.exceptions.InvalidIP: Failed to get IP address: … read timeout=5`，
    浏览器起不来（2026-11-01）。这里自己做、自己做超时控制，拿不到就返回空串，
    调用方退化成 `geoip=False`（实测能正常起浏览器，只是会打一条 LeakWarning）。
    """
    cfg = build_requests_proxy_config(proxy)
    if not cfg:
        return ""
    import requests

    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            resp = requests.get(url, proxies=cfg, timeout=timeout)
            ip = (resp.text or "").strip()
            if resp.ok and ip and len(ip) <= 45 and " " not in ip and "<" not in ip:
                return ip
        except Exception as exc:
            logger.debug("出口 IP 解析失败 %s: %s", url, exc)
    return ""


def _log(fn: LogFn, msg: str) -> None:
    if fn:
        try:
            fn(msg)
        except Exception:
            logger.debug("忽略异常（保留原行为）", exc_info=True)


def _wait_for_sso_cookie(
    page: Any, *, timeout: float = 35, poll_ms: int = 500
) -> str:
    """轮询等 `sso` cookie 落地，返回它的值（超时返回空串）。

    提交建号表单后 x.ai 用 **cookie-chain 跳链**下发 sso：首跳是 accounts.x.ai
    的 JS 中间页，再依次跨 auth.grokpedia.com / auth.x.ai /
    auth.grokusercontent.com / auth.grok.com / auth.cursor.com 各跳一次
    `/set-cookie?q=…`，最后才回落到 sso。

    **不能只等一个固定时长**（原实现是 15 秒）：跳链没跑完就返回，于是拿不到
    sso、把**成功**的注册误判成失败 —— 实测同一份代码时好时坏。参考项目的处理
    就是轮询（`reference/grok/grok-hub-clean/patched/grok_register/register.py:
    845-860`，`COOKIE_CHAIN_WAIT_SECONDS` 默认 30 秒）。
    """
    deadline = time.time() + timeout
    while True:
        cookies = {
            c["name"]: c["value"]
            for c in page.context.cookies()
            if "name" in c and "value" in c
        }
        sso = cookies.get("sso", "")
        if sso or time.time() >= deadline:
            return sso
        page.wait_for_timeout(poll_ms)


#: cookie 同意横幅的按钮文案兜底清单。
#: 实测（2026-10-06）：真机是 OneTrust 横幅，按钮文案为 'Allow All' /
#: 'Reject All' / 'Confirm My Choices' —— 旧实现只试 'Accept All Cookies' /
#: 'Close' / 'Accept all'，全部落空；横幅遮住表单时点击落空，
#: 表象是「找不到邮箱输入框」（排查方向全错）。
_COOKIE_BANNER_BUTTON_NAMES = (
    "Allow All",
    "Accept All Cookies",
    "Accept all",
    "Accept All",
    "I Accept",
    "Close",
)

#: OneTrust 横幅的 id 选择器（文案兜底之外的第二条路）。
_COOKIE_BANNER_SELECTORS = (
    "#onetrust-accept-btn-handler",
    ".onetrust-close-btn-handler",
    "#onetrust-pc-btn-handler",
)


def _dismiss_cookie_banner(page: Any) -> None:
    """尽力关掉 cookie 同意横幅（有就点，没有就过，绝不抛错）。

    两条路：文案兜底（`_COOKIE_BANNER_BUTTON_NAMES`）+ OneTrust id 选择器。
    """
    for name in _COOKIE_BANNER_BUTTON_NAMES:
        try:
            page.get_by_role("button", name=name).first.click(timeout=1200)
            page.wait_for_timeout(400)
            return
        except Exception:
            continue
    for sel in _COOKIE_BANNER_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count():
                loc.click(timeout=1200)
                page.wait_for_timeout(400)
                return
        except Exception:
            continue


#: 建号表单三个字段的选择器清单（按优先级）。
#: 命名约定两套并存：服务端 JSON 用 givenName/familyName，DOM 上实测
#: 还可能是 firstName/lastName（placeholder 'First name'/'Last name'）。
_SIGNUP_FIELD_SELECTORS = {
    "givenName": (
        "input[name=givenName]",
        "input#firstName",
        "input[name=firstName]",
    ),
    "familyName": (
        "input[name=familyName]",
        "input#lastName",
        "input[name=lastName]",
    ),
    "password": (
        "input[type=password]",
        "input[name=password]",
    ),
}

#: placeholder 兜底（选择器都落空时按可见文案找）。
_SIGNUP_FIELD_PLACEHOLDERS = {
    "givenName": ("First name", "Given name"),
    "familyName": ("Last name", "Family name"),
    "password": ("Password",),
}


def _fill_signup_form(page: Any, password: str) -> list[str]:
    """填建号表单，返回**读回为空**的字段名列表（空列表 = 全部填上）。

    实测教训（2026-10-06）：填充静默失败（选择器没命中 / 受控输入拒收）
    时照样提交，表象是「未拿到 SSO」—— 排查方向全错。这里每填一个字段
    就**读回校验**，读不回值即计入 missing，由调用方决定报错而不是盲提交。
    """
    missing: list[str] = []
    values = {"givenName": "James", "familyName": "Smith", "password": password}

    for field, value in values.items():
        filled = False
        for sel in _SIGNUP_FIELD_SELECTORS[field]:
            try:
                loc = page.locator(sel).first
                if loc.count():
                    loc.fill(value, timeout=3500)
                    filled = True
                    break
            except Exception:
                continue
        if not filled:
            # placeholder 兜底
            for name in _SIGNUP_FIELD_PLACEHOLDERS[field]:
                try:
                    loc = page.get_by_placeholder(name).first
                    if loc.count():
                        loc.fill(value, timeout=3500)
                        filled = True
                        break
                except Exception:
                    continue
        if not filled:
            missing.append(field)
            continue
        # 读回校验：fill 成功不代表值真的进去了（受控输入可能拒收）
        try:
            readback = page.evaluate(
                """(field) => {
                    const sels = {
                        givenName: ['input[name=givenName]', 'input#firstName', 'input[name=firstName]'],
                        familyName: ['input[name=familyName]', 'input#lastName', 'input[name=lastName]'],
                        password: ['input[type=password]', 'input[name=password]'],
                    }[field] || [];
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el && el.value) return el.value;
                    }
                    return '';
                }""",
                field,
            )
        except Exception:
            readback = ""
        if not str(readback or "").strip():
            missing.append(field)
    return missing


def _submit_signup_form(page: Any) -> None:
    """提交建号表单：按钮文案 → 表单 submit 按钮 → Enter，三档兜底。

    实测（2026-10-06）：真机按钮文案是 'Complete your sign up'；旧实现
    只匹配 complete sign up|create|sign up（缺 'your'），且只试一次。
    """
    for name in ("Complete your sign up", re.compile(r"complete sign ?up|create account|sign up", re.I)):
        try:
            page.get_by_role("button", name=name).last.click(timeout=6000)
            return
        except Exception:
            continue
    try:
        page.locator("form button[type=submit], button[type=submit]").last.click(timeout=3000)
        return
    except Exception:
        logger.debug("忽略异常（保留原行为）", exc_info=True)
    page.keyboard.press("Enter")


# 密码生成统一走 profile.generate_password（secrets + 与参考实现同格式）。
# 这里原先自己写了一份 random 版，等于同一件事有两套实现、两套强度 ——
# 评审发现后删掉，只保留别名，避免调用点还要改 import。
_gen_password = generate_password


#: 发码后「页面已进入验证码页」的确认文案
_CODE_PAGE_MARKERS = (
    "verify your email",
    "one time security code",
    "please enter it below",
    "didn't receive a code",
)


def _looks_like_code_page(text: str) -> bool:
    low = str(text or "").lower().replace("\u2019", "'")
    return any(m in low for m in _CODE_PAGE_MARKERS)


def _stage_verdict_fields(verdict: dict, email: str, *, log: LogFn = None) -> dict:
    """把 `signup_pages` 的判定翻成 `register_grok_via_browser` 的结果字段。"""
    kind = str((verdict or {}).get("kind") or "")
    out = {
        "ok": False,
        "kind": kind,
        "error": signup_pages.send_code_error_text(verdict, email),
        "retryable": bool((verdict or {}).get("retryable")),
        "consumes_address": bool((verdict or {}).get("consumes_address")),
        "already_registered": bool((verdict or {}).get("already_registered")),
        "email_unavailable": kind == signup_pages.SEND_RESULT_EMAIL_UNAVAILABLE,
        "rate_limited": kind == signup_pages.SEND_RESULT_RATE_LIMITED,
        "cloudflare_challenge": kind == signup_pages.SEND_RESULT_CF_CHALLENGE,
    }
    _log(log, f"[Grok] 发码结局 {kind}: {out['error'][:120]}")
    return out


def _send_code_stage(
    page: Any,
    email: str,
    *,
    log: LogFn = None,
    timeout: float | None = None,
    _test_responses: Optional[list] = None,
    proxy: str = "",
    captcha: Any = None,
) -> dict:
    """填邮箱 → 提交 → 判定发码结局。

    判定优先级：**API 响应的机器可读错误码 > 页面文案**。新前端的发码接口是
    `POST /api/auth/send-verification-code`，响应体带 `code`
    （`account:email-signup-unavailable` / `email:send-validation-code-rate-limited`），
    比文案可靠得多（文案是排版撇号、还会变）。抓不到响应时才看页面。
    """
    settle = SEND_CODE_SETTLE_TIMEOUT if timeout is None else float(timeout)
    captured: list[dict[str, Any]] = []
    seen: list[str] = []

    def _on_response(resp: Any) -> None:
        try:
            url = str(getattr(resp, "url", "") or "")
        except Exception:
            url = ""
        if "send-verification-code" not in url:
            # 不是发码接口也记一笔：判定成 `no_confirmation` 时最需要知道的
            # 就是「请求到底发出去没有、发去了哪个地址」。上游改过一次接口
            # 路径，监听器按子串匹配，改名后一声不响地什么都抓不到。
            if "/auth/" in url and len(seen) < 20:
                seen.append(f"{getattr(resp, 'status', '?')} {url}")
            return
        body = ""
        try:
            body = resp.text()
        except Exception:
            body = ""
        captured.append({"status": getattr(resp, "status", None), "body": body})

    # 抓**请求**（不只是响应）：如果我们的请求体和真页面不一样（少 Castle /
    # Turnstile 令牌、少某个头），服务端就会回 `email-signup-unavailable` ——
    # 那是通用拒绝，看响应永远看不出差在哪，只有对比请求才看得出来。
    def _on_request(req: Any) -> None:
        try:
            url = str(getattr(req, "url", "") or "")
            if "/auth/" not in url:
                return
            method = str(getattr(req, "method", "") or "")
            if method.upper() != "POST":
                return
            try:
                body = req.post_data or ""
            except Exception:
                body = ""
            try:
                hdrs = req.headers or {}
            except Exception:
                hdrs = {}
            keys = sorted(k for k in hdrs if any(
                s in k.lower() for s in ("castle", "turnstile", "cf-", "x-", "referer", "origin")
            ))
            if len(seen_req) < 10:
                seen_req.append(
                    f"{method} {url.split('?')[0]} body={str(body)[:160]!r} "
                    f"headers={ {k: hdrs.get(k) for k in keys} }"
                )
        except Exception as exc:
            logger.debug("记录请求失败: %s", exc)

    seen_req: list[str] = []

    try:
        page.on("request", _on_request)
    except Exception as exc:
        logger.debug("无法监听请求: %s", exc)
    try:
        page.on("response", _on_response)
    except Exception:
        logger.debug("无法监听网络响应，退化到只看页面文案", exc_info=True)

    # 加载后页面如果已经是失败态（限流 / 邮箱注册不可用 / 已有账号 / CF 挑战）
    # 就没必要再填表发一次码 —— 每发一次都在烧地址的发码配额（4–6 次/地址）。
    pre = signup_pages.detect_blocking_state(signup_pages.page_text(page))
    if pre is not None:
        return _stage_verdict_fields(pre, email, log=log)

    # 逐字**输入**，不用 `fill`。
    #
    # 参考实现（Grok-farm README）：「Click the email field and **type** the
    # address — `fill()` leaves React state unarmed and the submit never fires.」
    # 我们的 `set_field_value` 是原生 setter + 派发事件，通常够；但真机上有
    # 受控组件只认「真实按键」的情况，所以先试打字，失败再退回 setter。
    filled = False
    try:
        field = page.locator(signup_pages.EMAIL_INPUT).first
        field.click(timeout=6000)
        page.keyboard.type(email, delay=45)
        filled = str(field.input_value() or "").strip() == email
    except Exception:
        logger.debug("逐字输入邮箱失败，退回 setter", exc_info=True)
    if not filled:
        filled = signup_pages.set_field_value(page, signup_pages.EMAIL_INPUT, email)
    if not filled:
        try:
            page.locator(signup_pages.EMAIL_INPUT).first.fill(email, timeout=8000)
            filled = True
        except Exception:
            logger.debug("直接 fill 邮箱失败", exc_info=True)
        if not filled:
            return {
                "ok": False, "kind": "", "retryable": True, "consumes_address": False,
                "already_registered": False, "email_unavailable": False, "rate_limited": False,
                "cloudflare_challenge": False,
                "error": "找不到邮箱输入框（新版入口页未出现 Sign up with email）",
            }
    # 填完要**把表单「武装」起来**：受控组件只有收到真实按键 + blur 才会把值
    # 写进 React state，否则按钮一直 disabled。线上日志（2026-11-02）连续十次
    # 都是「按钮是禁用态（邮箱校验没过？）」—— 就是这个。
    for _arm in range(3):
        try:
            page.keyboard.press("Tab")          # blur，触发校验
        except Exception:
            logger.debug("Tab 失败", exc_info=True)
        page.wait_for_timeout(700)
        st = signup_pages.submit_button_state(page)
        if st.get("found") and not st.get("disabled"):
            break
        logger.debug("邮箱字段未武装（第 %s 次）：%s", _arm + 1, st)
        try:
            field = page.locator(signup_pages.EMAIL_INPUT).first
            field.click(timeout=6000)
            page.keyboard.press("ControlOrMeta+a")
            page.keyboard.press("Delete")
            page.keyboard.type(email, delay=60)
        except Exception:
            logger.debug("重新输入邮箱失败", exc_info=True)
    page.wait_for_timeout(300)
    _log(log, f"[Grok] 已填邮箱 {email}，提交发码…")
    state: dict[str, Any] = {}

    # 点提交要**确认它真的发出去了**。邮箱视图刚出现时 React 可能还没挂上
    # 事件处理，或者按钮因为校验没过而 disabled —— 这时 `el.click()` 什么都不会
    # 发生，但 `click_button` 照样返回按钮文案。线上实测（2026-11-01）日志写着
    # 「发码已提交（Sign up）」，实际一个请求都没发出去，9 秒后结论是
    # `no_confirmation: 未确认发信（页面未给出理由）` —— 把「点了个空」说成了
    # 「上游没确认」，排查方向全错。
    #
    # 所以：等按钮可用再点，点完看页面有没有动，没动就再点（最多 3 次）。
    # 提交梯子（三档：按钮 → form.requestSubmit → Enter，见
    # `signup_pages.submit_email_code`）。点了不等于发出去了，所以每轮都看
    # 「页面动没动」：动了（邮箱框没了 / 抓到响应）才收工。
    # 别抢跑：Castle / Turnstile 的反机器人令牌要几秒才落地，令牌没到手就提交，
    # 服务端只会回一句 `account:email-signup-unavailable`（通用拒绝）。
    # 参考实现的原话：「Turnstile/Castle needs 30-45 s to settle — do not shorten
    # it before the submit path.」我们取 12 秒上限、拿到令牌就立刻走。
    # Castle 就绪（桩或真 SDK）—— 没有它，点按钮**一个请求都不会发**。
    if not signup_pages.wait_for_castle(page, timeout=15.0):
        _log(log, "[Grok] Castle 未就绪（提交处理会静默 return），仍尝试提交")
    gate = signup_pages.wait_until_submittable(page, timeout=15.0)
    if not gate.get("token_len") and captcha is not None:
        # **邮箱页也有 Turnstile**（`input[name=cf-turnstile-response]`），
        # 和资料页是同一个 sitekey。之前只在资料页铸币，邮箱页全靠 widget 自己
        # 出票 —— 出不来时按钮就一直是禁用态，然后我们硬发 `form.requestSubmit()`
        # 拿到 `account:email-signup-unavailable`（2026-11-02 连续十次）。
        # 这里补上：先等 widget，超时就用同一套铸币 + 注入。
        _log(log, "[Grok] 邮箱页 Turnstile 无票，尝试铸币…")
        token = signup_pages.ensure_turnstile_token(
            page, page_url=signup_pages.SIGNUP_URL, proxy=proxy,
            log=log, captcha=captcha, timeout=20.0,
        )
        if token:
            _log(log, f"[Grok] 邮箱页 Turnstile 令牌已就绪（len={len(token)}）")
        gate = signup_pages.wait_until_submittable(page, timeout=10.0)
    elif not gate.get("token_len"):
        _log(log, "[Grok] 邮箱页 Turnstile 无票，且没配验证码解决器（只能等 widget 自己出票）")
    if not gate.get("ready"):
        # **不提交**。按钮禁用 / 没令牌时硬提交，服务端只会回
        # `account:email-signup-unavailable`（2026-11-02 线上日志连续 10 次都是
        # 这个组合），既拿不到码，又把这个出口的信任分白送掉。
        # 这一轮直接算「页面没准备好」，地址没消耗，换一轮重来。
        _log(log, "[Grok] 页面未就绪：Turnstile 没给令牌、提交按钮一直禁用，本轮不提交")
        return {
            "ok": False, "kind": "", "retryable": True, "consumes_address": False,
            "already_registered": False, "email_unavailable": False,
            "rate_limited": False, "cloudflare_challenge": False,
            "antibot_not_ready": True,
            "error": (
                "页面未就绪（Turnstile 未给令牌 / 提交按钮一直禁用），"
                "已跳过提交以免被上游判为不可信"
            ),
        }
    _log(log, f"[Grok] 可以提交（令牌 {gate.get('token_len')} 字符，按钮{'可点' if not gate.get('button_disabled') else '仍禁用'}）")
    # 提交前再确认一次邮箱还在框里：等令牌的十几秒里页面可能重渲染把受控字段
    # 清空，`_SUBMIT_EMAIL_JS` 会因为 `input-empty` 直接放弃（返回 found=true /
    # disabled=true，日志看着像「按钮是禁用态」，其实是**框空了**）。
    if not signup_pages.email_value_matches(page, email):
        _log(log, "[Grok] 邮箱框被清空，重新输入")
        signup_pages.retype_email(page, email)
    label, state = signup_pages.submit_email_code(page)
    first_state = dict(state or {})   # 第一次的结论才是「我们怎么发的」
    if state.get("reason") in ("input-empty", "input-invalid", "no-input"):
        _log(log, f"[Grok] 提交未生效（{state.get('reason')}），再补一次邮箱后重试")
        signup_pages.retype_email(page, email)
        label, state = signup_pages.submit_email_code(page)
    attempts = 1
    while attempts < 3:
        page.wait_for_timeout(1500)
        if captured or not signup_pages.email_input_present(page):
            break
        # **按钮变禁用 = 请求在飞**，不是「没点着」。
        # 2026-11-02 线上：第一次点成功后 React 把按钮置灰（pending），我们却
        # 以为没生效又发 `form.requestSubmit()` 两次 —— 重复提交把页面打成
        # `something went wrong` / 静默失败，最后归因成「未确认发信」。
        st = signup_pages.submit_button_state(page)
        if st.get("found") and st.get("disabled"):
            logger.debug("按钮已置灰（请求在飞），不再重复提交")
            break
        label, state = signup_pages.submit_email_code(page)
        attempts += 1
    # **兜底：页面自己不发请求时，直接调接口。**
    # 2026-11-02 实测：点「Sign up」一个 POST 都不发（页面提交处理坏了），
    # 而同一会话直接 fetch 这个接口回 `{"ok":true}`（四个域名全过）。
    # 发完 reload 一次，服务端已记下「这个邮箱待验证」，页面会自己往下走。
    if not captured:
        direct = signup_pages.send_code_via_fetch(page, email)
        if direct.get("ok"):
            _log(log, "[Grok] 页面未发请求，已直接调接口发码（ok:true）")
            try:
                page.reload(wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                page.wait_for_timeout(2500)
            except Exception as exc:
                logger.debug("发码后刷新失败: %s", exc)
            # **直接判定成功并返回**。不要把结论交给后面的响应分类：
            # 刷新后页面可能自己再发一次（重复发码 → 上游回 `email-signup-unavailable`），
            # 那条响应会把「已经发成功了」覆盖成「被拒」（2026-11-02 实测：
            # 03:06:42 拿到 ok:true，03:06:59 却判成 email_unavailable）。
            return {
                "ok": True, "kind": signup_pages.SEND_RESULT_SENT, "error": "",
                "retryable": False, "consumes_address": False, "already_registered": False,
                "email_unavailable": False, "rate_limited": False,
                "cloudflare_challenge": False, "sent_at": time.time(),
            }
        else:
            _log(log, f"[Grok] 直接调接口也没成功（HTTP {direct.get('status')}）：{str(direct.get('body'))[:120]}")
    how = str((first_state or {}).get("how") or (state or {}).get("how") or "")
    how_text = {"button": label or "按钮", "form": "form.requestSubmit()", "enter": "输入框 Enter"}.get(how, "")
    suffix = ""
    if state and not state.get("found") and state.get("reason") in ("no-button", "button-disabled"):
        suffix = "｜⚠ 没有可用的提交按钮"
    # 只有**找到了按钮但它禁用**才说这句。没找到按钮时 `disabled` 也是 True，
    # 再说一遍「按钮是禁用态」会让人去找一个不存在的东西。
    if first_state and first_state.get("disabled") and first_state.get("found"):
        suffix += "｜⚠ 按钮是禁用态（邮箱校验没过？）"
    if attempts > 1:
        suffix += f"｜重试 {attempts} 次"
    _log(log, f"[Grok] 发码已提交（{how_text or label or '？'}）{suffix}")
    # 记下发码时刻（墙钟）：读码只看比它晚到的信，否则同地址重试会读到
    # 上一轮留在邮箱里的旧码（线上实测 2026-11-01，别名 535）。
    sent_at = time.time()

    deadline = time.monotonic() + max(0.0, settle)
    poll = 0.5
    while True:
        for resp in _test_responses or ():
            _on_response(resp)
        if captured:
            verdict = signup_pages.classify_send_code_result(
                captured[-1].get("status"), captured[-1].get("body"),
                signup_pages.page_text(page),
            )
            if verdict["kind"] == signup_pages.SEND_RESULT_SENT:
                return {
                    "ok": True, "kind": verdict["kind"], "error": "", "retryable": False,
                    "consumes_address": False, "already_registered": False,
                    "email_unavailable": False, "rate_limited": False,
                    "cloudflare_challenge": False, "sent_at": sent_at,
                }
            return _stage_verdict_fields(verdict, email, log=log)
        text = signup_pages.page_text(page)
        if _looks_like_code_page(text):
            _log(log, "[Grok] 页面已进入验证码页")
            return {
                "ok": True, "kind": SEND_CODE_SENT, "error": "", "retryable": False,
                "consumes_address": False, "already_registered": False,
                "email_unavailable": False, "rate_limited": False, "cloudflare_challenge": False,
                "sent_at": sent_at,
            }
        blocking = signup_pages.detect_blocking_state(text)
        if blocking is not None:
            return _stage_verdict_fields(blocking, email, log=log)
        if _looks_already_registered(text):
            return _stage_verdict_fields(
                signup_pages._verdict_for_kind(signup_pages.SEND_RESULT_ALREADY_REGISTERED),
                email, log=log,
            )
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(int(poll * 1000))
    verdict = signup_pages.classify_send_code_page(text)
    if verdict.get("kind") == signup_pages.SEND_RESULT_EMAIL_UNAVAILABLE and seen_req:
        # 被拒时把**发出去的请求**也带上：请求体和真页面不一样（少 Castle /
        # Turnstile 令牌、少某个头）是这类通用拒绝最常见的原因，而看响应永远
        # 看不出来。
        verdict = dict(verdict)
        verdict["detail"] = (
            str(verdict.get("detail") or "") + "｜发出去的请求: " + " ;; ".join(seen_req[:2])[:500]
        )
    if verdict["kind"] == signup_pages.SEND_RESULT_NO_CONFIRMATION:
        verdict = dict(verdict)
        # detail 会被 `send_code_error_text` 套进「未确认发信（…）」里，
        # 所以这里不能再说一次「未确认发信」——线上实测套了两层（2026-11-01）。
        #
        # 还要把「请求有没有发出去」写清楚：没发出去（按钮禁用 / 没挂上事件）
        # 和发出去了但上游不认，是两件完全不同的事，提示里必须能分开。
        why = f"页面未给出理由: {str(text)[:120]}"
        if how_text:
            why += f"｜已用 {how_text} 触发提交"
        if not captured and not seen:
            why += "｜没抓到任何 /auth/ 请求 —— 提交大概率没生效"
        elif not captured:
            why += f"｜只见到 {len(seen)} 个其它 auth 请求（首个 {seen[0][:90]}）—— 发码接口可能改名了"
        if state and state.get("disabled"):
            why += "｜提交按钮是禁用态"
        if seen_req:
            why += "｜发出去的请求: " + " ;; ".join(seen_req[:2])[:400]
        diag = (state or {}).get("diag") or {}
        if diag:
            btns = "/".join(str(b) for b in (diag.get("buttons") or [])[:4]) or "无"
            ins = "/".join(str(x) for x in (diag.get("inputs") or [])[:4]) or "无"
            why += f"｜页面按钮[{btns}] 输入框[{ins}]"
        verdict["detail"] = why
    return _stage_verdict_fields(verdict, email, log=log)


#: 等入口页渲染出注册按钮的秒数（超时就刷新一次再等一轮）。
#: 15 秒够用了：实测正常出口上 `domcontentloaded` + 3 秒就渲染完；
#: 超过这个时间还不渲染基本就是出口 IP 被 CF 半拦，刷新也救不回来，早点换出口。
ENTRY_WAIT_TIMEOUT = 15.0


def _open_entry_page(
    page: Any,
    *,
    log: LogFn = None,
    timeout: float | None = None,
    diag: Optional[dict] = None,
) -> bool:
    """等入口页就绪；没就绪刷新一次再等。

    线上实测（2026-11-01）：`goto` 之后入口页偶尔停在 Cloudflare 挑战/白屏，
    这时 `open_email_form` 会盲点 6 次（约 72 秒）全部「未命中」才放弃，
    而那一次出口其实是好的。刷新一次几乎总能救回来，且失败也别把整轮时间耗光
    —— 只刷一次。
    """
    wait = ENTRY_WAIT_TIMEOUT if timeout is None else timeout
    if signup_pages.wait_for_entry(page, timeout=wait, log=log):
        return True
    _log(log, "[Grok] 入口页未渲染出注册按钮，刷新一次再等…")
    try:
        page.reload()
    except Exception as exc:
        logger.debug("刷新入口页失败: %s", exc)
    if signup_pages.wait_for_entry(page, timeout=wait, log=log):
        return True
    if diag is not None:
        # 把页面内容留成证据：批量跑时连着几个出口都报同一句话，没有页面文本
        # 就只能猜是 CF 挑战、前端没加载、还是文案变了（2026-11-01 实测）。
        text = signup_pages.page_text(page)
        verdict = signup_pages.detect_blocking_state(text)
        diag["cloudflare"] = bool(
            verdict and verdict.get("kind") == signup_pages.SEND_RESULT_CF_CHALLENGE
        )
        diag["page_text"] = str(text or "")[:300]
    return False


def _profile_stage(
    page: Any,
    password: str,
    *,
    given_name: str = "",
    family_name: str = "",
    proxy: str = "",
    log: LogFn = None,
    captcha: Any = None,
) -> dict:
    """资料页：填三件套 → 拿 Turnstile 令牌 → 提交。

    新版资料页多了一个 Cloudflare Turnstile widget（sitekey
    `0x4AAAAAAAhr9JGVDZbrZOo0`）。它的令牌只走 `render({callback})` 进 React
    state，**不落 DOM**：令牌为空时前端直接拦下提交、页面显示
    "Complete the verification challenge, then try again."（埋点
    `error_type=turnstile_failed`）。所以：先等 widget 自己出票，超时就用
    `turnstile_mint` 铸一个、再经劫持到的 callback 灌进应用状态。
    """
    out: dict[str, Any] = {
        "ok": False,
        "error": "",
        "turnstile_blocked": False,
        "turnstile_token": "",
        "retryable": True,
        "consumes_address": False,
        "missing_fields": [],
    }
    missing = signup_pages.fill_profile(
        page,
        given_name=given_name or random.choice(GIVEN_NAMES),
        family_name=family_name or random.choice(FAMILY_NAMES),
        password=password,
        log=log,
    )
    out["missing_fields"] = missing
    if missing:
        # 读回为空 = 没真填上。盲提交只会停在原页，表象是「未拿到 SSO」——
        # 排查方向全错（实测 2026-10-06）。直接报字段名。
        out["error"] = f"建号表单填充失败（读回为空）: {', '.join(missing)}"
        return out

    token = signup_pages.ensure_turnstile_token(
        page, page_url=signup_pages.PROFILE_PAGE_URL, proxy=proxy, log=log, captcha=captcha,
    )
    out["turnstile_token"] = token
    if not token:
        _log(log, "[Grok] 未拿到 Turnstile 令牌，仍尝试提交（大概率会被拦）")

    label = signup_pages.click_profile_submit(page) or signup_pages.click_profile_submit(page)
    _log(log, f"[Grok] 提交建号资料（{label or '未命中按钮'}）")
    page.wait_for_timeout(3000)

    text = signup_pages.page_text(page)
    if signup_pages.looks_turnstile_gated(text) or (not token and signup_pages.has_profile_form(text)):
        out["turnstile_blocked"] = True
        out["error"] = (
            "资料页未通过人机验证挑战（Turnstile）"
            + ("：没有拿到令牌" if not token else "")
            + "，换出口 IP 或稍后重试即可，地址仍然可用"
        )
        return out
    out["ok"] = True
    return out


def configured_captcha(config: Any = None, *, log: LogFn = None) -> Any:
    """按注册配置建验证码解决器；没配或不可用就返回 None。

    审计 R-21 的另一半：`RegisterConfig.captcha_solver` 长期没人读 —— 界面上
    「验证码解决器」配了 YesCaptcha，浏览器流程却只会走页面 widget / 屏外 Chrome /
    本地 solver（线上实测 2026-11-01：资料页卡住时配好的解决器一次都没被试）。

    刻意不接的取值：
      - `yescaptcha` 但没 key：接上只会让每次注册先白等一次外部 API，更亏；
      - `manual`：浏览器流程里没人能手工点；
      - 未知值（含 `2captcha`，`core/base_captcha.py` 里没有实现）：不能瞎猜。
    """
    if config is None:
        return None
    solver = str(getattr(config, "captcha_solver", "") or "").strip().lower()
    extra = dict(getattr(config, "extra", None) or {})
    try:
        if solver == "yescaptcha":
            key = str(extra.get("yescaptcha_key") or os.getenv("YESCAPTCHA_KEY") or "").strip()
            if not key:
                _log(log, "[Grok] 配置了 yescaptcha 但没填 key，跳过（避免每次注册白等外部 API）")
                return None
            from core.base_captcha import YesCaptcha
            return YesCaptcha(key)
        if solver == "local_solver":
            from core.base_captcha import LocalSolverCaptcha
            url = str(extra.get("solver_url") or os.getenv("LOCAL_SOLVER_URL") or "").strip() or None
            return LocalSolverCaptcha(url)
    except Exception as exc:  # noqa: BLE001 - 解决器起不来不该挡住注册
        _log(log, f"[Grok] 验证码解决器初始化失败（忽略）: {type(exc).__name__}: {exc}")
        return None
    return None


#: 打开注册页的超时（毫秒）。线上实测（2026-11-01）：出口挂掉时 Playwright 要等
#: 满 70 秒才抛，而批量跑每个地址要试 6 个出口 —— 70 秒 × 6 全花在等超时上。
#: 正常出口 `domcontentloaded` 只要 1-7 秒（JP/ES/IT/GB 探头实测），所以 45 秒
#: 还没打开就是出口不通，早点交回上层换出口。
NAV_TIMEOUT_MS = 45_000


def _nav_error_text(exc: BaseException) -> str:
    """导航失败的短描述：不搬运 Playwright 那一整段 `Call log`，也不带毫秒数。

    线上实测（批量第 2 轮第 5 个号）裸抛的是
    `TimeoutError: Page.goto: Timeout 70000ms exceeded.\nCall log:\n  - navigating to …`，
    原样写进结果等于把日志刷满，上层还看不出这是出口问题。
    """
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or name == "TimeoutError":
        return "打开超时"
    head = " ".join(str(exc).strip().splitlines()[:1]).strip()[:120] or name
    return f"{name}: {head}"


def register_grok_via_browser(
    email: str,
    *,
    alias_id: Optional[int] = None,
    proxy: str = "",
    code_timeout: float = 240,
    headless: bool = True,
    password: str = "",
    mailbox: Any = None,
    mailbox_account: Any = None,
    task_control: Any = None,
    log: LogFn = None,
    captcha: Any = None,
) -> dict[str, Any]:
    """用浏览器走完 x.ai 注册（新版 Next.js 四步流程），返回 `{ok, email, password, sso, error}`。

    验证码来源二选一：
      - `alias_id`：iCloud 号池（本项目主链路），走 `icloud_service` 读信；
      - `mailbox` + `mailbox_account`：其它渠道（Outlook/tempmail…），
        走渠道自己的 `wait_for_code()`。

    只给 `alias_id` 而不给 `mailbox` 时是纯 iCloud 路径；两者都没给则读不到码，
    会在 `error` 里说明原因（而不是静默超时）。

    `task_control` 用于让等码阶段可被「停止/跳过」打断（默认要等 240 秒）。

    失败时结果里会带 `retryable` / `consumes_address` / `already_registered`：只有
    「该邮箱已有账号」这类才允许把号池地址标成已消耗（新版线上实测结论见
    `platforms/grok/signup_pages` 模块 docstring）。
    """
    result: dict[str, Any] = {
        "ok": False, "email": email, "password": password or _gen_password(),
        "sso": "", "error": "",
    }

    try:
        from camoufox.sync_api import Camoufox
    except Exception as exc:
        result["error"] = f"camoufox 不可用: {exc}"
        return result

    # 语言钉成英文：camoufox 会按 `geoip` 对齐 `navigator.language` /
    # `Accept-Language`（`.venv/.../camoufox/fingerprints.py:1775`），于是 JP
    # 出口自动出日语页面、ES/FR/IT 出口出西/法/意语页面（2026-11-01 实测：
    # JP 出口 183.77.203.114 返回 `メールで登録`，被我们的英文文案表判成
    # 「入口页没渲染」）。显式传 `locale` 覆盖，让后续文案表/选择器不依赖出口国家。
    # camoufox 的强项要用上（2026-11-01）：原来只传了 headless/locale，等于
    # 开着一辆防弹车但没装防弹玻璃。
    #   humanize=True  —— 拟人化鼠标轨迹 / 打字节奏 / 滚动，这是它的核心；
    #   os=["windows"] —— 固定操作系统指纹，别每次启动随机一套。
    kwargs: dict[str, Any] = {
        "headless": headless,
        "locale": ["en-US", "en"],
        "humanize": True,
        "os": ["windows"],
    }
    if proxy:
        cfg = build_playwright_proxy_config(proxy) or {}
        if cfg:
            kwargs["proxy"] = cfg
            # 时区/语言要对齐出口 IP（不然指纹自相矛盾），但**不能让 camoufox
            # 自己去查**：它的 `geoip=True` 会用 6 个公有 IP 探测服务逐个反查、
            # 每个 5s 超时，住宅代理上直接抛
            # `camoufox.exceptions.InvalidIP: Failed to get IP address: … read
            # timeout=5`，浏览器起不来（2026-11-01 实测）。所以自己解析一次，
            # 拿得到就把 IP 传进去（camoufox 支持 `geoip="1.2.3.4"`），
            # 拿不到就退化成 False 保住启动可靠性。
            kwargs["geoip"] = _resolve_exit_ip(proxy) or False

    try:
        with Camoufox(**kwargs) as browser:
            page = browser.new_page()
            # Castle 桩：邮箱页的提交处理要 `window.Castle.createRequestToken()`，
            # 而 Castle 的第三方脚本在代理链路上经常没加载完 —— 拿不到就**静默
            # return**，一个请求都不发（2026-11-02 逆向实测）。桩在真 SDK 之前
            # 注入，真 SDK 加载后会覆盖它，所以不影响真实令牌。
            try:
                page.add_init_script(signup_pages.CASTLE_STUB_JS)
            except Exception as exc:
                logger.debug("注入 Castle 桩失败: %s", exc)
            _log(log, "[Grok] 打开注册页…")
            # 先落一次 grok.com 再进注册页（best-effort，失败继续）。
            #
            # 用户问「为什么不在 grok.com 注册」—— 注册确实在 accounts.x.ai
            # （grok.com 的「Sign up」就是跳到这里，`?redirect=grok-com` 正是
            # 把这次注册标记成 Grok 而不是通用 xAI 账号）。但先访问一次
            # grok.com 会拿到它的会话 cookie / 让 CF 把这一串请求看成「从应用
            # 点进来的人」，而不是「直接打开注册页的脚本」。参考实现的出口体检
            # 也是**两个域名都探**（`proxy_pool_v3.py:884` 同时打
            # accounts.x.ai 和 grok.com），说明这两个域名的信誉是分开算的。
            #
            # 失败不管：拿不到 cookie 只是少了这点加成，不该因此整轮失败。
            try:
                page.goto("https://grok.com/", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1200)
                _dismiss_cookie_banner(page)
            except Exception as exc:
                logger.debug("预热 grok.com 失败（不影响注册）: %s", exc)
            try:
                page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            except Exception as exc:
                # 导航失败（出口不通 / 代理连不上 / 被 CF 断连）必须归类，不能裸抛：
                # 线上实测（2026-11-01 批量第 2 轮）结果里只留下 `kind=None` 和一坨
                # Playwright 调用日志，上层判不出「该换出口」还是「流程坏了」。
                result["error"] = (
                    f"打开注册页失败（{_nav_error_text(exc)}）—— 出口不通或太慢，"
                    "换一个出口 IP 重试，地址仍然可用"
                )
                result["retryable"] = True
                result["consumes_address"] = False
                result["nav_failed"] = True
                return result
            page.wait_for_timeout(3000)
            _dismiss_cookie_banner(page)

            # 先等入口页真的渲染出来（`Sign up with email` 等文案出现）。
            # 等不到就直接收工：页面还是壳的时候按钮根本不存在，循环点 6 次只会
            # 「未命中」刷满日志、白烧一分钟（线上实测 2026-11-01），而换一个出口
            # 立刻就正常 —— 所以这里必须把它报成「出口问题」，让上层换出口重试。
            entry_diag: dict = {}
            if not _open_entry_page(page, log=log, diag=entry_diag):
                page_head = (entry_diag.get("page_text") or "").strip().replace("\n", " ")[:160]
                if entry_diag.get("cloudflare"):
                    why = "入口页被 Cloudflare 挑战挡住（Just a moment / cf-chl-widget）"
                else:
                    why = "入口页只返回了外壳，注册按钮没渲染出来"
                result["error"] = (
                    f"{why} —— 换一个出口 IP 重试，地址仍然可用"
                    + (f"；页面开头：{page_head}" if page_head else "")
                )
                result["entry_diag"] = entry_diag
                result["retryable"] = True
                result["consumes_address"] = False
                result["entry_not_rendered"] = True
                return result

            # 进邮箱注册视图：新版入口页有 6 个按钮（X/email/Apple/Google/GitHub/Sign in），
            # 必须按文案精确命中 “Sign up with email”，点一次还可能被 cookie 弹窗吃掉，
            # 所以要循环等 `input#email` 真的出现。
            if not signup_pages.open_email_form(page, log=log):
                result["error"] = "找不到邮箱输入框（新版入口页未出现 Sign up with email）"
                result["retryable"] = True
                return result
            _log(log, "[Grok] 已进入邮箱注册视图")

            stage = _send_code_stage(page, email, log=log, proxy=proxy, captcha=captcha)
            # `kind` 一起带上：上层（任务日志、重试决策）需要知道究竟落的是
            # 限流 / 出口被拒 / 已有账号 —— 只给错误文本的话又得再做一次字符串匹配。
            # 但 **`ok` 不能从这里带上来**：它在这里的含义是「信发出去了」，
            # 而顶层 `ok` 的含义是「拿到 SSO 了」。混用会导致没 SSO 也报成功
            # （线上实测 2026-11-01：资料页被 Turnstile 挡住却返回 ok=True）。
            result.update({k: v for k, v in stage.items() if k != "ok"})
            if not stage["ok"]:
                return result
            _log(log, "[Grok] 页面已发信，等验证码…")

            code = _read_code(
                alias_id, code_timeout, log,
                mailbox=mailbox, mailbox_account=mailbox_account,
                task_control=task_control,
                # 只认这次发码之后到的信：邮箱里还躺着历史邮件和上一轮的旧码，
                # 不设这条线就会读到别人的码（2026-11-01：读到营销信的 `for-ios`）。
                after=stage.get("sent_at"),
            )
            if not code:
                result["error"] = (
                    "未收到验证码"
                    if (alias_id is not None or mailbox is not None)
                    else "未收到验证码（既没给 alias_id 也没给 mailbox，读不到任何收件箱）"
                )
                return result
            _log(log, f"[Grok] 验证码 {code}")

            # 等「验证码框」或「资料页」先出现 —— 2026-11-01 现场快照证明，
            # 码进去之后页面**不换 URL**，只把 email 框隐藏、换成资料表单。
            # 只等验证码框的话，等到超时就报「验证码输入框未出现」，而实际上
            # 已经该填资料了（看着像失败，实际走到倒数第二步）。
            arrived = signup_pages.wait_for_code_or_profile(page, timeout=60)
            if not arrived:
                result["error"] = "验证码页未出现｜" + _page_snapshot(page)
                return result
            if arrived == "profile":
                _log(log, "[Grok] 页面已在资料页（验证码这步已过），直接填资料")
                code_entered = True
            else:
                code_entered = False
            # 新版是 `input[name=code]`、maxlength=6、满 6 位自动提交（码要去掉连字符）
            if not code_entered and not signup_pages.enter_code(page, code, log=log):
                txt = signup_pages.page_text(page)
                if _looks_already_registered(txt):
                    result["already_registered"] = True
                    result["consumes_address"] = True
                    result["error"] = "该邮箱已有 x.ai 账号（别名已消耗）"
                    return result
                result["error"] = f"验证码提交后未进入建号表单: {txt[:150]}"
                return result

            if not signup_pages.wait_for_profile_form(page, timeout=60):
                result["error"] = "验证码通过后未出现建号资料表单"
                return result

            _log(log, "[Grok] 填建号资料…")
            prof = _profile_stage(
                page, result["password"], proxy=proxy, log=log, captcha=captcha,
            )
            for key in ("error", "retryable", "consumes_address", "turnstile_blocked"):
                result[key] = prof[key]
            if not prof["ok"]:
                return result

            # 提交后 x.ai 用 cookie-chain 跳链下发 sso（详见 `_wait_for_sso_cookie`）。
            # 必须轮询等它落地，不能只等固定时长 —— 否则跳链没跑完就返回，
            # 把成功的注册误判成失败。
            sso = _wait_for_sso_cookie(page, timeout=35)

            if sso:
                result["ok"] = True
                result["sso"] = sso
                _log(log, f"[Grok] 注册成功，SSO len={len(sso)}")
            else:
                final = signup_pages.page_text(page)
                result["error"] = f"未拿到 SSO（等 35 秒）: {final[:150]}"
            return result
    except TaskInterruption:
        # 「停止/跳过」是控制流，不是失败：必须原样抛给上层（审计 R-07）。
        # 之前这里被 catch-all 吞掉，用户点了停止却看到「注册失败」。
        raise
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
        return result


#: 验证码**只认大写**：x.ai 的码固定是 `ABC-123` 形式。
#: 不能带 `re.IGNORECASE` —— 线上实测（2026-11-01，别名 535）营销信正文里的
#: `for-ios` 会被大小写不敏感的 `[A-Z0-9]{3}-[A-Z0-9]{3}` 当成验证码填进页面。
_CODE_UPPER_RE = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b")

#: 码信主题关键词（小写子串匹配）。x.ai 固定是 `Grok confirmation code: XXX-XXX`。
_CODE_MAIL_SUBJECT_MARKERS = (
    "confirmation code", "verification", "verify", "grok", "spacex", "xai",
)

#: 码信发件人特征。iCloud 隐藏邮箱会把 `@` 写成 `_at_`、`.` 写成 `_`，
#: 所以 `noreply@x.ai` 到手是 `noreply_at_x_ai_xxxx@icloud.com`。
_CODE_MAIL_SENDER_RE = re.compile(r"x[._-]?ai|grok", re.IGNORECASE)


def _sender_text(msg: Any) -> str:
    """发件人的可读文本（MailAddress 或裸字符串都能处理）。"""
    sender = getattr(msg, "sender", "")
    return str(getattr(sender, "email", None) or sender or "")[:48]


def _is_code_mail(msg: Any) -> bool:
    """这封信像不像 x.ai 的验证码信。

    为什么必须判：邮箱里本来就躺着历史邮件（营销信、其它站点通知），
    只看「有没有 `XXX-XXX` 形状的字符串」等于把别人的正文当验证码用。
    """
    subject = str(getattr(msg, "subject", "") or "").lower()
    if any(marker in subject for marker in _CODE_MAIL_SUBJECT_MARKERS):
        return True
    sender = getattr(msg, "sender", "")
    sender_text = getattr(sender, "email", None) or str(sender or "")
    return bool(_CODE_MAIL_SENDER_RE.search(sender_text))


def _mail_received_ts(msg: Any) -> Optional[float]:
    """取邮件的接收时间戳；拿不到返回 None（调用方按「时间未知」放行）。"""
    stamp = getattr(msg, "received_at", None) or getattr(msg, "sent_at", None)
    if stamp is None:
        return None
    try:
        if isinstance(stamp, (int, float)):
            return float(stamp)
        return stamp.timestamp()
    except Exception as exc:  # noqa: BLE001 - 时间解析失败只影响新旧判断
        logger.debug("邮件时间解析失败: %s", exc)
        return None


def _page_snapshot(page: Any) -> str:
    """验证码框没出现时的现场快照（URL / 标题 / 正文 / 所有 input / iframe）。"""
    js = """
    () => {
      const pick = (el) => ({
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        name: el.getAttribute('name') || '',
        id: el.id || '',
        maxlength: el.getAttribute('maxlength') || '',
        ph: el.getAttribute('placeholder') || '',
        ac: el.getAttribute('autocomplete') || '',
        vis: !!(el.offsetWidth || el.offsetHeight),
      });
      const inputs = Array.from(document.querySelectorAll('input,textarea'))
        .slice(0, 20).map(pick);
      const frames = Array.from(document.querySelectorAll('iframe'))
        .slice(0, 6).map(f => f.src || '(no src)');
      return {
        url: location.href,
        title: document.title,
        text: (document.body ? document.body.innerText : '').slice(0, 220),
        inputs, frames,
        otpInShadow: (() => {
          try {
            const all = document.querySelectorAll('*');
            for (const el of all) {
              if (el.shadowRoot && el.shadowRoot.querySelector('input')) return true;
            }
          } catch (e) {}
          return false;
        })(),
      };
    }
    """
    try:
        snap = page.evaluate(js)
    except Exception as exc:
        return f"快照失败: {exc}"
    if not isinstance(snap, dict):
        return f"快照异常: {snap!r}"
    parts = [
        f"url={str(snap.get('url'))[:90]}",
        f"title={str(snap.get('title'))[:60]!r}",
        f"inputs={snap.get('inputs')}",
        f"iframe={snap.get('frames')}",
        f"shadow_input={snap.get('otpInShadow')}",
        f"text={str(snap.get('text'))[:160]!r}",
    ]
    return " ".join(parts)[:900]


def _read_code(
    alias_id: Optional[int],
    timeout: float,
    log: LogFn,
    *,
    mailbox: Any = None,
    mailbox_account: Any = None,
    task_control: Any = None,
    after: Optional[float] = None,
) -> str:
    """等验证码：iCloud 走 `icloud_service`，其它渠道走渠道自己的 `wait_for_code`。

    `after` 是「本次发码的时刻」（墙钟秒）。只看比它晚到的信：同一地址重试时
    邮箱里还躺着上一轮的码和一堆历史邮件，不设这条线就会把旧码/别人的码填进去
    （线上实测 2026-11-01：读到营销信里的 `for-ios`，页面停在 `Verify your email`）。

    为什么两条路：iCloud 本地渠道的 `fetch_alias_messages(alias_id)` 是按别名
    过滤的专用接口，读得比通用 `wait_for_code` 更准；而 Outlook/tempmail 等
    渠道收码必须带 `account_id`（inbox token），只有邮箱地址读不到 —— 所以
    优先复用上层已经拿到的渠道账号对象。

    `task_control` 给定时每轮轮询都 checkpoint 一次：这条路径默认要等 240 秒，
    不给停止/跳过留入口的话，用户在面板上点「停止」得干等满超时（评审发现）。

    **控制流异常必须穿透**：`StopTaskRequested`/`SkipCurrentAttemptRequested`
    继承自 RuntimeError，被下面任何 `except Exception` 捕获都会让「停止」变成
    一次普通失败（评审实测复现）。所以内层 `except` 一律先放行 TaskInterruption。
    """
    deadline = time.time() + timeout

    # checkpoint 会把 StopTaskRequested / SkipCurrentAttemptRequested 抛上来，
    # 必须让它穿透（不能吞）—— 上层据此结束本轮。
    if task_control is not None and not callable(getattr(task_control, "checkpoint", None)):
        task_control = None

    if alias_id is not None:
        try:
            from services import icloud_service
        except Exception as exc:  # noqa: BLE001 - 回落到通用渠道
            if log:
                log(f"[Grok] iCloud 服务不可用，改用渠道收码: {type(exc).__name__}")
            icloud_service = None
        if icloud_service is not None:
            consecutive_errors = 0
            # 租约续约：等码超过回收阈值（2 小时）的长任务会被另一个取号点
            # 回收、号被重复领走（用户要求：使用中不能同时使用）。这条路径
            # 不走渠道的 wait_for_code，续约要在这里自己做。
            renew = getattr(icloud_service, "renew_alias_claim", None)
            last_renew = 0.0
            while time.time() < deadline:
                if task_control is not None:
                    task_control.checkpoint()
                if callable(renew):
                    # 用 monotonic 而不是墙钟：NTP 回拨会推迟续约
                    # （与两个渠道的续约时钟口径一致，评审建议统一）。
                    now = time.monotonic()
                    if not last_renew or now - last_renew >= LEASE_RENEW_INTERVAL_SECONDS:
                        last_renew = now
                        try:
                            renew(int(alias_id))
                        except TaskInterruption:
                            raise
                        except Exception as exc:  # noqa: BLE001 - 续约失败不该影响读码
                            if log:
                                log(f"[Grok] iCloud 租约续约失败（忽略）: {type(exc).__name__}")
                try:
                    msgs = icloud_service.fetch_alias_messages(int(alias_id), limit=15)
                    consecutive_errors = 0
                except TaskInterruption:
                    # 控制流异常必须穿透（见函数 docstring）
                    raise
                except Exception as exc:  # noqa: BLE001
                    # 读信失败要留痕：DB 坏了 / 会话过期 / 别名不存在 与
                    # 「还没收到信」是两回事，原先一律吞掉，用户只能看到
                    # 4 分钟后的「未收到验证码」，排查方向全错（评审发现）。
                    consecutive_errors += 1
                    if log and (consecutive_errors == 1 or consecutive_errors % 5 == 0):
                        log(
                            f"[Grok] iCloud 读码失败 ×{consecutive_errors}: "
                            f"{type(exc).__name__}: {str(exc)[:100]}"
                        )
                    msgs = []
                for m in msgs:
                    # 属性名对齐 platforms/icloud/models.MailMessage：它只有
                    # `subject` / `snippet` / `text_body`（没有 `text`/`preview`）。
                    # 原先读 `text`/`preview` 会全部落到空串 —— 实测只有主题
                    # 真的被搜到，x.ai 一旦把码移出主题就静默读不到。
                    blob = " ".join(
                        str(getattr(m, attr, "") or "")
                        for attr in ("subject", "snippet", "text_body", "html_body")
                    )
                    is_code_mail = _is_code_mail(m)
                    hit = _CODE_UPPER_RE.search(blob) if is_code_mail else None
                    if hit is None:
                        # 这封信没给出可用码。若里面有「像码」的串，留痕：历史邮件
                        # 里的营销文案（`for-ios`）和「x.ai 换了主题/发件人」都会
                        # 在这里现形，否则改版时排查方向全错（2026-11-01 实测）。
                        loose = None if is_code_mail else CODE_RE.search(blob)
                        if loose is not None and log:
                            log(
                                f"[Grok] 跳过可疑验证码 {loose.group(1)}（来自 "
                                f"{_sender_text(m)}「{str(getattr(m, 'subject', '') or '')[:40]}」）"
                            )
                        continue
                    candidate = hit.group(1)
                    if after is not None:
                        ts = _mail_received_ts(m)
                        if ts is not None and ts < after - 20.0:
                            # 上一轮留下的旧码：等新码，别拿过期码去填
                            if log:
                                log(f"[Grok] 跳过上一轮的旧码 {candidate}（早于本次发码）")
                            continue
                    if log:
                        log(f"[Grok] 验证码 {candidate}（来自 {_sender_text(m)}）")
                    return candidate
                time.sleep(10)
            return ""

    # 通用渠道：Outlook / tempmail / YYDS 等
    if mailbox is not None and mailbox_account is not None:
        waiter = getattr(mailbox, "wait_for_code", None)
        if callable(waiter):
            try:
                # 先记录现有邮件 id，避免把上一封的旧码当成新码
                before_ids = None
                getter = getattr(mailbox, "get_current_ids", None)
                if callable(getter):
                    try:
                        before_ids = getter(mailbox_account)
                    except Exception:
                        before_ids = None
                return str(
                    waiter(
                        mailbox_account,
                        timeout=int(max(1, deadline - time.time())),
                        before_ids=before_ids,
                        code_pattern=CODE_RE.pattern,
                    )
                    or ""
                )
            except TaskInterruption:
                # 渠道的 wait_for_code 内部也会 checkpoint —— 放行，别吞
                raise
            except Exception as exc:  # noqa: BLE001 - 读码失败只影响本轮
                if log:
                    log(f"[Grok] 渠道收码失败: {type(exc).__name__}: {str(exc)[:100]}")
    return ""


def exchange_oauth_via_browser(sso: str, *, proxy: str = "", timeout: int = 240,
                               log: LogFn = None) -> dict:
    """用浏览器兜底把 SSO 换成 OAuth token（协议 device flow 会被 CF 403）。"""
    try:
        from .oauth_browser import oauth_device_via_browser

        tokens = oauth_device_via_browser(sso, proxy=proxy, timeout=timeout, log=log)
        return tokens or {}
    except Exception as exc:
        _log(log, f"[Grok] 浏览器 OAuth 异常: {type(exc).__name__}: {str(exc)[:120]}")
        return {}


__all__ = ["register_grok_via_browser", "exchange_oauth_via_browser"]
