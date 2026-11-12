"""Kiro 平台插件（AWS Builder ID + Kiro IDE 授权，纯协议）。

模块结构：

- `constants.py`   Kiro 端点 / 默认 profileArn / 来源常量
- `credentials.py` kiro.json（v2）↔ 账号 extra 的双向映射
- `authorize.py`   Builder ID SSO → Kiro IDE 授权码 → Kiro AT/RT
- `probe.py`       Kiro token 刷新与测活
- `plugin.py`      `KiroPlatform` 平台实现

**来源语义**：Kiro 的账号就是 AWS Builder ID。注册时先走
`platforms.aws.client` 建号（并在 `aws` 平台留一条来源账号），再换 Kiro 凭据；
Kiro 记录的 `extra.source = "aws"`、`extra.source_email` 指向来源账号，
`password` 列存的就是 AWS 的密码。
"""
