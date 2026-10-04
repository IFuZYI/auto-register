"""iCloud Cookie 归一化。

账号凭据只保存一个扁平的 Cookie 头，供 setup / HME 复用。Apple Web 服务会
比对浏览器登录流程使用的“带引号”表示，因此写出时统一重新加引号。
"""

from __future__ import annotations

import json
import time
from typing import Any, Iterable, Mapping
from urllib.parse import unquote

from .constants import cookie_domain_for

_INVALID_NAME_CHARS = set(" \t\r\n;=,")


def is_valid_cookie_name(name: str) -> bool:
    name = str(name or "").strip()
    return bool(name) and not any(character in _INVALID_NAME_CHARS for character in name)


def _sanitize_value(value: str) -> str:
    """丢弃 Go net/http 同样会拒绝的字节，避免生成非法 Cookie 头。"""
    return "".join(
        character
        for character in str(value or "")
        if 0x20 < ord(character) < 0x7F and character not in '";\\'
    )


def parse_cookie_header(header: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for part in str(header or "").split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and is_valid_cookie_name(name):
            values[name.strip()] = value.strip()
    return values


def _unquote(value: str) -> str:
    value = str(value or "")
    if len(value) > 1 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def cookie_map_to_header(values: Mapping[str, str]) -> tuple[str, int]:
    parts = [f"{name}={values[name]}" for name in sorted(values)]
    return "; ".join(parts), len(parts)


def quote_cookie_header(header: str) -> tuple[str, int]:
    parts = []
    for name, value in parse_cookie_header(header).items():
        sanitized = _sanitize_value(_unquote(value))
        parts.append(f'{name}="{sanitized}"')
    return "; ".join(parts), len(parts)


def normalize_cookies(header: str = "", raw: Any = None) -> tuple[str, int]:
    """接受 Cookie 头、JSON 对象或浏览器导出的 Cookie 数组。"""
    values = parse_cookie_header(header)
    payload = raw
    if isinstance(payload, str):
        text = payload.strip()
        if not text or text == "null":
            payload = None
        else:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                values.update(parse_cookie_header(text))
                payload = None

    if isinstance(payload, Mapping):
        for name, value in payload.items():
            if isinstance(value, str) and is_valid_cookie_name(name):
                values[str(name)] = value
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            if is_valid_cookie_name(name):
                values[name] = str(item.get("value") or "")

    return cookie_map_to_header(values)


def _is_shared_icloud_cookie(cookie: Any, region: str) -> bool:
    """只有 iCloud 各子域共享的根 Cookie 才能安全地更新扁平化的 Cookie 头。"""
    domain = str(getattr(cookie, "domain", "") or "").strip().lower().lstrip(".")
    return getattr(cookie, "path", "") == "/" and domain == cookie_domain_for(region)


def merge_response_cookies(header: str, cookies: Iterable[Any], region: str) -> tuple[str, int]:
    values = parse_cookie_header(header)
    now = time.time()
    for cookie in cookies:
        name = str(getattr(cookie, "name", "") or "")
        if not is_valid_cookie_name(name) or not _is_shared_icloud_cookie(cookie, region):
            continue
        expires = getattr(cookie, "expires", None)
        if expires is not None and expires <= now:
            values.pop(name, None)
            continue
        values[name] = _sanitize_value(_unquote(str(getattr(cookie, "value", "") or "")))
    return cookie_map_to_header(values)


def dsid_from_cookie_header(header: str) -> str:
    """从 X-APPLE-WEBAUTH-USER Cookie 中解出账号 DSID。"""
    for name, raw_value in parse_cookie_header(header).items():
        if name.upper() != "X-APPLE-WEBAUTH-USER":
            continue
        value = _unquote(unquote(raw_value).strip()).strip()
        marker = value.rfind(":d=")
        if marker >= 0:
            value = value[marker + 3 :]
        elif value.startswith("d="):
            value = value[2:]
        else:
            return ""
        value = value.split(":", 1)[0].strip().strip('"')
        return value if value.isdigit() else ""
    return ""
