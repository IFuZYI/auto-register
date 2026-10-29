# client_id 必须跟着 RT 走

两条 RT 路线用不同的 `client_id` + `redirect_uri` + token 端点。刷新时用错 client 会得到 `401 invalid_client`，看起来像「RT 失效」。

## 重构约束

- `client_id` 随 RT 一起落库、加密、导出
- 不许在代码里给默认 client_id 兜底（Codex 的 id 只允许出现在构造 authorize 那一处）
- Codex 的 `/oauth/token` 与 platform 的 `/api/accounts/oauth/token` 不可混用

## 证据（18 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:47-54`

~~~
#: 合并后的「Token 方案」三取值 —— 一个值同时决定「要不要 RT」与「怎么换 RT」。
#:
#: 为什么合并：设置页原先渲染成两个独立下拉（有/无 RT + Codex/密码登录），
#: 而任务页早已合并成三选一（`frontend/src/lib/chatgptTokenPlan.ts`）。同一件事
#: 两处呈现不同，用户会以为设置页少了东西（用户 2026-10-26 要求合并）。
#:
#: 三个值都写进 `chatgpt_registration_mode`；`chatgpt_rt_route` 保留为**兼容输入**
#: （老配置里存过它），但不再单独渲染。
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:58-64`

~~~
#: 设置页「Token 方案」的声明默认值 —— 必须与运行时行为一致。
#:
#: 选密码登录作为默认：2026-10-26 实测 Codex 路线在新号上卡在
#: choose-an-account / session.select（拿不到 RT），而密码登录路线在新号与
#: 老号上都能拿到。`DEFAULT_CHATGPT_REGISTRATION_MODE` 仍是 `refresh_token`
#: （合并值会归一成它），两者不冲突：前者是**声明层的默认选项**，后者是
#: **归一后的运行时模式**。
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:68-70`

~~~
#: 旧值 → 合并方案的**单一来源**。设置页的 `legacy_aliases` 与
#: `normalize_chatgpt_registration_mode` 都从这里取 —— 各写一份的话，
#: 下拉里显示的与运行时解析的会漂移（代码审查 2026-10-27 I2）。
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:97-101`

~~~
    # 运行时归一：无 RT 的旧写法 → `access_token_only`；其余有 RT 的写法
    # → `refresh_token`。这里**不复用** `LEGACY_TOKEN_PLAN_ALIASES` ——
    # 那张表是给显示层用的（旧值 → 合并方案三选一），两者的目标值域不同：
    # 显示层要 `refresh_token_login`，运行时归一要 `refresh_token`。
    # 硬把两者合并会让 `normalize` 返回显示层的值（实测踩过）。
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:125-135`

~~~
#: 换取 RT 的路线。
#:
#: - `codex`：现有行为，走 Codex CLI 的 client_id 做 OAuth 授权；撞 add-phone
#:   时要靠接码平台才能过。
#: - `login`：走 platform 的 client_id 做**密码登录**（password/verify →
#:   mfa/verify → code → token）；实测不需要手机号，但要求账号有密码与
#:   TOTP 密钥。
#:
#: 2026-10-26 实测：`codex` 在新号上卡在 choose-an-account / session.select，
#: 拿不到 RT；`login` 在同样的号上拿到。默认保持 `codex`（不改既有行为），
#: 由用户在设置里显式切换。
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:138-140`

~~~
#: 路线默认值 —— 必须与「Token 方案」的声明默认（`DEFAULT_CHATGPT_TOKEN_PLAN`
#: = 密码登录）一致，否则会出现「设置页默认显示密码登录、运行时却按 Codex 走」
#: 的静默错位（实测 2026-10-27：补 RT 空配置时仍先跑 Codex）。
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:152-160`

~~~
把任意历史配置归一成**合并后的**「Token 方案」三取值之一。

    设置页的下拉要显示当前选中项。已保存的旧值（`refresh_token` /
    `access_token_only`，或更早的 `chatgpt_rt_route` 独立键）都不是新选项，
    antd `Select` 只能把原始串显示出来 —— 用户看到 `refresh_token` 这种内部
    标识符，不知道实际选了哪个方案（实测 2026-10-26）。

    这个函数是前端的显示依据：任何旧配置都能还原成三个方案之一。
    
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:197-200`

~~~
    # 旧的独立键：**存过**才用它，没存过就跟随默认值。
    # 不能直接把 None 丢给 `normalize_chatgpt_rt_route` —— 它把「没值」也
    # 归一成 codex，于是「设置页默认显示密码登录、运行时按 Codex 走」
    # （实测 2026-10-27）。
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:275-291`

~~~
决定**落库**用哪个 client_id。

    优先 `flow_client_id` —— 它是服务端 `client_auth_session` dump 里的
    `openai_client_id`，也就是「这个 RT 到底是谁签的」的权威答案
    （`protocol/mixins/trace.py:301` 把它同步到 `flow._oauth_client_id`）。
    拿不到才回落默认值。

    为什么不能写死一个常量：本仓有**两条**产出 RT 的路线，client 不同 ——

    | 路线 | client_id |
    | --- | --- |
    | Codex OAuth | `app_EMoamEEZ73f0CkXaXp7hrann` |
    | platform 密码登录 | `app_2SKx67EdpoN0G6j64rFvigXD` |

    写死任一个，另一条路线的号刷新时都会 `401 invalid_client`
    （实测 2026-10-28）—— 而错误信息看起来像「令牌失效」，极易误判。
    
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:360-366`

~~~
            # 按**实际拿到的凭证**写，而不是按模式写。
            #
            # 原来写的是 `self.mode == REFRESH_TOKEN`（意图），于是「选了 RT 模式
            # 但 Codex 交换撞上 add-phone 墙、RT 为空」的号也被标成 True ——
            # 下游据此以为它有 RT 方案，实际 `refresh_token` 是空串（实测踩过）。
            # 这个键是**读取方**用的（`resolve_chatgpt_registration_mode` 靠它
            # 反推模式），写意图会让「补 RT」挑不到该补的号。
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:371-377`

~~~
            # **client_id 必须落库**：OAuth 刷新要它。空值换来的是
            # `invalid_client`（HTTP 401），而不是能自查的「参数缺失」——
            # 排查时极易误判成「RT 失效」。
            #
            # 实测（2026-10-28）：663 个 chatgpt 账号 **0 个** 有 client_id，
            # OAuth 刷新全部 401 → 回落 session → AT 未到期时原样返回旧值
            # （`refreshed=False`）→ 用户看到「刷不出新的」。
~~~

### `platforms/chatgpt/chatgpt_registration_mode_adapter.py:397-406`

~~~
把路线设置翻译成协议层 `env_overrides` 里的两个键。

    `AuthFlow._speculative_rt_attempt` 从 `env_overrides` 读路线决定要不要抢跑
    Codex。三处构造 flow 的地方都要带（注册引擎、补 RT、登录刷新）——
    此前只有注册引擎带了，另外两处读空后按默认判定，抢跑行为与设置不符
    （代码审查 2026-10-27 I4）。

    抽成一个函数而不是各写一遍：这份映射是**单一来源**，漏一处就是一处
    静默不一致。
    
~~~

### `platforms/chatgpt/protocol/mixins/platform_rt.py:1-17`

~~~
PlatformRtMixin：走 platform client_id 的「密码登录」路线换 refresh_token。

**为什么不复用 Codex 路线**（2026-10-26 实测）：

| | Codex 路线 | platform 路线（本模块） |
|---|---|---|
| client_id | `app_EMoamEEZ73f0CkXaXp7hrann` | `app_2SKx67EdpoN0G6j64rFvigXD` |
| redirect_uri | `http://localhost:1455/auth/callback` | `https://platform.openai.com/auth/callback` |
| 换 code 的方式 | 跟随 authorize 302 链 | 从登录响应里直接读 `?code=` |
| 新号结果 | 卡在 choose-an-account / session.select | **拿到 RT** |
| 是否需要手机号 | 可能撞 add-phone | **全程不碰** |

参照实现：`/home/chatgpt2api/internal/service/password_login.go`（Go）。
本模块只补「platform 侧特有」的两步（构造 authorize URL、用 platform 参数换 token），
中间的 `authorize_continue` / `login_password_verify` / `submit_mfa_totp` 复用本仓
既有实现 —— 那三处已由 2026-10-26 的 4 次成功交换验证可用，不照搬 Go 的写法。

~~~

### `platforms/chatgpt/protocol/mixins/platform_rt.py:225-231`

~~~
        # **邮箱验证码**：服务端要 email OTP 时不能直接放弃 —— 这条路是 RT
        # 的最后兜底（Codex 也失败时），它一放弃 RT 就彻底拿不到。
        #
        # 实测 2026-10-28（真实注册 task_1793184821307）：走到
        # `page_type=email_otp_verification` 后原实现直接返回 False，
        # 账号落库 AT 有 / session 有 / **RT 空**、totp_secret 空，
        # `partial=True missing=refresh_token`。
~~~

### `platforms/chatgpt/token_refresh.py:150-160`

~~~
        # **刷新要用 platform 的 client_id，不是 Codex 的**。
        #
        # 实测（2026-10-28，真实账号 + 真实 RT）：
        #   Codex `app_EMoamEEZ73f0CkXaXp7hrann`      → 401 invalid_client
        #   platform `app_2SKx67EdpoN0G6j64rFvigXD`   → ✅ 拿到新 AT + 新 RT
        #
        # 原因：本仓注册链走的是 platform 密码登录路线
        # （`protocol/mixins/platform_rt.py`），签发 RT 的就是那个 client。
        # 用 Codex 的 client_id 去刷别人签发的 RT，服务端只会回
        # `invalid_client` —— 而不是「RT 失效」那种能自查的错误，所以很容易
        # 误判成「RT 不对」。
~~~

### `platforms/chatgpt/token_refresh.py:210-212`

~~~
                # 非 200 也要读响应体：号被停用时服务端会在 body 里写明
                # 「deleted or deactivated」（用户实测 2026-10-06），只报
                # 「HTTP 401」会把封号与普通过期混成同一种失败。
~~~

### `platforms/chatgpt/token_refresh.py:231-234`

~~~
            # 保存轮换后的 session token：OpenAI 对每次 /api/auth/session 响应
            # 都会下发新的 `sessionToken`（滑动窗口，实测 2026-10-06：连续两次
            # 请求返回的 ST 都不同）。此前只读 accessToken，轮换值从未保存 ——
            # 旧值滑出窗口后账号就登不上了。只写非空值。
~~~

### `services/chatgpt_rt_backfill.py:148-153`

~~~
    # **client_id 必须落库**：OAuth 刷新要它，空值换来的是
    # `invalid_client`（HTTP 401），而不是「参数缺失」那种能自查的错误 ——
    # 排查时极易误判成「RT 失效」（实测 2026-10-28：663 个账号 0 个有它，
    # OAuth 刷新全部 401 后回落 session，AT 拿不回新的）。
    # **记签发这个 RT 的那个 client**，不是写死一个常量 —— 两条路线
    # （Codex / platform 登录）client 不同，写错会让刷新 401 invalid_client。
~~~
