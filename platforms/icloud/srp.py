"""Apple ID 使用的 SRP-6a 客户端。

采用 RFC 5054 的 2048 位群，并遵循 Apple 的 NoUsernameInX 变体：
计算私钥 x 时用空用户名，但保留 `用户名:密码` 的分隔符。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from .errors import invalid_response

_GROUP_WIDTH = 256
_MAX_ITERATIONS = 1_000_000
_MODULUS_HEX = (
    "AC6BDB41324A9A9BF166DE5E1389582FAF72B6651987EE07FC3192943DB56050A37329CBB4"
    "A099ED8193E0757767A13DD52312AB4B03310DCD7F48A9DA04FD50E8083969EDB767B0CF60"
    "95179A163AB3661A05FBD5FAAAE82918A9962F0B93B855F97993EC975EEAA80D740ADBF4FF"
    "747359D041D5C33EA71D281E446B14773BCA97B43A23FB801676BD207A436C6481F1D2B907"
    "8717461A5B9D32E688F87748544523B524B0D57D5EA77A2775D2ECFA032CFBDBF52FB37861"
    "60279004E57AE6AF874E7303CE53299CCC041C7BC308D82A5698F3A8D0C38271AE35F8E9DB"
    "FBB694B5C803D89F7AE435DE236D525F54759B65E372FCD68EF20FA7111F9E4AFF73"
)

MODULUS = int(_MODULUS_HEX, 16)
GENERATOR = 2

SUPPORTED_PROTOCOLS = ("s2k", "s2k_fo")


def _hash(*parts: bytes) -> bytes:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part)
    return digest.digest()


def _hash_int(*parts: bytes) -> int:
    return int.from_bytes(_hash(*parts), "big")


def _to_bytes(value: int) -> bytes:
    """Apple 期望整数使用最小长度的大端编码。"""
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _pad(value: int) -> bytes:
    encoded = _to_bytes(value)
    if len(encoded) > _GROUP_WIDTH:
        raise invalid_response("SRP 整数超出群宽度")
    return encoded.rjust(_GROUP_WIDTH, b"\x00")


def derive_password(password: str, salt: bytes, iterations: int, protocol: str) -> bytes:
    """按 Apple 声明的 s2k 协议派生 SRP 口令。"""
    if iterations <= 0 or iterations > _MAX_ITERATIONS:
        raise invalid_response(f"SRP 迭代次数无效: {iterations}")
    if protocol not in SUPPORTED_PROTOCOLS:
        raise invalid_response(f"Apple 登录返回了不支持的验证协议: {protocol}")

    digest = hashlib.sha256(password.encode("utf-8")).digest()
    material = digest if protocol == "s2k" else digest.hex().encode("ascii")
    return hashlib.pbkdf2_hmac("sha256", material, salt, iterations, dklen=32)


@dataclass
class SRPProofs:
    client_proof: bytes
    server_proof: bytes


class AppleSRPClient:
    """一次性 SRP 握手状态。"""

    def __init__(self, private_value: int | None = None) -> None:
        """`private_value` 仅用于复现固定向量，正常握手应让其随机生成。"""
        if private_value is None:
            # pysrp 会把首字节最高位置 1，使私钥恒为 256 位。
            private_bytes = bytearray(secrets.token_bytes(32))
            private_bytes[0] |= 0x80
            private_value = int.from_bytes(private_bytes, "big")
        self._private_value = private_value
        self._public_value = pow(GENERATOR, self._private_value, MODULUS)

    @property
    def public_bytes(self) -> bytes:
        return _to_bytes(self._public_value)

    def process_challenge(
        self,
        username: bytes,
        derived_key: bytes,
        salt: bytes,
        server_public: bytes,
    ) -> SRPProofs:
        if not derived_key:
            raise invalid_response("SRP 派生口令为空")

        server_value = int.from_bytes(server_public, "big")
        if server_value == 0 or server_value >= MODULUS:
            raise invalid_response("Apple 返回的 SRP 公开值无效")

        padded_g = _pad(GENERATOR)
        multiplier = _hash_int(_pad(MODULUS), padded_g)
        scrambler = _hash_int(_pad(self._public_value), _pad(server_value))
        if scrambler == 0:
            raise invalid_response("SRP 混淆参数无效")

        private_key = _hash_int(salt, _hash(b":" + derived_key))
        verifier = pow(GENERATOR, private_key, MODULUS)
        base = (server_value - multiplier * verifier) % MODULUS
        if base == 0:
            raise invalid_response("SRP 共享密钥基数无效")

        shared_secret = pow(base, scrambler * private_key + self._private_value, MODULUS)
        if shared_secret == 0:
            raise invalid_response("SRP 共享密钥无效")
        session_key = _hash(_to_bytes(shared_secret))

        group_hash = bytes(
            left ^ right for left, right in zip(_hash(_to_bytes(MODULUS)), _hash(padded_g))
        )
        client_proof = _hash(
            group_hash,
            _hash(username),
            salt,
            self.public_bytes,
            _to_bytes(server_value),
            session_key,
        )
        server_proof = _hash(self.public_bytes, client_proof, session_key)
        return SRPProofs(client_proof=client_proof, server_proof=server_proof)


