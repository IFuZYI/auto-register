# 账号管理台 API 接口文档

> 基于当前 FastAPI 路由源码整理（入口：`main.py`）。默认服务地址为 `http://localhost:8000`，全部请求和响应均为 JSON，除非另有说明。
>
> 本文不记录或展示真实凭据；调用时请通过 HTTPS、环境变量或受控的密钥管理方式提供密码、Token、支付卡等敏感数据。

## 1. 通用约定

### 1.1 鉴权

- 当尚未设置面板密码时，所有 `/api/*` 接口均可直接访问。
- 设置密码后，除 `/api/auth/*` 外的 `/api/*` 请求都必须携带：

```http
Authorization: Bearer <access_token>
```

- 登录态失效或缺失时，服务返回 `401`，并附带 `X-Panel-Auth-Required: 1` 响应头。
- `/m/{share_token}` 是 iCloud 共享邮件的公开链接，**不带 `/api` 前缀且不经过面板鉴权**；`share_token` 本身即访问凭证。

### 1.2 常见响应与状态码

| 状态码 | 含义 |
| --- | --- |
| `200` | 请求成功 |
| `400` | 参数校验或业务参数错误 |
| `401` | 面板未认证或会话失效（带 `X-Panel-Auth-Required: 1`） |
| `403` | 被上游或业务规则拒绝 |
| `404` | 资源、平台或渠道不存在 |
| `409` | 资源状态冲突，例如已结束任务继续控制 |
| `422` | FastAPI 请求体校验失败，或 iCloud 上游明确拒绝 |
| `429` | 上游限流 |
| `502` / `503` | 外部服务调用失败或不可用 |

业务错误通常是：

```json
{"detail":"错误说明"}
```

### 1.3 时间、分页与异步任务

- 时间参数使用 ISO 8601，例如 `2026-09-17T10:30:00+08:00`。
- 账号列表分页参数：`page` 默认 `1`，`page_size` 默认 `20`。
- 多数耗时操作以后台任务执行，先返回 `task_id`，再通过 `GET /api/tasks/{task_id}` 查询状态，或订阅 SSE 日志流。
- 批量删除、批量操作通常限制最多 `1000` 个对象。

### 1.4 核心数据对象

**账号（`AccountModel`）**

| 字段 | 说明 |
| --- | --- |
| `id` | 账号 ID |
| `platform` | 平台标识，例如 `chatgpt`、`icloud` |
| `email` / `password` | 账号邮箱（或手机号）与密码；均属敏感数据 |
| `user_id` / `region` | 平台用户 ID、地区 |
| `token` | Access Token，敏感数据 |
| `status` | 账号状态：`registered` / `expired` / `invalid` / `banned`，默认 `registered` |
| `cashier_url` | 支付/升级链接 |
| `extra_json` | 平台扩展字段的 JSON 字符串，例如 RT、Cookie、TOTP |
| `created_at` / `updated_at` | 创建、更新时间 |

**任务快照**

`GET /api/tasks` 和 `GET /api/tasks/{task_id}` 返回任务快照，常用字段为：`id`、`status`（`pending`、`running`、`done`、`failed`、`stopped`）、`platform`、`source`、`total`、`progress`、`success`、`registered`、`skipped`、`logs`、`errors`、`meta`、`control`、`created_at`、`updated_at`。

---

## 2. 认证 `/api/auth`

| 方法 | 路径 | 请求体 / 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/auth/status` | — | 返回 `has_password`、`has_totp` |
| POST | `/api/auth/setup` | `{password}` | 首次设置面板密码；已有密码时须 Bearer 鉴权。密码最少 6 位，成功返回 `access_token` |
| POST | `/api/auth/disable` | — | 关闭面板密码与 TOTP；已启用密码时须 Bearer 鉴权 |
| POST | `/api/auth/login` | `{password}` | 登录。未启用 TOTP 时返回 Token；启用时返回 `{requires_2fa:true,temp_token}` |
| POST | `/api/auth/verify-totp` | `{temp_token,code}` | 校验登录过程中的 6 位 TOTP，返回 Token |
| POST | `/api/auth/logout` | — | 返回 `{ok:true}`；Token 为无状态 JWT，客户端仍需自行清除 |
| POST | `/api/auth/change-password` | `{current_password,new_password}` | 修改密码，须 Bearer 鉴权 |
| GET | `/api/auth/2fa/setup` | — | 生成待启用的 `{secret,uri}`，须 Bearer 鉴权 |
| POST | `/api/auth/2fa/enable` | `{secret,code}` | 校验并启用 TOTP，须 Bearer 鉴权 |
| POST | `/api/auth/2fa/disable` | — | 关闭 TOTP，须 Bearer 鉴权 |

JWT 默认有效期为 7 天。密码与 JWT 签名密钥存入配置存储；可通过 `APP_JWT_SECRET` 覆盖签名密钥。

---

## 3. 账号管理 `/api/accounts`

| 方法 | 路径 | 请求体 / 查询参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/accounts` | `platform,status,email,plus_status,created_at_start,created_at_end,page,page_size` | 分页列出账号，返回 `{total,page,items}` |
| POST | `/api/accounts` | `{platform,email,password,status?,token?,cashier_url?}` | 手工创建账号 |
| GET | `/api/accounts/stats` | — | 返回总数、按平台及状态统计 |
| GET | `/api/accounts/export` | `platform?,status?` | 下载 CSV，响应为 `text/csv` |
| GET | `/api/accounts/export-formats` | — | 返回导出格式清单和默认格式 |
| POST | `/api/accounts/export-text` | 见下方 | 按格式导出文本/JSON内容，不直接下载 |
| POST | `/api/accounts/import` | `{platform,lines,format?}` | 批量导入。`format="text"`（默认）每行 `email password [cashier_url]`；`format="json"` 传导出格式的 JSON 数组，**全字段**（凭证进 extra）；返回 `created`/`updated`/`skipped` |
| POST | `/api/accounts/batch-delete` | `{ids:[1,2]}` | 批量删除，返回 `deleted`、`not_found`、`total_requested` |
| POST | `/api/accounts/check-all` | `platform?`（查询参数） | 后台检查一个平台或全部账号 |
| GET | `/api/accounts/{account_id}` | — | 获取单个账号 |
| PATCH | `/api/accounts/{account_id}` | `{status?,token?,cashier_url?}` | 更新允许修改的字段 |
| DELETE | `/api/accounts/{account_id}` | — | 删除账号 |
| POST | `/api/accounts/{account_id}/check` | — | 后台检查单个账号可用性 |

`POST /api/accounts/export-text`：

```json
{
  "format":"email_pw_2fa_at_rt",
  "platform":"chatgpt",
  "account_ids":[1,2],
  "status":"registered",
  "email":"@example.com",
  "plus_status":"",
  "created_at_start":"2026-09-01T00:00:00+08:00",
  "created_at_end":"2026-09-30T23:59:59+08:00"
}
```

`account_ids` 非空时优先按给定顺序导出；否则使用筛选项。成功返回 `format`、`total`、`lines`、`content`、`filename`。可用格式由服务端动态返回，现有格式包括 `email_pw`、`email_pw_2fa`、`email_pw_2fa_at`、`email_pw_2fa_rt`、`email_pw_2fa_at_rt`、`email_pw_2fa_phone`、`email_pw_rt`、`email_2fa`、`at`、`rt`、`totp`、`csv`、`json`。

---

## 4. 注册任务与任务历史 `/api/tasks`

### 4.1 创建及控制

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/tasks/register` | `RegisterTaskRequest` | 创建通用平台注册任务，返回 `{task_id}` |
| POST | `/api/tasks/backfill-rt` | `AccountBatchTaskRequest` | 为 ChatGPT 账号批量补 Refresh Token |
| POST | `/api/tasks/bind-2fa` | `AccountBatchTaskRequest` | 为 ChatGPT 账号批量绑定 TOTP 2FA |
| POST | `/api/tasks/{task_id}/skip-current` | — | 跳过正在执行的当前账号 |
| POST | `/api/tasks/{task_id}/stop` | — | 请求停止任务 |

`RegisterTaskRequest`：

```json
{
  "platform":"chatgpt",
  "email":null,
  "password":null,
  "count":1,
  "concurrency":1,
  "register_retry_times":1,
  "register_delay_seconds":0,
  "proxy":null,
  "executor_type":"protocol",
  "captcha_solver":"yescaptcha",
  "extra":{}
}
```

`extra` 用于平台或邮箱服务的任务级覆盖配置；它会覆盖全局配置中同名的非空字段。`register_retry_times` 取值会被限制在 `0` 到 `10`。

**数量与并发有上界**：`count ≤ 10000`、`concurrency ≤ 200`（`api/tasks.py` 的 `MAX_REGISTER_COUNT` / `MAX_REGISTER_CONCURRENCY`），超界返回 **422**。下界仍是 `≥ 1`。上界的存在理由是保证误填一个大数不会产生**停不掉的任务** —— 线程池按 `count` 提交 future，`stop` 只置标志位，已提交的任务还在排队；实测 `count=1000000` 时内存涨到 863MB、CPU 37.6%，而 `DELETE` 因「运行中」被 409 拒绝。前端两个入口（账号页注册弹窗、注册任务页）的 `max` 与这里保持一致（`frontend/src/lib/registerLimits.ts`）。

`AccountBatchTaskRequest` 用于补 RT 与绑 2FA：

```json
{
  "account_ids":[1,2],
  "all_filtered":false,
  "email":"",
  "status":"",
  "plus_status":"",
  "only_missing_rt":true,
  "only_missing_2fa":true,
  "allow_login":true,
  "concurrency":1,
  "delay_seconds":5,
  "proxy":null
}
```

补 RT 使用 `only_missing_rt`，绑定 2FA 使用 `only_missing_2fa`；未提供 `account_ids` 时必须设 `all_filtered:true`。

### 4.2 查询、日志与删除

| 方法 | 路径 | 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/tasks` | — | 列出持久化任务快照 |
| GET | `/api/tasks/{task_id}` | — | 获取一个任务快照 |
| GET | `/api/tasks/{task_id}/logs/stream` | `since=0` | SSE 日志流，事件形如 `data: {line,success,registered,total}`；结束时有 `done:true` |
| DELETE | `/api/tasks/{task_id}` | — | 删除已结束任务；运行中任务返回 `409` |
| GET | `/api/tasks/logs` | `platform?,page=1,page_size=50` | 获取注册任务历史记录，返回 `{total,items}` |
| POST | `/api/tasks/logs/batch-delete` | `{ids:[1,2]}` | 批量删除任务历史 |

---

## 5. 平台与通用操作

### 5.1 平台 `/api/platforms`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/platforms` | 返回注册表中已加载的平台及其能力 |

### 5.2 账号操作 `/api/actions`

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/actions/{platform}` | — | 获取指定平台支持的动作和参数描述 |
| POST | `/api/actions/{platform}/{account_id}/{action_id}` | `{params:{}}` | 对一个账号执行平台动作 |
| POST | `/api/actions/{platform}/{action_id}/batch` | `BatchActionRequest` | 对指定账号或筛选结果批量执行动作 |

`BatchActionRequest`：

```json
{
  "account_ids":[1,2],
  "all_filtered":false,
  "email":"",
  "status":"",
  "plus_status":"",
  "params":{}
}
```

批量响应为 `{total,success,failed,items}`；每个 `items` 元素包含 `id`、`email`、`ok`、`message`、`status`。当不提供 `account_ids` 时必须传 `all_filtered:true`。

当前内置动作（以 `GET /api/actions/{platform}` 实际返回为准）。

带 `scope: "panel"` 的动作是**面板动作**：它们的目标是外部面板（CPA / Sub2API /
grok2api），操作面在界面「面板管理」页（那里有本地 ↔ 远端对比，能看出谁没传）。
账号页的菜单按 `scope` 过滤掉它们 —— 「平台管理主要管账号」。声明仍留在平台侧，
因为面板注册表与批量端点都按动作 id 找它们。

| 平台 | 动作 ID | 含义 | scope |
| --- | --- | --- | --- |
| `chatgpt` | `probe_local_status` | 本地认证、订阅、Codex 状态探测 | — |
| `chatgpt` | `check_plus_trial` | 检测 Plus 试用状态 | — |
| `chatgpt` | `refresh_token` | 刷新 Access Token | — |
| `chatgpt` | `backfill_refresh_token` | 补 Refresh Token | — |
| `chatgpt` | `bind_2fa` | 绑定 TOTP 2FA | — |
| `chatgpt` | `sync_cliproxyapi_status` | 同步 CLIProxyAPI 状态 | `panel` |
| `chatgpt` | `upload_cpa` / `upload_sub2api` | 上传到外部系统；通常传 `api_url,api_key` | `panel` |
| `grok` | `probe` | 测活（CLI Proxy 发一次最小请求） | — |
| `grok` | `probe_refresh` | 检测有效性（refresh grant 换新凭证；`invalid_grant` 等永久错误判失效） | — |
| `grok` | `refresh_token` | 刷新 Token（**登录协议**：浏览器完成 Device Flow 授权） | — |
| `grok` | `refresh_oauth` | 已弃用（协议 device flow 被 CF 挡死）—— 转发到 `refresh_token` | — |
| `grok` | `export_cpa_json` | 导出 CPA JSON | — |
| `grok` | `upload_cpa` / `upload_sub2api` / `upload_grok2api` | 上传到外部系统 | `panel` |
| `icloud` | `fetch_inbox` | 收取隐私邮箱邮件，可传 `limit` | — |
| `icloud` | `delete_alias` | 删除隐私邮箱 | — |

---

## 6. 代理池 `/api/proxies`

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/proxies` | — | 列出代理 |
| POST | `/api/proxies` | `{url,region?}` | 新增单条代理；重复 URL 返回 `400` |
| POST | `/api/proxies/bulk` | `{proxies:["http://..."],region?}` | 批量新增，自动跳过空行和重复项 |
| DELETE | `/api/proxies/{proxy_id}` | — | 删除代理 |
| POST | `/api/proxies/batch-delete` | `{ids:[1,2]}` | 批量删除 |
| PATCH | `/api/proxies/{proxy_id}/toggle` | — | 切换启用状态，返回 `{is_active}` |
| POST | `/api/proxies/check` | — | 异步检查整个代理池 |

代理对象包含 `id,url,region,success_count,fail_count,is_active,last_checked`。

---

## 7. 全局配置与邮箱导入

### 8.1 配置 `/api/config`

| 方法 | 路径 | 请求体 / 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/config` | — | 返回白名单中的全局配置；未设置项为 `""` |
| PUT | `/api/config` | `{data:{"key":"value"}}` | 更新白名单内配置，返回实际更新的 `updated` 字段列表 |

配置白名单覆盖邮箱提供商、验证码、CPA/Sub2API/grok2api、iCloud 隐私邮箱（本地主号 `icloud_local_*`）、SMS、贡献服务等。未知 key 会被静默忽略。敏感配置（API Key、密码等）只应通过受控客户端提交。

> `mail_provider` 的已弃用值 `applemail`（小苹果 / AppleMail）在读写两侧都会收敛到
> `microsoft`，`mail_import_source` 里对应的视图收敛到 `outlook`。该渠道本身已删除。

> 前端导航：平台级集成（CPA / Sub2API / grok2api）的连接配置在「全局配置 → 面板配置」；「面板管理」只提供跳转与补传。两者读写本接口的同一份白名单配置。CLIProxyAPI 与「CPA 面板」是同一个服务，注册表里只有 `cpa` 一项（旧 key `cliproxyapi` 由 `resolve_panel_key` 归一）。

### 8.2 通用邮件导入 `/api/mail-imports`

| 方法 | 路径 | 请求体 / 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/mail-imports/providers` | — | 返回可用邮件导入提供商描述 |
| GET | `/api/mail-imports/snapshot` | `type`（必填）、`pool_dir?`,`pool_file?`,`preview_limit=100` | 获取对应邮箱池快照 |
| POST | `/api/mail-imports` | `MailImportExecuteRequest` | 执行指定类型的邮件导入 |
| POST | `/api/mail-imports/delete` | `MailImportDeleteRequest` | 删除一个导入项 |
| POST | `/api/mail-imports/batch-delete` | `MailImportBatchDeleteRequest` | 批量删除导入项 |

邮件导入具体字段由 `GET /api/mail-imports/providers` 返回的描述决定；至少应提供 `type`，导入操作一般还需 `content`。

### 8.3 Microsoft/Outlook 导入 `/api/outlook`

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/outlook/batch-import` | `{data,enabled:true}` | 批量导入 Outlook/Hotmail 邮箱 |
| GET | `/api/outlook/pool-summary` | — | 号池状态计数（未入池 / 可用 / 使用中 / 已使用 / 失败 / 总数） |
| POST | `/api/outlook/pool-status/import` | `{ids}` | 把勾选的账号从「未入池」转为可领取。只动 `unpooled` 的行 |

`data` 每行一条，使用 `----` 分隔，支持：

```text
邮箱----密码----client_id----refresh_token
邮箱----mailapi_url
```

返回 `total,success,failed,accounts,errors`。

---

## 8. 手机接码 `/api/sms`

| 方法 | 路径 | 请求体 / 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/sms/providers` | — | 返回接码平台、默认服务码和 OpenAI 纯 SMS 白名单国家 |
| GET | `/api/sms/country-options` | — | 返回国家 ID、中文名称和可用标记 |
| POST | `/api/sms/balance` | `{provider?,api_key?,service?,limit?}` | 查询余额；空字段回退到已保存配置 |
| POST | `/api/sms/countries` | `{provider?,api_key?,service?,limit?}` | 查询国家库存/价格排名，`limit` 范围 1–200 |

---

## 9. iCloud 隐私邮箱 `/api/icloud`

> 本节是 **iCloud 控制台页**用的本地直连接口：主号 Apple ID 登录、
> Cookie 导入、别名增删都由本项目自己跟 Apple 打交道。
>
> 页面位置是「邮箱服务 > iCloud 隐私邮箱（本地）」（`/mail/icloud`）。
> 它原先挂在「平台管理 > iCloud」（`/icloud`），现已移入邮箱服务 —— iCloud
> 本身就是邮箱来源，管理界面跟其它平台的通用账号列表不是一回事；旧的
> `/icloud` 路径保留为跳转，老书签不会失效。
>
> 别和历史上那个「邮箱导入（AppleMail / 小苹果）」搞混：那是 appleemail.top 的第三方
> 临时邮箱服务，与 iCloud 无关，已弃用删除。
>
> **注册任务的邮箱来源**：`icloud_local`（`modules/mail/icloud_local.py`）走的就是
> 本接口背后的 `services/icloud_service.py`，凭据在本机 `data/platforms/icloud.db`，
> 注册页只配 `icloud_local_account_id` / `icloud_local_label` / `icloud_local_note`。
> （历史上另有远程 `icloud_hme` 链路，已随服务删除。）

### 10.1 登录会话

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/icloud/login-sessions` | `{email,password,display_name?,region?,imap_host?,imap_port?,imap_username?,imap_password?}` | 创建 Apple ID 登录会话；可能返回待二次验证状态，完成时附带 `account` |
| GET | `/api/icloud/login-sessions/{login_id}` | — | 查询登录会话状态 |
| POST | `/api/icloud/login-sessions/{login_id}/verify` | `{code}` | 提交双重认证验证码 |
| POST | `/api/icloud/login-sessions/{login_id}/resend` | — | 重新发送验证码 |
| POST | `/api/icloud/login-sessions/{login_id}/sms` | `{phone_id,mode?}` | 请求向可信手机号发送短信验证码 |
| DELETE | `/api/icloud/login-sessions/{login_id}` | — | 取消登录会话 |

### 10.2 iCloud 主号

| 方法 | 路径 | 请求体 / 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/icloud/accounts` | — | 列出 iCloud 主号 |
| POST | `/api/icloud/accounts/import-cookie` | `{email?,display_name?,region?,cookie_header?,cookies_json?,imap_host?,imap_port?,imap_username?,imap_password?}` | 导入 iCloud Web Cookie 会话 |
| PATCH | `/api/icloud/accounts/{account_id}` | `{enabled}` | 启用或禁用主号 |
| DELETE | `/api/icloud/accounts/{account_id}` | — | 删除主号 |
| POST | `/api/icloud/accounts/{account_id}/sync` | — | 从 Apple 同步隐私邮箱别名 |
| GET | `/api/icloud/accounts/{account_id}/messages` | `limit=50,recipient?` | 拉取主号收件邮件；可按收件别名过滤 |

### 10.3 Hide My Email 别名

| 方法 | 路径 | 请求体 / 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/icloud/aliases` | `account_id?` | 列出隐私邮箱别名。每行带 `used_platforms`（池记账）与 `registered_platforms`（`accounts` 表里的权威注册证据，跨库查出来）；界面按后者显示「已注册平台」并做平台筛选 |
| POST | `/api/icloud/aliases` | `{account_id?,account_email?,label?,note?,count:1}` | 生成别名；`count` 范围 1–5 |
| POST | `/api/icloud/aliases/{alias_id}/pool-status` | `{pool_status}` | 手动改号池状态（`available` / `in_use` / `used`）。自动记账已接通（取号标 `in_use`、注册收尾记回），此端点留给人工干预。注意 `unpooled`（未入池）**不可经此设置**（会 400）—— 移出号池走 `/api/icloud/aliases/unpool` |
| POST | `/api/icloud/aliases/import-to-pool` | `{ids}` | 把选中的别名从「未入池」导入号池（→ 未使用）。生成/同步进来的默认未入池，取号会跳过 |
| POST | `/api/icloud/aliases/unpool` | `{ids}` | 把选中的别名移出号池（未使用 → 未入池）。导入的反向操作 |
| POST | `/api/icloud/aliases/{alias_id}/deactivate` | — | 停用隐私邮箱（不删除，可逆） |
| POST | `/api/icloud/aliases/{alias_id}/reactivate` | — | 重新激活已停用的隐私邮箱 |
| POST | `/api/icloud/aliases/batch-delete` | `{ids,remote:true}` | 批量删除别名；`remote` 决定是否同步删除 Apple 侧资源 |
| DELETE | `/api/icloud/aliases/{alias_id}` | `remote=true` | 删除一个别名 |
| GET | `/api/icloud/aliases/{alias_id}/messages` | `limit=50` | 拉取一个别名的邮件 |

iCloud 业务错误不使用 `401`，以免被前端误判为面板登录过期；凭据无效等错误通常为 `400`、`409` 或上游 `5xx`。

### 10.4 公开共享邮件页

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/m/{share_token}` | 返回某隐私邮箱最新一封邮件的 HTML 页面。该接口不在 `/api` 下、不出现在 OpenAPI Schema 中，且不需要面板 Token。不得公开或记录完整共享链接。 |

---

## 10. 集成服务 `/api/integrations`

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/integrations/panels` | — | 面板清单 + 当前地址 + **可跑的动作**（`platform` / `upload_action` / `sync_action`）；口令只回 `secret_set` 布尔，不回明文 |
| GET | `/api/integrations/panels/{key}/comparison` | — | 本地账号 ↔ 远端面板对比；`?refresh=1` 绕过缓存 |
| POST | `/api/integrations/panels/{key}/sync` | — | **更新本地凭证**：把远端较新的凭证拉回本地（覆盖 AT/RT/id_token，其它字段保留）；`?platform=chatgpt\|grok` 只处理该平台（多平台面板用，缺省全量） |
| POST | `/api/integrations/panels/{key}/push` | — | **更新远程凭证**：把「未上传 + 本地较新」的凭证推到远端；`?platform=` 同 sync，`?delete_old=true` 对新建式面板清理被替换的旧记录 |
| POST | `/api/integrations/backfill` | `{platforms?,account_ids?,pending_only?,status?,email?,plus_status?}` | 将筛选账号补传到已配置外部系统 |

> **面板管理页的动作**（用户要求把账号页那批操作集成进来）：上传 / 同步远端状态
> 不新增端点，复用 `POST /api/actions/{platform}/{action_id}/batch` ——
> 面板注册表声明每个面板对应哪个平台与哪个动作 id，界面按对比结果里的
> `local_id` 组 `account_ids` 发出去。四个面板都有 `sync_action`（CPA /
> Sub2API / grok2api / chatgpt2api）。
>
> **CPA 探活的状态词**（`services/cliproxyapi_sync.py`）：`usable`（探活
> 200）/ `payment_required`（402/403，没额度但号有效）/ `quota_exhausted`
> （429）/ `access_token_invalidated`（401）/ `account_deactivated`（封号）/
> `not_found`（CPA 里没有这个账号）/ `unreachable`（**CPA 连不上**：连接错误、
> 超时）/ `credential_error`（**CPA 可达但拒绝账号凭证**，如 400
> `auth token refresh failed` —— 那是账号 RT 已死，要重新登录，不是 CPA 故障）。
> `unreachable` 与 `credential_error` 分开报：混在一起排查方向会被带偏
> （实测 32 个 xai 账号里 21 个被误报成「无法连接」）。两者都算同步失败
> （`is_sync_ok`，三个调用点共用一份口径）。

> 历史：本节曾列出 7 个 `/api/integrations/services*` 端点（安装/启停/卸载本机
> 插件进程），随「本地插件管理」整块删除 —— 面板全部是远程服务，本应用不再
> 在本机 clone、编译或拉起任何面板进程。

`backfill` 返回 `{total,success,failed,skipped,items}`。默认目标平台是 `chatgpt`。

### 面板对比 `/api/integrations/panels/{key}/comparison`

`key` ∈ `cpa` / `sub2api` / `grok2api`（`cliproxyapi` 由 `resolve_panel_key` 归一到 `cpa`）。

返回：

```json
{
  "panel": "cpa",
  "fetched_at": "2026-10-01T22:36:00+00:00",
  "cached": false,
  "summary": {"local_only": 1, "remote_only": 2, "synced": 1, "local_newer": 1,
              "remote_newer": 0, "unknown_time": 0, "total": 5},
  "rows": [{"email": "a@b.c", "state": "credential_diff", "label": "凭证不同",
            "local_updated_at": "2026-10-01T21:00:00+00:00", "local_updated_hour": "2026-10-01T21:00Z",
            "remote_updated_at": "2026-10-01T19:00:00+00:00", "remote_updated_at_raw": "2026-10-02T03:00:00+08:00",
            "remote_updated_hour": "2026-10-01T19:00Z", "remote_status": "active",
            "credential_differences": ["access_token"], "credential_compared": 2,
            "time_relation": "local_newer", "time_basis": "credential",
            "local_credential_issued_at": "2026-10-01T21:00:00+00:00",
            "remote_credential_issued_at": "2026-10-01T19:00:00+00:00",
            "differences": [], "remote_extra": {}}],
  "remote_error": "",
  "local_count": 3,
  "remote_count": 4
}
```

口径要点：

- **时间判定分两层**（`time_relation`：local_newer / remote_newer / time_synced）：
  优先按**凭证签发时间**（JWT `iat`，取每侧最新签发的一个字段）比较 ——
  记录更新时间会被「同步远端状态」等回写操作 touch 成噪声（实测把 10 个远端
  较新的账号顶成本地较新，方向判反）；凭证解不出 iat 时回落记录更新时间，
  都按小时粒度比（同一小时内不算谁新）。行上的 `time_basis` 标明用了哪种
  （`credential` / `record`）。
- 两边时间都**归一成 UTC** 再给前端（远端原始串可能带 `+08:00`）；原始串留在
  `remote_updated_at_raw` 供 tooltip 显示。
- **界面上的「本地/远端更新时间」两列显示的是 AT 生成时间**（`local_credential_issued_at` /
  `remote_credential_issued_at`，JWT `iat` 每侧最新签发的一个字段）——
  记录更新时间会被回写操作 touch 成噪声，用户口径要的是「AT 生成的时间」；
  解不出 iat 时前端回落显示记录更新时间（`local_updated_at` / `remote_updated_at`）。
- `state=local_only` = 本地有、远端没有 = **未上传**；`remote_only` = 远端多出来的。
- **「是否同步」按凭证本体判定**（用户口径「AT、RT 这种全相同就是同步」）：
  两边都有值的 `access_token` / `refresh_token` / `session_token` / `id_token` /
  `sso` 全部相同 → `synced`；有任一不同 → `credential_diff`（列出字段）；
  远端接口不返回凭证 → `unknown_credential`（**不能当已同步**，那会给出错误的安全感）。
- 时间只作**辅助信息**（`time_relation`：local_newer / remote_newer / time_synced，
  依据见上行；`time_basis` 标明比较基准），凭证不同时用来说明是哪边动的，
  并给「更新本地 / 更新远程」的动作面做方向判定。
- **远端拉不到不是错误**：本地账号照常返回，`remote_error` 说明原因。
- 结果缓存 60 秒（`services/panel_comparison_cache.py`）；失败结果按 5 秒短 TTL
  （一次抖动不该被钉在界面上整整一分钟）；`?refresh=1` 绕过。

### 更新本地凭证 `POST /api/integrations/panels/{key}/sync`

把**远端较新**的凭证拉回本地（与「重新拉取对比」不同：那个只重拉对比表，
这个会改本地账号）。方向判定在 `services/panel_sync.py`：

- 远端较新且远端有凭证 → 拉回（覆盖 `access_token` / `refresh_token` /
  `id_token` / `sso`，本地其它字段保留）；
- 本地较新 / 同小时 / 无法判定时间 / 远端没有凭证 → 不动（保守，不拿不确定
  的数据覆盖本地）。

方向判定优先按**凭证签发时间**（JWT `iat`）；凭证解不出 iat 时回落记录更新
时间。原因：记录时间会被「同步远端状态」等回写操作 touch 成噪声（实测把
10 个远端较新的账号顶成本地较新，导致拉不回新鲜的 AT/RT）。

可选查询参数 `platform`（`chatgpt` / `grok`）：多平台面板（CPA）用 —— 界面上的
平台筛选只作用在前端，用户筛了 Grok 再点同步时后端必须按同一口径过滤，否则
ChatGPT 的凭证也会被一起拉回（「看到的」与「被改的」对不上）。缺省 = 全量。

返回 `{panel,total,pulled,skipped,items:[{email,platform,pulled,reason,fields}],
remote_error}`；`reason` ∈ `synced` / `local_newer` / `remote_newer` /
`unknown_time` / `remote_missing_credential` / `no_pullable_field`。

**为什么需要**：x.ai 的 RT 每次刷新都会轮换 —— 远端面板（grok2api / CPA）
刷新过 token 后，本地存的 RT 就成了死值（实测 22 个账号全部 `invalid_grant`），
拉回来才能继续用。

**时区**：远端面板服务器时区可能与本地不同（实测 grok2api / CPA 都是
`+08:00`，本地库写 UTC）。比较在服务端**先归一成 epoch**（`parse_timestamp`）；
界面上的时间列统一按**浏览器本地时区**显示，并在表头标注当前时区
（`UTC+8` 等）—— 见 `frontend/src/lib/time.ts`。

### 更新远程凭证 `POST /api/integrations/panels/{key}/push`

与「更新本地凭证」是一对**方向互斥**的动作：把「未上传 + 本地较新」的凭证
推到远端。方向判定在 `services/panel_push.py`：

- 远端没有（未上传）→ 推送（补传）；
- 凭证不同且**本地较新** → 推送；
- 远端较新 / 同小时 / 无法判定时间 / 凭证相同 / 无法比对 → 不动
  （推上去会用本地旧凭证覆盖远端新的）。

方向判定同样优先按**凭证签发时间**（JWT `iat`）—— 记录时间被状态回写 touch
后会判反（实测把本地旧凭证的行顶成「本地较新」，会把死凭证推上去）。

查询参数：

- `platform`（`chatgpt` / `grok`）：多平台面板用，同 `/sync`；
- `delete_old=true`：**新建式面板**（sub2api / chatgpt2api）推成功后删除被
  替换的旧远端记录 —— 它们的上传每次新增一条，不删会留重复。覆盖式面板
  （CPA / grok2api）的重传是原地更新，后端自动忽略该参数。

返回 `{panel,total,pushed,deleted,skipped,items:[{email,platform,push,reason,
remote_id,pushed,deleted,message,fields}],remote_error}`；`reason` ∈
`not_uploaded` / `local_newer` / `remote_newer` / `synced` / `unknown_time` /
`unknown_credential` / `no_pushable_field`。

---

## 11. 贡献服务代理 `/api/contribution`

这组接口会根据请求中的 `server_url` 或全局 `contribution_server_url` 转发到外部贡献服务器；未指定地址时使用项目默认地址。支持把 `key` 放在请求体中，空值时回退全局配置。

| 方法 | 路径 | 请求体 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/contribution/quota-stats` | `{server_url?,key?}` | 获取服务器额度统计；有 key 时额外尝试读取 key 信息 |
| POST | `/api/contribution/key-info` | `{server_url?,key?}` | 获取指定贡献 Key 信息；必须提供 Key |
| POST | `/api/contribution/redeem` | `{server_url?,key?,amount_usd}` | 按 USD 额度兑换，`amount_usd > 0` |
| POST | `/api/contribution/generate-key` | `{server_url?,name?}` | 向外部服务生成 Key |

服务端会对兼容性路径进行依次尝试，并在失败响应中携带尝试过的端点信息。该功能涉及外部服务和额度，请在受信任网络中调用。

---

## 12. Solver 健康与重启 `/api/solver`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/solver/status` | 返回 `{running:boolean}`，表示本地 Turnstile Solver 是否运行 |
| POST | `/api/solver/restart` | 停止后异步重新拉起 Solver，立即返回 `{"message":"重启中"}` |

---

## 13. 数据备份与迁移 `/api/backup`

一键导出/导入全部数据与配置（服务迁移用）。导出包含各库的 SQLite 一致性快照
（`VACUUM INTO`，不是直接拷运行中的文件）、凭据加密密钥、以及一份
`manifest.json`（格式版本 + 逐库 sha256 + 行数）。日志与历史备份不进包。

| 方法 | 路径 | 请求体 / 参数 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/backup/export` | — | 返回 ZIP 下载（`application/zip`） |
| GET | `/api/backup/info` | — | 导出前预览：各库大小/行数、是否有密钥、是否有运行中任务 |
| POST | `/api/backup/import` | ZIP 的**原始字节**（`Content-Type: application/zip`） | 校验后导入；落盘前自动备份当前数据 |

**导入语义**（细节见 `services/data_bundle.py` 的模块 docstring）：

1. **先校验后落盘** —— ZIP 结构、manifest、每个库的 sha256 全部通过才动现有数据。
2. **导入前自动备份**到 `data/import_backups/<时间戳>-<随机后缀>-import/`（失败或想回退都能捞）。
3. **导入后重载引擎**（`dispose` + `init_db`）并重置密钥缓存与面板对比缓存 ——
   无需重启进程即可读到新数据。
4. **有运行中任务时拒绝**（HTTP 409，提示先停任务）：导入会把任务脚下的库换掉。

请求体是原始字节而非 `multipart/form-data` —— 本项目刻意不引 `python-multipart`
依赖。安全上两个端点都在 `/api/` 下，与其它业务接口同一道认证门。

> 前端入口：**全局配置 → 数据迁移**（`DataBackupPanel`）。

---

## 14. OpenAPI 与前端对接建议

- FastAPI 会在服务运行后自动提供交互式文档：`/docs`，原始 Schema：`/openapi.json`。`/m/{share_token}` 因设置 `include_in_schema=False` 不在其中。
- 前端统一处理 `401` 时应检查 `X-Panel-Auth-Required: 1`，不要将 iCloud 上游的业务失败误跳转到登录页。
- 所有账号、导出、支付、Cookie、TOTP、Token、邮箱导入接口都可能承载敏感信息：建议禁用代理/访问日志中的请求体记录，并限制 API 的网络暴露范围。

### 14.1 运行态版本戳 `GET /api/runtime`

返回 `{code_version, disk_version, stale, booted_at, pid, python}`。

`code_version` 是**进程启动时加载的那版代码**，`disk_version` 是磁盘上此刻的 HEAD；两者不一致（`stale: true`）= 改完代码还没重启 —— 这个服务手工拉起、不重启就还跑旧代码。排查「修复没生效」时先看这里，不要假设代码已加载。
