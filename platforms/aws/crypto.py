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

    一台设备（一个注册流程）用一个实例：布局参数与 canvas 直方图**整个会话
    共享**，随机化发生在实例创建时。`update_app_js()` 可以从 `app.js` 里刷新
    XXTEA 密钥与 TES 版本 —— 取不到就沿用回退值（不阻塞注册）。
    """

    def __init__(self) -> None:
        self.chrome_version = str(random.randint(131, 144))  # noqa: S311 - 指纹随机化
        self.user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{self.chrome_version}.0.0.0 Safari/537.36"
        )
        self.sec_ch_ua = (
            f'"Not/A)Brand";v="24", "Chromium";v="{self.chrome_version}", '
            f'"Google Chrome";v="{self.chrome_version}"'
        )
        self.key = _FALLBACK_KEY
        self.identifier = IDENTIFIER
        self.version = FALLBACK_TES_VERSION
        self.visitor_seed = visitor_id()
        self._start_ms = int(time.time() * 1000) - random.randint(400, 900)
        self._canvas_hash = random.randint(5_000, 105_000)
        self._bins = self._build_bins()
        self._profile_ubid = ""

    @staticmethod
    def _build_bins() -> list[int]:
        """canvas 直方图：256 个桶，和为 36000（与参考实现同构）。"""
        bins = [0] * 256
        bins[0] = random.randint(9_000, 13_000)
        bins[255] = random.randint(11_000, 15_000)
        bins[102] = random.randint(500, 680)
        bins[153] = random.randint(400, 650)
        remaining = max(0, 36_000 - sum(bins))
        for index in range(1, 255):
            if bins[index] == 0:
                value = min(remaining, random.randint(2, 18))
                bins[index] = value
                remaining -= value
        bins[0] += remaining
        return bins

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

    def _performance(self, now_ms: int) -> dict[str, int]:
        load_end = now_ms - random.randint(500, 1_500)
        base = load_end - random.randint(2_000, 4_000)
        connect_end = base + random.randint(300, 600)
        response = connect_end + random.randint(200, 600)
        interactive = load_end - random.randint(5, 15)
        return {
            "connectStart": base + 5,
            "secureConnectionStart": base + 8,
            "unloadEventEnd": 0,
            "domainLookupStart": base + 3,
            "domainLookupEnd": base + 4,
            "responseStart": response,
            "connectEnd": connect_end,
            "responseEnd": response + 2,
            "requestStart": connect_end,
            "domLoading": response + 3,
            "redirectStart": 0,
            "loadEventEnd": load_end,
            "domComplete": load_end,
            "navigationStart": base,
            "loadEventStart": load_end,
            "domContentLoadedEventEnd": load_end,
            "unloadEventStart": 0,
            "redirectEnd": 0,
            "domInteractive": interactive,
            "fetchStart": base + 3,
            "domContentLoadedEventStart": interactive + 1,
        }

    @staticmethod
    def _interaction(event_type: str) -> dict[str, Any]:
        if event_type in {"PageLoad", "first_load", "EmailVerification"}:
            if event_type == "EmailVerification":
                keys = random.randint(4, 20)
                return {
                    "clicks": 1,
                    "touches": 0,
                    "keyPresses": keys,
                    "cuts": 0,
                    "copies": 0,
                    "pastes": 1,
                    "keyPressTimeIntervals": [random.randint(30, 900) for _ in range(max(1, keys // 3))],
                    "mouseClickPositions": ["120.5,20.5"],
                    "keyCycles": [random.randint(10, 700) for _ in range(max(2, keys // 2))],
                    "mouseCycles": [random.randint(20, 300)],
                    "touchCycles": [],
                }
            return {
                "clicks": 0,
                "touches": 0,
                "keyPresses": 0,
                "cuts": 0,
                "copies": 0,
                "pastes": 0,
                "keyPressTimeIntervals": [],
                "mouseClickPositions": [],
                "keyCycles": [],
                "mouseCycles": [],
                "touchCycles": [],
            }
        keys = random.randint(4, 20)
        return {
            "clicks": random.randint(1, 5),
            "touches": 0,
            "keyPresses": keys,
            "cuts": 0,
            "copies": 0,
            "pastes": 0,
            "keyPressTimeIntervals": [random.randint(30, 900) for _ in range(max(1, keys // 3))],
            "mouseClickPositions": [
                f"{random.randint(50, 1500)},{random.randint(50, 800)}" for _ in range(2)
            ],
            "keyCycles": [random.randint(10, 700) for _ in range(max(2, keys // 2))],
            "mouseCycles": [random.randint(20, 300) for _ in range(2)],
            "touchCycles": [],
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
    ) -> str:
        """生成一次事件的指纹信封。`email` 非空时带上表单校验字段。"""
        now_ms = int(time.time() * 1000)
        performance = self._performance(now_ms)
        if page_type == "profile" and not self._profile_ubid:
            self._profile_ubid = (
                f"X{random.choice((10, 19, 42, 55, 73, 81, 96))}-"
                f"{random.randrange(10**7):07d}-{random.randrange(10**7):07d}:"
                f"{performance['loadEventEnd'] // 1000}"
            )
        start = now_ms - time_on_page if time_on_page else self._start_ms
        dynamic = (
            [f"/dist/main/app_{random.getrandbits(64):x}"[:26] + ".min.js"]
            if page_type == "profile"
            else ["/assets/js/app.js"]
        )
        payload: dict[str, Any] = {
            "metrics": {
                name: (random.randint(0, 3) if name == "perf" else 0)
                for name in (
                    "el", "script", "h", "batt", "perf", "auto", "tz", "fp2", "lsubid",
                    "browser", "capabilities", "gpu", "dnt", "math", "tts", "input",
                    "canvas", "captchainput", "pow",
                )
            },
            "start": start,
            "interaction": self._interaction(event_type),
            "scripts": {
                "dynamicUrls": dynamic,
                "inlineHashes": [],
                "elapsed": 0,
                "dynamicUrlCount": len(dynamic),
                "inlineHashesCount": 0,
            },
            "history": {"length": 3 if page_type == "profile" else 1},
            "battery": {},
            "performance": {"timing": performance},
            "automation": {
                "wd": {"properties": {"document": [], "window": [], "navigator": []}},
                "phantom": {"properties": {"window": []}},
            },
            "end": now_ms + random.randint(0, 50),
            "timeZone": 8,
            "flashVersion": None,
            "plugins": "PDF Viewer Chrome PDF Viewer ||1920-1080-1032-24-*-*-*",
            "dupedPlugins": "PDF Viewer Chrome PDF Viewer ||1920-1080-1032-24-*-*-*",
            "screenInfo": "1920-1080-1032-24-*-*-*",
            "lsUbid": self._profile_ubid if page_type == "profile" else self.visitor_seed,
            "referrer": referrer,
            "userAgent": self.user_agent,
            "deviceMemory": 8,
            "hardwareConcurrency": 8,
            "platform": "Win32",
            "location": location,
            "webDriver": False,
            "capabilities": {
                "css": {
                    "textShadow": 1,
                    "WebkitTextStroke": 1,
                    "boxShadow": 1,
                    "borderRadius": 1,
                    "borderImage": 1,
                    "opacity": 1,
                    "transform": 1,
                    "transition": 1,
                },
                "js": {
                    "audio": True,
                    "geolocation": True,
                    "localStorage": "supported",
                    "touch": False,
                    "video": True,
                    "webWorker": True,
                },
                "elapsed": 0,
            },
            "gpu": {
                "vendor": "Google Inc. (Intel)",
                "model": (
                    "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
                ),
                "extensions": [],
            },
            "dnt": None,
            "math": {
                "tan": "-1.4214488238747245",
                "sin": "0.8178819121159085",
                "cos": "-0.5753861119575491",
            },
            "canvas": {
                "hash": self._canvas_hash,
                "emailHash": None,
                "histogramBins": list(self._bins),
            },
            "token": {
                "isCompatible": page_type in {"profile", "signup"},
                "pageHasCaptcha": 0,
            },
            "auth": {"form": {"method": "get"}},
            "errors": [],
            "version": self.version,
        }
        if page_type == "profile":
            payload["timeToSubmit"] = max(1, time_on_page or random.randint(1_000, 4_000))
        if email:
            checksum = f"{binascii.crc32(email.encode('utf-8')) & 0xFFFFFFFF:08X}"
            payload["form"] = {
                f"formField29-{now_ms}-{random.randint(1_000, 9_999)}": {
                    "clicks": 1,
                    "touches": 0,
                    "keyPresses": max(3, len(email) // 2),
                    "cuts": 0,
                    "copies": 0,
                    "pastes": 0,
                    "keyPressTimeIntervals": [80, 120],
                    "mouseClickPositions": ["120.5,20.5"],
                    "keyCycles": [100, 120],
                    "mouseCycles": [90],
                    "touchCycles": [],
                    "width": 180,
                    "height": 32,
                    "totalFocusTime": 0,
                    "checksum": checksum,
                    "autocomplete": False,
                    "prefilled": False,
                }
            }
        else:
            payload["form"] = {}
        plain = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        crc = binascii.crc32(plain.encode("utf-8")) & 0xFFFFFFFF
        sealed = xxtea_encrypt(f"{crc:08X}#{plain}", self.key)
        return f"{self.identifier}:{base64.b64encode(sealed).decode('ascii')}"


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