"""出口预检：这个代理能不能用来注册 Grok。

## 为什么需要

`POST /api/auth/send-verification-code` 对**被判定不可注册的出口**会直接回
`400 {"code":"account:email-signup-unavailable"}`，而且这个判断**跟出口 IP 走、
不跟邮箱走**（`platforms/grok/signup_pages.py` 顶部记着实测结论：同一地址换住宅
出口立刻能发码）。

也就是说：出口坏不坏，在**拿到邮箱之前**就能问出来。可原来的顺序是
「先领 iCloud 别名 → 起浏览器 → 填表 → 提交 → 才知道出口不行」，一次白烧
一个别名加半分钟，两轮就是两个别名。

这个模块就是那个「先问一句」：用一个**永远收不到信的合成地址**
（`@example.invalid`，RFC 2606 保留域名）打同一个接口 ——

- 回 `email-signup-unavailable` → 这个出口不能用，换下一个；
- 回别的（格式错、限流、200）→ **不能证明出口坏**，按「可用」放行（fail-open）。

fail-open 是故意的：预检只是省钱的手段，不该变成新的失败点。它自己超时、
curl 缺失、DNS 挂了，都按「不知道 → 放行」处理。

## 为什么不复用 requests

代理是 `socks5h://`，`requests` 要装 PySocks 才支持，而 `curl` 原生支持
`socks5h`（DNS 也走代理解析，这点很关键 —— 本地解析会泄漏且经常解析不到）。
一条 curl 子进程最省事，也不给运行时加依赖。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

LogFn = Optional[Callable[[str], None]]

#: 预检用的地址。`.invalid` 是 RFC 2606 保留域名，永远不可能真的收信。
PROBE_ADDRESS = "probe-not-a-real-address@example.invalid"

#: 打哪个接口。就是注册页提交邮箱时打的那个。
SEND_CODE_URL = "https://accounts.x.ai/api/auth/send-verification-code"

#: 只有这个错误码能证明「出口被判定不可注册」。
UNAVAILABLE_CODE = "account:email-signup-unavailable"

#: 单次预检的墙钟上限（秒）。curl 自己也带 `--max-time`，这里是兜底。
PROBE_TIMEOUT = 18


def _curl_bin() -> str:
    return shutil.which("curl") or ""


def probe_exit(
    proxy: str,
    *,
    timeout: int = PROBE_TIMEOUT,
    log: LogFn = None,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """探一个出口能不能用来注册。

    返回 `{"ok": bool, "code": str, "detail": str, "skipped": bool}`。

    - `ok=True`：**没有**拿到「出口不可注册」的证据 —— 可以用（fail-open）；
    - `ok=False`：明确被拒，这个出口别用了；
    - `skipped=True`：没探成（没代理 / 没 curl / 超时 / 网络错），按可用处理。

    `runner` 只为测试注入，签名与 `subprocess.run` 一致。
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return {"ok": True, "code": "", "detail": "没有代理，跳过预检", "skipped": True}
    if runner is None and not _curl_bin():
        # 没有 curl 就别把注册挡住 —— 预检是省钱手段，不是前置条件。
        return {"ok": True, "code": "", "detail": "没有 curl，跳过预检", "skipped": True}

    body = json.dumps({"email": PROBE_ADDRESS})
    cmd = [
        "curl", "-sS", "--max-time", str(int(timeout)),
        "-x", proxy,
        "-X", "POST", SEND_CODE_URL,
        "-H", "content-type: application/json",
        "-H", "origin: https://accounts.x.ai",
        "-H", "referer: https://accounts.x.ai/sign-up?redirect=grok-com",
        "-d", body,
        "-w", "\n__STATUS__%{http_code}",
    ]
    run = runner or subprocess.run
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except Exception as exc:
        logger.debug("出口预检调用失败: %s", exc)
        return {"ok": True, "code": "", "detail": f"预检没跑成（{exc}），按可用处理", "skipped": True}

    out = str(getattr(proc, "stdout", "") or "")
    status = ""
    if "__STATUS__" in out:
        out, _, status = out.rpartition("__STATUS__")
        status = status.strip()
    text = out.strip()

    code = ""
    try:
        doc = json.loads(text) if text else {}
        if isinstance(doc, dict):
            code = str(doc.get("code") or doc.get("error") or "")
    except Exception as exc:
        # 不是 JSON 也没关系：下面按子串兜底。绑上异常并留痕 ——
        # 上游偶尔回 HTML 错误页，知道「解不开的是什么」比静默有用。
        logger.debug("出口预检响应不是 JSON（%s）: %s", exc, text[:120])
    if not code and UNAVAILABLE_CODE in text:
        code = UNAVAILABLE_CODE

    if UNAVAILABLE_CODE in code or UNAVAILABLE_CODE in text:
        detail = f"出口被判定不可注册（{status or '?'} {UNAVAILABLE_CODE}）"
        if log:
            log(f"[Grok] 出口预检：{detail}，换下一个")
        return {"ok": False, "code": UNAVAILABLE_CODE, "detail": detail, "skipped": False}

    return {
        "ok": True,
        "code": code,
        "detail": f"出口预检通过（{status or '?'}{' ' + code if code else ''}）",
        "skipped": False,
    }


#: 连通性检查打的目标：**只要连得上就行**，不看内容。
REACH_URL = "https://grok.com"
#: 连通性检查的墙钟上限（秒）。比注册接口短 —— 这里只判传输层。
REACH_TIMEOUT = 12


def probe_reachable(
    proxy: str,
    *,
    url: str = REACH_URL,
    timeout: int = REACH_TIMEOUT,
    runner: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """探出口**连通性**：能不能连出去，不判「能不能注册」。

    与 `probe_exit` 的分工：
    - `probe_exit` 问的是业务判定（打发码接口看有没有被企业级拒），只能 fail-open
      —— 403 挑战页、限流、格式错都「证明不了出口坏」；
    - 本函数问的是**传输层**：curl 连不上（进程超时 / 退出码非 0 / HTTP 000）
      就是确定坏 —— 实测这类出口在浏览器里表现为
      `net::ERR_CONNECTION_CLOSED`（Turnstile 铸币三次全挂），白烧一个别名
      加两三分钟。

    **HTTP 4xx/5xx 一律算可用**：`accounts.x.ai` 对裸 curl 回 Cloudflare 403
    是常态（浏览器能过挑战），不能据此摘出口。

    返回 `{"ok": bool, "detail": str, "status": str, "skipped": bool}`。
    """
    proxy = str(proxy or "").strip()
    if not proxy:
        return {"ok": True, "detail": "没有代理，跳过连通性检查", "status": "", "skipped": True}
    if runner is None and not _curl_bin():
        return {"ok": True, "detail": "没有 curl，跳过连通性检查", "status": "", "skipped": True}

    cmd = [
        "curl", "-sS", "-o", os.devnull,
        "--max-time", str(int(timeout)),
        "-x", proxy,
        "-w", "%{http_code}",
        str(url),
    ]
    run = runner or subprocess.run
    try:
        proc = run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except Exception as exc:
        # 进程级失败（超时被杀 / 起不来）＝ 这个出口此刻连不上，换下一个。
        return {"ok": False, "detail": f"出口连不上（{type(exc).__name__}）", "status": "", "skipped": False}

    status = str(getattr(proc, "stdout", "") or "").strip()
    rc = int(getattr(proc, "returncode", 0) or 0)
    if rc != 0 or status in ("", "000"):
        return {
            "ok": False,
            "detail": f"出口连不上（curl rc={rc} status={status or '000'}）",
            "status": status,
            "skipped": False,
        }
    return {"ok": True, "detail": f"出口连通（HTTP {status}）", "status": status, "skipped": False}


__all__ = [
    "PROBE_ADDRESS",
    "REACH_TIMEOUT",
    "REACH_URL",
    "SEND_CODE_URL",
    "UNAVAILABLE_CODE",
    "probe_exit",
    "probe_reachable",
]
