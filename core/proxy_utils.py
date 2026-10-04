from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional
from urllib.parse import quote, unquote, urlsplit, urlunsplit


@dataclass(frozen=True)
class _LegacyProxyParts:
    scheme: str
    host: str
    port: str
    username: str = ""
    password: str = ""


def _parse_legacy_proxy(value: str) -> _LegacyProxyParts | None:
    fields = value.split(":")
    if len(fields) == 2 and fields[1].isdigit():
        return _LegacyProxyParts("http", fields[0], fields[1])
    if len(fields) == 4 and fields[1].isdigit():
        return _LegacyProxyParts("http", fields[0], fields[1], fields[2], fields[3])
    if len(fields) >= 5 and fields[2].isdigit() and fields[0].lower() in {
        "http",
        "https",
        "socks4",
        "socks5",
        "socks5h",
    }:
        return _LegacyProxyParts(
            fields[0].lower(),
            fields[1],
            fields[2],
            fields[3],
            ":".join(fields[4:]),
        )
    return None


def _legacy_to_url(parts: _LegacyProxyParts) -> str:
    scheme = "socks5h" if parts.scheme == "socks5" else parts.scheme
    host = parts.host
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = ""
    if parts.username or parts.password:
        username = quote(parts.username, safe="")
        password = quote(parts.password, safe="")
        auth = f"{username}:{password}@"
    return f"{scheme}://{auth}{host}:{parts.port}"


def _is_auth_socks_proxy(scheme: str, username: str, password: str) -> bool:
    normalized = (scheme or "").lower()
    return normalized in {"socks5", "socks5h"} and bool(username or password)


def is_authenticated_socks5_proxy(proxy_url: Optional[str]) -> bool:
    if not proxy_url:
        return False

    value = str(proxy_url).strip()
    if not value:
        return False

    if value.startswith("{"):
        try:
            data = json.loads(value)
            if isinstance(data, dict):
                server = str(data.get("server") or "").strip()
                if not server:
                    return False
                scheme = (urlsplit(server).scheme or "").lower()
                username = str(data.get("username") or "").strip()
                password = str(data.get("password") or "").strip()
                return _is_auth_socks_proxy(scheme, username, password)
        except Exception:
            return False

    parts = urlsplit(value)
    return _is_auth_socks_proxy(
        parts.scheme or "",
        unquote(parts.username or ""),
        unquote(parts.password or ""),
    )


def normalize_proxy_url(proxy_url: Optional[str]) -> Optional[str]:
    """将 socks5:// 规范化为 socks5h://，避免本地 DNS 泄漏。"""
    if proxy_url is None:
        return None

    value = str(proxy_url).strip()
    if not value:
        return None

    legacy = _parse_legacy_proxy(value)
    if legacy:
        return _legacy_to_url(legacy)

    parts = urlsplit(value)
    if (parts.scheme or "").lower() == "socks5":
        parts = parts._replace(scheme="socks5h")
        return urlunsplit(parts)
    return value


def redact_proxy_url(proxy_url: Optional[str]) -> str:
    """Return a log-safe proxy URL without authentication credentials."""
    value = str(proxy_url or "").strip()
    if not value:
        return ""

    legacy = _parse_legacy_proxy(value)
    if legacy and "://" not in value:
        if legacy.username or legacy.password:
            if value.split(":", 1)[0].lower() in {
                "http",
                "https",
                "socks4",
                "socks5",
                "socks5h",
            }:
                return f"{legacy.scheme}:{legacy.host}:{legacy.port}:***:***"
            return f"{legacy.host}:{legacy.port}:***:***"
        return f"{legacy.host}:{legacy.port}"

    parts = urlsplit(value)
    if not parts.scheme:
        return "(configured proxy)"

    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        return f"{parts.scheme}://(configured proxy)"
    auth = (
        "***:***@" if parts.username is not None or parts.password is not None else ""
    )
    return urlunsplit(
        (parts.scheme, f"{auth}{host}{port}", parts.path, parts.query, parts.fragment)
    )


def build_requests_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    normalized = normalize_proxy_url(proxy_url)
    if not normalized:
        return None
    return {"http": normalized, "https": normalized}


def build_playwright_proxy_config(proxy_url: Optional[str]) -> Optional[dict[str, str]]:
    """把代理 URL 转成 Playwright 的 proxy 配置。

    **关键限制：Chromium 不支持带认证的 SOCKS5。**

    实测（2026-09-30）：
      - `{"server": "socks5://user:pass@host:port"}` → `net::ERR_NO_SUPPORTED_PROXIES`
        （Chromium 把整串当 host 解析，认证部分让它直接拒连）
      - `{"server": "socks5://host:port", "username": ..., "password": ...}`
        → `Browser does not support socks5 proxy authentication`（启动即失败）
      - 同一个端点改成 `{"server": "http://host:port", "username": ...}` → ✅ 通

    所以带认证的 socks5 不能直接交给浏览器。多数住宅代理商（含本项目在用的那家）
    同一端口同时提供 HTTP CONNECT，因此这里**把带认证的 socks5 降级成 http**：
    认证照样带上，只是用 HTTP CONNECT 隧道而不是 SOCKS 握手。

    代价要说清楚：HTTP CONNECT 只对 TCP 隧道，不走 SOCKS 的远程 DNS 解析，
    所以 `socks5h://` 那种「域名交给代理解析」的隐私收益没有了。这是唯一能让
    Chromium 用上带认证代理的办法；不想要这个代价就只能换不带认证的 socks5，
    或换支持 socks5 认证的 Firefox。

    不带认证的 socks5 原样保留 —— Chromium 支持它，没必要降级。
    """
    value = normalize_proxy_url(proxy_url)
    if not value:
        return None
    parts = urlsplit(value)
    if not parts.scheme or not parts.hostname or parts.port is None:
        server = value
        if server.startswith("socks5h://"):
            server = "socks5://" + server[len("socks5h://") :]
        return {"server": server}

    scheme = (parts.scheme or "").lower()
    if scheme == "socks5h":
        scheme = "socks5"

    username = unquote(parts.username or "")
    password = unquote(parts.password or "")

    # 带认证的 socks5 → 降级成 http（Chromium 的限制，见 docstring）
    if scheme in {"socks4", "socks5"} and (username or password):
        scheme = "http"

    config = {"server": f"{scheme}://{parts.hostname}:{parts.port}"}
    if username:
        config["username"] = username
    if password:
        config["password"] = password
    return config
