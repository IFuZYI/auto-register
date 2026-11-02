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

import logging

logger = logging.getLogger(__name__)

import os
import shutil
import subprocess
import sys
import threading
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
            logger.debug("忽略异常（保留原行为）", exc_info=True)


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


#: 系统里的 Chrome/Chromium 常见位置（按优先级）。
#: 单独列成模块常量是为了让测试可以把它清空、从而验证后面的兜底路径。
_SYSTEM_CHROME_CANDIDATES = (
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)

#: Playwright 浏览器缓存里 Chromium 的相对路径（各平台布局）。
_PLAYWRIGHT_CHROMIUM_RELATIVE = (
    "chrome-linux64/chrome",
    "chrome-linux/chrome",
    "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
    "chrome-win/chrome.exe",
)


def _playwright_chromium_candidates() -> list[str]:
    """扫 Playwright 的浏览器缓存目录，返回其中的 Chromium 可执行文件。

    为什么不问 Playwright 自己：`p.chromium.executable_path` 要把 driver
    进程（node）拉起来，本机实测那个进程会被杀掉
    （`TargetClosedError: Target page, context or browser has been closed`），
    于是 `find_chrome()` 返回空串 → 铸币三连 `chrome not found`，
    而真正可用的二进制一直躺在
    `~/.cache/ms-playwright/chromium-1248/chrome-linux64/chrome` 里。
    目录布局是确定性的，直接扫，快且无副作用。

    `PLAYWRIGHT_BROWSERS_PATH` 按 Playwright 的约定支持多个目录（os.pathsep 分隔）。
    """
    roots: list[Path] = []
    for part in os.getenv("PLAYWRIGHT_BROWSERS_PATH", "").split(os.pathsep):
        part = part.strip()
        if part:
            roots.append(Path(part))
    roots.append(Path.home() / ".cache" / "ms-playwright")

    found: list[str] = []
    for root in roots:
        if not root.is_dir():
            continue
        for rel in _PLAYWRIGHT_CHROMIUM_RELATIVE:
            found.extend(
                sorted(str(p) for p in root.glob(f"chromium*/{rel}") if p.is_file())
            )
    return found


def find_chrome() -> str:
    """定位可用的 Chrome/Chromium 可执行文件。

    优先级：真二进制 > Playwright 缓存里的 Chromium > Playwright driver 兜底 >
    snap 转发脚本。snap 排最后是因为它在 root 下必失败（见 `_is_snap_wrapper`）。

    结果缓存：本函数在每次浏览器启动路径上都会被调用（Grok 注册 / oauth /
    sso 三条链路各一处，外加每次 mint），而 driver 兜底分支为了读一个静态路径
    要把整个 node 进程拉起来（实测 1.28s）。同进程内 PATH 与环境不会变，
    缓存掉这次开销。
    """
    return _find_chrome_cached()


@lru_cache(maxsize=1)
def _find_chrome_cached() -> str:
    candidates = [
        os.getenv("GROK_CHROME_PATH", ""),
        os.getenv("CHROME_PATH", ""),
        *_SYSTEM_CHROME_CANDIDATES,
    ]
    existing = [c for c in candidates if c and Path(c).exists()]

    # 第一轮：非 snap 的候选
    for c in existing:
        if not _is_snap_wrapper(c):
            return c

    # 第二轮：Playwright 浏览器缓存（确定性路径，不需要起 driver）
    for c in _playwright_chromium_candidates():
        if not _is_snap_wrapper(c):
            return c

    # 第三轮：问 Playwright driver（会起 node 进程，可能被杀掉 → 见 _playwright_chromium_candidates）
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path or ""
            if exe and Path(exe).exists():
                return exe
    except Exception:
        logger.debug("忽略异常（保留原行为）", exc_info=True)

    # 最后才接受 snap 脚本（至少路径是对的，失败时错误信息也算明确）
    return existing[0] if existing else ""


def mint_via_captcha(captcha: Any, site_key: str, page_url: str) -> str:
    """用本项目 BaseCaptcha 实现（YesCaptcha / LocalSolver / Manual）。"""
    token = captcha.solve_turnstile(page_url, site_key)
    token = str(token or "").strip()
    if len(token) <= 10:
        raise RuntimeError(f"captcha solver 返回的 token 无效 (len={len(token)})")
    return token


#: X11 socket 目录（用来判断 `DISPLAY` 指向的 display 是不是真的在跑）。
#: 单独列成模块常量是为了让测试指到临时目录。
_X11_SOCKET_DIR = "/tmp/.X11-unix"

#: 没有 X display 时给 headed 浏览器套的虚拟屏幕（`xvfb-run` 参数）。
_XVFB_ARGS = ("xvfb-run", "-a", "-s", "-screen 0 1280x800x24")


def _display_is_usable() -> bool:
    """`DISPLAY` 是否指向一个真实存在的 X display。"""
    display = (os.environ.get("DISPLAY") or "").strip()
    if not display:
        return False
    # `:77` / `localhost:77.0` → 77；socket 名是 `X77`
    num = display.split(":")[-1].split(".")[0].strip()
    if not num:
        return False
    return Path(_X11_SOCKET_DIR, f"X{num}").exists()


def _virtual_display_prefix() -> list[str]:
    """headed 浏览器需要的 display 前缀（没有真的 X server 时用 xvfb-run）。

    为什么要这个：offscreen mint 拉起的是 **headed** 浏览器（真 headless 会被
    Turnstile 拦），而线上服务器没有 X server，Playwright 会报
    `Looks like you launched a headed browser without having a XServer running.`
    再把进程关掉 —— 外面只能看到退化成 `TargetClosedError: BrowserType.launch`，
    完全指不到真因（实测 2026-11-01，三连失败都是这个）。

    本机 `/usr/bin/Xvfb` 与 `/usr/bin/xvfb-run` 都在，所以直接套 `xvfb-run -a`
    （`-a` 自动挑空闲 display 号，避免和已有 Xvfb 抢编号）。
    """
    if _display_is_usable():
        return []
    if shutil.which("xvfb-run"):
        return list(_XVFB_ARGS)
    logger.warning(
        "没有可用的 X display，也没装 xvfb-run：headed 浏览器会启动失败"
        "（Turnstile mint 需要一块屏幕）"
    )
    return []


#: 屏外铸币的**全局串行锁**：每铸一次都会起一整个 Chromium（含 xvfb），
#: 并发跑 3 个注册就等于 3 个 Chromium 抢内存 —— 本机 2GB RAM 上实测直接把
#: 主进程 OOM 掉（2026-11-02 18:25，服务无 traceback 直接消失，日志停在
#: 「已进入邮箱注册视图」）。串行之后多出来的等待（十几秒）远比崩一次便宜。
_MINT_LOCK = threading.Lock()

#: 低于这个可用内存（MB）就不再起铸币浏览器 —— 宁可这一轮报「拿不到令牌」，
#: 也不要为了补一张票把整个服务搞崩。默认 200MB：本机（2GB RAM）空载时
#: MemAvailable 也只有 ~300MB，阈值定太高会等于永久关掉内建铸币。
#: 可用 `GROK_MINT_MIN_FREE_MB` 覆盖（调小=更激进，调大=更保守）。
MIN_FREE_MB_FOR_MINT = int(os.getenv("GROK_MINT_MIN_FREE_MB", "200") or 200)


def _available_mb() -> int:
    """读 `/proc/meminfo` 的 MemAvailable（MB）；读不到返回一个大数（=放行）。"""
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception as exc:  # noqa: BLE001 - 读不到就别拦
        logger.debug("读 MemAvailable 失败: %s", exc)
    return 10**6


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

    free_mb = _available_mb()
    if free_mb < MIN_FREE_MB_FOR_MINT:
        # 内存不够就别起 Chromium：宁可这一轮没票，也不要 OOM 掉整个服务
        # （2026-11-02 实测：3 个并发注册 + 铸币把 2GB 的机器打爆）。
        raise RuntimeError(
            f"可用内存不足（{free_mb}MB < {MIN_FREE_MB_FOR_MINT}MB），跳过屏外铸币"
        )

    chrome = find_chrome()
    last_err = ""
    with _MINT_LOCK:
        return _mint_offscreen_locked(site_key, page_url, proxy, timeout, retries, log, chrome)


def _mint_offscreen_locked(
    site_key: str,
    page_url: str,
    proxy: str,
    timeout: float,
    retries: int,
    log: LogFn,
    chrome: str,
) -> str:
    """锁内实现（见 `mint_via_offscreen_chrome` 的串行锁与内存闸门）。"""
    last_err = ""
    for attempt in range(1, max(1, retries) + 1):
        args = [
            *_virtual_display_prefix(),
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
            f"(chrome={bool(chrome)}, proxy={bool(proxy)}, headed-offscreen,"
            f" display={os.environ.get('DISPLAY') or 'xvfb-run'})",
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
