# 面板的方向语义

推送 / 拉回 / 登记是三个不同动作。历史上都叫「同步」，是排查时最容易走错方向的混淆源。

## 重构约束

- 新增面板必须走 `PanelAdapter`（阶段 3 T3.3）
- 不许再往 `FETCHERS` 里加分支
- 方向由用户点，不自动判定；不确定 → 不动

## 证据（25 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `api/actions.py:68-74`

~~~
#: 面板凭据上传类动作 —— 禁用（banned）账号一律跳过（用户要求 2026-10-07
#: 「禁用的不参与同步」「已封禁的没必要在面板同步凭据」）。
#:
#: 对比页的两个方向（`panel_push.plan_push` / `panel_sync.plan_sync`）已排除
#: banned；动作端点（批量/单账号）是另一条路径，不排除的话直调仍会把死凭证
#: 推给远端（远端拿死凭证刷 token、污染号池）。集合覆盖 ChatGPT 与 Grok 的
#: 全部上传动作 —— 新增上传动作时这里要同步加。
~~~

### `core/credential_fields.py:1-34`

~~~
凭证字段注册表 —— 一处定义，处处消费。

背景（用户要求「有 sso_token AT RT 这些 请好好思考、整理数据结构」）

整理前，凭证字段表散落在六处、各自维护、已经漂移：

- `services/panel_comparison.py` 的 `CREDENTIAL_FIELDS`（5 字段，参与比对）
- `services/panel_sync.py` 的 `_PULLABLE_FIELDS`（4 字段，漏了 session_token）
- `services/panel_push.py` 的 `_PUSHABLE_FIELDS`（4 字段，同样漏）
- `services/account_export.py` 的导出字段（漏了 sso —— grok 账号 JSON
  导出→导入会丢 SSO，实测真 bug）
- `api/accounts.py` 的 `_IMPORT_EXTRA_KEYS`（漏了 sso，同上）
- `api/actions.py` 的 `tracked_keys`（手写集合，混着 camelCase 拼写与
  `clientId` / `clientSecret` / `webAccessToken` 三个**零生产方的死键**）

再加上 `token` 列语义混乱：grok 注册写 SSO，AT 刷新路径又把它盖成 AT
（线上实测 32 行里 12 行是 AT）——读侧的 `or account.token` 兜底会拿错值。

本模块把这些收敛成一处：

- `CREDENTIAL_FIELDS` —— 规范名 + 别名 + 短标签（每个字段认蛇形与 camelCase）；
- `compare_aliases()` / `sync_aliases()` —— 比对/同步用的别名表；
- `export_names()` —— 导出用的规范名清单；
- `first_present()` / `get_credential()` / `canonical_writes()` —— 读写助手；
- `token_column_field()` / `sync_token_column()` —— `token` 列的镜像规则
  （token 列 = 平台主凭证的镜像：chatgpt → AT，grok → SSO）。

**同步范围与对比范围当前一致**（`session_token` 也纳入同步）：整理前
session_token 只参与对比不参与同步，那种「能比出来却没法同步」的半截状态
说不清是故意还是漏了。统一规则：凭证字段全量参与对比与同步；面板实际消费
哪些由各上传器决定。

改动本文件 = 改全系统凭证口径。字段增删时跟着跑 `tests/test_credential_fields.py`。

~~~

### `core/panel_tls.py:1-16`

~~~
面板请求的 TLS 校验策略。

安全审查（2026-10-27）F3：此前 9 处面板客户端**全部硬编码 `verify=False`**，
涵盖 CPA / Sub2API / chatgpt2api / grok2api / cliproxyapi 与面板对比。

后果：本地与面板之间若有中间人（同机房、被劫持的路由、恶意代理），攻击者
出示任意证书即可收到面板 API Key **以及**推送的全部凭据（AT / RT / id_token /
sso，chatgpt2api 还含 password + totp_secret）—— 证书校验这个控制被完全绕过。

**默认校验**。自签名面板（内网自建）由用户显式二选一：

- `panel_tls_ca_bundle` = CA 文件路径 —— 推荐，仍然校验，只是信任自签 CA；
- `panel_tls_insecure` = `1` —— 完全关闭校验，只在临时排障时用。

把「不安全」变成**显式选择**而不是默认值，是这个模块存在的全部理由。

~~~

### `platforms/chatgpt/cpa_upload.py:166-170`

~~~
    # 凭据**优先从 `extra` 取**：本仓的 `core.base_platform.Account` 把凭据放在
    # `extra` 字典里，对象上根本没有 `access_token` / `refresh_token` 这些属性。
    # 只读属性会让面板推送那两条路径（`api/integrations.py` 构造账号时只给
    # `token=AT`）产出**空凭据**上传给对端 —— 实测复现过：AT/RT/IDT 全是空串。
    # 属性兜底保留：协议层另有调用方是按属性挂的（如 grok 的同步账号）。
~~~

### `services/chatgpt_maintenance.py:706-715`

~~~
跑一轮 chatgpt2api 凭证自动同步（本地较新 → 推送远端）。

    复用 `POST /api/integrations/panels/chatgpt2api/push` 的完整管线
    （方向判定 + 推送 + 旧记录清理 + 落库），`delete_old=true` ——
    chatgpt2api 是新建式面板，不删旧记录会留重复。

    **有实际推送动作**时创建任务记录（source=`chatgpt2api_sync`）——
    「任务运行」页可见、日志可回看（用户要求 2026-10-07）；纯空转 /
    远端读取失败（下一轮会重试）不建记录。
    
~~~

### `services/chatgpt_sync.py:105-110`

~~~
    # 顺带登记该面板：上传成功才算（用户要求「只有筛选上传的账号才参与同步」，
    # 见 `docs/adr/0002-panel-enrollment-and-bidirectional-sync.md`）。
    #
    # 为什么接在这里：自动上传、面板推送、单账号动作三条路径都调用本函数，
    # 一处接入即全覆盖。`sync_statuses.uploaded` 是粘性布尔、区分不了撤销，
    # 所以登记另存一份（`panel_uploads`），两者各司其职。
~~~

### `services/external_sync.py:26-32`

~~~
#: 平台 → 「CPA 自动上传」开关的配置键。
#:
#: CPA 自动上传的平台开关。
#:
#: **只有 chatgpt**：Grok 自 2026-10-27 起不再支持 CPA（只保留 SSO + grok2api），
#: `cpa_upload_grok_enabled` 随之删除 —— 留着会变成一个「界面能调、实际没人读」
#: 的死旋钮。
~~~

### `services/external_sync.py:62-64`

~~~
    # **不在表里的平台恒为 False**：Grok 自 2026-10-27 起不再支持 CPA。
    # 不拦的话它会落到下面的历史开关 / 地址兜底 —— 老配置里 `cpa_enabled=1`
    # 或「填了地址」就会让一个已删除的上传路径复活（实测断言抓到）。
~~~

### `services/panel_comparison.py:1-25`

~~~
本地账号 ↔ 远程面板的对比。

面板管理页选中一个面板后展示：本地有哪些账号、远端有哪些账号、哪些本地号
还没上传、**凭证是否同步**。核心判定有两层：

1. **凭证是否相同（主判据）** —— 用户要求：「本地和远端同步是判断 AT 这些是否
   相同，AT、RT 这种全相同就是同步」。所以两边都有账号时，比的是
   `access_token` / `refresh_token` / `session_token` 这些**凭证本体**：
   全相同 → `synced`（已同步）；有任一不同 → `credential_diff`（凭证不同）。
2. **谁更新（辅助信息）** —— 凭证不同时，再用时间戳说清是哪边动的（按小时比，
   分秒是噪声）。时间只作展示与排序，不再单独决定「同步」与否。
   **优先按凭证签发时间**（JWT `iat`，`compare_credential_time`）—— 记录
   更新时间会被「同步远端状态」等回写操作 touch 成噪声（实测把 10 个远端较新
   的账号顶成本地较新，方向判反）；凭证解不出 iat 时回落记录时间
   （`compare_by_hour`），行上的 `time_basis` 标明用了哪种。

- 两边的时间戳格式不统一（远端 `last_refresh` 是 `+08:00` 带偏移的字符串，
  本地 `updated_at` 是 UTC datetime），必须先归一成 epoch 再比。
- 远端没有账号 = 没上传；本地有远端也有 = 已上传，再看凭证是否一致。
- 远端有本地没有 = 远端多出来的（别人传的、或本地删过），单独一档。
- 凭证比不了（远端接口不返回 AT/RT）→ `unknown_credential`，说清是比不了
  而不是「一致」—— 把「读不到」当成「相同」会给出错误的安全感。

结果带缓存：远端接口是外网调用，面板管理页每次渲染都打一遍既慢又容易被限流。

~~~

### `services/panel_comparison.py:69-76`

~~~
把两边的各种时间写法解析成带时区的 datetime。

    认得的形态（都是实测见过的）：
    - ISO 8601 带偏移：`2026-03-31T12:00:00+08:00`
    - ISO 8601 带 Z：`2026-03-31T04:00:00Z`
    - 不带时区的 ISO：按 UTC 处理（本应用自己写的时间都是 UTC）
    - epoch 秒 / 毫秒（数字或数字字符串）
    
~~~

### `services/panel_comparison.py:161-168`

~~~
按**小时**粒度比先后（用户要求：不管分秒）。

    返回 `local_newer` / `remote_newer` / `time_synced` / `unknown_time`。
    同一小时内 → `time_synced`：两边各动过一次的话，分秒差异是噪声，报谁新都是误导。

    这个结论现在只作**辅助信息**（凭证不同时用来说明是哪边动的）与排序用；
    「是否同步」由 `compare_credentials` 决定。
    
~~~

### `services/panel_comparison.py:209-236`

~~~
按「每侧最有信息量的时间」比先后 —— iat 优先，无则回落该侧记录时间。

    返回 `local_newer` / `remote_newer` / `time_synced` / `""`（无结论）。
    至少一侧连记录时间都取不到时返回空串：调用方按「无法判定 → 不动」处理。

    **粒度分两档**（用户实测的「同步了还是凭证不同」死锁的修复，2026-10-05）：

    - 两侧都能解出 iat → **秒级**比：iat 是签发事实，后签发的持有活凭证
      （x.ai 的 RT 每次刷新都轮换）。旧实现按小时比，「同小时 → time_synced
      → 两个方向都跳过」—— 同一小时内的真实轮换会永远卡在「凭证不同」，
      点多少次「更新本地」都不拉（实测：本地 05:10:31 vs 远端 05:14:24）。
    - 任一侧回落记录时间 → **小时级**（`compare_by_hour`）：记录时间会被
      「同步远端状态」等回写操作 touch 成噪声，分秒差异不可信（用户口径
      「以小时为单位，即不管分秒」）。两侧时间完全相同才判 `time_synced`。

    为什么需要它（实测 2026-10-05）：grok2api 面板 10 个账号的**行更新时间**
    被「同步远端状态」顶到了本地较新（状态回写会 touch `updated_at`），而
    凭证实际是远端新（本地 AT 已过期、远端是刚签发的）—— 只看记录时间会把
    方向判反：「更新本地」不动、「更新远程」反而拿本地旧凭证去覆盖远端新的。

    **单侧回落**（用户实测报的 bug，2026-10-05）：grok2api 的 web 线账号远端
    只有 SSO（解不出 iat）而本地有 AT。旧实现要求两侧都能解出 iat 才出结论、
    否则**整体**回落记录时间 —— 本地记录时间恰被状态同步顶成噪声，于是
    「本地 10-02（iat）vs 远端 10-04（记录）」的行被判成「本地较新」（拿
    10-05 的噪声记录时间判的），与两列显示自相矛盾。现在**每侧独立**
    「iat 优先、无则回落该侧记录时间」，与「本地/远端更新时间」两列的显示
    口径完全一致 —— 显示什么就比什么。
    
~~~

### `services/panel_comparison.py:289-298`

~~~
    #: 这个远端账号属于哪个平台（`chatgpt` / `grok`）。
    #:
    #: CPA 面板同时托管两类凭据（codex / xai），而**同一个邮箱可能两边都有**
    #: （本项目的邮箱池按平台消耗：一个地址能注册 ChatGPT 也能注册 Grok）。
    #: 只按邮箱匹配会把两条不同的账号压成一行，所以匹配键是 (平台, 邮箱)。
    #:
    #: **fetcher 必须给行打上平台**（本地行永远带平台，见
    #: `_local_accounts_for_panel`）：留空的话键退化成纯邮箱，与本地行对不上，
    #: 整个面板会显示成「全部未上传 + 全部仅远端」—— 看起来像功能坏了。
    #: grok2api 面板实测踩过：22 个本地账号全部显示未上传，而两边邮箱 100% 重合。
~~~

### `services/panel_comparison.py:450-462`

~~~
本地账号里处于「禁用」的 (平台, 邮箱) 匹配键集合。

    用户要求（2026-10-10）：「封禁账号不参与凭据同步与对比」。
    禁用账号的凭证已死 —— 对比里显示「未上传 / 凭证不同」只会给出错误的
    待办提示（推送/拉回两个方向都会跳过它们），所以对比时**两侧一起排除**：

    - 本地行不出现在对比结果里；
    - 远端对应记录一并排除 —— 只排本地的话那条远端记录会变成「仅远端」
      行，看起来像别人传的号，同样误导。

    同步（`panel_push.plan_push` / `panel_sync.plan_sync`）各自在 plan 层
    跳过禁用账号（`reason=banned`），与此处互为防线。
    
~~~

### `services/panel_comparison.py:481-491`

~~~
把两边的账号列表对照起来，逐 (平台, 邮箱) 产出一行。

    `local_accounts` 每项：`{id, email, platform, status, updated_at, extra}`。
    匹配键是 (平台, 邮箱)：邮箱按小写归一（两边大小写都可能不一致），
    平台参与匹配是因为 CPA 上同一个邮箱可能同时有 ChatGPT 与 Grok 两条凭据。
    平台为空的老数据退化成只按邮箱（单平台面板与历史调用方不受影响）。

    **禁用（banned）账号不参与对比**（用户要求 2026-10-10）：本地禁用行与
    对应的远端记录都不出现在结果里（理由见 `banned_local_keys`）——「未上传 /
    凭证不同」这类状态对死凭证没有意义，两个同步方向也都会跳过它们。
    
~~~

### `services/panel_comparison.py:549-552`

~~~
        # 辅助信息：凭证不同时，时间说明是哪边动的（按小时）。
        # 每侧独立取「iat 优先、无则回落该侧记录时间」—— 与「本地/远端
        # 更新时间」两列的显示口径一致（显示什么就比什么）。整体回落记录
        # 时间会被状态回写的噪声带反（用户实测报过的 bug）。
~~~

### `services/panel_comparison.py:615-620`

~~~
#: 一次对比最多为多少个账号抓完整凭证。
#:
#: 列表接口不返回凭证，得逐个走 download（实测 ~94ms/次）。只对**本地也有的**
#: 账号抓（那些才是要比的），再设个上限兜底 —— 本地几千号时不至于把一次刷新
#: 拖成几分钟。超出的账号凭证留空 → 判 `unknown_credential`（说清"没比"），
#: 不会误报成「已同步」。
~~~

### `services/panel_comparison.py:730-736`

~~~
从 Sub2API 的列表响应里挖出账号数组。

    响应形状不统一（实测 + 参考实现都见过）：
    - `{code:0, data:{items:[...]}}`（Sub2API 约定）
    - `{items:[...]}` / `{data:[...]}` / `{records:[...]}` / `{list:[...]}`
    逐层找数组，比写死一条路径稳。抄的是 reg-factory `extractItems` 的口径。
    
~~~

### `services/panel_comparison.py:764-769`

~~~
Sub2API：`GET /api/v1/admin/accounts`（分页）。

    分页参数与响应形状按 reg-factory 的实测用法：`page` / `page_size`，
    响应可能是 `{code:0,data:{items}}` 也可能是裸 `{items}` —— 解析见
    `_sub2api_extract_items`。认证走 `x-api-key`。
    
~~~

### `services/panel_comparison.py:855-870`

~~~
grok2api：复用 `Grok2ApiClient` 的登录 + 分页账号列表。

    `api_key` 这个位置实际传的是账号密码（grok2api 用 username/password 登录，
    见 `Grok2ApiClient.from_config`）；调用方从配置里取好再传进来。

    字段名按实测的响应来（不是猜的）：时间在 `lastUsedAt` / `createdAt` /
    `observedModelAt`，状态在 `authStatus` + `enabled`，没有 `status` 字段。
    同一条记录还会带一个 `linkedAccounts`（grok_web 子账号），邮箱与父记录
    相同 —— 按邮箱去重，只保留最近有活动的那条，否则同一个号会出现两行、
    把"仅远端"一栏灌满重复项。

    **凭证**走 `/api/admin/v1/accounts/export` 的 **web + build 两条线**并合并
    —— web 线给 `sso_token`，build 线给完整 AT/RT（只抓 web 线时 build 账号的
    AT/RT 差异会漏报成「已同步」）。列表接口不含凭证。只为 `emails` 里的账号
    查（见 `_MAX_CREDENTIAL_FETCH` 的理由）。
    
~~~

### `services/panel_comparison.py:936-951`

~~~
拉 grok2api 的凭证导出表（email → 凭证字典）。

    抓**两条线**并合并（导出接口按 provider 给全量，各一次调用）：

    - `grok_web`：SSO 那条线，返回 `sso_token`；
    - `grok_build`：Build 凭据线，返回完整 `access_token` / `refresh_token`
      （实测 2026-10-05：10 个账号 AT/RT 非空 10/10）。**之前只抓 web 线**，
      结果这 10 个账号的 AT/RT 差异全部漏报成「已同步」——本地 AT 已过期、
      build 线是新鲜的，对比却显示一致。

    合并规则：同邮箱两条线的凭证取并集（web 给 sso，build 给 AT/RT）。
    单条线失败只丢该线的数据，另一条线照常出（不能整表判 unknown）。

    注意：web 线的 `token` 字段**不是** id_token —— 它是 grok2api 导入时
    `sso_token` 的 fallback 位，导出时恒为空，不映射。
    
~~~

### `services/panel_comparison.py:1065-1080`

~~~
                # **键名按变体不同，这里是 yukkcat 的 `account_ids`。**
                #
                # 两个同名变体在这个端点上互不兼容（读源码确认，2026-10-26）：
                #
                # - yukkcat（`reference/panel/chatgpt2api/api/accounts.py:110-114`）
                #   两个键都收，但把 `access_tokens` 当 **legacy token** ——
                #   查出对不上就 400（`missing_legacy_tokens` 那段）。所以
                #   management_id 只能放 `account_ids`。
                # - 部署版（IFuZYI 2.3.0，`internal/api/account_refresh.go:160`）
                #   只读 `access_tokens`，忽略这里。
                #
                # 这不是缺陷：**部署版根本走不到这里** —— 它的列表直接带
                # `access_token`（`_chatgpt2api_credentials_from_item` 非空），
                # 于是 `credential_targets` 不填、导出分支不触发。
                # 曾按「部署版只认 access_tokens」改过这一行，结果只破坏了
                # yukkcat、没修好任何可达路径，已撤回。
~~~

### `services/panel_comparison.py:1145-1157`

~~~
chatgpt2api：`GET /api/accounts`（分页）。

    两个同名不同源的实现都兼容（字段形状实测过，不是猜的）：

    - **yukkcat 变体**（本项目对接的部署）：列表**不返回** token —— 只有
      `access_token_status` / `credential_availability` 这类生命周期标签。
      凭证走 `POST /api/accounts/export`（按 `account_ids` = 列表里的 `id`，
      即 management_id）批量取。
    - **basketikun 变体**：列表直接带 `access_token`，就地取。

    `emails` 给定时（本地已有的邮箱集合），**只为这些账号**抓凭证 —— 与
    CPA 同一理由：仅远端的行不参与凭证比对，不值得为它们付导出的钱。
    
~~~

### `services/panel_comparison.py:1232-1235`

~~~
                        # 凭据生命周期标签（yukkcat 变体实测返回，比中文
                        # status_label 更权威）：usable / recoverable / unavailable。
                        # 消费方 `panel_status_sync.classify_remote_status` 读它 ——
                        # 不传播的话那个分支在生产上永远不可达（复审发现）。
~~~

### `services/panel_comparison_cache.py:89-105`

~~~
这个面板对应的本地账号。

    - cpa / sub2api：ChatGPT 账号（它们都是 ChatGPT 账号的推送目标）
    - grok2api：Grok 账号
    面板与平台的对应关系写在 `_PANEL_PLATFORMS` 里 —— 加新面板时只改那一处。

    **`token` 列要并进 extra 再交出去**：账号表的 `token` 列是历史遗留的凭证位
    （前端编辑弹窗的「Token / Access Token」字段、`POST /api/accounts` 都写它），
    而较新的落库路径把主凭证写进 `extra`。对比只看 extra 的话，凭证在列上的
    账号会被当成「本地没有」—— 与远端一比对得出「两边都没有」→
    `unknown_credential`，或者更糟：本地有 RT 而远端 AT 不同时判成 `synced`
    （实测复现）。这里是两边凭证汇合的唯一入口，统一在这里补齐。

    镜像规则见注册表（`core/credential_fields.py`）：token 列 = 平台主凭证
    （chatgpt → AT，grok → SSO）；grok 列上是 OAuth 形态的 JWT（被 AT 盖过的
    脏值）时不认 —— 那是启动迁移要修的行，读侧兜底也要挡住。
    
~~~
