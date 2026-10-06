"""Grok 全流程注册（浏览器主导）—— 本项目实测跑通的路径。

## 为什么是「浏览器主导」
本机逐项实测（2026-09-30）把每个环节的可行路径逼出来了：

| 环节 | 结论 |
|---|---|
| 浏览器引擎 | **camoufox 是唯一能过 x.ai CF 的**。chromium（playwright）无论直连还是住宅代理，提交发码都被 CF 403；camoufox 通过并拿到 x.ai 真实答复 |
| 发码 | 必须走真实页面（协议 gRPC/REST 都「假接受」：grpc=0 / ok:true 但零投递） |
| 域名 | x.ai 接受主流域（@icloud.com 实测通过）；拒绝小域名（YYDS 8/8 被拒） |
| 验证码 | 页面会自动格式化（`955-995` → `955995`），用键盘逐字输入才稳 |
| 提交按钮 | 是 `type=submit` 且**无文本**，按 Enter 最可靠（按文本匹配 "Sign up" 会落空） |
| 建号表单 | 验证码通过后出现 givenName / familyName / password → 提交即得 SSO |
| OAuth | 协议 device flow 的 approve 被 CF 403，**必须走浏览器兜底** |

## 与 plugin.py 的关系
`plugin.py` 是任务框架调用的入口（邮箱由号池注入）。本模块把上面这套
「浏览器主导」的实现独立出来，供需要真投递的场景直接调用；两者共用
`grok2api` 等基础件（Turnstile 取 token 走 `turnstile_mint.py`）。
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from core.proxy_utils import build_playwright_proxy_config
from core.task_runtime import TaskInterruption

# SIGNUP_URL / CODE_RE 复用 constants 里的定义：各自已有单一来源，
# 在这里再写一份意味着改一处要同步 N 处（评审发现）。
from .constants import CODE_RE_ANY as CODE_RE
from .constants import SIGNUP_URL
from .profile import generate_password

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


def _looks_already_registered(text: str) -> bool:
    """页面文本是否表明「这个邮箱已经有账号」。"""
    blob = str(text or "")
    if not blob:
        return False
    return any(p.search(blob) for p in _ALREADY_REGISTERED_PATTERNS)


def _log(fn: LogFn, msg: str) -> None:
    if fn:
        try:
            fn(msg)
        except Exception:
            pass


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
        pass
    page.keyboard.press("Enter")


# 密码生成统一走 profile.generate_password（secrets + 与参考实现同格式）。
# 这里原先自己写了一份 random 版，等于同一件事有两套实现、两套强度 ——
# 评审发现后删掉，只保留别名，避免调用点还要改 import。
_gen_password = generate_password


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
) -> dict[str, Any]:
    """用浏览器走完 x.ai 注册，返回 `{ok, email, password, sso, error}`。

    验证码来源二选一：
      - `alias_id`：iCloud 号池（本项目主链路），走 `icloud_service` 读信；
      - `mailbox` + `mailbox_account`：其它渠道（Outlook/tempmail…），
        走渠道自己的 `wait_for_code()`。

    只给 `alias_id` 而不给 `mailbox` 时是纯 iCloud 路径；两者都没给则读不到码，
    会在 `error` 里说明原因（而不是静默超时）。

    `task_control` 用于让等码阶段可被「停止/跳过」打断（默认要等 240 秒）。
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

    kwargs: dict[str, Any] = {"headless": headless}
    if proxy:
        cfg = build_playwright_proxy_config(proxy) or {}
        if cfg:
            kwargs["proxy"] = cfg
            kwargs["geoip"] = True

    try:
        with Camoufox(**kwargs) as browser:
            page = browser.new_page()
            _log(log, "[Grok] 打开注册页…")
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=70000)
            page.wait_for_timeout(5000)
            _log(log, "[Grok] 页面已加载")

            # 勾条款（有就勾）
            try:
                cb = page.locator("input[type=checkbox]").first
                if cb.count() and not cb.is_checked():
                    cb.check(timeout=4000)
                    page.wait_for_timeout(700)
            except Exception:
                pass

            # 进邮箱注册视图。
            #
            # **等表单就绪，不等固定时长**（出处：reference/grok/grok-hub-clean
            # 的 `register.py`，那是参考项目里最新且跑通的实现）。
            # 原实现是「点一次 + 固定等 3 秒 + 找不到输入框就判死」——实测
            # 会间歇失败：x.ai 有时先渲染 cookie 弹窗、有时过 CF 后才出表单，
            # 3 秒不够，于是「找不到邮箱输入框」。
            # 参考实现改成最多 3 轮：每轮先关弹窗 → 检查输入框 → 没出就再点
            # 一次「Sign up with email」→ 仍没出就 reload 页面重来。
            email_input = "input#email, input[type=email], input[name=email]"
            for attempt in range(1, 4):
                page.wait_for_timeout(3000)
                # 关 cookie 弹窗：它盖住按钮时点击会落空（文案兜底清单见
                # `_dismiss_cookie_banner` —— 真机是 OneTrust 'Allow All'）
                _dismiss_cookie_banner(page)
                if page.locator(email_input).count():
                    break
                try:
                    page.get_by_role("button", name="Sign up with email").click(timeout=6000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
                if page.locator(email_input).count():
                    break
                _log(log, f"[Grok] 邮箱表单未就绪，重载页面（第 {attempt} 轮）")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=70000)
                except Exception:
                    pass
            page.wait_for_timeout(800)
            _log(log, "[Grok] 已进入邮箱注册视图")

            # 填邮箱
            filled = False
            for sel in ("input#email", "input[type=email]", "input[name=email]"):
                try:
                    page.locator(sel).first.fill(email, timeout=5000)
                    filled = True
                    break
                except Exception:
                    continue
            if not filled:
                result["error"] = "找不到邮箱输入框（已重试 3 轮）"
                return result
            page.wait_for_timeout(800)
            _log(log, "[Grok] 已填入邮箱，提交发码…")

            # 提交发码
            try:
                page.get_by_role("button", name="Sign up").last.click(timeout=5000)
            except Exception:
                page.keyboard.press("Enter")
            page.wait_for_timeout(8000)
            _log(log, "[Grok] 发码已提交")

            txt = page.evaluate("() => document.body.innerText.slice(0,300)") or ""
            low = txt.lower()
            if "too many" in low:
                result["error"] = "x.ai 取码限流（稍后再试）"
                return result
            if "invalid" in low:
                result["error"] = f"域名被 x.ai 拒收: {email.split('@')[-1]}"
                return result
            # 「该邮箱已有账号」也可能在**发码阶段**就出现（不等验证码环节）。
            # 不在这里认出来的话，会走到下面的「页面未确认发信」分支，把
            # 已消耗的别名记成 failed → 放回 available → 反复重领（评审发现）。
            if _looks_already_registered(txt):
                result["already_registered"] = True
                result["error"] = "该邮箱已有 x.ai 账号（别名已消耗）"
                return result
            if "we've emailed" not in low and "verify your email" not in low:
                result["error"] = f"页面未确认发信: {txt[:120]}"
                return result
            _log(log, "[Grok] 页面已发信，等验证码…")

            # 读验证码
            code = _read_code(
                alias_id, code_timeout, log,
                mailbox=mailbox, mailbox_account=mailbox_account,
                task_control=task_control,
            )
            if not code:
                result["error"] = (
                    "未收到验证码"
                    if (alias_id is not None or mailbox is not None)
                    else "未收到验证码（既没给 alias_id 也没给 mailbox，读不到任何收件箱）"
                )
                return result
            _log(log, f"[Grok] 验证码 {code}")

            # 填码：键盘逐字输入（页面会自动格式化去掉连字符）
            field = None
            deadline = time.time() + 60
            while time.time() < deadline and field is None:
                for sel in ("input[name=code]", "input#code",
                            "input[autocomplete=one-time-code]", "input[inputmode=numeric]"):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible():
                            field = sel
                            break
                    except Exception:
                        continue
                if field is None:
                    page.wait_for_timeout(2500)
            if field is None:
                result["error"] = "验证码输入框未出现"
                return result
            page.locator(field).first.click(timeout=5000)
            page.wait_for_timeout(400)
            for ch in code:
                page.keyboard.type(ch, delay=120)
            page.wait_for_timeout(1500)

            # 提交：按钮是 type=submit 且无文本，按 Enter 最稳
            page.keyboard.press("Enter")
            page.wait_for_timeout(12000)

            step = page.evaluate("() => document.body.innerText.slice(0,300)") or ""
            # 「该邮箱已有账号」= 这个别名早先已经注册过（号池里领到了旧号）。
            # 它是**已消耗**的，不是失败的 —— 上层要据此标 `used` 而不是
            # `failed`，否则会被放回 available 再领一次，反复撞同一堵墙。
            if _looks_already_registered(step):
                result["already_registered"] = True
                result["error"] = "该邮箱已有 x.ai 账号（别名已消耗）"
                return result
            inputs = page.evaluate(
                """() => [...document.querySelectorAll('input')]
                        .filter(i => i.offsetParent)
                        .map(i => ({t: i.type, n: i.name}))"""
            )
            if not any(i.get("t") == "password" for i in inputs):
                result["error"] = f"未进入建号表单: {step[:150]}"
                return result

            # 建号表单
            _log(log, "[Grok] 填建号资料…")
            missing = _fill_signup_form(page, result["password"])
            if missing:
                # 读回为空 = 没真填上。盲提交只会停在原页，表象是「未拿到
                # SSO」—— 排查方向全错（实测 2026-10-06）。直接报字段名。
                result["error"] = f"建号表单填充失败（读回为空）: {', '.join(missing)}"
                return result
            page.wait_for_timeout(1500)
            _submit_signup_form(page)

            # 提交后 x.ai 用 cookie-chain 跳链下发 sso（详见 `_wait_for_sso_cookie`）。
            # 必须轮询等它落地，不能只等固定时长 —— 否则跳链没跑完就返回，
            # 把成功的注册误判成失败。
            sso = _wait_for_sso_cookie(page, timeout=35)

            if sso:
                result["ok"] = True
                result["sso"] = sso
                _log(log, f"[Grok] 注册成功，SSO len={len(sso)}")
            else:
                final = page.evaluate("() => document.body.innerText.slice(0,300)") or ""
                result["error"] = f"未拿到 SSO（等 35 秒）: {final[:150]}"
            return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
        return result


def _read_code(
    alias_id: Optional[int],
    timeout: float,
    log: LogFn,
    *,
    mailbox: Any = None,
    mailbox_account: Any = None,
    task_control: Any = None,
) -> str:
    """等验证码：iCloud 走 `icloud_service`，其它渠道走渠道自己的 `wait_for_code`。

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
            while time.time() < deadline:
                if task_control is not None:
                    task_control.checkpoint()
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
                    hit = CODE_RE.search(
                        " ".join(
                            str(getattr(m, attr, "") or "")
                            for attr in ("subject", "snippet", "text_body", "html_body")
                        )
                    )
                    if hit:
                        return hit.group(1)
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
