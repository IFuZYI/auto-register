"""AWS Builder ID 注册用到的端点与固定参数。

值来自参考实现（`reference/register/kirox/internal/core/config.go`）。
`DIRECTORY_ID` 是 AWS 为 Builder ID 公开分配目录 —— 换掉会直接 404，不是配置项。
"""
from __future__ import annotations

#: OIDC / 设备授权端点。
OIDC_BASE = "https://oidc.us-east-1.amazonaws.com"
#: 登录工作流（signin.aws 的 SPA 后端）。
SIGNIN_BASE = "https://us-east-1.signin.aws"
#: 资料页（邮箱验证 / 建身份）。
PROFILE_BASE = "https://profile.aws.amazon.com"
#: AWS access portal（SSO token 与 device 确认）。
PORTAL_BASE = "https://portal.sso.us-east-1.amazonaws.com"
#: view.awsapps.com（start 页面）。
VIEW_BASE = "https://view.awsapps.com"
#: 设备指纹 / D2C token 下发端点。
D2C_URL = "https://vs.aws.amazon.com/token"

#: Builder ID 的目录 id（参考实现同值）。
DIRECTORY_ID = "d-9067642ac7"
#: 设备授权里的 startUrl。
START_URL = f"{VIEW_BASE}/start"
#: 固定 region：Builder ID 目前只有 us-east-1。
REGION = "us-east-1"

#: OIDC 客户端申请的 scopes（Amazon Q Developer for command line 同款）。
SCOPES = [
    "codewhisperer:completions",
    "codewhisperer:analysis",
    "codewhisperer:conversations",
    "codewhisperer:transformations",
    "codewhisperer:taskassist",
]

#: 注册时申请的 OIDC 客户端名（服务端会记录，别随意改）。
CLIENT_NAME = "Amazon Q Developer for command line"

#: 注册产物的 provider 标识（与 kiro.rs / 面板导出的 `BuilderId` 对齐）。
PROVIDER = "BuilderId"

#: 网络超时（秒）。注册链路单步偶发慢，给 60s。
HTTP_TIMEOUT = 60

__all__ = [
    "CLIENT_NAME",
    "D2C_URL",
    "DIRECTORY_ID",
    "HTTP_TIMEOUT",
    "OIDC_BASE",
    "PORTAL_BASE",
    "PROFILE_BASE",
    "PROVIDER",
    "REGION",
    "SCOPES",
    "SIGNIN_BASE",
    "START_URL",
    "VIEW_BASE",
]
