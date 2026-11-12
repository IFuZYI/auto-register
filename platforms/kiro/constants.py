"""Kiro 侧端点与固定值。

Kiro IDE 的 OAuth 走的是 AWS OIDC 的**标准 authorization_code + PKCE**，客户端
自己在 `client/register` 时申请（`clientName = "Kiro IDE"`）。这些值来自
`reference/register/kirox/internal/core/kiro_auth.go`。
"""
from __future__ import annotations

from platforms.aws.constants import OIDC_BASE, REGION, SCOPES, START_URL

#: 注册 Kiro IDE 客户端时的固定名（服务端按它识别调用方）。
KIRO_CLIENT_NAME = "Kiro IDE"
#: 客户端注册时声明的回调（真实端口由 PKCE 流程随机挑，这里只是登记）。
KIRO_REDIRECT_URI = "http://127.0.0.1/oauth/callback"
#: 授权码换 token 时用的 grant。
AUTHORIZATION_CODE_GRANT = "authorization_code"
REFRESH_TOKEN_GRANT = "refresh_token"

#: Kiro 凭据的默认 profileArn（`getUsageLimits` / IDE 认这个 ARN）。
#: 取自参考实现；如果 OIDC token 响应里带了 `profileArn`，以响应为准。
DEFAULT_PROFILE_ARN = (
    "arn:aws:codewhisperer:us-east-1:699475941385:profile/EHGA3GRVQMUK"
)

#: 用量查询（Kiro 的「账号还活着吗」最直接的判据）。
USAGE_URL = (
    "https://q.us-east-1.amazonaws.com/getUsageLimits"
    "?origin=AI_EDITOR&resourceType=AGENTIC_REQUEST&isEmailRequired=true"
)
MODELS_URL = "https://q.us-east-1.amazonaws.com/ListAvailableModels?origin=AI_EDITOR"

#: 调 Kiro 后端时用的 UA（与 IDE 一致，缺了会被判成未知客户端）。
KIRO_USER_AGENT = (
    "aws-sdk-js/1.0.18 ua/2.1 os/windows lang/js md/nodejs#20.16.0 "
    "api/codewhispererstreaming#1.0.18 m/E KiroIDE-0.6.18"
)

#: 来源平台名 —— Kiro 账号的 `extra.source`。
SOURCE_AWS = "aws"

#: 默认套餐名（与 kiro.json 的 `plan` 字段同值）。
DEFAULT_PLAN = "Kiro FREE"

#: 认证方式：`idc` = AWS Builder ID（本项目注册出来的都是这种）；
#: `social` = GitHub / Google 登录（只能导入，不能由本项目注册）。
AUTH_METHOD_IDC = "idc"
AUTH_METHOD_SOCIAL = "social"

__all__ = [
    "AUTH_METHOD_IDC",
    "AUTH_METHOD_SOCIAL",
    "AUTHORIZATION_CODE_GRANT",
    "DEFAULT_PLAN",
    "DEFAULT_PROFILE_ARN",
    "KIRO_CLIENT_NAME",
    "KIRO_REDIRECT_URI",
    "KIRO_USER_AGENT",
    "MODELS_URL",
    "OIDC_BASE",
    "REFRESH_TOKEN_GRANT",
    "REGION",
    "SCOPES",
    "SOURCE_AWS",
    "START_URL",
    "USAGE_URL",
]
