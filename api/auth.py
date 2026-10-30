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


def _rotate_jwt_secret() -> bool:
    """换掉 JWT 签名密钥 —— 等价于「把已签发的令牌全部作废」。

    无状态 JWT 没有服务端会话表，logout / 改密 / 关鉴权要真的生效，只能换密钥。
    返回 False 表示密钥来自环境变量 `APP_JWT_SECRET`（部署方自己管的），
    进程内改不了 —— 调用方要把这件事如实告诉前端，而不是假装吊销成功。
    """
    if os.getenv("APP_JWT_SECRET", ""):
        return False
    _cfg().set("auth_jwt_secret", secrets.token_hex(32))
    return True


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

    原来无条件采信 `X-Forwarded-For` 首段 —— 那是请求方随手能写的头，
    伪造它就能让每次尝试都落在一个新 key 上，**退避完全失效**（安全审查 F7）。
    现在只有部署方显式声明 `APP_TRUST_PROXY=1`（真挂了反代）才采信，
    否则一律用 TCP 源地址；判定逻辑在 `core/net_utils.client_ip`。
    """
    from core.net_utils import client_ip

    return client_ip(request)


# ── Schemas ────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    password: str


class TotpVerifyRequest(BaseModel):
    temp_token: str
    code: str


class SetupPasswordRequest(BaseModel):
    password: str
    #: 已有密码时**必须**提供当前密码 —— 只凭一个 Bearer 令牌就能改密，
    #: 等于「令牌泄露一次 = 永久接管」（安全审查 F10）。
    old_password: str = ""


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
    request: Request,
    body: SetupPasswordRequest,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
):
    """首次设置密码（仅本机），或已认证后改密（需当前密码）。

    这条路由在 `main.py` 的鉴权中间件**之外**（`/api/auth/*` 整段跳过），
    所以它必须自己把关 —— 原来的写法是「没设密码就直接设」，于是：

    - 任何能连到端口的人都能**抢先设一个密码**，然后拿返回的 token 接管面板
      （安全审查 F1，严重）；
    - 已经设了密码时，只要持有一个有效 token 就能改密，不需要旧密码，
      令牌泄露即永久接管（F10）。

    现在的规则：没设密码 → 只接受本机回环（与中间件「未设密码只放行本机」
    的口径一致）；已设密码 → 必须同时给出 token **和**当前密码。
    """
    if not body.password or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="密码至少需要 6 位")
    cfg = _cfg()
    stored = cfg.get("auth_password_hash", "")
    if stored:
        if credentials is None:
            raise HTTPException(status_code=401, detail="未认证", headers=PANEL_AUTH_HEADERS)
        verify_token(credentials.credentials)
        if not body.old_password:
            raise HTTPException(status_code=400, detail="修改密码需要提供当前密码")
        if not verify_password(body.old_password, stored):
            raise HTTPException(status_code=400, detail="当前密码错误")
    else:
        from core.net_utils import is_loopback_client

        if not is_loopback_client(request) and os.getenv("APP_ALLOW_REMOTE_SETUP", "") != "1":
            raise HTTPException(
                status_code=403,
                detail=(
                    "首次设置密码只允许本机访问。请在服务器上用 127.0.0.1 打开面板，"
                    "或显式设置 APP_ALLOW_REMOTE_SETUP=1（容器部署用）。"
                ),
            )
    cfg.set("auth_password_hash", _hash_pw(body.password))
    _rotate_jwt_secret()
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
    revoked = _rotate_jwt_secret()
    return {"ok": True, "revoked": revoked}


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
    """注销 = 换密钥，把已签发的令牌全部作废。

    原来是空实现：token 有效期 7 天且无法吊销，「退出登录」只是前端删掉了
    localStorage 里的那串字符（安全审查 F6）。
    """
    revoked = _rotate_jwt_secret()
    return {"ok": True, "revoked": revoked}


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
    revoked = _rotate_jwt_secret()
    # 密钥换过之后**当前这个 token 也失效了**，得给前端一个新令牌，
    # 否则用户改完密码立刻被踢回登录页（体验上像「改密失败」）。
    return {"ok": True, "revoked": revoked, "access_token": create_token(), "token_type": "bearer"}


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
