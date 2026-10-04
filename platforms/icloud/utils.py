"""iCloud 通用小工具。"""

from __future__ import annotations

import re
import uuid
from email.utils import getaddresses
from html.parser import HTMLParser

_EMAIL_PATTERN = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+$")
_SKIPPED_HTML_TAGS = {"head", "style", "script", "noscript", "template", "svg"}


def new_uuid() -> str:
    return str(uuid.uuid4())


def six_digit_code(value: str) -> bool:
    text = str(value or "").strip()
    return len(text) == 6 and text.isdigit()


def normalize_email_address(value: str) -> str:
    address = str(value or "").strip().strip("<>").strip().lower()
    return address if _EMAIL_PATTERN.match(address) else ""


def parse_address_list(value: str) -> list[tuple[str, str]]:
    """解析邮件头中的地址列表，返回 (显示名, 邮箱) 列表。"""
    parsed = []
    for name, address in getaddresses([str(value or "")]):
        normalized = normalize_email_address(address)
        if normalized:
            parsed.append((str(name or "").strip(), normalized))
    return parsed


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, _attrs) -> None:
        if tag.lower() in _SKIPPED_HTML_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in _SKIPPED_HTML_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    @property
    def text(self) -> str:
        return " ".join("".join(self._chunks).split())


def strip_html(value: str) -> str:
    extractor = _TextExtractor()
    try:
        extractor.feed(str(value or ""))
        extractor.close()
    except Exception:
        return ""
    return extractor.text


def truncate_text(value: str, maximum: int) -> str:
    collapsed = " ".join(str(value or "").split())
    if len(collapsed) <= maximum:
        return collapsed
    return collapsed[:maximum] + "..."
