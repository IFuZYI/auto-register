"""Turnstile token 获取。

三条路线（按可用性自动选择）：
1. 本项目 captcha solver（YesCaptcha / LocalSolver / Manual）—— 默认
2. 屏外 headed Chrome mint（移植 cpa scripts/turnstile_mint.py）—— 兜底
3. 外部 Solver 服务（cloudTemp 风格 HTTP API）—— 可选

参考：
- grokRegister-cpa/scripts/turnstile_mint.py（屏外 mint）
- cloudTemp-grokzhuce/api_solver_p2.py（本地 Solver）
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Optional

from core.proxy_utils import redact_proxy_url

LogFn = Optional[Callable[[str], None]]

# 项目内脚本位置
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "grok_turnstile_mint.py"


def _log(fn: LogFn, msg: str) -> None:
    if fn:
        try:
            fn(msg)
        except Exception:
            pass


def _is_snap_wrapper(path: str) -> bool:
    """判断是不是 snap 的转发脚本。

    Ubuntu 把 `/usr/bin/chromium-browser` 做成一个小 shell 脚本，转发到
    `/snap/bin/chromium`。snap 版受 AppArmor 限制，root 下启动必失败
    （实测报 `Failed to create a ProcessSingleton for your profile directory`），
    而报错文案完全指不到真因。所以认出来并降级处理。
    """
    try:
        p = Path(path)
        if not p.is_file() or p.stat().st_size > 4096:
            return False
        head = p.read_bytes()[:256].decode("utf-8", "ignore")
        return "/snap/" in head or "snap/bin" in head
    except Exception:
        return False


def find_chrome() -> str:
    """定位可用的 Chrome/Chromium 可执行文件。

    优先级：真二进制 > Playwright 自带 chromium > snap 转发脚本。
    snap 排最后是因为它在 root 下必失败（见 `_is_snap_wrapper`），
    而 Playwright 自带的 Chromium 是能用的，不该被 snap 挡住。

    结果缓存：本函数在每次浏览器启动路径上都会被调用（Grok 注册 / oauth /
    sso 三条链路各一处，外加每次 mint），而 Playwright 兜底分支为了读一个
    静态路径要把整个 driver 进程拉起来（实测 1.28s）。同进程内 PATH 与环境
    不会变，缓存掉这次开销。
    """
    return _find_chrome_cached()


@lru_cache(maxsize=1)
def _find_chrome_cached() -> str:
    candidates = [
        os.getenv("GROK_CHROME_PATH", ""),
        os.getenv("CHROME_PATH", ""),
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    existing = [c for c in candidates if c and Path(c).exists()]

    # 第一轮：非 snap 的候选
    for c in existing:
        if not _is_snap_wrapper(c):
            return c

    # 第二轮：Playwright 自带 chromium（非 snap，root 下可用）
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path or ""
            if exe and Path(exe).exists():
                return exe
    except Exception:
        pass

    # 最后才接受 snap 脚本（至少路径是对的，失败时错误信息也算明确）
    return existing[0] if existing else ""


def mint_via_captcha(captcha: Any, site_key: str, page_url: str) -> str:
    """用本项目 BaseCaptcha 实现（YesCaptcha / LocalSolver / Manual）。"""
    token = captcha.solve_turnstile(page_url, site_key)
    token = str(token or "").strip()
    if len(token) <= 10:
        raise RuntimeError(f"captcha solver 返回的 token 无效 (len={len(token)})")
    return token


def mint_via_offscreen_chrome(
    site_key: str,
    page_url: str,
    proxy: str = "",
    timeout: float = 90,
    retries: int = 3,
    log: LogFn = None,
) -> str:
    """屏外 headed Chrome mint。出处：protocol_signup.py:355-419

    需要 scripts/grok_turnstile_mint.py 与 playwright。
    """
    if not _SCRIPT_PATH.exists():
        raise RuntimeError(f"未找到 mint 脚本: {_SCRIPT_PATH}")

    chrome = find_chrome()
    last_err = ""
    for attempt in range(1, max(1, retries) + 1):
        args = [
            sys.executable,
            str(_SCRIPT_PATH),
            "--site-key",
            site_key,
            "--url",
            page_url,
            "--timeout",
            str(int(timeout)),
            "--no-headless",
        ]
        if proxy:
            args.extend(["--proxy", proxy])
        if chrome:
            args.extend(["--chrome", chrome])
        _log(
            log,
            f"[Grok] Turnstile mint {attempt}/{retries} "
            f"(chrome={bool(chrome)}, proxy={bool(proxy)}, headed-offscreen)",
        )
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout + 30,
                creationflags=creationflags,
            )
        except subprocess.TimeoutExpired as exc:
            # 不能直接 `f"timeout: {exc}"` —— TimeoutExpired.__str__ 会带上完整
            # argv，而 args 里有 `--proxy http://user:pass@host`，于是代理密码
            # 会顺着「任务错误」一路回给客户端（实测 `contains SECRETPW -> True`）。
            # 只报时长与重试次数，细节用脱敏后的代理地址。
            last_err = (
                f"timeout after {timeout + 30}s"
                f"（proxy={redact_proxy_url(proxy) or '(none)'}）"
            )
            _log(log, f"[Grok] Turnstile mint 超时，重试（{attempt}/{retries}）")
            continue

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode == 0 and len(out) > 10:
            if "\n" in out:
                out = out.split("\n", 1)[0].strip()
            _log(log, f"[Grok] Turnstile token 就绪 (len={len(out)})")
            return out
        last_err = (err or out or "empty")[:300]
        _log(log, f"[Grok] Turnstile mint 失败: {last_err}")
        time.sleep(min(2 * attempt, 6))
    raise RuntimeError(f"turnstile mint 失败: {last_err}")


def mint_via_solver_service(
    site_key: str,
    page_url: str,
    solver_url: str = "",
    timeout: float = 120,
    log: LogFn = None,
) -> str:
    """外部 Solver 服务（cloudTemp 风格）。

    默认端口必须与**本项目自己启动的** solver 一致（`SOLVER_PORT`，默认 8889），
    不能是参考项目的 5072 —— 那个端口在本机根本没人监听，于是这条兜底路径
    每次都以 `Connection refused` 收场，日志里却看着像「solver 挂了」。
    """
    import requests

    base = (
        solver_url
        or os.getenv("GROK_SOLVER_URL")
        or os.getenv("LOCAL_SOLVER_URL")
        or f"http://127.0.0.1:{os.getenv('SOLVER_PORT', '8889')}"
    ).rstrip("/")
    _log(log, f"[Grok] 请求 Solver 服务: {base}")
    r = requests.get(
        f"{base}/turnstile",
        params={"url": page_url, "sitekey": site_key},
        timeout=20,
    )
    r.raise_for_status()
    task_id = r.json().get("taskId")
    if not task_id:
        raise RuntimeError(f"Solver 未返回 taskId: {r.text[:200]}")

    deadline = time.time() + max(timeout, 30)
    while time.time() < deadline:
        time.sleep(2)
        res = requests.get(f"{base}/result", params={"id": task_id}, timeout=10)
        if res.status_code != 200:
            continue
        data = res.json()
        status = data.get("status")
        if status == "ready":
            token = (data.get("solution") or {}).get("token")
            if token:
                _log(log, f"[Grok] Solver token 就绪 (len={len(token)})")
                return token
        elif status in ("CAPTCHA_FAIL", "fail", "error"):
            raise RuntimeError(f"Solver 失败: {data}")
    raise TimeoutError("Solver 超时")


def mint_turnstile(
    site_key: str,
    page_url: str,
    captcha: Any = None,
    proxy: str = "",
    log: LogFn = None,
    prefer: str = "",
) -> str:
    """统一入口：按 prefer → captcha → 屏外 Chrome → Solver 服务 顺序尝试。"""
    order = [prefer] if prefer else []
    order.extend(["captcha", "offscreen", "solver"])

    errors: list[str] = []
    for mode in order:
        try:
            if mode == "captcha" and captcha is not None:
                return mint_via_captcha(captcha, site_key, page_url)
            if mode == "offscreen":
                return mint_via_offscreen_chrome(
                    site_key, page_url, proxy=proxy, log=log
                )
            if mode == "solver":
                return mint_via_solver_service(site_key, page_url, log=log)
        except Exception as exc:
            errors.append(f"{mode}: {exc}")
            _log(log, f"[Grok] Turnstile {mode} 失败: {exc}")

    raise RuntimeError("所有 Turnstile 方案均失败 → " + " | ".join(errors))


__all__ = [
    "mint_turnstile",
    "mint_via_captcha",
    "mint_via_offscreen_chrome",
    "mint_via_solver_service",
    "find_chrome",
]
