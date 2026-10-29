# 代理粘性绑定

同一账号反复走不同出口 IP 会被上游判异常登录。复用时要回到出生 IP，原代理不可用才换。

## 重构约束

- `fallback_provider` 必须是惰性 callable —— `get_next()` 会推进轮转游标
- 「原代理在池外」视为可用（手工一次性代理，无证据说它坏）
- 注册成功必须 `report_success` —— 它会把 `is_active=False` 的代理复活

## 证据（12 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `api/actions.py:265-273`

~~~
给「复用账号」的动作挑代理：优先它注册时那个，不可用才换。

    返回空串表示不用代理。**无绑定或绑定不可用时都会写回** `register_proxy`：
    用户要求「不再是仅有注册才绑定，若无绑定、绑定代理不可用，后续也能更新」。

    这里和批量任务（补 RT / 绑 2FA）走**同一套**决策（`proxy_pool.
    resolve_for_account`），否则同一个账号在「测活」按钮和「补 RT」任务里
    会走不同出口，行为对不上。
    
~~~

### `core/proxy_pool.py:155-165`

~~~
把所有被自动停用的代理重新启用，返回启用条数。

        为什么需要：`report_fail` 在「0 成功 + 5 次失败」时会 `is_active=False`，
        而 `get_next()` 只取 active 的 —— 于是一个**本来能用**的代理只要连撞 5 次
        失败（例如那 5 次用的是不支持它的调用路径），就会被永久拉黑，之后无论
        怎么重试都取不到它，界面还会显示成「没有可用代理」。实测踩到过：
        住宅代理是活的（curl 直接连通），却因为 6 次失败躺在库里 is_active=0。

        界面上「检测全部代理」会逐条真实探活并 `report_success` 自动复活；
        这个方法是给「我知道代理没问题、想直接恢复」的场景用的。
        
~~~

### `core/proxy_utils.py:156-177`

~~~
把代理 URL 转成 Playwright 的 proxy 配置。

    **关键限制：Chromium 不支持带认证的 SOCKS5。**

    实测（2026-09-30）：
      - `{"server": "socks5://user:pass@host:port"}` → `net::ERR_NO_SUPPORTED_PROXIES`
        （Chromium 把整串当 host 解析，认证部分让它直接拒连）
      - `{"server": "socks5://host:port", "username": ..., "password": ...}`
        → `Browser does not support socks5 proxy authentication`（启动即失败）
      - 同一个端点改成 `{"server": "http://host:port", "username": ...}` → ✅ 通

    所以带认证的 socks5 不能直接交给浏览器。多数住宅代理商（含本项目在用的那家）
    同一端口同时提供 HTTP CONNECT，因此这里**把带认证的 socks5 降级成 http**：
    认证照样带上，只是用 HTTP CONNECT 隧道而不是 SOCKS 握手。

    代价要说清楚：HTTP CONNECT 只对 TCP 隧道，不走 SOCKS 的远程 DNS 解析，
    所以 `socks5h://` 那种「域名交给代理解析」的隐私收益没有了。这是唯一能让
    Chromium 用上带认证代理的办法；不想要这个代价就只能换不带认证的 socks5，
    或换支持 socks5 认证的 Firefox。

    不带认证的 socks5 原样保留 —— Chromium 支持它，没必要降级。
    
~~~

### `platforms/grok/upload.py:38-52`

~~~
上传 CPA auth 记录到 CLIProxyAPI Management API。

    必须走 **multipart**（字段名 `file`，文件名 `<email>.json`）：
    CLIProxyAPI 的 `POST /v0/management/auth-files` 只认 multipart 或
    `?name=xxx.json` 的裸 JSON 体。此前这里发的是 `{"name":…,"content":…}`
    的 JSON —— 实测三个候选路径全 404（`/v0/management/auth-files` 收到 JSON
    体时没有匹配路由），于是 Grok 的 CPA 上传从来没成功过。
    对照实现：`platforms/chatgpt/cpa_upload.py` 用的就是 multipart。

    `proxy` 非空时写进记录顶层 `proxy_url`（CPA 读它给这个账号固定出口）。
    由「上传代理」开关控制（见 `services.chatgpt_sync.upload_proxy_for`）。

    出处：sso_to_auth_json.py:745-786（原参考实现）+ CLIProxyAPI
    `internal/api/handlers/management/auth_files_crud.go:UploadAuthFile`。
    
~~~

### `services/chatgpt_maintenance.py:331-337`

~~~
生产执行器：走 `refresh_token` 动作的完整链（与手动批量刷新同路径）。

    代理规则与批量刷新一致（`_run_account_batch_task` 的
    `_resolve_proxy_for_account`）：优先账号注册时那个；不可用/未绑定时
    从池里取备用（惰性来源 —— 避免白推轮转游标）并**写回** `register_proxy`
    （用户要求「不再是仅有注册才绑定」）。
    
~~~

### `services/solver_manager.py:47-54`

~~~
是否让 solver 的解题浏览器也走代理。

    默认**开启**：Turnstile 判的就是 IP 信誉，注册任务走住宅代理而解题走机房
    IP 是自相矛盾的（实测：直连出口过不了 x.ai 的挑战，住宅代理能过）。
    没有可用代理时 solver 内部会回落到直连，所以开着不会有副作用。

    显式设 `SOLVER_PROXY_ENABLED=0` 可关掉。
    
~~~

### `services/task_runners.py:71-74`

~~~
        # 任务级环境预检：环境不就绪（如 camoufox 浏览器没装/与包版本不配对）
        # 在分配邮箱/代理**之前**终止整个任务 —— 否则每个账号白跑一遍、重试
        # 轮全烧在同一个环境错误上（实测事故：task_1791300512055 两轮 3 秒
        # 全挂在 CamoufoxNotInstalled，还白领了别名）。平台没实现预检时默认放行。
~~~

### `services/task_runners.py:121-123`

~~~
            # 不给兜底 provider：用户要求默认留空、必须显式选。空串会让
            # create_mailbox 抛「未知邮箱提供商」并列出可用渠道，比悄悄用某个
            # 号池更安全。
~~~

### `services/task_runners.py:257-264`

~~~
                    # 记下**注册时用的代理**：之后复用这个账号（测活、补 RT、
                    # 绑 2FA…）时优先回到同一个出口 IP —— 同一账号反复换 IP
                    # 容易被上游当成异常登录。代理不可用时会被换成新的并
                    # 覆盖这个字段（见 `proxy_pool.resolve_for_account`）。
                    #
                    # 注册时没走代理（`_proxy` 为空，如池子当时是空的）就**不写**：
                    # 不写空值，让复用路径的 resolve 知道「还没绑定」并补上 ——
                    # 绑定不再只发生在注册这一刻（用户要求）。
~~~

### `services/task_runners.py:385-394`

~~~
把一行账号读成纯数据（含它注册时用的代理）。

    ``platform`` 必须由调用方传入：这里原来**两处**硬编码 ``"chatgpt"``
    （库名与 platform 过滤），于是 Grok 的批量动作虽然任务能建起来，
    执行时每个号都报「账号不存在」（实测 2026-10-28）。

    后面那几十秒网络请求期间不能占着数据库连接不放：连接池就那么几条，攥在手里
    会把面板其它请求一起拖住。代理字段也一并读出来 —— 复用账号时要优先回到
    它出生时的那个出口（见 `_run_account_batch_task` 里的代理选择）。
    
~~~

### `services/task_runners.py:453-461`

~~~
复用账号时挑代理：优先它注册时那个，不可用才换新的。

        **无绑定或绑定不可用时都会写回**账号的 `register_proxy` —— 用户要求
        「不再是仅有注册才绑定，若无绑定、绑定代理不可用，后续也能更新」。
        写回失败只记日志，不该让整轮任务失败。

        `fallback_provider` 传的是**函数**而不是 `_resolve_proxy()` 的结果：
        后者会立刻从池里取一个代理（推一格轮转游标），原代理可用时那格就白推了。
        
~~~

### `services/turnstile_solver/api_solver.py:159-171`

~~~
挑一个代理给解题浏览器用。

        两个来源，按优先级：
          1. `PROXY_URL` / `SOLVER_PROXY` 环境变量 —— 单代理部署最省事，
             solver 是独立进程，读不到主进程的内存状态；
          2. 主库 `proxies` 表里 active 的那批 —— 与注册任务共用同一份代理池，
             注册走哪个出口、解题就走哪个，避免「任务用住宅代理、解题用机房 IP」
             这种自相矛盾（Turnstile 恰恰是看 IP 信誉的那一步）。

        读不到就返回 None（=直连）。**不要**再退回 `proxies.txt`：那个文件在本
        项目里从来不存在（实测全盘无此文件），旧实现因此每次都静默直连，
        日志里只有一行 `Proxy file not found`。
        
~~~
