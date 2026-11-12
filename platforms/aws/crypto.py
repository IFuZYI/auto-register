"""AWS Builder ID 注册用到的加密与身份值。

三块，都是**纯函数 + 可断言字节**的：

* `xxtea_encrypt` / `xxtea_decrypt` —— 指纹信封的载荷加密。AWS 的
  `signin.aws` 前端把 `"<CRC32>#<JSON>"` 用 XXTEA 加密后 base64 成
  `ECdITeCs:<base64>` 上交；密钥藏在 `app.js` 里，解不出来就换一个（见
  `FingerprintBuilder`）。
* `encrypt_password` —— 密码走 JWE compact（RSA-OAEP-256 + A256GCM），公钥
  由服务端在 `encryptionContextResponse.publicKey` 下发。密码**绝不能明文上行**。
* `awsccc` / `ubid` / `visitor_id` / `pkce` —— 服务端用来做设备指纹与风控的
  几个值，形态固定，测试直接钉住正则。

算法与常量都来自 `reference/register/kirox/internal/crypto/`（Go 原版）与
`reference/register/reg-factory/common/kiro_crypto.py`（Python 移植）。
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import random
import re
import time
import uuid
from typing import Any, Mapping

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .fingerprint_data import GPU_EXT, REAL_HIST

#: XXTEA 的魔数（与参考实现同值；不是随便选的常量）。
_DELTA = 0x9E3779B9

#: `app.js` 里取不到密钥时的回退值 —— 参考实现实测可用，且 app.js 结构变动时
#: 仍能跑一次（拿到 4xx 就说明该更新了，而不是整个流程起不来）。
_FALLBACK_KEY: tuple[int, int, int, int] = (1888420705, 2576816180, 2347232058, 874813317)

#: 指纹信封的前缀标识（服务端按它选解密实现）。
IDENTIFIER = "ECdITeCs"

#: TES 版本号回退值。
FALLBACK_TES_VERSION = "4.0.0"


def _u32(value: int) -> int:
    return value & 0xFFFFFFFF


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(str(text) + "=" * (-len(str(text)) % 4))


def xxtea_encrypt(text: str, key: tuple[int, int, int, int]) -> bytes:
    """XXTEA 加密（小端 32 位字、补零到 4 字节对齐）。"""
    raw = text.encode("utf-8")
    if not raw:
        return b""
    padded = raw + b"\0" * (-len(raw) % 4)
    values = [
        int.from_bytes(padded[offset : offset + 4], "little")
        for offset in range(0, len(padded), 4)
    ]
    count = len(values)
    rounds = 6 + 52 // count
    total = 0
    z = values[-1]
    for _ in range(rounds):
        total = _u32(total + _DELTA)
        shift = (total >> 2) & 3
        for index in range(count):
            y = values[(index + 1) % count]
            mix = (
                ((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4))
                ^ ((total ^ y) + (key[(index & 3) ^ shift] ^ z))
            )
            values[index] = _u32(values[index] + mix)
            z = values[index]
    return b"".join(value.to_bytes(4, "little") for value in values)


def xxtea_decrypt(data: bytes, key: tuple[int, int, int, int]) -> str:
    """XXTEA 解密（与 `xxtea_encrypt` 对称，去掉尾部补零）。"""
    count = len(data) // 4
    if count < 2:
        return ""
    values = [
        int.from_bytes(data[offset : offset + 4], "little")
        for offset in range(0, count * 4, 4)
    ]
    total = _u32((6 + 52 // count) * _DELTA)
    y = values[0]
    for _ in range(6 + 52 // count):
        shift = (total >> 2) & 3
        for index in range(count - 1, -1, -1):
            z = values[(index - 1) % count]
            mix = (
                ((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4))
                ^ ((total ^ y) + (key[(index & 3) ^ shift] ^ z))
            )
            values[index] = _u32(values[index] - mix)
            y = values[index]
        total = _u32(total - _DELTA)
    joined = b"".join(value.to_bytes(4, "little") for value in values)
    return joined.rstrip(b"\0").decode("utf-8", "replace")


def encrypt_password(
    password: str,
    public_jwk: Mapping[str, Any],
    *,
    issuer: str = "signin",
    audience: str = "AWSPasswordService",
    region: str = "us-east-1",
    ttl_seconds: int = 300,
) -> str:
    """把密码包成 AWS 的 JWE compact 串（RSA-OAEP-256 + A256GCM）。

    形状（五段，`.` 分隔）：`header.encrypted_key.iv.ciphertext.tag`。
    AAD 是 header 的 base64url 串 —— 少给或给错，服务端解不开且不会说明原因。
    """
    numbers_n = int.from_bytes(_b64url_decode(str(public_jwk["n"])), "big")
    numbers_e = int.from_bytes(_b64url_decode(str(public_jwk["e"])), "big")
    public_key = rsa.RSAPublicNumbers(numbers_e, numbers_n).public_key()

    header = {
        "alg": "RSA-OAEP-256",
        "kid": str(public_jwk.get("kid") or ""),
        "enc": "A256GCM",
        "cty": "enc",
        "typ": "application/aws+signin+jwe",
    }
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))

    content_key = os.urandom(32)
    encrypted_key = public_key.encrypt(
        content_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

    now = int(time.time())
    claims = {
        "iss": f"{region}.{issuer}",
        "iat": now,
        "nbf": now,
        "jti": str(uuid.uuid4()),
        "exp": now + int(ttl_seconds),
        "aud": f"{region}.{audience}",
        "password": password,
    }
    plaintext = json.dumps(claims, separators=(",", ":")).encode("utf-8")
    iv = os.urandom(12)
    sealed = AESGCM(content_key).encrypt(iv, plaintext, header_b64.encode("ascii"))
    ciphertext, tag = sealed[:-16], sealed[-16:]
    return ".".join(
        (
            header_b64,
            _b64url(encrypted_key),
            _b64url(iv),
            _b64url(ciphertext),
            _b64url(tag),
        )
    )


def awsccc() -> str:
    """`awsccc` cookie 的值（base64 的标准 JSON，不含 URL-safe 替换）。"""
    payload = {
        "e": 1,
        "p": 1,
        "f": 1,
        "a": 1,
        "i": str(uuid.uuid4()),
        "v": "1",
    }
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def ubid() -> str:
    """`aws-user-profile-ubid` cookie 的值：`186-<7 位>-<6 位>`。"""
    return f"186-{random.randrange(10**7):07d}-{random.randrange(10**6):06d}"


def visitor_id() -> str:
    """`visitorId`：UUID 形态，第三段固定 `7` 开头（参考实现的拼接规则）。"""
    hex4 = lambda: f"{random.getrandbits(16):04x}"  # noqa: E731 - 与参考实现同构
    return (
        f"{hex4()}{hex4()}-{hex4()}-7{hex4()[1:]}-{hex4()}-"
        f"{hex4()}{hex4()}{hex4()}"
    )


def pkce() -> tuple[str, str]:
    """OAuth PKCE：返回 `(code_verifier, code_challenge)`（S256）。"""
    verifier = _b64url(os.urandom(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


_APP_JS_KEY_RE = re.compile(
    r"var\s+\w+\s*=\s*\[(\d+),\s*[\"']([A-Za-z0-9]+)[\"'],\s*(\d+),\s*(\d+),\s*(\d+)\]"
)
_APP_JS_VERSION_RE = re.compile(r"FWCIM_VERSION\s*=\s*[\"'](\d+\.\d+\.\d+)[\"']")


class FingerprintBuilder:
    """生成 AWS 的加密指纹信封（`ECdITeCs:<base64>`）。

    形状照抄参考实现 `gen_fwcim`（HAR 实测）：macOS Chrome 131、Apple M4、
    真实 canvas 直方图、`lsUbid = X<ubid>:<epoch>`。设备/浏览器身份是**固定**的，
    随机化的只有时间戳、canvas hash、history 长度 —— 之前那版把 UA 随机成
    Windows Chrome 131~144、GPU 写 Intel、扩展表留空，UA 与 GPU/平台互相矛盾，
    是典型机器人特征（AWS 直接回 BLOCKED）。
    """

    def __init__(self) -> None:
        self.chrome_version = "131"
        self.user_agent = (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        self.sec_ch_ua = '"Chromium";v="131", "Not_A Brand";v="24"'
        self.sec_ch_ua_platform = '"macOS"'
        self.key = _FALLBACK_KEY
        self.identifier = IDENTIFIER
        self.version = FALLBACK_TES_VERSION
        self.canvas_hash = random.randint(1_000_000_000, 2_147_483_647)
        self.platform_ubid = ubid()

    def update_app_js(self, text: str) -> None:
        """从 `signin.aws/assets/js/app.js` 刷新 XXTEA 密钥与 TES 版本。"""
        match = _APP_JS_KEY_RE.search(text or "")
        if match:
            first, _identifier, third, fourth, fifth = match.groups()
            self.key = (int(third), int(first), int(fourth), int(fifth))
            self.identifier = match.group(2)
        version = _APP_JS_VERSION_RE.search(text or "")
        if version:
            self.version = version.group(1)

    @staticmethod
    def _performance(nav_start: int) -> dict[str, int]:
        """参考实现的 `_gen_perf`：导航/资源时序（相对 navigationStart）。"""
        dns = nav_start + random.randint(30, 40)
        connect_end = dns + random.randint(1, 3)
        request = connect_end + random.randint(0, 2)
        response_start = request + random.randint(250, 350)
        dom_load = response_start + random.randint(2, 5)
        dom_interactive = dom_load + random.randint(2_800, 3_500)
        dom_complete = dom_interactive + random.randint(800, 1_200)
        return {
            "connectStart": dns,
            "secureConnectionStart": dns,
            "unloadEventEnd": 0,
            "domainLookupStart": dns,
            "domainLookupEnd": dns,
            "responseStart": response_start,
            "connectEnd": connect_end,
            "responseEnd": response_start,
            "requestStart": request,
            "domLoading": dom_load,
            "redirectStart": 0,
            "loadEventEnd": dom_complete,
            "domComplete": dom_complete,
            "navigationStart": nav_start,
            "loadEventStart": dom_complete,
            "domContentLoadedEventEnd": dom_interactive,
            "unloadEventStart": 0,
            "redirectEnd": 0,
            "domInteractive": dom_interactive,
            "fetchStart": dns,
            "domContentLoadedEventStart": dom_interactive,
        }

    def fingerprint(
        self,
        *,
        page_type: str,
        event_type: str,
        location: str,
        referrer: str,
        time_on_page: int = 0,
        email: str = "",
        ubid_main: str = "",
    ) -> str:
        """生成一次事件的指纹信封（形状与 `gen_fwcim` 逐字段一致）。"""
        now_ms = int(time.time() * 1000)
        nav_start = now_ms - random.randint(3_500, 5_000)
        ubid = str(ubid_main or self.platform_ubid)
        payload = {
            "metrics": {
                "el": 0, "script": 0, "h": 0, "batt": 0, "perf": 0, "auto": 0,
                "tz": 0, "fp2": 0, "lsubid": 1, "browser": 0, "capabilities": 0,
                "gpu": 0, "dnt": 0, "math": 0, "tts": 0, "input": 0, "canvas": 0,
                "captchainput": 0, "pow": 0,
            },
            "start": now_ms - random.randint(20, 60),
            "interaction": {
                "clicks": 0, "touches": 0, "keyPresses": 0, "cuts": 0,
                "copies": 0, "pastes": 0, "keyPressTimeIntervals": [],
                "mouseClickPositions": [], "keyCycles": [], "mouseCycles": [],
                "touchCycles": [],
            },
            "scripts": {
                "dynamicUrls": ["/assets/js/app.js"], "inlineHashes": [],
                "elapsed": 0, "dynamicUrlCount": 1, "inlineHashesCount": 0,
            },
            "history": {"length": random.randint(2, 8)},
            "battery": {},
            "performance": {"timing": self._performance(nav_start)},
            "automation": {
                "wd": {"properties": {"document": [], "window": [], "navigator": []}},
                "phantom": {"properties": {"window": []}},
            },
            "end": now_ms,
            "timeZone": 8,
            "flashVersion": None,
            "plugins": (
                "PDF Viewer Chrome PDF Viewer Chromium PDF Viewer "
                "Microsoft Edge PDF Viewer WebKit built-in PDF "
                "||1440-900-900-30-*-*-*"
            ),
            "dupedPlugins": (
                "PDF Viewer Chrome PDF Viewer Chromium PDF Viewer "
                "Microsoft Edge PDF Viewer WebKit built-in PDF "
                "||1440-900-900-30-*-*-*"
            ),
            "screenInfo": "1440-900-900-30-*-*-*",
            "lsUbid": f"X{ubid}:{now_ms // 1000}",
            "referrer": "https://view.awsapps.com/",
            "userAgent": self.user_agent,
            "location": location,
            "webDriver": False,
            "capabilities": {
                "css": {
                    "textShadow": 1, "WebkitTextStroke": 1, "boxShadow": 1,
                    "borderRadius": 1, "borderImage": 1, "opacity": 1,
                    "transform": 1, "transition": 1,
                },
                "js": {
                    "audio": True, "geolocation": True, "localStorage": "supported",
                    "touch": False, "video": True, "webWorker": True,
                },
                "elapsed": 0,
            },
            "gpu": {
                "vendor": "Google Inc. (Apple)",
                "model": "ANGLE (Apple, ANGLE Metal Renderer: Apple M4, Unspecified Version)",
                "extensions": list(GPU_EXT),
            },
            "dnt": None,
            "math": {
                "tan": "-1.4214488238747245",
                "sin": "0.8178819121159085",
                "cos": "-0.5753861119575491",
            },
            "form": {},
            "canvas": {
                "hash": self.canvas_hash,
                "emailHash": None,
                "histogramBins": list(REAL_HIST),
            },
            "token": {"isCompatible": False, "pageHasCaptcha": 0},
            "auth": {"form": {"method": "get"}},
            "errors": [],
            "version": self.version,
        }
        plain = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        crc = f"{binascii.crc32(plain.encode('utf-8')) & 0xFFFFFFFF:08X}"
        sealed = xxtea_encrypt(f"{crc}#{plain}", self.key)
        # 参考实现去掉了 base64 的 `=` 填充
        encoded = base64.b64encode(sealed).decode("ascii").rstrip("=")
        return f"{self.identifier}:{encoded}"


__all__ = [
    "FALLBACK_TES_VERSION",
    "FingerprintBuilder",
    "IDENTIFIER",
    "awsccc",
    "encrypt_password",
    "pkce",
    "ubid",
    "visitor_id",
    "xxtea_decrypt",
    "xxtea_encrypt",
]