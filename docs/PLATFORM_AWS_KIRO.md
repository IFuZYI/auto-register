# AWS Builder ID / Kiro 平台

两个平台共用一条 **AWS Builder ID 注册链路**：

```
platforms/aws/client.py          16 步固定流程 + 1 步条件（`complete_signup`）（OIDC → device → portal → workflow
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

## 1.1 管理入口

AWS 不再是「平台管理」里的一个平台（2026-11-13 调整）：它是 Kiro 的**授权根**，
入口是侧栏 **「授权账号 → AWS」**（`/authorized-accounts/aws`；父级
`/authorized-accounts` 会重定向过来）。

- 列表只列 `platform=aws` 的账号；
- 「授权链」列按 Kiro 账号的 `extra.source_email` 归组，显示每个 Builder ID
  派生出了几个 Kiro 账号；抽屉里是纵向的授权树（根 + 派生的 Kiro 账号/套餐）；
- 注册、导入、导出、测活、刷新凭据都在这一页；`/accounts/aws` 会重定向过来；
- 「平台管理」下只剩 ChatGPT / Grok / Kiro；注册页的平台下拉也不再列 AWS。

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

## 3. 凭据导出

### 3.1 Agent2API（专用入口）

**平台管理 → Kiro → 更多 → 导出凭据** 导出的是 **Agent2API** 的 Kiro 导入文件
（格式 id `kiro_auth_token`，`hidden`，不在通用「导出」下拉里）：

- **勾选/筛选到 1 个账号** → 扁平的 `kiro-auth-token.json`，直接喂给 Agent2API
  的 Kiro「文件」入口；
- **多个账号** → `agent2api-accounts.json`（`{"version":2,"accounts":[…]}`），
  走 Agent2API 的批量账号导入（`POST /api/accounts/import`）。

```json
{
  "id": "kiro-3f2a…（email+uid 的哈希）",
  "provider": "kiro",
  "name": "a@b.com",
  "email": "a@b.com",
  "accessToken": "aoa…",
  "refreshToken": "aor…",
  "expires": 1794478346773,
  "expiresAt": "2026-11-12T19:32:26.773Z",
  "region": "us-east-1",
  "profileArn": "arn:aws:codewhisperer:…",
  "clientId": "…",
  "clientSecret": "…",
  "method": "idc",
  "loginProvider": "builderid",
  "accountId": "a@b.com",
  "plan": "Kiro FREE"
}
```

Agent2API 的 Kiro 解析器读扁平字段（`accessToken` / `refreshToken` /
`expiresAt`（RFC3339 字符串）/ `region` / `profileArn` / `clientId` /
`clientSecret`），不认 `pluginSecret` 嵌套；`expires`（毫秒）同时给出，
账号迁移导入也能用。social 账号的 `clientId` / `clientSecret` 为空。

账号迁移导入额外要求：每条记录有**唯一 `id`**（用 `email:uid` 的哈希，
同一邮箱的 GitHub/Google 两个 provider 不会撞），身份回退读 `email`；
`loginProvider` 小写（插件的 refresh 分支按 `"builderid"` 比较）；
**不导出 `uid`**（它会把 Agent2API 的身份压成同一个 `"builderid"`，
23 个号互相覆盖）。这些约束由 `scripts/check_kiro_agent2api_export.py`
（`tests/test_kiro_agent2api_contract.py`）守住。

### 3.2 Kiro 账号管理器（`kiro_json`，保留格式）

`services/account_export.py` 仍注册 `kiro_json`（v2，账号管理器格式），
但**不在任何 UI 下拉里**——需要时按 id 调用：

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
    "provider": "kiro", "source": "plugin", "uid": "builderid"
  }],
  "customProviders": [],
  "exportedAt": 1794474836094,
  "version": 2
}
```

**导入**：`POST /api/accounts/import`（`format=json`）能直接吃这份文件 ——
`api/accounts.py` 认出 `pluginSecret` 后调用
`platforms.kiro.credentials.parse_entry()` 拍平，凭据落进 `extra`。

`source` 的口径：导出写 `plugin`（管理器的取值），导入时对 `auth_method=idc`
的账号映射回 `aws` —— 本项目注册的号来源是 AWS，这条链不能因为一次导出/导入就断。

> `client_secret` 与 `kiro_aws_{access_token,refresh_token,client_secret,sso}`
> 在 `credential_crypto.SENSITIVE_EXTRA_KEYS` 里 —— 落盘即密文。它们与 RT
> 同级：有这三件套就能无限续期 AT。

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

## 6. 真机验证记录（2026-11-13）

### 6.1 结果

在有出口的机器上真实注册了 **3 个 Kiro 账号**（iCloud 远程别名 + SOCKS5 代理）：

| 邮箱 | Kiro | AWS 来源 | 刷新验证 |
| --- | --- | --- | --- |
| `t***@icloud.com` | ✅ AT/RT + profileArn | ✅ | ✅ `refresh_token` 换到新 AT |
| `tr***@icloud.com` | ✅ | ✅ | — |
| `u***@icloud.com` | ✅ | ✅ | — |

> 真实地址只留在运行库里（`accounts` 表），文档与提交记录不落 PII。

产物：`source=aws`、`plan=Kiro FREE`、`profile_arn=arn:aws:codewhisperer:us-east-1:…`。

### 6.2 之前为什么 `BLOCKED`

第一版在「设置密码」被 AWS 回 `400 errorCode: BLOCKED`。根因是**指纹自相矛盾**：

- UA 随机成 Windows Chrome 131–144，但 TLS 指纹是 `chrome131`、GPU 写 Intel、
  WebGL 扩展为空、canvas 直方图是自造的 —— 典型机器人特征。

修复（照 `any-auto-register/platforms/kiro/core.py` 的 `gen_fwcim` 逐字段移植）：

- UA/`sec-ch-ua`/`sec-ch-ua-platform` 固定为 **macOS Chrome 131**（与
  `curl_cffi impersonate="chrome131"` 一致）；
- GPU = Apple M4 + 39 项 WebGL 扩展；canvas 直方图用**真实** 256 桶
  （和 36000）；`lsUbid = X<aws-ubid-main>:<epoch>`；`history` 2–8；
- 指纹信封去掉 base64 的 `=` 填充（与参考实现一致）。

### 6.3 之后还卡在 SSO

账号创建成功后，SSO 工作流在 `get-password` 停下（会话里的 `aws-usi-authn`
已 `deleted`）。真机逐步验证出的登录路径：

```
portal login → step "" → start → get-identity-user（只发 username + 指纹）
            → get-password（JWE 密码 + PAGE_SUBMIT 事件 + 指纹）→ end-of-workflow-success
```

两个坑：登录路径的 `get-identity-user` **只认两件输入**（带
`ApplicationType`/`UserEvent` 会 400）；`get-password` 必须带
`UserEventRequestInput`（与注册设密码同款）。

### 6.4 Kiro 的 AWS 来源（复用 / 指定 / 新建）

`KiroPlatform.register` 按 `extra.kiro_source` 分派：

| 值 | 行为 |
| --- | --- |
| `reuse`（默认） | 先挑一个「没被 Kiro 用过」的 AWS 账号（按 `source_email` 判定，FIFO）复用登录；没有可用号时按 `kiro_auto_register` 决定是否新建 |
| `manual`（前端「指定 AWS 账号」） | 用 `extra.kiro_aws_email` 指定的号；不存在 / 没密码 / 已注册 Kiro 都直接报错 |
| `auto` | 直接新建 AWS + Kiro |

`kiro_auto_register`（默认开）：没有未注册 Kiro 的 AWS 时，自动新建 AWS + Kiro。

复用登录的完整路径（真机 2026-11-13）：

```
portal login → "" → start → get-identity-user（username + 指纹）
  → get-password（JWE 密码）
  → get-email-otp-login-credential（EmailOTPLoginRequestInput.emailOTPLoginResponseCode）
  → end-of-workflow-success → authCode → SSO token → Kiro 授权
```

注意：登录 OTP 由 AWS 在到达该步时发到账号邮箱；同一出口短时间内多次登录会被
限流（400「Please try signing in again」），真机验证时先换出口再重试。

### 6.5 失败恢复（半成品账号）

密码已设置、但 SSO / 换 token 失败的账号不再丢：

* `AwsBuilderIdClient.password_set` 在设密码成功后置位；
* `AwsPlatform.register` 捕获到 `AwsRegistrationError` 且 `password_set=True` 时，
  把账号落库为「待补凭据」（`extra.needs_sso=True`），并抛出
  「AWS 账号已创建（密码已设置），但拿凭据失败…账号已保存」；
* 「授权账号」页对这类号显示 `待补凭据` 标签，动作菜单里给
  「补 Kiro（复用登录）」——用 Kiro 的 `kiro_source=manual` +
  `kiro_aws_email` 起一个复用任务；
* 复用登录走 `sso_workflow`（含邮箱验证码），成功即补回 Kiro 凭据。

### 6.6 出口

同一出口连续注册多次后会被限流（同一个 400）。实测 **SOCKS5 代理 + `socks5h://`**
（远端 DNS）稳定；直连只在前几次成功。

### 6.7 自动维护（本地定时任务）

AWS / Kiro 与 ChatGPT / Grok 共用同一套本地维护骨架
（`services/local_maintenance.py`）：每个平台 **10 个配置键**，前缀 `aws_` / `kiro_`
（键表在 `core/config_schema.py` 的 `maintenance` 分区）。

| 键（`<平台>_` 前缀） | 作用 | 默认 |
| --- | --- | --- |
| `auto_refresh_enabled` | 自动刷新 Token 的总开关 | 关 |
| `refresh_scope` | 刷谁：`failed`（失效/过期/受限）· `all` · `random` | `failed` |
| `refresh_random_count` | `random` 档每轮刷多少个（1–1000） | `50` |
| `refresh_interval` | 刷新间隔：`30m` / `6h` / `1d`（最短 5 分钟、最长 30 天） | `6h` |
| `refresh_disable_banned` | 上游回答「已停用」时把账号标成 `banned` | 开 |
| `auto_probe_enabled` | 自动检测的总开关 | 关 |
| `probe_mode` | `sample`（按比例抽检）· `all`（全量） | `sample` |
| `probe_sample_percent` | 抽检比例（1–100） | `20` |
| `probe_strategy` | 抽检挑谁：`oldest`（最久未检测优先）· `random` | `oldest` |
| `probe_interval` | 检测间隔（同 `refresh_interval` 的单位与上下限） | `6h` |

周期任务**按 `PLATFORM_ACTIONS` 遍历注册**（`services/local_maintenance.py`
末尾的循环）：AWS / Kiro 各两条，共 **4 条** —— `aws_auto_probe` /
`aws_auto_refresh` / `kiro_auto_probe` / `kiro_auto_refresh`。它们复用手动动作
同一条链（`probe` / `refresh_token`），所以定时与手动的判定、写回规则不会分叉。
各自的开关关闭时（`auto_probe_enabled` / `auto_refresh_enabled`），或
`probe_mode` / `refresh_scope` 不在白名单时，对应任务的 `interval_seconds`
返回 0（= 不跑）—— 非法值不会退化成「全量」，静默放大执行量比不跑更危险。

设置页的「本地维护」分区对 AWS / Kiro **不渲染「刷新策略」**
（`*_sync_strategy`，即面板同步策略）：这两个平台没有可同步的面板
（`PLATFORM_ACTIONS` 只有 probe / refresh），多一个永远是 `local_only` 的下拉
只会让人以为配了会生效。前端键表在 `frontend/src/lib/maintenanceKeys.ts`
（`syncable: false`），后端也**没有** `aws_sync_strategy` / `kiro_sync_strategy`
这两个键 —— 提交上来会被当未知键丢弃。

> **`probe_checked_at` 是抽检轮转的输入**：AWS / Kiro 的探测不像 ChatGPT 那样
> 落 `chatgpt_local.checked_at`，只写 `extra.probe_checked_at`（毫秒 epoch，
> `core/probe_credentials.mark_probe_checked`，**失败也写** —— 400/403 同样是
> 「已经看过一眼」）。`services/local_maintenance.last_probe_at` 读它排序；
> 没有这个戳时所有号都是 0，`oldest` 退化成 id 序、每轮都挑同一批号。

## 7. 已知边界

> 历史记录：本节早期版本写「当前出口被 AWS 判 BLOCKED」「没有接码/打码」——
> 那是 6.2 修复前的状态，已被 6.1 的 3 个成功账号推翻。现在保留的是**仍然成立**的边界。

- **AWS 风控**：同一出口连续注册会被限流（`400 Please try signing in again`），
  换出口或等窗口即可；`BLOCKED` 的根因（指纹自相矛盾）已在 6.2 修复。
- **验证码**：Builder ID 注册只发邮箱验证码；AWS WAF 需要 token 时走
  CapMonster → YesCaptcha → AI 的降级链，链上没有可用 provider 时
  `AwsCaptchaRequired` 明确失败（不是静默卡住）。
- **自动维护**：探测与刷新已接入本地定时任务（每平台 10 个配置键、AWS / Kiro
  各两条周期任务），见 §6.7；手动触发仍可用「账号页 → 测活 / 刷新 Token」或
  批量动作。
- **打码需要预付余额**：CapMonster 的 `AmazonTask` 与 YesCaptcha 的
  `AwsClassification` 都按次计费；余额不足时 `AwsCaptchaRequired` 的 message
  里会带 provider 的原始报错（便于排查余额/网络）。
