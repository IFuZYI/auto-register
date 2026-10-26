"""Authentication API: password login, JWT session, TOTP 2FA."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json as _json
import os
import secrets
import struct
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

router = APIRouter(prefix="/auth", tags=["auth"])

_bearer = HTTPBearer(auto_error=False)

# 只有"面板登录态失效"才带这个头。业务接口同样会用 401（例如 iCloud 主号的
# Apple 密码错误），前端靠这个头区分，避免把用户踢回登录页。
PANEL_AUTH_HEADERS = {"X-Panel-Auth-Required": "1"}


# ── Config helpers ─────────────────────────────────────────────────────────────

def _cfg():
    from core.config_store import config_store
    return config_store


# ── JWT (HS256, stdlib only) ───────────────────────────────────────────────────

def _jwt_secret() -> str:
    env_secret = os.getenv("APP_JWT_SECRET", "")
    if env_secret:
        return env_secret
    stored = _cfg().get("auth_jwt_secret", "")
    if not stored:
        stored = secrets.token_hex(32)
        _cfg().set("auth_jwt_secret", stored)
    return stored


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.urlsafe_b64decode(s)


def create_token(expire_seconds: int = 86400 * 7) -> str:
    header = _b64url_encode(_json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = _b64url_encode(_json.dumps({
        "sub": "admin",
        "exp": int(time.time()) + expire_seconds,
        "iat": int(time.time()),
    }).encode())
    sig = _b64url_encode(
        hmac.new(_jwt_secret().encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )
    return f"{header}.{payload}.{sig}"


def verify_token(token: str) -> dict:
    try:
        header, payload, sig = token.split(".")
    except ValueError:
        raise HTTPException(status_code=401, detail="无效的令牌", headers=PANEL_AUTH_HEADERS)
    expected = _b64url_encode(
        hmac.new(_jwt_secret().encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    )
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(status_code=401, detail="令牌签名无效", headers=PANEL_AUTH_HEADERS)
    try:
        data = _json.loads(_b64url_decode(payload))
    except Exception:
        raise HTTPException(status_code=401, detail="令牌格式错误", headers=PANEL_AUTH_HEADERS)
    if data.get("exp", 0) < time.time():
        raise HTTPException(
            status_code=401, detail="令牌已过期，请重新登录", headers=PANEL_AUTH_HEADERS
        )
    return data


def require_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> None:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未认证", headers=PANEL_AUTH_HEADERS)
    verify_token(credentials.credentials)


# ── Password ───────────────────────────────────────────────────────────────────

#: 慢 KDF 参数。迭代次数按 2023 年 OWASP 建议取 600k（PBKDF2-HMAC-SHA256）。
#: 登录是低频操作，几十毫秒的代价换取离线爆破成本提高约 4 个数量级。
_PBKDF2_ITERATIONS = 600_000
_PBKDF2_PREFIX = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    """加盐慢哈希（PBKDF2-HMAC-SHA256）。

    此前是**单轮无盐 SHA-256**（安全审查 F5a）：面板密码是唯一保护整个凭据库
    的东西，而裸 SHA-256 在 GPU 上是十亿次/秒量级 —— 库或备份一旦泄漏，密码
    当场还原，加密的凭据全部可解。
    """
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS
    )
    return (
        f"{_PBKDF2_PREFIX}${_PBKDF2_ITERATIONS}$"
        f"{base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"
    )


def needs_rehash(stored: str) -> bool:
    """这个哈希是否该升级（老格式或迭代次数偏低）。"""
    text = str(stored or "")
    if not text.startswith(f"{_PBKDF2_PREFIX}$"):
        return True  # 旧的无盐 SHA-256
    try:
        iterations = int(text.split("$", 2)[1])
    except (IndexError, ValueError):
        return True
    return iterations < _PBKDF2_ITERATIONS


def verify_password(password: str, stored: str) -> bool:
    """校验密码，**兼容旧的裸 SHA-256 哈希**。

    兼容是必须的：不认旧格式就等于把已有用户锁在门外，且他们唯一能自救的
    途径（改密码）也要先登录。旧哈希验证通过后由调用方用 `needs_rehash`
    触发透明升级。
    """
    text = str(stored or "")
    if not text:
        return False
    if not text.startswith(f"{_PBKDF2_PREFIX}$"):
        # 旧格式：单轮 SHA-256（保持可比对，但绝不新写）
        legacy = hashlib.sha256(password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(legacy, text)
    try:
        _, raw_iterations, raw_salt, raw_digest = text.split("$", 3)
        expected = base64.b64decode(raw_digest)
        salt = base64.b64decode(raw_salt)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(raw_iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _hash_pw(password: str) -> str:
    """兼容旧调用点：写新格式用 `hash_password`。"""
    return hash_password(password)


# ── TOTP (RFC 6238, stdlib only) ───────────────────────────────────────────────

def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def totp_uri(secret: str, issuer: str = "AccountManager") -> str:
    from urllib.parse import quote
    return f"otpauth://totp/{quote(issuer)}?secret={secret}&issuer={quote(issuer)}"


def _totp_at(secret: str, counter: int) -> str:
    key = base64.b32decode(secret.upper())
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(code % 1_000_000).zfill(6)


def verify_totp(secret: str, code: str) -> bool:
    counter = int(time.time()) // 30
    user_code = str(code).strip().zfill(6)
    for delta in (-1, 0, 1):
        if hmac.compare_digest(_totp_at(secret, counter + delta), user_code):
            return True
    return False


# ── Pending 2FA sessions (in-memory) ──────────────────────────────────────────

_pending_2fa: dict[str, float] = {}  # temp_token -> expires_at


def _prune_pending_2fa() -> None:
    """清掉过期的临时令牌。

    此前只在**验证成功**时 `pop` —— 每次密码登录都加一条、过期的从不清理，
    长期运行下无界增长（安全审查 F11）。这里在签发与校验前都清一次。
    """
    now = time.time()
    for token in [t for t, exp in _pending_2fa.items() if exp <= now]:
        _pending_2fa.pop(token, None)


# ── 登录失败退避（防爆破）─────────────────────────────────────────────────────

#: 客户端标识（IP）→ 连续失败次数与最后一次失败时刻
_login_failures: dict[str, tuple[int, float]] = {}
#: 连续失败到这个次数后开始退避
_LOGIN_FAILURE_THRESHOLD = 5
#: 退避时长（秒）；每次退避按失败次数线性增长，上限 5 分钟
_LOGIN_BACKOFF_BASE = 5.0
_LOGIN_BACKOFF_MAX = 300.0


def _record_login_failure(client: str) -> None:
    count, _last = _login_failures.get(str(client), (0, 0.0))
    _login_failures[str(client)] = (count + 1, time.time())


def _clear_login_failures(client: str) -> None:
    _login_failures.pop(str(client), None)


def _login_retry_after(client: str) -> float:
    """还要等多少秒才能再试；0 = 现在可以试。"""
    count, last = _login_failures.get(str(client), (0, 0.0))
    if count < _LOGIN_FAILURE_THRESHOLD:
        return 0.0
    penalty = min(
        _LOGIN_BACKOFF_BASE * (count - _LOGIN_FAILURE_THRESHOLD + 1),
        _LOGIN_BACKOFF_MAX,
    )
    remaining = penalty - (time.time() - last)
    return max(0.0, remaining)


def _reset_login_failures() -> None:
    """测试用：清空失败计数。"""
    _login_failures.clear()
    _pending_2fa_attempts.clear()


#: 临时令牌 → 已失败的 TOTP 次数（超过上限即作废该令牌）
_pending_2fa_attempts: dict[str, int] = {}
#: TOTP 允许的连续错误次数
_TOTP_MAX_ATTEMPTS = 5


def _client_key(request: Request) -> str:
    """退避计数用的客户端标识。

    优先取反向代理传来的真实 IP —— 面板通常挂在 nginx/caddy 后面，直接读
    `request.client.host` 会让所有请求都算到代理那一个 IP 上，退避会误伤
    全部用户。
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real = request.headers.get("x-real-ip", "").strip()
    if real:
        return real
    return request.client.host if request.client else "unknown"


# ── Schemas ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


class TotpVerifyRequest(BaseModel):
    temp_token: str
    code: str


class SetupPasswordRequest(BaseModel):
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class EnableTotpRequest(BaseModel):
    secret: str
    code: str


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/status")
def auth_status():
    cfg = _cfg()
    return {
        "has_password": bool(cfg.get("auth_password_hash", "")),
        "has_totp": bool(cfg.get("auth_totp_secret", "")),
    }


@router.post("/setup")
def setup_password(
    body: SetupPasswordRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
):
    """Set initial password, or update it only when the caller is already authenticated."""
    if not body.password or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少需要 6 位")
    cfg = _cfg()
    if cfg.get("auth_password_hash", ""):
        if credentials is None:
            raise HTTPException(status_code=401, detail="未认证", headers=PANEL_AUTH_HEADERS)
        verify_token(credentials.credentials)
    cfg.set("auth_password_hash", _hash_pw(body.password))
    token = create_token()
    return {"ok": True, "access_token": token, "token_type": "bearer"}


@router.post("/disable")
def disable_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)):
    """Disable password protection. Requires auth only if a password is currently set."""
    cfg = _cfg()
    if cfg.get("auth_password_hash", ""):
        if credentials is None:
            raise HTTPException(status_code=401, detail="未认证", headers=PANEL_AUTH_HEADERS)
        verify_token(credentials.credentials)
    cfg.set("auth_password_hash", "")
    cfg.set("auth_totp_secret", "")
    return {"ok": True}


@router.post("/login")
def login(body: LoginRequest, request: Request):
    client = _client_key(request)
    retry_after = _login_retry_after(client)
    if retry_after > 0:
        # 失败退避：不告诉调用方「密码对不对」，只说还要等多久。
        raise HTTPException(
            status_code=429,
            detail=f"尝试过于频繁，请 {int(retry_after) + 1} 秒后重试",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    cfg = _cfg()
    stored = cfg.get("auth_password_hash", "")
    if not stored:
        raise HTTPException(status_code=403, detail="no_password_set")
    if not verify_password(body.password, stored):
        _record_login_failure(client)
        raise HTTPException(status_code=401, detail="密码错误")

    _clear_login_failures(client)
    # 老格式（无盐 SHA-256）验证通过后透明升级 —— 用户无感，且不会因为
    # 「旧哈希也要能登录」而永远留着弱哈希。
    if needs_rehash(stored):
        cfg.set("auth_password_hash", hash_password(body.password))

    totp_secret = cfg.get("auth_totp_secret", "")
    if totp_secret:
        _prune_pending_2fa()
        temp = secrets.token_hex(24)
        _pending_2fa[temp] = time.time() + 300  # 5 min expiry
        return {"requires_2fa": True, "temp_token": temp}
    token = create_token()
    return {"requires_2fa": False, "access_token": token, "token_type": "bearer"}


@router.post("/verify-totp")
def verify_totp_route(body: TotpVerifyRequest):
    _prune_pending_2fa()
    expiry = _pending_2fa.get(body.temp_token)
    if not expiry or time.time() > expiry:
        raise HTTPException(status_code=401, detail="临时令牌无效或已过期，请重新登录")
    cfg = _cfg()
    secret = cfg.get("auth_totp_secret", "")
    if not secret:
        raise HTTPException(status_code=400, detail="2FA 未启用")
    if not verify_totp(secret, body.code):
        # 失败的码要**吃掉这次尝试**：此前失败既不消费临时令牌也不计数，
        # 同一个令牌上可以无限次试 TOTP（安全审查 F5b）。TOTP 只有 6 位，
        # 无限试码等于没有第二因子。
        attempts = int(_pending_2fa_attempts.get(body.temp_token, 0)) + 1
        if attempts >= _TOTP_MAX_ATTEMPTS:
            _pending_2fa.pop(body.temp_token, None)
            _pending_2fa_attempts.pop(body.temp_token, None)
            raise HTTPException(status_code=401, detail="验证码错误次数过多，请重新登录")
        _pending_2fa_attempts[body.temp_token] = attempts
        raise HTTPException(status_code=400, detail="验证码错误")
    _pending_2fa.pop(body.temp_token, None)
    token = create_token()
    return {"access_token": token, "token_type": "bearer"}


@router.post("/logout")
def logout():
    return {"ok": True}


@router.post("/change-password", dependencies=[Depends(require_auth)])
def change_password(body: ChangePasswordRequest):
    cfg = _cfg()
    stored = cfg.get("auth_password_hash", "")
    # 用 `verify_password` 而不是比相等：哈希是加盐的，两次算出不同串。
    if stored and not verify_password(body.current_password, stored):
        raise HTTPException(status_code=400, detail="当前密码错误")
    if not body.new_password or len(body.new_password) < 6:
        raise HTTPException(status_code=400, detail="新密码至少需要 6 位")
    cfg.set("auth_password_hash", hash_password(body.new_password))
    return {"ok": True}


@router.get("/2fa/setup", dependencies=[Depends(require_auth)])
def setup_2fa():
    secret = generate_totp_secret()
    return {"secret": secret, "uri": totp_uri(secret)}


@router.post("/2fa/enable", dependencies=[Depends(require_auth)])
def enable_2fa(body: EnableTotpRequest):
    if not body.secret or len(body.secret) < 16:
        raise HTTPException(status_code=400, detail="无效的密钥")
    if not verify_totp(body.secret, body.code):
        raise HTTPException(status_code=400, detail="验证码错误，请重试")
    _cfg().set("auth_totp_secret", body.secret)
    return {"ok": True}


@router.post("/2fa/disable", dependencies=[Depends(require_auth)])
def disable_2fa():
    _cfg().set("auth_totp_secret", "")
    return {"ok": True}
