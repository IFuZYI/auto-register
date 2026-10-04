"""ChatGPT 平台常量。

历史说明：这里曾定义端点表、页面类型、OTP 正则、默认设置、错误消息、
Outlook 再导出等一大批常量 —— 它们全部零引用（真正的注册链在
``platforms/chatgpt/protocol/``，自带各自的实现），已随 2026-10 清理删除。
现在只保留有真实消费者的 OAuth 客户端配置（``token_refresh.py`` 刷新 AT 时用）。
"""
from __future__ import annotations

# OpenAI OAuth 客户端（token_refresh.py 用）
OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
OAUTH_REDIRECT_URI = "http://localhost:1455/auth/callback"
