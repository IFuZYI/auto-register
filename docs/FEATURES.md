# 功能详解

本文是 README 的展开篇：平台能力、邮箱服务、面板对接与导出格式的完整说明。
「怎么跑起来」看 [README 的部署方式](../README.md#部署方式)；
「改完代码还要动哪些文件」看 [MAINTENANCE.md](MAINTENANCE.md)。

## ChatGPT 专项能力

ChatGPT 是当前功能最完整的平台：支持注册、Token 生命周期管理、状态探测和外部系统同步。

### 1. 注册协议（纯协议，无需浏览器）

整条注册链路位于 `platforms/chatgpt/protocol/`：

- `http_client.py` —— 基于 `curl_cffi` 的 TLS 指纹会话
- `auth_flow.py` —— 驱动 OpenAI authorize 状态机（注册、OTP、add-phone、Codex OAuth）
- `sentinel_quickjs.py` + `openai_sentinel_quickjs.js` —— 在 Node 沙箱里跑 OpenAI 真实的 `sdk.js` 求解 Sentinel PoW

> ⚠️ **Sentinel 需要 Node 运行时。** 自己合成的 PoW token 能骗过 `/sentinel/req` 的表层校验，但发码服务会在服务端复核，结果是验证码邮件被静默丢弃 —— 链路看着一切正常却永远收不到码。所以必须有可执行的 `node`（>= 18），不在 `PATH` 里时用 `OPENAI_SENTINEL_NODE_PATH` 指定绝对路径。

协议层之上只有一组注入点：邮箱池适配器、手机接码控制器、密码生效回调；开启 2FA 绑定时还会再挂一个 session 就绪钩子。任务级配置通过实例参数下传，**不写进程环境变量**，因此多个注册任务并发时互不干扰。

### 2. Token 方案（有 RT / 无 RT）

| 方案 | 行为 | 产出 |
| --- | --- | --- |
| **有 RT**（默认推荐） | 完整跑一遍 Codex OAuth | Access Token + Refresh Token |
| **无 RT** | 跳过 Codex OAuth（每次省约 10 秒） | 仅 Access Token / Session；依赖 RT 的后续能力可能不可用 |

全局默认在「全局配置 → 注册设置」；注册任务页与 ChatGPT 注册弹窗都可临时覆盖（任务页的值随任务提交，优先级更高）。

### 3. 注册流程（邮箱 / 手机 / 手机 + 邮箱）

| 流程 | 行为 |
| --- | --- |
| **邮箱注册**（默认） | 从邮箱池领地址，收邮件验证码 |
| **手机注册** | 从接码平台租号，收短信验证码，不占用邮箱；账号以手机号为标识 |
| **手机注册 + 绑定邮箱** | 先用号码注册，再把邮箱池里的地址绑到账号上，收一次邮件验证码；绑定成功后账号按邮箱记账，手机号随账号保存（导出格式 `email_pw_2fa_phone` 可带出） |

后两种都要先在「全局配置 → 手机接码」启用接码并填好 API Key，否则任务直接报错。
绑定邮箱这一步只在 OpenAI 把 add-email 摆进当前 authorize 流程时才会被接受；
被拒时账号照常保留，失败原因记在账号记录里（`bind_email_error`）；暂无单独补绑的入口。

**关于「失败重试轮数」**：整条流程失败后自动重开一轮（换新邮箱 / 号码 / 会话），与接码层的「最多换号」是两回事。手机注册中「号已建好、接码平台一条短信都没收到」这类重开也没用的失败，连续出现两轮后会提前收手、不再消耗剩余轮次 —— 再开只会多几个没人认领的号。

### 4. TOTP 2FA 绑定

注册任务页与 ChatGPT 注册弹窗上都有「绑定 2FA」开关，**默认关闭**。打开后注册成功的号会顺手绑一个 TOTP 双因素：

- **快路径**：复用注册那条会话直接申请密钥并激活。注册链几十秒前才做完验证，服务端认这是「最近认证过」，所以不用重新登录、不用再收邮件，几秒钟完事。
- **慢路径**：快路径没成时才走，用邮箱 + 密码重跑一遍登录正式链再绑，要多花一次 PoW，多半还要收一封验证码邮件。手机号身份的号没有邮箱可登，会跳过这一步。

密钥随号落库（写进账号 `extra` 的 `totp_secret`），三个地方都能复制导入验证器 App：
账号列表的「2FA 已绑」标签、账号操作菜单的「复制 2FA 密钥」、账号详情的密钥栏。
批量拿密钥用导出格式 `email_pw_2fa` 之类。

库里的老号可在账号操作菜单点「绑定 2FA」单独补绑，同样先试复用会话、会话失效再走重新登录。
这个动作和「补 RT」一样跑成后台任务：点完立刻弹出日志窗口，实时看到走哪条路、
是不是在等验证码，也能中途停掉。

> ⚠️ 密钥只在绑定那一刻由服务端下发一次，任何接口都取不回。绑定即生效，之后该号每次登录都要动态码 —— 补 RT、重新登录这些链路会自动用库里的密钥算码通过，但**密钥丢了这个号就再也登不进去**。

### 5. 账号操作与批量能力

**账号页（单账号 + 批量）**：探测本地状态、检测 Plus 试用、刷新 Token、补 RT、绑定 2FA、
复制 2FA 密钥。批量入口按「所选」或「当前筛选范围」作用。

**补 RT** 的语义：先用库里的会话直接换 `refresh_token`；会话失效时再用邮箱密码重新登录
（可能需要收一封验证码）。跑成后台任务，日志弹窗实时显示。

**刷新 Token** 的语义：走 session token → OAuth RT → 登录链拿 AT，并用 `/backend-api/me`
真校验（校验不过不写库）。**校验「AT 是否真的换发」**：只返还原本的 AT（未换发）不算
刷新成功 —— 会继续走登录流程换发新 AT；界面如实区分「AT 已换发」与「无需换发」。
批量入口在「更多」菜单，选号规则与补 RT 相同。

**状态语义**（探测 / 刷新 / 同步共用一套判定）：

| 状态 | 含义 | 判定来源 |
| --- | --- | --- |
| **正常** | 正常能使用的账号 | 正向确认（探测可用 / 刷新成功 / 远端 usable） |
| **过期** | AT 已过期（JWT exp 已过） | 凭证被拒且 AT 已过期；刷新可能救回 |
| **失效** | 需要重新登录的账号 | 凭证被拒但 AT 未过期 / 缺凭证；走流程登录 |
| **禁用** | 被封了的账号 | 登录链发掘（`deleted or deactivated` 这类「号没了」措辞）；不自动恢复。会话/state 类错误（`invalid_state`）**不算封禁**，可重试。登录链任意步骤（密码校验、TOTP 提交、发码）撞上封禁措辞都当场终止、不被探测回退吞掉 |

判定优先级：**禁用 > 过期 > 失效**。正向确认才恢复「正常」——禁用是强判断，不因一次可用探测复活；且**禁用是粘性的**：弱信号（401 / 缺凭证 / 远端失效）不得把它降级回过期/失效（要人工确认才解除），否则刚发掘出的封禁会被下一次状态同步洗掉。禁用账号不参与面板同步（更新本地 / 更新远程都跳过）。

**面板动作**（上传 CPA / 上传 Sub2API / 上传 chatgpt2api / 同步 CLIProxyAPI 状态）
已从账号页移到「面板管理」页 —— 目标是外部面板，与账号自身状态不是一回事。
账号页菜单按 `scope` 过滤掉它们（见 `platforms/chatgpt/plugin.py` 的 `get_platform_actions`）。

### 6. 自动维护与任务可见性

**自动刷新 Token**（配置开关 `chatgpt_auto_refresh_enabled`）：后台定期扫描 ChatGPT
账号，对 AT 临期（剩余 ≤ 24h）或已过期的账号，在临期窗口内**随机**一个时刻刷新
（不设固定更新时刻；窗口上限 = 到期前 1 小时）。封禁的、连续失败 3 次的不重复尝试。
每轮限量执行、串行 + 间隔，避免高并发。

**chatgpt2api 凭证自动同步**（`chatgpt2api_auto_sync_enabled`）：定期对比本地与远端
凭证，本地较新时自动推送（复用面板推送管线），使远端始终持有最新凭证。

**任务可见性**：两趟维护在**有实际动作**时创建任务记录（`auto_refresh` /
`chatgpt2api_sync`），出现在「任务运行」页 —— 日志逐账号可回看、支持停止/跳过；
空轮不建记录（每 5/10 分钟一条空任务会把列表淹掉）。任务类型以彩色标签区分：
注册（蓝）、补 RT（橙）、绑 2FA（紫）、刷新 Token（绿）、自动刷新（青）、
凭证同步（品红）。

### 7. 设备标识（oai-did）复用

ChatGPT 对每个浏览器会话发一个设备标识 `oai-did`（cookie + `ext-oai-did`
参数），注册时落库到账号 `extra.device_id`。**同一账号后续登录沿用同一个
设备标识**（用户要求 2026-10-07「指纹能复用吗，能不能减少后续登录封号的风险」）：

- 三条登录链（刷新 Token 的登录兜底、补 RT 的协议重登、绑 2FA 慢路径）
  都经 `AuthFlow.seed_device_id` 预置库里的 device_id；
- 实测服务端**保留**客户端预置的 oai-did（预置 A → GET chatgpt.com 返回
  还是 A）；预置后 check_proxy → warmup → get_auth_url → auth_oauth_init
  整条链保持同一标识；
- 没有存量 device_id 的账号（注册早于该字段落库）：首次登录拿服务端分配值
  并**落库收敛**，此后每次登录都复用 —— 不再每次登录换一台「新设备」；
- 预置值只在 warmup **成功后**写进 cookie（提前种会让 warmup 的「服务端是否
  种上」判据永远为真、CF 403 被误报成功）；浏览器指纹本身（TLS/UA/CH）仍
  逐会话随机，不跨会话复用。

## Grok 专项能力

Grok 注册**只有浏览器一条路径**：x.ai 的 Cloudflare 只有 camoufox 能过，
协议层的发码是「假接受」（gRPC `grpc-status:0` / REST `ok:true` 但零投递）。

| 环节 | 实测结论 |
| --- | --- |
| 浏览器引擎 | **camoufox 是唯一能过 x.ai CF 的**；chromium 无论直连还是住宅代理，提交发码都被 403 |
| 发码 | 必须走真实页面（协议「假接受」） |
| 域名 | x.ai 接受主流域（@icloud.com 实测通过）；拒绝小域名 |
| 验证码 | 页面会自动格式化（`955-995` → `955995`），用键盘逐字输入才稳 |
| 提交按钮 | 是 `type=submit` 且**无文本**，按 Enter 最可靠 |
| OAuth | 协议 device flow 的 approve 被 CF 403，**必须走浏览器兜底** |

**发码节流**（`grok_send_code_min_interval`）：x.ai 对同一 IP 的取码有频率限制，太密会把
出口打进长冷却。界面在「注册设置 → 各平台设置 → Grok」卡片里渲染。

**账号操作**：测活（CLI Proxy）、重换 OAuth、导出 CPA JSON；面板动作同 ChatGPT
（上传 CPA / 同步 CPA 状态 / 上传 Sub2API / 上传 grok2api）。

**grok2api 接入**（`platforms/grok/grok2api.py`）：上传 SSO（Web）→ 开启 NSFW。
只调 grok2api 现成的管理 API。Console / Build 两类凭据**不在这里派生** ——
用户自己在 grok2api 里手动转换。几个关键点：

- 上传文件名**必须是** `grok-web-sso-tokens.txt`（grok2api 靠文件名识别 token 类型）
- 账号级动作（条款/生日/NSFW）只对 Web 账号有效，必须先按邮箱找到 `provider=grok_web` 那条
- NSFW 顺序不能换：`accept-terms` → `birth-date` → `nsfw`
- 管理面要**用户名+密码换 token**（不是固定 API Key），有效期约 10 分钟

## 邮箱服务

邮箱服务是**独立的一级菜单**，二级按「邮箱从哪来」分：

| 二级菜单 | 管什么 | 数据在哪 |
| --- | --- | --- |
| iCloud 隐私邮箱（本地） | iCloud 主号登录（SRP / Cookie）、Hide My Email 别名生成/停用、收件箱 | `data/platforms/icloud.db` |
| Outlook（本地） | 微软号池导入、收信方式（Graph / IMAP） | `data/platforms/outlook.db` |

两个号池**各自单独一个数据库文件**：它们是「邮箱来源」而不是注册平台，被多个注册流程共用，
单独成库才能一个文件备份、一个文件清空。详见 [EXTENDING.md 第 6.1 节](EXTENDING.md)。

### 注册页的邮箱服务选项

| 选项 | 标识 | 取号范围 |
| --- | --- | --- |
| Outlook（微软号池） | `microsoft` | 见下方「导入类型」 |
| iCloud 隐私邮箱（本地主号） | `icloud_local` | 从已启用的 iCloud 主号生成隐私邮箱 |

**导入类型**（仅微软号池）：Outlook / Hotmail / MailAPI URL 三类共用同一个微软号池，
三类账号互不顶替 —— 选 MailAPI URL 只会取 `account_type=mailapi_url` 的账号，
选 Outlook / Hotmail 只会取 OAuth 账号。

**微软收信方式**：默认 Graph；没有 OAuth 凭据的账号运行时自动回退 IMAP。

### iCloud 隐私邮箱说明

**使用流程**：

1. 在「iCloud 隐私邮箱（本地）→ 主号管理」添加 Apple ID 主号：
   - **账号密码登录**：走 Apple SRP 协议，按需完成双重认证（可信设备推送或短信验证码）
   - **Cookie 导入**：直接粘贴浏览器导出的 iCloud Cookie
2. 登录成功后在「隐私邮箱」标签页批量生成别名。Apple 侧限制为**每主号每小时最多 5 个**，
   系统按主号维度自行限流。
3. 主号的 IMAP 密码（应用专用密码）用于拉取别名收件箱；未配置时回退 Web API（只有摘要）。

凭据均以 **AES-256-GCM** 加密后落库。加密密钥取自环境变量 `CREDENTIAL_ENCRYPTION_KEY`；
未设置时在 `data/secrets/credential_key` 自动生成一份本地密钥。

### 号池的两步流程（重要）

**粘贴邮箱只是把它们存进号池（状态「未入池」），注册任务不会取用。**
在预览表里勾选要用的邮箱，点「导入邮箱池」之后它们才可被领取。

- 「未入池」（`unpooled`）的地址注册取号会跳过，不会并进「可领」
- 取号优先复用「未使用」的地址
- 重复点「导入邮箱池」是幂等的：已在用的号不会被改回可领取
  （否则同一个号会被两个任务同时领走，两边互相顶掉验证码邮件）

## 面板对接

本应用**不安装、不启动**任何面板。面板都是独立部署的远程服务，这里只负责：

- 在「面板管理」页存它们的地址，提供跳转入口与 GitHub 项目主页直达
- 选中面板后展示**本地 ↔ 远程账号对比**：哪些本地号还没上传、凭证是否一致
  （AT/RT 全相同 = 已同步）、远端多出来的账号；带缓存
- **多平台面板支持按平台筛选**（CPA 同时托管 ChatGPT 与 Grok）：表头上方一个
  「全部 / ChatGPT / Grok」选择器，带各自的账号数；选中的平台**同时**作用于
  表格、状态筛选计数与批量动作 —— 筛了 Grok 点上传，传的就是 Grok 那批，
  不会把 ChatGPT 的一起带上。「更新本地/远程凭证」也按当前平台筛选
  （后端 `?platform=`）
- **对比页上直接操作**（每个面板三个动作，方向严格分开）：
  **同步远端状态**读远端状态回写本地（含封禁/失效判定）；**更新远程凭证**把
  「未上传 + 本地较新」的凭证推到远端（新建式面板会清理被替换的旧记录）；
  **更新本地凭证**把远端较新的凭证拉回本地。凭证不同的行只按 `time_relation`
  走对应方向（本地较新 → 推；远端较新 → 拉；两侧时间相同（`time_synced`）或
  无法判定 → 都不动），绝不拿旧凭证覆盖新的；谁更新**优先按凭证签发时间
  （JWT `iat`）**判定 —— 记录时间会被状态回写操作顶成噪声。**禁用（banned）
  的账号不参与同步**：更新本地 / 更新远程都跳过（凭证已死，推上去污染远端、
  拉回来也救不活），按钮计数同步排除
- 把注册好的账号推过去（`services/external_sync.py`）

| 面板 | 用途 | 项目主页 |
| --- | --- | --- |
| CPA 面板 | ChatGPT 账号上传与状态同步（CLIProxyAPI 的 `/v0/management/auth-files`） | <https://github.com/router-for-me/CLIProxyAPI> |
| Sub2API | 账号上传到 Sub2API 后台 | <https://github.com/Wei-Shaw/sub2api> |
| grok2api | Grok 账号池与 API 网关 | <https://github.com/chenyme/grok2api> |
| chatgpt2api | ChatGPT 网页号池（只认 `access_token`） | <https://github.com/yukkcat/chatgpt2api> |

**对比的匹配与判定**：匹配键是 **(平台, 邮箱)**（CPA 同时托管 ChatGPT 的 `codex` 与
Grok 的 `xai` 两类凭据）；主判据是**凭证本体是否相同**（AT/RT/session/id_token/sso），
时间（优先凭证签发时间 JWT `iat`，两侧可比时秒级；回落记录时间时小时档）降为辅助信息。

**上传开关按平台分开**：CPA 的 ChatGPT 与 Grok 各自控制（`cpa_upload_chatgpt_enabled` /
`cpa_upload_grok_enabled`）；其余面板各有自己的 `*_enabled` 开关。

> **chatgpt2api 有两个同名项目**：`yukkcat/chatgpt2api`（v3.2.3）与
> `basketikun/chatgpt2api`（v1.8.0），上传器同一份代码两边都能用，但**读回账号状态
> 的字段形状不兼容**，本项目按 yukkcat 版的接口对接（列表不带 token、凭证走 export）。

**新增一个面板要同步注册几处**（漏一处症状都很难查）：
`services/panel_comparison.py` 的 `FETCHERS`（漏了报「未知面板」404）、
`services/panel_comparison_cache.py` 的 `_PANEL_PLATFORMS` 与 `_panel_credentials`、
平台插件的 `get_platform_actions`（`scope="panel"`）与 `api/actions.py::_apply_action_result`、
配置键白名单（`api/config.py`）+ 前端 `PanelConfigPanel.tsx` 表单。清单也写在
`services/panel_registry.py` 的模块 docstring 里。

## 导出格式

账号列表右上角「导出」打开导出弹窗：先选范围（勾选的账号 / 当前筛选出的全部账号，
翻页不影响），再选格式，右边直接出预览，然后一键复制或下载。

| 格式 | 一行长什么样 |
| --- | --- |
| `email_pw` | `邮箱----密码` |
| `email_pw_2fa`（默认） | `邮箱----密码----2FA 密钥` |
| `email_pw_2fa_at` | 再接 `----AccessToken` |
| `email_pw_2fa_rt` | 再接 `----RefreshToken` |
| `email_pw_2fa_at_rt` | 登录与调用凭证一次带齐 |
| `email_pw_2fa_phone` | 再接 `----手机号` |
| `email_pw_rt` | `邮箱----密码----RefreshToken` |
| `email_2fa` | `邮箱----2FA 密钥` |
| `at` / `rt` / `totp` | 一行一个 token，没有该字段的账号自动跳过 |
| `csv` / `json` | 全字段，给 Excel 或脚本用 |

**空字段照样占位**（`a@b.com----pw----` 结尾那个分隔符不会省），按 `----` 切列的脚本不会错位。
格式清单由后端 `services/account_export.py` 的 `EXPORT_FORMATS` 统一定义，加一个新格式
只改这一处，前端下拉框自动跟上。

## 核心概念

| 概念 | 说明 |
| --- | --- |
| **平台插件** | 一个平台 = 一个 `BasePlatform` 子类（`platforms/<name>/plugin.py`），启动时自注册。新增平台见 [EXTENDING.md](EXTENDING.md) |
| **执行器** | 注册执行方式。**每个平台声明自己支持哪些**，没有全局默认 —— ChatGPT 只有 `protocol`，Grok 只有 `browser` |
| **邮箱渠道** | 可插拔收件来源，统一 `BaseMailbox` 接口。**邮箱按平台消耗** —— 同一地址注册过 ChatGPT 仍可注册 Grok |
| **号池状态** | `unpooled`（未入池）/ `available`（可领）/ `in_use`（使用中）/ `used`（已用）/ `failed`（失败） |
| **任务** | 一次注册作业。支持协作式停止 / 跳过，靠各层主动调用 `checkpoint()` 生效 |
| **面板** | 注册产物的下游消费方（CPA / Sub2API / grok2api / chatgpt2api），**全部是独立部署的远程服务**，本应用不安装、不启动它们 |
