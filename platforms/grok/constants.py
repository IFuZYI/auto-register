"""Grok (x.ai) 协议常量。

出处（reference/grok/）：
- grokRegister-cpa/protocol_signup.py:36-71
- grokRegister-cpa/sso_to_auth_json.py:44-99
- grokRegister-cpa/email_providers/common.py
- grok-reg/grok.py:31（state_tree 兜底）
"""
from __future__ import annotations

import os
import re

# ---------- 端点 ----------
SITE_URL = "https://accounts.x.ai"
CONNECT_CREATE = f"{SITE_URL}/auth_mgmt.AuthManagement/CreateEmailValidationCode"
CONNECT_VERIFY = f"{SITE_URL}/auth_mgmt.AuthManagement/VerifyEmailValidationCode"
SIGNUP_URL = f"{SITE_URL}/sign-up?redirect=grok-com"
SIGNUP_PAGE_URL = f"{SITE_URL}/sign-up"

# ---------- OAuth（4 个参考项目一致）----------
CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
OIDC_ISSUER = "https://auth.x.ai"
SCOPES = "openid profile email offline_access grok-cli:access api:access"
DEVICE_CODE_URL = f"{OIDC_ISSUER}/oauth2/device/code"
DEVICE_VERIFY_URL = f"{OIDC_ISSUER}/oauth2/device/verify"
DEVICE_APPROVE_URL = f"{OIDC_ISSUER}/oauth2/device/approve"
TOKEN_URL = f"{OIDC_ISSUER}/oauth2/token"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

# ---------- 测活 ----------
CPA_GROK_BASE_URL = "https://cli-chat-proxy.grok.com/v1"
CPA_PROBE_URL = f"{CPA_GROK_BASE_URL}/responses"
CPA_PROBE_MODEL = "grok-4.5"
CPA_TOKEN_ENDPOINT = TOKEN_URL

# 版本闸：x.ai 会对过旧的 grok-shell 客户端返回 426
# （"Your Grok CLI version (0.2.93) is outdated. Please update to
#   version 1.0.13 or later"）。实测矩阵：
#   0.2.93 → 426   0.2.102 → 426   1.0.13 → 200   1.0.14 → 200   1.1.0 → 200
# 用 0.2.x 会让**所有** Grok 账号测活失败（426 被当成无效）。
# 取值对齐 reference/grok/grok2api（最新参考实现，Sep 30）的
# `RecommendedBuildClientVersion = 1.0.40`：比最低门槛 1.0.13 留更多余量，
# 免得上游一抬门槛又全挂。可用环境变量 GROK_CLI_VERSION 覆盖。
GROK_CLI_VERSION = (os.getenv("GROK_CLI_VERSION") or "").strip() or "1.0.40"
CPA_GROK_HEADERS = {
    "x-grok-client-version": GROK_CLI_VERSION,
    "x-grok-client-identifier": "grok-shell",
    "User-Agent": f"grok-shell/{GROK_CLI_VERSION} (linux; x86_64)",
}

# ---------- UA ----------
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)

# ---------- 验证码（xAI 格式 XXX-XXX）----------
CODE_RE_ANY = re.compile(r"\b([A-Z0-9]{3}-[A-Z0-9]{3})\b", re.IGNORECASE)

# ---------- 注册资料池（出处：reference 的 protocol_signup.py:64-71）----------
GIVEN_NAMES = [
    "James", "John", "Robert", "Michael", "William", "David", "Richard",
    "Joseph", "Thomas", "Charles", "Neo", "Ethan", "Liam", "Noah", "Lucas",
]
FAMILY_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Lin", "Wang", "Zhao", "Liu", "Chen",
]

# ---------- 运行时可调参数（环境变量）----------
def signup_cfg_ttl() -> float:
    """signup config（site_key/action_id/state_tree）进程内缓存秒数。"""
    try:
        return float(os.getenv("GROK_SIGNUP_CFG_TTL", "1200") or "1200")
    except ValueError:
        return 1200.0


def signup_retries() -> int:
    """signup 提交失败后的重试次数。"""
    try:
        return max(1, min(5, int(os.getenv("GROK_SIGNUP_RETRIES", "3") or "3")))
    except ValueError:
        return 3


def mail_retries() -> int:
    """未收到验证码时换邮箱重试的次数。

    上游对部分临时邮箱域名直接拒发（发码 200 但不投递），免费渠道域名池有限，
    命中拒发域名必须换邮箱重试，否则整轮注册白跑。
    """
    try:
        return max(1, min(8, int(os.getenv("GROK_MAIL_RETRIES", "3") or "3")))
    except ValueError:
        return 3


def probe_warmup_seconds() -> float:
    """测活前预热等待（新 token 常有瞬时 403）。"""
    try:
        return max(0.0, float(os.getenv("GROK_PROBE_WARMUP", "3") or "3"))
    except ValueError:
        return 3.0


def probe_retries() -> int:
    try:
        return max(1, min(6, int(os.getenv("GROK_PROBE_RETRIES", "3") or "3")))
    except ValueError:
        return 3


def cpa_include_sso() -> bool:
    return str(os.getenv("CPA_INCLUDE_SSO", "")).strip().lower() in ("1", "true", "yes")
