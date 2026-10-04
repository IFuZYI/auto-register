"""隐私邮箱投递归类。

Apple 把发往 Hide My Email 地址的邮件转发到主号收件箱，原始收件人保留在
投递头里。归类时按“已知别名 → 投递头 → 可见收件人”的优先级取地址。
"""

from __future__ import annotations

from typing import Iterable, Mapping

from .constants import DELIVERY_HEADER_NAMES
from .utils import normalize_email_address, parse_address_list


def _addresses_in(value: str) -> list[str]:
    parsed = [address for _name, address in parse_address_list(value)]
    if parsed:
        return parsed
    # 部分投递头使用 `rfc822;user@example.com` 形式，标准解析器无法识别。
    addresses = []
    for candidate in str(value or "").split(","):
        _, _, tail = candidate.rpartition(";")
        normalized = normalize_email_address(tail or candidate)
        if normalized:
            addresses.append(normalized)
    return addresses


def header_value(headers: Mapping[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return ""


def delivery_addresses(
    *,
    alias_address: str = "",
    headers: Mapping[str, str] | None = None,
    to: Iterable[str] = (),
) -> list[str]:
    headers = headers or {}
    ordered: list[str] = []
    sources = [alias_address, *(header_value(headers, name) for name in DELIVERY_HEADER_NAMES), *to]
    for source in sources:
        for address in _addresses_in(source):
            if address not in ordered:
                ordered.append(address)
    return ordered


def delivered_to(candidates: Iterable[str], recipient: str) -> bool:
    wanted = _addresses_in(recipient)
    if not wanted:
        return False
    candidate_set = {address.lower() for address in candidates}
    return any(address.lower() in candidate_set for address in wanted)
