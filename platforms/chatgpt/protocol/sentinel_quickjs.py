"""Sentinel PoW 求解器：在 Node 沙箱里跑 OpenAI 真实的 sdk.js。

为什么不能纯 Python 算：
  自己实现的 PoW 能通过 OpenAI 的表层校验（``/sentinel/req``、
  ``/authorize/continue`` 都返 200），但发码服务会在服务端跑真正的 sentinel SDK
  复核 token，合成的 token 过不了那一关 —— 表现是验证码邮件被静默丢弃，链路
  看起来一切正常却永远收不到码。唯一可行的办法是把 OpenAI 的 ``sdk.js`` 下下来
  在 JS 沙箱里跑一遍，输出跟真实浏览器一模一样的 token。

流程：
  每次取 token 起一个 ``node`` 子进程，脚本在 ``node:vm`` 沙箱里装好
  document / navigator / canvas / WebGL 等一套浏览器运行时后加载 sdk.js，
  然后分两趟：``action=requirements`` 拿 ``request_p`` → 打 ``/sentinel/req``
  换 challenge → ``action=solve`` 出主 token 和 SO token。solve 那一趟还会派发
  一段拟真的鼠标/滚轮/键盘事件（sdk.js 的行为采集器要看到这些才肯出 SO token）。

公开 API：
  ``get_sentinel_token_via_quickjs(session, device_id, flow, ...) -> (token, so_token) | None``
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


SENTINEL_VERSION = "20260219f9f6"
SENTINEL_SDK_URL = f"https://sentinel.openai.com/sentinel/{SENTINEL_VERSION}/sdk.js"
SENTINEL_REQ_URL = "https://sentinel.openai.com/backend-api/sentinel/req"


def _resolve_node_binary() -> str:
    return (os.getenv("OPENAI_SENTINEL_NODE_PATH", "") or "").strip() or "node"


def _quickjs_script_path() -> Path:
    return Path(__file__).resolve().parent / "openai_sentinel_quickjs.js"


_sdk_file_cache: Optional[Path] = None


def _ensure_sdk_file(session: Any, timeout_ms: int) -> Path:
    """Download OpenAI's actual sdk.js to /tmp cache (one-shot per version)."""
    global _sdk_file_cache
    if _sdk_file_cache and _sdk_file_cache.exists():
        return _sdk_file_cache

    cache_dir = Path(tempfile.gettempdir()) / "openai-sentinel-demo" / SENTINEL_VERSION
    cache_dir.mkdir(parents=True, exist_ok=True)
    sdk_file = cache_dir / "sdk.js"
    if sdk_file.exists() and sdk_file.stat().st_size > 0:
        _sdk_file_cache = sdk_file
        return sdk_file

    resp = session.get(
        SENTINEL_SDK_URL,
        headers={
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9",
            "referer": "https://auth.openai.com/",
            "sec-fetch-dest": "script",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-site",
        },
        timeout=max(10, int(timeout_ms / 1000)),
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"下载 sdk.js 失败: HTTP {resp.status_code}")
    content = getattr(resp, "content", b"") or (resp.text or "").encode()
    if not content:
        raise RuntimeError("下载 sdk.js 失败: 响应为空")
    sdk_file.write_bytes(content)
    _sdk_file_cache = sdk_file
    return sdk_file


def _run_quickjs_action(
    *,
    action: str,
    sdk_file: Path,
    quickjs_script: Path,
    payload: dict,
    timeout_ms: int,
) -> dict:
    body = dict(payload)
    body["action"] = action
    node = _resolve_node_binary()
    try:
        proc = subprocess.run(
            [node, str(quickjs_script)],
            input=json.dumps(body, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=max(10, int(timeout_ms / 1000) + 5),
            env={
                **os.environ,
                "OPENAI_SENTINEL_SDK_FILE": str(sdk_file),
            },
        )
    except FileNotFoundError as exc:
        # 缺 node 时的表象是"注册流程一切正常但验证码永远收不到"，
        # 排查成本极高，所以这里把原因说透而不是让它混进通用异常。
        raise RuntimeError(
            f"找不到 Node 运行时 ({node})：Sentinel PoW 必须在 Node 沙箱里跑 OpenAI 的 sdk.js，"
            "缺少它会导致验证码邮件被服务端静默丢弃。请安装 Node.js 18+，"
            "或用 OPENAI_SENTINEL_NODE_PATH 指定可执行文件的绝对路径。"
        ) from exc
    if proc.returncode != 0:
        raise RuntimeError(f"QuickJS 执行失败: {(proc.stderr or proc.stdout or 'unknown').strip()[:300]}")
    out = (proc.stdout or "").strip()
    if not out:
        raise RuntimeError("QuickJS 返回空输出")
    data = json.loads(out)
    if not isinstance(data, dict):
        raise RuntimeError("QuickJS 输出不是 JSON 对象")
    return data


def _fetch_sentinel_challenge(
    session: Any,
    *,
    device_id: str,
    flow: str,
    request_p: str,
    timeout_ms: int,
) -> dict:
    body = {"p": request_p, "id": device_id, "flow": flow}
    resp = session.post(
        SENTINEL_REQ_URL,
        data=json.dumps(body, separators=(",", ":")),
        headers={
            "origin": "https://sentinel.openai.com",
            "referer": f"https://sentinel.openai.com/backend-api/sentinel/frame.html?sv={SENTINEL_VERSION}",
            "content-type": "text/plain;charset=UTF-8",
            "accept": "*/*",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "zh-CN,zh;q=0.9",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        },
        timeout=max(10, int(timeout_ms / 1000)),
    )
    if getattr(resp, "status_code", 0) != 200:
        raise RuntimeError(f"/sentinel/req HTTP {resp.status_code}")
    payload = resp.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Sentinel challenge 响应不是 JSON 对象")
    return payload


def get_sentinel_token_via_quickjs(
    session: Any,
    device_id: str,
    *,
    flow: str = "authorize_continue",
    timeout_ms: int = 45000,
    log: Optional[Callable[[str], None]] = None,
    user_agent: str = "",
    screen: str = "",
    lang: str = "",
    lang_full: str = "",
    browser_type: str = "",
    platform: str = "",
    vendor: Optional[str] = None,
    hardware_concurrency: int = 0,
    device_memory: Optional[int] = None,
    max_touch_points: int = 0,
    device_pixel_ratio: float = 0.0,
    timezone: str = "",  # IANA 时区名（如 Asia/Tokyo）
    # Client Hints 全套（QuickJS 路径不直接用，但为了签名统一接收）
    sec_ch_ua_full_version_list: str = "",
    sec_ch_ua_arch: str = "",
    sec_ch_ua_bitness: str = "",
    sec_ch_ua_model: str = "",
    sec_ch_ua_platform_version: str = "",
) -> Optional[tuple[str, str]]:
    """Try the QuickJS path. Return JSON string on success, None on any failure.

    Caller is expected to fall back to pure-Python sentinel on None.

    指纹一致性：``platform`` / ``vendor`` / ``hardware_concurrency`` 等按调用方
    传入的浏览器家族画像喂给 sdk.js 的 navigator，避免 UA 说 Windows Chrome 但
    navigator 报 MacIntel/Apple 的硬伤。未传时按 UA 推断合理默认值。
    """
    log = log or (lambda m: logger.info(m))
    quickjs_script = _quickjs_script_path()
    if not quickjs_script.exists():
        log(f"Sentinel QuickJS 脚本不存在: {quickjs_script}")
        return None

    did = str(device_id or uuid.uuid4())

    screen_w, screen_h = "1920", "1080"
    if screen and "x" in screen:
        parts = screen.split("x", 1)
        screen_w, screen_h = parts[0], parts[1]

    lang_primary = lang or "en-US"
    languages = [lang_primary]
    if lang_full:
        for part in lang_full.split(","):
            tag = part.split(";")[0].strip()
            if tag and tag not in languages:
                languages.append(tag)

    # ── 指纹一致性：platform / vendor 未显式传入时按 UA 推断，绝不写死 MacIntel ──
    ua_l = (user_agent or "").lower()
    if not platform:
        if "iphone" in ua_l:
            platform = "iPhone"
        elif "windows" in ua_l:
            platform = "Win32"
        elif "mac" in ua_l:
            platform = "MacIntel"
        else:
            platform = "Win32"
    if vendor is None:
        if "firefox" in ua_l:
            vendor = ""                       # Firefox navigator.vendor 为空串
        elif "chrome" in ua_l:
            vendor = "Google Inc."
        else:
            vendor = "Apple Computer, Inc."   # Safari / iOS
    hw_conc = int(hardware_concurrency) if hardware_concurrency else 8

    env_payload = {
        "device_id": did,
        "user_agent": user_agent or "Mozilla/5.0",
        "screen_width": screen_w,
        "screen_height": screen_h,
        "language": lang_primary,
        "languages": languages,
        "platform": platform,
        "vendor": vendor,
        "hardware_concurrency": hw_conc,
        "browser_type": browser_type or "",
        "device_pixel_ratio": float(device_pixel_ratio) if device_pixel_ratio else 1.0,
        "max_touch_points": int(max_touch_points),
        "timezone": timezone or "UTC",  # IANA 时区名
    }
    # deviceMemory 仅 Chromium 暴露；None 时不下发该键，JS 侧保持 undefined
    if device_memory is not None:
        env_payload["device_memory"] = int(device_memory)

    try:
        sdk_file = _ensure_sdk_file(session, timeout_ms)

        requirements = _run_quickjs_action(
            action="requirements",
            sdk_file=sdk_file,
            quickjs_script=quickjs_script,
            payload=env_payload,
            timeout_ms=timeout_ms,
        )
        request_p = str(requirements.get("request_p") or "").strip()
        if not request_p:
            log("Sentinel QuickJS 失败: requirements 未返回 request_p")
            return None

        challenge = _fetch_sentinel_challenge(
            session, device_id=did, flow=flow, request_p=request_p, timeout_ms=timeout_ms,
        )
        c_value = str(challenge.get("token") or "").strip()
        if not c_value:
            log("Sentinel QuickJS 失败: challenge token 为空")
            return None

        solve_payload = dict(env_payload)
        solve_payload.update({
            "request_p": request_p,
            "challenge": challenge,
            "flow": flow,
            "behavior_duration_ms": 4200,
        })
        solved = _run_quickjs_action(
            action="solve",
            sdk_file=sdk_file,
            quickjs_script=quickjs_script,
            payload=solve_payload,
            timeout_ms=timeout_ms,
        )

        so_token_raw = str(solved.get("so_token") or "").strip()

        # SO token 要不要，是**服务端在 challenge 里说了算**的，不是每个 flow 都有。
        # sdk.js 里 SO 采集器的启动条件（去混淆）：
        #     challenge.so.required === true && typeof challenge.so.collector_dx === 'string'
        # 实测 2026-08-06 三个 flow 的 /sentinel/req 响应：
        #     authorize_continue    → 有 so 块, required=true
        #     oauth_create_account  → 有 so 块, required=true
        #     username_password_create → **顶层根本没有 so 键**
        # 也就是说真实浏览器跑 username_password_create 同样不会有 SO token。
        # 以前这里无条件要求 so_token 非空，把「服务端没要」误判成「我们没算出来」，
        # 打出「中止以避免封号」——是误报。更糟的是调用方降级时会沿用上一个 flow 的
        # SO token 继续发，等于给一个明说不需要 SO 的请求塞了个别的 flow 的凭证，
        # 比不发更像异常特征。现在按服务端的要求判定。
        so_required = bool((challenge.get("so") or {}).get("required") is True)

        sdk_token = str(solved.get("token") or "").strip()
        if not sdk_token:
            log("Sentinel QuickJS 失败: SDK token 为空，中止以避免封号")
            return None
        if so_required and not so_token_raw:
            # 服务端确实要了 SO token 但我们没算出来 —— 这才是真异常，保持中止
            log("Sentinel QuickJS 失败: 服务端要求 SO token 但求解为空，中止以避免封号")
            return None
        log(f"Sentinel QuickJS OK (len={len(sdk_token)}, "
            f"so={'Y' if so_token_raw else 'N/A(服务端未要求)'})")
        return (sdk_token, so_token_raw)
    except Exception as e:
        # ⚠️ 这里曾经是个纯 catch-all：任何异常都降级成一行 INFO 日志 + return None，
        #    上层只能看到"主 token 缺失"，真因全被掩盖。2026-08-10 主人批量跑 10 个号，
        #    其中一次失败日志是「Sentinel QuickJS 失败（主 token 缺失…）」，看着像 PoW
        #    算不出来，实际是 /sentinel/req 那个 POST 撞了链路级 TLS 瞬断
        #    （curl:(35)，全局 5.4% 偶发）—— 排查方向被带偏了一整轮。
        #    网络类异常现在原样抛出去，让 registrar 的 classify_error 判成 network，
        #    也让 http_client 的 TLS 重试有机会先兜住；真正的 JS/PoW 问题才 return None。
        from platforms.chatgpt.protocol.http_client import _is_tls_handshake_error

        if _is_tls_handshake_error(e):
            log(f"Sentinel 网络异常（非 PoW 问题，原样上抛）: {e}")
            raise
        log(f"Sentinel QuickJS 异常: {e}")
        return None
