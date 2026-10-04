"""RFC 6238 动态口令。

绑 2FA 和登录撞上 mfa-challenge 都要算这个码，两边共用一份实现，免得哪天
改了容差窗口只改到一半。手写而不是拉 ``pyotp``：算法一共二十行，为它多一个
运行时依赖不划算，而且这条链路在 Docker 镜像里也要能跑。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import time
from urllib.parse import quote

TOTP_PERIOD = 30
TOTP_DIGITS = 6


def normalize_secret(secret: str) -> str:
    """把展示用的密钥还原成能 b32decode 的样子（去空格、补大写）。"""
    return "".join(str(secret or "").split()).upper()


def hotp(secret_b32: str, counter: int, digits: int = TOTP_DIGITS) -> str:
    """HOTP（RFC 4226）。"""
    normalized = normalize_secret(secret_b32)
    key = base64.b32decode(normalized + "=" * (-len(normalized) % 8))
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp_now(secret_b32: str, *, at: float | None = None) -> str:
    """当前 30 秒窗口的 6 位码。"""
    moment = time.time() if at is None else at
    return hotp(secret_b32, int(moment) // TOTP_PERIOD)


def verify_totp(secret_b32: str, code: str, *, at: float | None = None) -> bool:
    """前后各放一个窗口的本地自检，激活前用来确认密钥没抄错。"""
    moment = time.time() if at is None else at
    counter = int(moment) // TOTP_PERIOD
    return str(code or "") in {hotp(secret_b32, counter + delta) for delta in (-1, 0, 1)}


def otpauth_uri(secret_b32: str, account: str, issuer: str = "ChatGPT") -> str:
    """验证器 App 认的 otpauth:// 链接，前端可以直接渲染成二维码。"""
    normalized = normalize_secret(secret_b32)
    if not normalized:
        return ""
    label = quote(f"{issuer}:{account}" if account else issuer, safe="")
    query = f"secret={normalized}&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits={TOTP_DIGITS}&period={TOTP_PERIOD}"
    return f"otpauth://totp/{label}?{query}"
