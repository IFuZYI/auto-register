"""把账号绑定的代理交给面板 —— 各面板的「上传代理」接线。

四个面板对「这个账号走哪条出口」的表达方式完全不同：

| 面板 | 表达方式 | 接线 |
|---|---|---|
| CPA | auth 文件顶层 `proxy_url` | 上传时直接写进记录（已在 `cpa_upload` 里） |
| chatgpt2api | 导入对象 `proxy` 字段 | 同上（已在 `chatgpt2api_upload` 里） |
| Sub2API | 账号对象 `proxy_id`（外键，不是 URL） | **先建代理记录拿 id**，再随账号提交 |
| Grok2API | 账号没有代理字段，出口是 **egress 节点** | **建节点带 `proxyURL`**，再把账号绑上去 |

前两个是「把 URL 塞进 payload」，后两个是「先在对面建一条资源」。这个模块
就负责后两个 —— 它们都需要**先查再建**（幂等），否则每推一个账号就会在对端
堆一条重复代理。

## 幂等键

- Sub2API：`(protocol, host, port, username)` —— 同一出口换个密码不算新代理，
  但用户名不同就是不同的出口账号，必须分开。
- Grok2API：节点名 `auto-<host>-<port>`。名字是幂等键（对端允许同名，
  但重名节点会让"这条代理归谁"变得不可判），所以先按名字查。

## 失败策略

**绑定代理失败不阻断账号上传。** 代理是「让出口更干净」的优化，不是账号能不能
用的前提；因为建不出代理记录就把账号丢掉，等于让一个次要功能拖垮主流程。
失败时返回 `(None, 原因)`，调用方把原因写进上传结果的消息里 —— 用户看得到
「账号传上去了、代理没绑上」，而不是看到一个笼统的失败。
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)

#: 面板协议名归一：`socks5` 与 `socks5h` 在 Sub2API 里只有前者是合法枚举值。
_SUB2API_PROTOCOLS = {"http", "https", "socks5", "socks5h"}


def parse_proxy_url(url: str) -> Optional[dict[str, Any]]:
    """把代理 URL 拆成面板要的字段。拆不出（缺 host/port）返回 None。

    返回 `{protocol, host, port, username, password}`，用户名密码已 URL 解码
    （配置里存的是编码过的形式，`p%40ss` 这种不解码会被对面当成错误的密码）。
    """
    text = str(url or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    host = str(parts.hostname or "").strip()
    try:
        port = int(parts.port or 0)
    except ValueError:
        return None
    if not host or port <= 0:
        return None
    protocol = str(parts.scheme or "http").strip().lower() or "http"
    if protocol not in _SUB2API_PROTOCOLS:
        # socks4 之类对端不认，按 http 提交会被当成另一条代理 —— 宁可不做
        return None
    return {
        "protocol": protocol,
        "host": host,
        "port": port,
        "username": unquote(str(parts.username or "")),
        "password": unquote(str(parts.password or "")),
    }


def _dig_items(payload: Any) -> list:
    """从面板列表响应里挖出数组。两种形状都吃：`{items: []}` 与 `{data: {items: []}}`。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("items", "data", "list", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = _dig_items(value)
            if nested:
                return nested
    return []


def _dig_obj(payload: Any) -> dict:
    """从面板响应里挖出单个对象（同样兼容 `data` 包一层）。"""
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        return data
    return payload


def ensure_sub2api_proxy(
    api_url: str,
    api_key: str,
    proxy_url: str,
    *,
    timeout: int = 20,
) -> tuple[Optional[int], str]:
    """确保 Sub2API 里有一条对应这条代理的记录，返回 `(proxy_id, 说明)`。

    先按 `(host, port, username)` 在现有代理里找，找不到再建 —— 幂等键里
    **不含密码**：同一个出口换了密码还是那条代理，重复建只会让面板里出现
    两条看起来一样的记录，而账号绑在哪一条上完全不可预期。
    """
    parsed = parse_proxy_url(proxy_url)
    if not parsed:
        return None, "代理地址无法解析（缺 host/port 或不支持的协议）"

    api_url = str(api_url or "").strip().rstrip("/")
    api_key = str(api_key or "").strip()
    if not api_url or not api_key:
        return None, "Sub2API 地址或 API Key 未配置"

    from core.panel_tls import verify_for
    from curl_cffi import requests as cffi_requests

    headers = {
        "Accept": "application/json",
        "x-api-key": api_key,
        "Referer": f"{api_url}/admin/proxies",
    }

    # ── 先找 ──
    try:
        resp = cffi_requests.get(
            f"{api_url}/api/v1/admin/proxies",
            headers=headers,
            params={"page": 1, "page_size": 200, "search": parsed["host"]},
            verify=verify_for("sub2api"),
            timeout=timeout,
            impersonate="chrome110",
        )
        if int(getattr(resp, "status_code", 0) or 0) == 200:
            for row in _dig_items(resp.json()):
                if not isinstance(row, dict):
                    continue
                same = (
                    str(row.get("host") or "").strip().lower() == parsed["host"].lower()
                    and int(row.get("port") or 0) == parsed["port"]
                    and str(row.get("username") or "") == parsed["username"]
                )
                if same:
                    try:
                        return int(row.get("id")), "复用已有代理记录"
                    except (TypeError, ValueError):
                        continue
    except Exception as exc:  # noqa: BLE001 - 查列表失败就往下走「建」
        logger.debug("Sub2API 代理列表读取失败，尝试直接创建: %s", exc)

    # ── 再建 ──
    body = {
        "name": f"auto-{parsed['host']}-{parsed['port']}",
        "protocol": parsed["protocol"],
        "host": parsed["host"],
        "port": parsed["port"],
        "username": parsed["username"],
        "password": parsed["password"],
    }
    try:
        resp = cffi_requests.post(
            f"{api_url}/api/v1/admin/proxies",
            headers={**headers, "Content-Type": "application/json"},
            json=body,
            verify=verify_for("sub2api"),
            timeout=timeout,
            impersonate="chrome110",
        )
    except Exception as exc:  # noqa: BLE001 - 绑定失败不该让上传失败
        return None, f"创建代理记录异常: {exc}"

    status = int(getattr(resp, "status_code", 0) or 0)
    if status not in (200, 201):
        brief = " ".join(str(getattr(resp, "text", "") or "").split())[:120]
        return None, f"创建代理记录失败 HTTP {status}: {brief}"

    obj = _dig_obj(resp.json() if callable(getattr(resp, "json", None)) else {})
    try:
        return int(obj.get("id")), "已创建代理记录"
    except (TypeError, ValueError):
        return None, "创建代理记录成功但响应里没有 id"


def ensure_grok2api_egress(
    client,
    proxy_url: str,
    *,
    scope: str = "grok_web",
) -> tuple[Optional[str], str]:
    """确保 grok2api 里有一条对应这条代理的 egress 节点，返回 `(node_id, 说明)`。

    grok2api 的账号本身没有代理字段 —— 出口由 **egress 节点**提供，节点带
    `proxyURL`，账号通过 `POST /egress-nodes/:id/accounts` 绑上去。所以这里
    建的是节点，不是账号属性。

    幂等键是**节点名** `auto-<host>-<port>`：同名节点已存在就复用（用户可能
    已经手动改过容量/UA，重建会把这些设置冲掉）。
    """
    parsed = parse_proxy_url(proxy_url)
    if not parsed:
        return None, "代理地址无法解析（缺 host/port 或不支持的协议）"

    name = f"auto-{parsed['host']}-{parsed['port']}"
    try:
        # 注意：grok2api 的 `_request` **没有 `params` 参数**，查询串得自己拼进
        # path；body 的参数名是 `json_body` 而不是 `json`（见 platforms/grok/grok2api.py）。
        resp = client._request(
            "GET",
            f"/api/admin/v1/egress-nodes?scope={scope}",
            headers=client._auth_headers(),
            timeout=30,
        )
        if int(getattr(resp, "status_code", 0) or 0) == 200:
            for row in _dig_items(resp.json()):
                if not isinstance(row, dict):
                    continue
                if str(row.get("name") or "") == name:
                    node_id = str(row.get("id") or "").strip()
                    if node_id:
                        return node_id, "复用已有出口节点"
    except Exception as exc:  # noqa: BLE001 - 查不到就往下走「建」
        logger.debug("grok2api egress 列表读取失败，尝试直接创建: %s", exc)

    body = {
        "name": name,
        "scope": scope,
        "enabled": True,
        "proxyURL": str(proxy_url or "").strip(),
    }
    try:
        resp = client._request(
            "POST",
            "/api/admin/v1/egress-nodes",
            headers=client._auth_headers(content_type="application/json"),
            json_body=body,
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - 绑定失败不该让上传失败
        return None, f"创建出口节点异常: {exc}"

    status = int(getattr(resp, "status_code", 0) or 0)
    if status not in (200, 201):
        brief = " ".join(str(getattr(resp, "text", "") or "").split())[:120]
        return None, f"创建出口节点失败 HTTP {status}: {brief}"

    obj = _dig_obj(resp.json() if callable(getattr(resp, "json", None)) else {})
    node_id = str(obj.get("id") or "").strip()
    if not node_id:
        return None, "创建出口节点成功但响应里没有 id"
    return node_id, "已创建出口节点"


def assign_grok2api_account(client, node_id: str, account_id: Any) -> tuple[bool, str]:
    """把一个账号绑到 egress 节点上（`mode=manual`，不让对端重新调度走）。"""
    aid = str(account_id or "").strip()
    if not aid:
        return False, "缺少账号 id，无法绑定出口节点"
    try:
        resp = client._request(
            "POST",
            f"/api/admin/v1/egress-nodes/{node_id}/accounts",
            headers=client._auth_headers(content_type="application/json"),
            json_body={"ids": [aid], "mode": "manual"},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 - 绑定失败不该让上传失败
        return False, f"绑定出口节点异常: {exc}"

    status = int(getattr(resp, "status_code", 0) or 0)
    if status not in (200, 201, 204):
        brief = " ".join(str(getattr(resp, "text", "") or "").split())[:120]
        return False, f"绑定出口节点失败 HTTP {status}: {brief}"
    return True, "已绑定出口节点"


__all__ = [
    "assign_grok2api_account",
    "ensure_grok2api_egress",
    "ensure_sub2api_proxy",
    "parse_proxy_url",
]
