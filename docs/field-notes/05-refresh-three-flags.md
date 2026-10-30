# 刷新的三个正交标志

`success` / `verified` / `refreshed` 表达三件不同的事：调用成没成、新 AT 能不能用、是不是真的换发了。缺一个就会误判。

## 重构约束

- 不许合并成一个 bool
- `verify_access_token` 与 `validate_token` 必须分开 —— 网络故障 ≠ 令牌失效
- 失败时 `refreshed` 强制为 False，避免 `ok=false + refreshed=true` 的矛盾记录
- 合并登录链结果时只覆盖非空字段，空值不许冲掉刚轮换的 ST

## 证据（15 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `platforms/chatgpt/plugin.py:54-57`

~~~
            # 合并前存过的值 → 对应哪个新选项。前端据此把旧值显示成正确标签，
            # 而不是把 `refresh_token` 这种内部标识符直接摆在下拉里（实测）。
            # 从适配器的**单一来源**取，不在这里手抄一份 —— 手抄会漂移
            # （代码审查 2026-10-27 I2）。
~~~

### `platforms/chatgpt/plugin.py:177-181`

~~~
            # 下面三个是**面板动作**（`scope: "panel"`）：上传/同步的目标是外部
            # 面板，操作面在「面板管理」页。账号页的菜单按 scope 过滤掉它们 ——
            # 用户要求「平台管理主要管账号就行」。声明仍留在这里：面板注册表
            # 按动作 id 找到它们（`services/panel_registry.py` 的 upload_action /
            # sync_action），批量端点也要靠 `execute_action` 分发。
~~~

### `platforms/chatgpt/plugin.py:282-285`

~~~
            # 登录链拿到的 AT 也要**真校验**（打一次 /backend-api/me）——
            # 用户要求「GPT刷新token你要确认AT真的更新了」。此前这里
            # `verified = True` 是写死的：登录链刚跑完就假定可用，若服务端
            # 返回一个已失效的令牌，会被当成功写回库，下一个任务全线失败。
~~~

### `platforms/chatgpt/plugin.py:444-454`

~~~
            # 兜底三条触发条件：
            # ① 刷新调用回了 200 但 AT 没通过校验（见 verify_access_token）；
            # ② 刷新调用成功、AT 也校验通过，但**只返还了原本的 AT**（未换发）
            #    —— 用户要求（2026-10-06）：「刷新AT的使用sessiontoken刷新
            #    gptAT请校验AT是否真的刷新成功，还是只返还了原本的AT。如果
            #    没成功需要走登录流程」；
            # ③ 整链失败（session/OAuth 都拿不到可用 AT）—— 登录链是最后一条
            #    能拿回 AT 的路，也是「号没了」措辞唯一会出现的地方（用户
            #    要求：禁用靠登录流程发掘）。实测 chatgpt 账号多为 session-only，
            #    会话死了刷新链自身无路可走。
            # 封禁是终局结论，不兜底（登录链只会被同样拒绝）。
~~~

### `platforms/chatgpt/plugin.py:505-507`

~~~
                        # 失败分类（permanent / configuration / retryable）：
                        # 状态策略据此决定要不要落「失效」—— 没有它，永久失效的
                        # RT 会被反复重试且状态一直显示正常（实测 2026-10-28）。
~~~

### `platforms/chatgpt/plugin.py:514-516`

~~~
            # 如实区分「真的换发了」与「服务端认为无需换发」：AT 未到期时
            # session 端点原样返回旧 AT（实测 2026-10-06），此时显示「已换新」
            # 是误导 —— 用户看到的 AT 根本没变。
~~~

### `platforms/chatgpt/plugin.py:614-616`

~~~
                # 密钥只下发这一次，返回给前端让用户当场导入验证器。
                # banned 是登录链发掘的封禁结论（用户要求：禁用靠登录流程发掘），
                # 状态接线（api/actions.py）与批量界面都读它。
~~~

### `platforms/chatgpt/token_refresh.py:36-41`

~~~
    #: 新 AT 是否**真的**通过了 /backend-api/me 校验。
    #:
    #: 与 `success` 分开：`success` 只说「这次刷新调用本身拿到了 AT」，
    #: 而拿到的 AT 可能是服务端返回的一个**已失效**的令牌（实测：某些账号
    #: 刷新接口回 200 + AT，但那个 AT 打任何接口都是 401）。只看 `success`
    #: 会把它当成功写回库，下一个任务拿着废 AT 全线失败。
~~~

### `platforms/chatgpt/token_refresh.py:43-50`

~~~
    #: 新 AT 与旧值**不同**（真的换发了）。
    #:
    #: 实测（2026-10-06）：AT 未到期时，session 端点返回的 AT 与请求方
    #: 已有的**完全一致**（iat/exp 不变）—— 服务端不签发新令牌。这不是
    #: 错误（旧 AT 仍有效），但界面上不能把它说成「已换新」。`refreshed`
    #: 让调用方能如实区分「真的换发了」与「服务端认为无需换发」；
    #: **未换发不算刷新成功** —— 插件层据此继续走登录流程换发新 AT
    #: （用户修正 2026-10-06）。
~~~

### `platforms/chatgpt/token_refresh.py:61-68`

~~~
    #: 失败**分类**（空串 = 没分类）。与 Grok 侧同一套语义
    #: （`services/grok_account_state.py` 的 `refresh_kind`）：
    #:
    #: - `permanent`：`invalid_grant` 等终局结论 —— RT 已废，只能重新登录。
    #:   调用方应落「失效」并停止重试（实测：失效的号被反复重试，每次白打一轮）。
    #: - `configuration`：`invalid_client` / client_id 为空 —— **我们的**配错，
    #:   不是账号问题。归 permanent 会误杀一批好号。
    #: - `retryable`：5xx / 429 / 网络 —— 瞬时，重试即可，不动状态。
~~~

### `platforms/chatgpt/token_refresh.py:80-83`

~~~
#: 终局：RT 已废，只能重新登录。
#:
#: 服务端原话（实测 2026-10-28）：
#: `invalid_grant The refresh token has been invalidated. A fresh sign-in is required.`
~~~

### `platforms/chatgpt/token_refresh.py:102-111`

~~~
把刷新失败分成 permanent / configuration / retryable / 空串。

    为什么要分：调用方要据此决定「落失效并停手」还是「重试」还是「修配置」。
    只看一个错误字符串的话，失效的号会被反复重试（实测：5 个真实账号里 2 个
    永久失效，每次刷新都白打一轮上游），而界面一直显示正常。

    判据顺序：先 configuration 再 permanent —— `invalid_client` 的响应体里
    也可能出现别的词，而**配错 client 比 RT 失效更可能是我们的问题**，
    误判成 permanent 会静默把好号标失效。
    
~~~

### `platforms/chatgpt/token_refresh.py:356-378`

~~~

        刷新账号的 Token

        优先级：
        1. Session Token 刷新
        2. OAuth Refresh Token 刷新

        两条都拿到 AT 之后都会**再校验一次**（`verify_access_token`）：
        刷新接口回 200 不代表这个 AT 真能用 —— 实测有账号拿回一个打接口就
        401 的令牌。校验不通过的结果会把 `verified` 留成 False，调用方据此
        决定「别写库」或「换下一条路」。

        `refreshed` 区分「真的换发了新 AT」与「服务端返回原值」：AT 未到期时
        session 端点会原样返回旧 AT（实测 2026-10-06），此时 `success=True`
        但 `refreshed=False` —— 调用方（插件层）据此继续走登录流程换发新 AT
        （用户修正：未换发不算刷新成功），而不是把它当「已换新」写库。

        Args:
            account: 账号对象

        Returns:
            TokenRefreshResult: 刷新结果
        
~~~

### `platforms/chatgpt/token_refresh.py:416-423`

~~~
            # OAuth 这条路**失败**时不能直接把它还回去：session 那条路可能刚拿回
            # 一个没通过校验的 AT（success=True + verified=False），调用方正是靠
            # 这个信号去走登录兜底。丢掉它的话，两条刷新都失败 → 报「刷新失败」→
            # 登录兜底被跳过，而那是我们最后一条拿 AT 的路。
            #
            # 但封禁结论优先（复审发现 2026-10-07）：OAuth 端点认出「号没了」时
            # 直接回它 —— 否则封禁会被 session 的未校验结果吞掉，账号被判成
            # 普通失败、还会再走一次注定失败的登录链。
~~~

### `platforms/grok/token_refresh.py:1-23`

~~~
Grok token 刷新与有效性检测。

三条实测事实（2026-10-04 逐项验证）：

1. **RT 会轮换**：x.ai 的 OAuth refresh grant 每次成功都返回**新的
   refresh_token**，旧值随即失效。实测：浏览器换到新 RT 后，新 RT 走
   refresh grant 返回 200 并再次轮换；本地存的旧 RT 全部
   `invalid_grant`（因为上游已经轮换过一轮，本地没跟上）。
2. **协议 device flow 被 CF 挡**：device/verify 与 device/approve 纯 HTTP
   返回 403，只有真实浏览器里完成授权才走得通（浏览器 flow 实测成功，
   拿到全新 AT + 轮换 RT）。
3. **错误码要分类**（口径对齐参考实现 grok2api 的
   `IsPermanentCredentialRefreshErrorCode`）：
   - `permanent`：`invalid_grant` 等 —— 账号需要重新授权（走登录协议刷新）；
   - `configuration`：`invalid_client` 等 —— 网关配置问题，**不该标账号失效**；
   - `retryable`：`rate_limited` / 5xx / 网络异常 —— 瞬时，应重试；
   - `unknown`：无法归类，保守处理（不误杀）。

两条刷新路径：
- `refresh_via_grant`：RT → 新 token（纯协议快路径，用于日常检测/刷新）；
- `refresh_via_device_flow`：SSO → 登录协议重新获取（浏览器完成授权，
  被 CF 挡住协议路径时的兜底）。

~~~
