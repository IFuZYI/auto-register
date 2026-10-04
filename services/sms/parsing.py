"""sms 接码服务的纯函数：展示标签、兜底类型转换、状态文本解析。

无运行时状态，只依赖标准库与 constants 里的表。
"""
from __future__ import annotations

import hashlib
from typing import Optional

from services.sms.constants import SMS_COUNTRY_NAMES_CN, SMS_STATUS_CANCELED  # noqa: F401


def country_label(country_id) -> str:
    """返回 ``52 泰国`` 这样的展示标签。"""
    cid = str(country_id or "").strip()
    return f"{cid} {SMS_COUNTRY_NAMES_CN.get(cid, '')}".strip()


def _hash_secret(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_int(value, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _safe_float(value, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return default


def _safe_bool(value, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "否"}


def _status_token(text) -> str:
    """平台的纯文本状态本身就是一行短 token（``BAD_KEY`` 这种）。

    长得不像 token 的都是错误页正文之类的响应体，日志里不需要它们 —— 丢掉。
    """
    if isinstance(text, dict):
        text = text.get("message") or text.get("code") or ""
    if not isinstance(text, str):
        return ""
    token = " ".join(text.split())
    if not token or len(token) > 60 or "<" in token:
        return ""
    return token


def _parse_sms_status_text(text: str) -> dict:
    text = str(text or "").strip()
    if text == "STATUS_WAIT_CODE":
        return {"status": "wait_code"}
    if text.startswith("STATUS_WAIT_RETRY"):
        return {"status": "wait_retry", "raw": text}
    if text == "STATUS_WAIT_RESEND":
        return {"status": "wait_resend"}
    if text.startswith("STATUS_OK:"):
        return {"status": "ok", "code": text.split(":", 1)[1]}
    if text == "STATUS_CANCEL":
        return {"status": "cancel"}
    return {"status": "unknown", "raw": _status_token(text)}


def _make_sms_candidate(activation_id: str, source: str, code) -> Optional[dict]:
    code = str(code or "").strip()
    if not code or code in {"null", "None"}:
        return None
    return {
        "status": "ok",
        "code": code,
        "source": source,
        "sms_key": hashlib.sha256(f"{activation_id}:{code}".encode("utf-8")).hexdigest(),
    }
