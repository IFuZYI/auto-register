"""Grok (x.ai) 平台插件。

模块结构：
- constants.py         端点 / CLIENT_ID / SCOPES / 正则 / 姓名池
- profile.py           姓名 / 密码生成
- register_browser.py  浏览器注册全流程（camoufox）—— **唯一注册路径**
- turnstile_mint.py    Turnstile（captcha solver / 屏外 Chrome / 外部 Solver）
- oauth_device.py      SSO → OAuth Device Flow + CPA 记录
- oauth_browser.py     Device Flow 的浏览器兜底（协议版 approve 会被 CF 403）
- probe.py             账号测活
- grok2api.py          grok2api 管理面客户端
- upload.py            CPA / Sub2API 上传
- plugin.py            GrokPlatform 平台实现

协议注册路径（`protocol_signup.py` / `send_code_ui.py` / `signup_browser.py` /
`sso_exchange_browser.py`）已于 2026-10-01 整体删除：x.ai 对协议层发码「假接受」
（回 grpc-status:0 但零投递）、协议验码在无 CF 通行证的客户端里恒为 grpc=3，
参考项目 grok-hub-clean 也放弃了这条路。详见 `plugin.py` 的 `register()`。
"""

from .plugin import GrokPlatform

__all__ = ["GrokPlatform"]
