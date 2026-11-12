"""AWS Builder ID 平台插件（纯协议）。

模块结构：

- `constants.py`  端点 / OIDC scopes / 目录 id
- `crypto.py`     指纹信封（XXTEA）· 密码 JWE · 设备身份值
- `client.py`     注册状态机（OIDC → device → portal → workflow → OTP → 密码 → SSO）
- `credentials.py` 注册产物 → 账号 extra（凭证规范化）
- `probe.py`      Builder ID token 刷新与测活
- `plugin.py`     `AwsPlatform` 平台实现

参考实现：`reference/register/kirox/`（Go）与
`reference/register/reg-factory/register_kiro.py`（Python）。

**不要在包 `__init__` 里 import `plugin`**：`core.registry.load_all()` 是按
`platforms.<name>.plugin` 逐个导入的，包级 import 会让「只想要 crypto 的测试」
也被平台依赖拖起来（并制造循环导入）。
"""
