"""Outlook / Microsoft 邮箱池常量。

为什么在 core 而不是 platforms/chatgpt
-------------------------------------
这些常量描述的是 **Microsoft 邮箱服务本身**（IMAP 服务器、OAuth 端点、scope），
任何用到 Outlook 邮箱池的平台都要用（ChatGPT、Grok、以及后续新增平台）。

原先它们定义在 `platforms/chatgpt/constants.py`，被 `core/base_mailbox.py`
反向 import —— 破坏了 `core → modules → platforms` 的依赖方向（core 不该知道
具体平台）。上移到 core 后，平台侧改为从 core 引入。
"""
from __future__ import annotations

# Microsoft OAuth2 Token 端点
MICROSOFT_TOKEN_ENDPOINTS = {
    # 旧版 IMAP 使用的端点
    "LIVE": "https://login.live.com/oauth20_token.srf",
    # 新版 IMAP 使用的端点（需要特定 scope）
    "CONSUMERS": "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
    # Graph API 使用的端点
    "COMMON": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
}

# IMAP 服务器配置
OUTLOOK_IMAP_SERVERS = {
    "OLD": "outlook.office365.com",  # 旧版 IMAP
    "NEW": "outlook.live.com",       # 新版 IMAP
}

# Microsoft OAuth2 Scopes
MICROSOFT_SCOPES = {
    # 旧版 IMAP 不需要特定 scope
    "IMAP_OLD": "",
    # 新版 IMAP 需要的 scope
    "IMAP_NEW": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
    # Graph API 需要的 scope
    "GRAPH_API": "https://graph.microsoft.com/.default",
}

# Outlook 提供者默认优先级
OUTLOOK_PROVIDER_PRIORITY = ["imap_new", "imap_old", "graph_api"]


__all__ = [
    "MICROSOFT_SCOPES",
    "MICROSOFT_TOKEN_ENDPOINTS",
    "OUTLOOK_IMAP_SERVERS",
    "OUTLOOK_PROVIDER_PRIORITY",
]
