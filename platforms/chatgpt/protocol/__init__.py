"""ChatGPT 注册协议栈。

整套协议完全无浏览器：用 ``curl_cffi`` 模拟 TLS 指纹，用 Node 跑 OpenAI 真实
``sdk.js`` 解 Sentinel PoW，直接驱动 ``auth.openai.com`` 的 authorize 状态机。

链路：

    warmup → csrf → signin/openai → authorize 初始化 → sentinel PoW
    → authorize/continue → 设密码 → 发码 → 验码 → create_account
    → 重定向链 → auth/session → Codex OAuth 换 refresh_token

其中 ``add-phone`` 环节由 ``services.sms_service`` 提供的接码控制器接管。
"""

from platforms.chatgpt.protocol.auth_flow import AuthFlow, AuthResult
from platforms.chatgpt.protocol.config import Config
from platforms.chatgpt.protocol.mail_provider import (
    MailProvider,
    MailProviderError,
    extract_otp,
)
from platforms.chatgpt.protocol.totp import otpauth_uri, totp_now, verify_totp
from platforms.chatgpt.protocol.two_factor import (
    TwoFactorBindResult,
    bind_totp_inline,
    bind_totp_via_login,
)

__all__ = [
    "AuthFlow",
    "AuthResult",
    "Config",
    "MailProvider",
    "MailProviderError",
    "TwoFactorBindResult",
    "bind_totp_inline",
    "bind_totp_via_login",
    "extract_otp",
    "otpauth_uri",
    "totp_now",
    "verify_totp",
]
