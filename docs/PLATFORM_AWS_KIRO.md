# AWS Builder ID / Kiro 平台

两个平台共用一条 **AWS Builder ID 注册链路**：

```
platforms/aws/client.py          15 步协议状态机（OIDC → device → portal → workflow
                                 → 提交邮箱 → 注册 → 资料页 → 发码 → 建身份 → 设密码
                                 → SSO 工作流 → SSO token → device token）
platforms/aws/crypto.py          XXTEA 指纹信封 · 密码 JWE（RSA-OAEP-256 + A256GCM）
platforms/kiro/authorize.py      Builder ID SSO → Kiro IDE 授权码 → Kiro AT/RT
```

参考实现：`reference/register/kirox/`（Go 原版）与
`reference/register/reg-factory/register_kiro.py`（Python 移植）。

## 1. 两个平台分别产出什么

| 平台 | 注册动作 | 产物 |
| --- | --- | --- |
| `aws` | 只做 Builder ID 注册 | 邮箱 + **AWS 密码** + `{client_id, client_secret, access_token, refresh_token, sso, region}` |
| `kiro` | Builder ID 注册 + Kiro IDE 授权 | 邮箱 + **AWS 密码** + Kiro 凭据（`access_token` / `refresh_token` / `client_id` / `client_secret` / `profile_arn` / `expires_at` …） |

`kiro` 注册时会**顺带在 `aws` 平台留一条来源账号**（同邮箱、同密码），
Kiro 记录通过 `extra.source = "aws"` 与 `extra.source_email` 指回去 —— 这就是
「kiro 保存来源（aws）」的落地方式。

## 2. 账号记录口径

### `aws` 平台

| 位置 | 内容 |
| --- | --- |
| `email` / `password` | AWS Builder ID 的邮箱与密码（密码加密落盘） |
| `token` 列 | `refresh_token` 的镜像（长期凭据；AT 只有 1 小时，镜像它没有意义） |
| `extra.access_token` / `refresh_token` | OIDC 的 AT / RT（加密） |
| `extra.client_id` / `client_secret` | OIDC 客户端（`client_secret` 加密） |
| `extra.sso` | access portal 的会话令牌（加密；换 Kiro 凭据要用） |
| `extra.region` / `provider` | `us-east-1` / `BuilderId` |

### `kiro` 平台

| 位置 | 内容 |
| --- | --- |
| `email` / `password` | AWS Builder ID 的邮箱与密码（**Kiro 的登录就是它**） |
| `token` 列 | `refresh_token` 的镜像 |
| `extra.source` | 固定 `aws` |
| `extra.source_email` | 来源 AWS 账号的邮箱 |
| `extra.access_token` / `refresh_token` / `client_id` / `client_secret` | Kiro IDE 的凭据（`kiro.json` 的 `pluginSecret` 同款） |
| `extra.profile_arn` / `region` / `plan` / `expires_at` | 调用 Kiro 后端与展示用 |
| `extra.auth_method` / `login_provider` / `uid` | `idc` + `BuilderId`（本项目注册的号都是 IdC） |
| `extra.kiro_aws_*` | 来源账号的 Builder ID 凭据快照（加密；追溯/复核用） |

## 3. 凭据格式：`kiro.json`（v2）

`services/account_export.py` 注册了 `kiro_json` 导出格式，产物与 Kiro 账号管理器
的导出文件同构：

```json
{
  "accounts": [{
    "id": "kiro-account-<sha256>",
    "name": "a@b.com (BuilderId)",
    "pluginIdentity": "kiro:a@b.com:builderid",
    "pluginPublic": {"hasAccessToken": true, "tokenTail": "…", "expiresAtMs": 1794478346773},
    "pluginSecret": {
      "access": "…", "refresh": "…", "clientId": "…", "clientSecret": "…",
      "expires": 1794478346773, "profileArn": "arn:aws:codewhisperer:…",
      "region": "us-east-1", "method": "idc", "loginProvider": "BuilderId",
      "plan": "Kiro FREE", "accountId": "a@b.com", "uid": "builderid"
    },
    "provider": "kiro", "source": "aws", "uid": "builderid"
  }],
  "customProviders": [],
  "exportedAt": 1794474836094,
  "version": 2
}
```

**导入**：`POST /api/accounts/import`（`format=json`）能直接吃这份文件 ——
`api/accounts.py` 认出 `pluginSecret` 后调用
`platforms.kiro.credentials.parse_entry()` 拍平，凭据落进 `extra`（`source`
保留文件里的值：从管理器导入的号是 `plugin`，本项目注册的是 `aws`）。

> `client_secret` / `kiro_aws_*` 都在 `credential_crypto.SENSITIVE_EXTRA_KEYS`
> 里 —— 落盘即密文。它们与 RT 同级：有这三件套就能无限续期 AT。

## 4. 刷新与测活

两个平台共用一组 OIDC 端点（`POST https://oidc.us-east-1.amazonaws.com/token`，
`grant_type=refresh_token`），判读：

| 上游响应 | 结论 | 落库状态 |
| --- | --- | --- |
| 200 | 凭证有效 | `registered` |
| 400 `invalid_grant` | RT 被吊销/过期 | `invalid` |
| 403 | 封禁/停用 | `banned` |

Kiro 测活额外查一次 `getUsageLimits`（Kiro 后端的账号状态判据，403 即停用）。
状态映射只写在插件的 `apply_probe_status_policy` 一处 ——
`services/action_execution.py` 与 `core/scheduler.py` 都通过钩子调用它。

> **social 账号**（GitHub / Google 登录，只能从 `kiro.json` 导入）没有
> `client_id` / `client_secret`，无法用 IdC 刷新 —— 测活会如实报
> 「缺少 IdC 客户端凭据」，不会假装探测过。

## 5. 人机验证（AWS WAF）

AWS 在 `signin.aws` / `profile.aws.amazon.com` 前面挂了 WAF。被拦时响应是
**405 + `x-amzn-waf-action: captcha`**，HTML 里带 `window.gokuProps`
（`key` / `context` / `iv`）与 `challenge.js` / `captcha.js` 的地址。

链路（`platforms/aws/client.py` 的 `_solve_waf`）：

```
收到挑战 → 抠 gokuProps + 脚本地址
        → 打码链 solve_aws_waf(...)
        → 拿到 aws-waf-token 塞进 cookie jar
        → 原请求重试一次（只重试一次，不无限循环）
```

打码链按「全局配置 → 验证码」里的 key 现场拼（`core/captcha_providers.py`）：

| provider | AWS WAF token | Turnstile | 图片 / 3×3 |
| --- | --- | --- | --- |
| **CapMonster**（`AmazonTask`） | ✅ 返回 `aws-waf-token` | ✅ | ✅ |
| YesCaptcha | ❌ 只有 `AwsClassification`（返回格子，不签发 token） | ✅ | ✅ |
| AI 视觉（OpenAI 兼容） | ❌ 只能说「点哪几个格子」 | ❌ | ✅ |

> **纯协议链路只有 CapMonster 能解锁 WAF**：没有浏览器可以点格子，必须拿到
> token。YesCaptcha / AI 在链里是「不支持的 provider」—— 它们会明确报错、
> 让链继续往后走，而不是返回空 token 假装成功。

默认顺序 `capmonster,yescaptcha,ai`（`captcha_provider_order` 可改）；没配 key
的 provider 不进链。链为空时遇到挑战直接抛 `AwsCaptchaRequired` —— 任务层据此
提示「去配 CapMonster」，而不是把一次人机验证当网络失败重试一百遍。

AI 兜底用的是 OpenAI 兼容的视觉接口（`/v1/chat/completions`，图片以 data URI
传给模型）。**推理型模型会把 token 花在 `reasoning_content` 上**，所以
`max_tokens` 给到 512，并且 `content` 为空时明确报错（而不是返回空串）。

## 6. 已知边界

- **网络未联调**：注册链路的每一步都按参考实现对齐（请求形状、指纹信封、
  JWE 头、SSO 流程），测试用 `ScriptedSession` 驱动真实状态机；但真实 AWS
  环境下的成功与否需要在有出口的机器上跑一次（`scripts/` 下可加 e2e 脚本）。
- **没有接码/打码**：AWS 目前对 Builder ID 注册只发邮箱验证码；若上游开始
  要求手机号或人机验证，链路会以 `AwsRegistrationError` 明确失败（不是静默卡住）。
- **自动维护**：`PLATFORM_ACTIONS` 已登记 aws / kiro 的探测与刷新动作，
  但周期任务仍只为 chatgpt / grok 注册（配置键与界面还没做）—— 目前用
  「账号页 → 测活 / 刷新 Token」手动触发，或批量动作。
- **打码需要预付余额**：CapMonster 的 `AmazonTask` 与 YesCaptcha 的
  `AwsClassification` 都按次计费；余额不足时 `AwsCaptchaRequired` 的 detail
  里会带 provider 的原始报错。
