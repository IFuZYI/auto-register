"""账号凭据的 AES-256-GCM 信封加密。

密钥优先取环境变量 `CREDENTIAL_ENCRYPTION_KEY`（base64 或 hex 编码的 32 字节），
否则在数据目录下生成并复用一份仅属主可读的密钥文件。
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from core.paths import CREDENTIAL_KEY_FILE

KEY_ENV_VAR = "CREDENTIAL_ENCRYPTION_KEY"
KEY_FILE_ENV_VAR = "CREDENTIAL_ENCRYPTION_KEY_FILE"
DEFAULT_KEY_FILE = CREDENTIAL_KEY_FILE

_KEY_BYTES = 32
_NONCE_BYTES = 12
_ENVELOPE_PREFIX = "v1:"


class CredentialKeyError(RuntimeError):
    """密钥缺失或格式非法。"""


def _decode_key(material: str) -> bytes:
    text = str(material or "").strip()
    if not text:
        raise CredentialKeyError("凭据加密密钥为空")
    for decode in (base64.b64decode, binascii.unhexlify):
        try:
            key = decode(text)
        except (binascii.Error, ValueError):
            continue
        if len(key) == _KEY_BYTES:
            return key
    raise CredentialKeyError(f"凭据加密密钥必须是 base64 或 hex 编码的 {_KEY_BYTES} 字节")


def _key_file() -> Path:
    configured = os.getenv(KEY_FILE_ENV_VAR, "").strip()
    if not configured:
        return DEFAULT_KEY_FILE
    path = Path(configured).expanduser()
    # 显式配了相对路径也按仓库根解析：密钥落到哪完全取决于启动时的工作目录，
    # 会出现"重启后换了把密钥、旧凭据全部解不开"这种最难查的故障。
    if path.is_absolute():
        return path
    from core.paths import project_root

    return project_root() / path


def key_file_path() -> Path:
    """密钥文件的**实际**路径（含 `CREDENTIAL_ENCRYPTION_KEY_FILE` 覆盖）。

    数据导出/导入用这个而不是 `core.paths.CREDENTIAL_KEY_FILE` 常量 ——
    自定义部署把密钥指到别处时，常量路径会导错/漏导文件，
    导入端拿不到密钥，症状是「数据都在但凭据全用不了」。
    """
    return _key_file()


def _load_or_create_key() -> bytes:
    material = os.getenv(KEY_ENV_VAR, "").strip()
    if material:
        return _decode_key(material)

    path = _key_file()
    if path.exists():
        return _decode_key(path.read_text(encoding="utf-8"))

    key = secrets.token_bytes(_KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先以 0600 创建再写入，避免密钥短暂地对其他用户可读。
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(base64.b64encode(key).decode())
    return key


class SecretBox:
    """惰性初始化密钥的 AES-256-GCM 加解密器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cipher: AESGCM | None = None

    def _aead(self) -> AESGCM:
        if self._cipher is None:
            with self._lock:
                if self._cipher is None:
                    self._cipher = AESGCM(_load_or_create_key())
        return self._cipher

    def encrypt(self, plaintext: bytes) -> str:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        sealed = self._aead().encrypt(nonce, plaintext, None)
        return _ENVELOPE_PREFIX + base64.b64encode(nonce + sealed).decode()

    def decrypt(self, envelope: str) -> bytes:
        text = str(envelope or "").strip()
        if not text.startswith(_ENVELOPE_PREFIX):
            raise CredentialKeyError("凭据密文格式无法识别")
        raw = base64.b64decode(text[len(_ENVELOPE_PREFIX) :])
        if len(raw) <= _NONCE_BYTES:
            raise CredentialKeyError("凭据密文长度不足")
        return self._aead().decrypt(raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], None)

    def encrypt_json(self, payload: Any) -> str:
        return self.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def decrypt_json(self, envelope: str) -> Any:
        if not str(envelope or "").strip():
            return {}
        return json.loads(self.decrypt(envelope).decode("utf-8"))

    def reset(self) -> None:
        """丢弃缓存的密钥，下次加解密时重新加载。

        用于**密钥文件被替换**的场景（数据导入会连密钥一起恢复）——
        不重置的话缓存里还是旧密钥，新导入的密文全部解不开，
        症状是「导入成功但 iCloud 凭据全用不了」。
        """
        with self._lock:
            self._cipher = None


secret_box = SecretBox()
