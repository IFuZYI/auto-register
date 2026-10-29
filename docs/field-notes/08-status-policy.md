# 状态判定优先级与粘性

优先级 **禁用 > 过期 > 失效**。`banned` 是粘性的：弱信号（401 / 缺凭证 / 远端失效）不得把它降级。

## 重构约束

- `configuration` 类错误（`invalid_client`）**不动状态** —— 那是我们的配错，不是号坏
- 只有 `expired` / `invalid` 能恢复为 `registered`
- `banned` 只能人工解除；探测可用也不自动复活

## 证据（9 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `platforms/chatgpt/login_refresh.py:37-43`

~~~
#: 「号没了」的判定统一放在 `platforms.chatgpt.protocol.banned_signals`，
#: 这里只做转发（登录链、补 RT、绑 2FA 三条路共用同一份标记表）。
#:
#: 用户修正（2026-10-06）：「Your sign-in session is no longer valid. Please
#: start over to continue.」**不是封禁** —— 会话/state 不同步（Cookie、
#: 会话或跳转不同步），可重开重试。封禁判定只认「deleted or deactivated」
#: 这类「号没了」措辞。
~~~

### `platforms/chatgpt/protocol/banned_signals.py:1-17`

~~~
「号没了」措辞的唯一判定源。

OpenAI 对已停用/删除的账号会回一些固定措辞 —— 不管出现在登录链的哪一步
（authorize/continue 的 409、密码验证的 401、发码接口的 409…），都应判成
同一件事：**账号已封禁，重登救不回**。此前每个模块各存一份标记表，加一句
新措辞要改三处、漏一处就出现「这条路认得出、那条路认不出」。

用户实测原文（2026-10-06）：

- ``You do not have an account because it has been deleted or deactivated.``
  （账号被停用的报错）—— 这是封禁判定唯一的措辞来源。

用户修正（2026-10-06）：``Your sign-in session is no longer valid. Please
start over to continue.`` / ``invalid_state`` **不是封禁** —— 那是会话
失效或 OAuth state 参数不匹配（Cookie、会话或跳转不同步），可重开重试。
不要把这两句认成封禁（此前误认，实测把正常账号标成了「禁用」）。

~~~

### `services/chatgpt_account_state.py:1-11`

~~~
ChatGPT 账号状态判定辅助逻辑。

状态语义（用户要求 2026-10-06）：

- `registered`（正常）：正常能使用的账号；
- `expired`（过期）：AT 已过期（JWT exp 已过）—— 刷新可能救回；
- `invalid`（失效）：需要重新登录的（凭证被拒但 AT 未过期 / 缺凭证）；
- `banned`（禁用）：被封了的账号 —— 登录流程发掘。

判定优先级（探测发现凭证被拒时）：禁用 > 过期 > 失效。

~~~

### `services/chatgpt_account_state.py:192-219`

~~~
按探测/同步结论落账号状态，返回判定理由（空串 = 没判定）。

    **禁用 / 过期 / 失效三档要分开**（用户要求 2026-10-06）：

    - `banned`（禁用）：号已删除/停用（"deleted or deactivated"），怎么登
      都登不回来。判重语义上它与 registered 同侧：号虽然废了但**邮箱仍被
      这个平台占用**，不该被当成"没注册过"再拿这个邮箱去注册一次。
    - `expired`（过期）：AT 已过期（JWT exp 已过）—— 刷新/重登可能救回。
    - `invalid`（失效）：凭证被拒但 AT 未过期（被吊销/会话失效）或缺凭证
      —— 需要重新登录，走流程登录。

    判定优先级：禁用 > 过期 > 失效。封禁措辞与过期同时存在时按禁用落
    （号都没了，谈过期没意义）。

    **正向恢复**：探测明确可用（`probe_confirms_usable`）或刷新成功
    （`usable=True`）时，过期/失效恢复为「正常」—— 状态跟着实际可用性走。
    禁用不自动恢复（强判断，要人工确认）；且**禁用是粘性的**——弱信号
    （401 / 缺凭证 / 远端失效）不得把它降级回过期/失效（用户要求
    2026-10-07「禁用的不参与同步」的同源问题：降级会让刚发掘的封禁被
    下一次状态同步洗掉）。

    这条判定必须留在这里（共享策略）而不是各个 action 的调用点：探测、同步、
    刷新三条路都会得出"号没了"的结论，只在其中一条上特判的话，另外两条仍然
    把封号写成 invalid。

    `banned=True` 给刷新链用 —— 那条路已经自己认过封禁措辞
    （`login_refresh.looks_like_banned`），这里只负责落状态。
    
~~~

### `services/chatgpt_account_state.py:224-231`

~~~
    # 刷新的失败分类（`platforms/chatgpt/token_refresh.classify_refresh_error`）：
    # - `permanent`（invalid_grant 等终局结论）→ 落失效，与其它「需要重新登录」
    #   的信号同侧。**必须**落，否则失效的号会被反复重试、界面一直显示正常
    #   （实测：5 个真实账号里 2 个永久失效，每次刷新白打一轮上游）。
    # - `configuration`（invalid_client / client_id 为空）→ **不动状态**：那是
    #   我们的配错，账号可能是好的（实测：正确 client 下同一批号 4/12 刷新成功）。
    #   落状态会误杀一批好号。
    # - `retryable` / 空 → 不动。
~~~

### `services/chatgpt_maintenance.py:1-43`

~~~
ChatGPT 账号自动维护（用户要求 2026-10-06）。

两条功能，都由「全局配置 → 面板配置」里的开关控制：

1. **自动刷新临期/过期 Token**（`chatgpt_auto_refresh_enabled`）：
   后台定期扫描 ChatGPT 账号，对 AT 临期（剩余 ≤ 24h）或已过期的账号，
   在临期窗口内**随机**一个时刻刷新 —— 不设固定更新时刻（用户要求：
   「不要设定固定值来更新，应该在临期时间段内随机一个时间更新」），
   且至少提前 1 小时（「尽量不要拖到过期，比如至少在过期前 1 小时」）。
   **失效（`invalid`）/ 过期（`expired`）状态的账号也进重试队列**
   （用户要求 2026-10-10：「定期尝试自动刷新 gpt 失效的账号」）——
   这类账号不管 AT 是否临期、能否解出 exp，都尽快 + 抖动排一次刷新
   （刷新链会先试 session/OAuth，再走登录兜底，可能把它们救回来）。
   封禁的、连续失败达上限的不重复尝试（「封禁/无法更新 token 的不要
   重复尝试」）。刷新走 `refresh_token` 动作的完整链（session → OAuth →
   登录兜底），与手动批量刷新同一条路径。

   **检出封禁 → 标注 + 移出队列**（用户要求 2026-10-10：「若检测出账号
   被封禁，就标注为封禁账号」）：刷新链认出「号没了」措辞时，执行器把
   状态落成 `banned`（共享状态策略），维护侧清掉该账号的自动刷新计划
   —— 封禁是终局结论，不再占用重试名额。封禁账号不参与后续的自动刷新、
   凭据同步与对比。

2. **chatgpt2api 凭证自动维护**（`chatgpt2api_auto_sync_enabled`）：
   定期对比本地与 chatgpt2api 远端凭证，本地较新时自动推送更新
   （复用 `panel_push` 的方向判定），使远端始终持有最新凭证。

设计约束：

- **避免高并发**：每轮扫描限量（`_SCAN_BATCH_SIZE`），逐个串行执行，
  每个之间留间隔（`_PER_ACCOUNT_DELAY_SECONDS`）；随机时刻本身就是
  错峰机制（各账号的窗口不同、采样独立）。
- **随机时刻持久化**：计划存 extra 的 `chatgpt_auto_refresh`
  （`{at, for_exp, attempts, disabled}`），进程重启不丢；AT 换发后
  （exp 变了）旧计划自动作废重排。
- **退避**：失败后 1h → 6h 退避重试；连续 3 次失败置 `disabled`，
  同一 AT 周期内不再尝试。手动刷新成功（AT 换发）后 exp 变化会自然
  复位重排。
- **任务可见性**（用户要求 2026-10-07）：「任务运行」页要能看到这些维护
  任务、方便查看日志。两趟维护在**有实际动作**时创建任务记录
  （source = `auto_refresh` / `chatgpt2api_sync`），日志逐行进记录、
  支持停止/跳过；空轮不建记录（每 5/10 分钟一条空任务会把列表淹掉）。

~~~

### `services/chatgpt_maintenance.py:550-553`

~~~
        # 封禁是终局结论（用户要求 2026-10-10「检测出封禁就标注」）：
        # 执行器已把状态落成 banned（`_default_execute` → 共享状态策略），
        # 这里把该账号移出自动刷新队列 —— 清计划而不是记一次退避失败，
        # 封禁账号不该保留任何待办状态。
~~~

### `services/grok_account_state.py:1-17`

~~~
Grok 账号状态判定。

状态语义（用户要求 2026-10-06，与全站统一）：

- `registered`（正常）：正常能用；
- `expired`（过期）：AT 已过期（JWT exp 已过）—— 刷新可能救回；
- `invalid`（失效）：需要重新登录 —— **对齐 grok2api 的 reauthRequired 语义**：
  SSO / OAuth 被上游明确拒绝（401、`invalid_grant` 等永久错误），
  与 grok2api 的 `markSSOCredentialRejected`（"SSO credential rejected" →
  reauthRequired）同一口径；
- `banned`（禁用）：被封（上游明确 `blocked-user` / `user is blocked` ——
  对齐 grok2api 的 `IsDefinitiveAccountBlockBody`）。

判定优先级：禁用 > 过期 > 失效。正向确认（probe 200 / 402 无额度 / 刷新成功）
时过期/失效恢复「正常」；禁用不自动恢复，且**禁用是粘性的**：弱信号（401 /
SSO 被拒 / 远端失效）不得把它降级回失效/过期（要人工确认才解除）。

~~~

### `services/grok_account_state.py:121-124`

~~~
        # 禁用是**粘性**的：弱信号（401 / SSO 被拒 / 远端失效）不得把它
        # 降级回失效/过期。封禁要人工确认才解除（同「不因一次可用探测
        # 复活」的强判断语义）—— 降级会让刚发掘的封禁被下一次同步洗掉、
        # 账号重新进重试队列（与 chatgpt 侧同口径，用户要求 2026-10-07）。
~~~
