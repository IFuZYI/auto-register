"""请求来源判定：回环识别 + 客户端 IP。

为什么单独一个模块：这两个判断被 `main.py`（鉴权中间件）和 `api/auth.py`
（登录退避、首次设密码）同时需要，而 `api.auth` 不能 import `main`（循环）。

两条纪律：

1. **代理头默认不信**。`X-Forwarded-For` / `X-Real-IP` 是请求方随手能写的，
   拿它决定「是否放行」或「按 IP 限速」等于把开关交给调用方。只有部署方
   显式声明 `APP_TRUST_PROXY=1`（真的挂在 nginx/caddy 后面）才采信。
2. **回环判定不看任何头**。反向代理在同一个 host 上连进来，源地址本来就是
   127.0.0.1，判定依然成立。
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["client_ip", "is_loopback_client", "trust_proxy_enabled"]

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def trust_proxy_enabled() -> bool:
    """部署方是否显式声明「我前面有反代，代理头可信」。"""
    return os.getenv("APP_TRUST_PROXY", "").strip() == "1"


def is_loopback_client(request: Any) -> bool:
    """请求是否来自本机回环。

    测试客户端（`TestClient`）的主机名是 `testclient`，不是回环地址；只有在
    显式设置了 `APP_TRUST_TEST_CLIENT=1` 的测试环境里才放行 —— 生产没有这个
    变量，所以不会因为「测试能过」而在线上开个洞。
    """
    client = getattr(request, "client", None)
    if client is None:
        return False
    host = str(getattr(client, "host", "") or "")
    if host in _LOOPBACK_HOSTS or host.startswith("127."):
        return True
    if host == "testclient" and os.getenv("APP_TRUST_TEST_CLIENT", "") == "1":
        return True
    return False


def client_ip(request: Any) -> str:
    """用于限速/退避计数的客户端标识。

    默认取 `request.client.host`；只有 `APP_TRUST_PROXY=1` 时才看
    `X-Forwarded-For` / `X-Real-IP`（取第一个非空段）。
    """
    if trust_proxy_enabled():
        forwarded = str(request.headers.get("x-forwarded-for", "") or "")
        for part in forwarded.split(","):
            candidate = part.strip()
            if candidate:
                return candidate
        real = str(request.headers.get("x-real-ip", "") or "").strip()
        if real:
            return real
    client = getattr(request, "client", None)
    host = str(getattr(client, "host", "") or "") if client is not None else ""
    return host or "unknown"
