# registration 审计

[MODE: SANDBOX]

- 仓库：`/data/workspace/Register`，分支 `main`，HEAD `4680c8d`
- 解释器：`.venv/bin/python`（3.12）
- 生产库：`data/account_manager.db`（**只读**，`mode=ro` 打开；本报告未写入任何一行）
- 审计方式：静态通读 + 只读 SQL 抽样 + 3 个可复现探针 + 3 个既有测试文件（73 passed）
- 本审计**未修改任何源码**；唯一写入的文件是本报告

范围（30 个文件，约 15.5k 行）：

| 区域 | 文件（行数） |
|---|---|
| 注册引擎 | `registration_engine.py`(546)、`chatgpt_registration_mode_adapter.py`(405) |
| 补 RT / 刷新 | `rt_backfill.py`(456)、`login_refresh.py`(212)、`token_refresh.py`(520) |
| 协议层 | `auth_flow.py`(1299)、`mixins/signup.py`(709)、`mixins/codex.py`(504)、`mixins/platform_rt.py`(276)、`mixins/add_phone.py`(429)、`mixins/redirect.py`(506)、`mixins/trace.py`(463)、`mixins/session.py`(115)、`mailbox_adapter.py`(172)、`mail_provider.py`(132)、`sentinel.py`(94)、`two_factor.py`(406)、`http_client.py`(169) |
| Grok | `plugin.py`(1036)、`register_browser.py`(716)、`token_refresh.py`(261)、`turnstile_mint.py`(279)、`oauth_device.py`(437)、`oauth_browser.py`(287) |
| 邮箱 / 接码 | `modules/mail/icloud_local.py`(374)、`icloud_remote.py`(337)、`services/chatgpt_otp_mailbox.py`(298)、`services/sms/controller.py`(301)、`providers.py`(789)、`sms_service.py`(105) |
| 代理 / 执行器 | `modules/proxy/pool.py`(50)、`core/proxy_pool.py`(425)、`core/executors/protocol.py`(50)、`playwright.py`(132)、`factory.py`(45) |

---

## 0. 结论摘要

| # | 维度 | 判定 | 最严重问题 |
|---|---|---|---|
| 1 | RT 三条路线一致性 | **不一致**：三条链对 `chatgpt_rt_route` 的解释各不相同，`login` 仍会跑 Codex | R-02 / R-03 / R-04 |
| 2 | 重试 / 回退语义 | **有静默降级与吞中断**：4 处 catch-all 吞 `TaskInterruption`；注册失败分类过粗 | R-07 / R-08 / R-09 |
| 3 | 邮箱与接码 | **会烧号**：指定邮箱仍从池里领号并标 used；add-phone 未配置时静默放行 | R-16 / R-17 |
| 4 | 验证码 / Turnstile solver | **未接入**：`mint_turnstile`/`captcha_solver` 是死代码；Grok 把未识别页面当"地址已消耗" | R-21 / R-22 |
| 5 | 凭证落库完整性 | **两处硬伤**：邮箱渠道的 RT/client_id 会覆盖账号的；`client_id` 在三个结果类型里全部丢失 | R-01 / R-24 / R-25 |
| 6 | 并发共享状态 | **有全局串行锁**：SMS 验证锁跨整个租号周期、无超时；Grok 发码锁内 sleep | R-18 / R-30 / R-31 |

一句话：**注册主链（协议 + 引擎）结构清晰、单号路径能跑通，但"换 RT"这条横切能力在注册 / 补RT / 刷新三处的路线语义、收件渠道类型和 client_id 归属上各说各话；其中两处会把账号凭证写错（邮箱 RT 覆盖账号 RT、Codex RT 配 platform client_id），一处会让指定邮箱的注册永久烧掉一个号池别名。**

---

## 1. RT 获取三条路线的一致性

### 1.1 事实基线（route 解析与三处调用点）

`chatgpt_rt_route` 的单一解析入口是 `chatgpt_registration_mode_adapter.py:179-200`，默认值 `RT_ROUTE_LOGIN`（`:137`）。三条链的实际顺序：

| 链路 | 入口 | `route=codex` | `route=login` |
|---|---|---|---|
| 注册 | `registration_engine._acquire_refresh_token`（`:240-287`） | Codex → 登录链回退 → 协议层 Codex 兜底（被 `_codex_rt_attempted` 挡住，见 R-02） | 登录链 → **协议层 Codex 兜底仍会跑**（R-03） |
| 补 RT | `rt_backfill.RefreshTokenBackfiller.run`（`:200-235`） | session → codex → platform | platform → session → codex |
| 刷新 AT | `login_refresh` → `run_protocol_login` | 登录 → `_ensure_refresh_token` → Codex | 登录 → **`_ensure_refresh_token` → Codex**（R-03） |

补 RT 的顺序本身有合理之处（有会话就先复用），但它与注册链的 `codex → login` 顺序并不等价；真正的问题是下面几条。

### R-01 [P0] 注册链把邮箱渠道的 `refresh_token` / `client_id` 覆盖到账号 extra 上

- **位置**：`platforms/chatgpt/registration_engine.py:200-213`（`_attach_mailbox_credentials` 的键表）、`platforms/chatgpt/chatgpt_registration_mode_adapter.py:343-375`（`_build_account_extra`）。
- **证据**：`_attach_mailbox_credentials` 把邮箱账号 `extra` 里的 `password` / `client_id` / `refresh_token` / `mailbox_token` 原样 `setdefault` 进 `RegistrationResult.metadata`；`_build_account_extra` 在 `:346` 先写入账号自己的 RT，随后 `**{k: v for k, v in result.metadata.items() ...}`（`:374`）把 metadata **整体展开在最后** —— 同名的 `refresh_token` 被覆盖成邮箱的 RT。`client_id` 更直接：`:372` 的兜底值就是 `result.metadata["client_id"]`，即邮箱的 client id。
  用真实形状的 `RegistrationResult` 复现（Outlook OAuth 邮箱的 extra 带 `client_id`/`refresh_token`/`password`）：

  ```
  extra = {'access_token': 'AT', 'refresh_token': 'MS_RT', 'session_token': 'ST',
           'client_id': 'MS_CLIENT_ID', 'password': 'MS_PW'}
  ```

  即 ChatGPT 账号的 RT 被替换成**微软邮箱的 OAuth refresh_token**，client_id 被替换成微软的 client id。
- **影响**：使用 Outlook 号池注册时，每个新号的 `extra.refresh_token` 都不是 OpenAI 签发的 —— OAuth 刷新必然 `invalid_client`/`invalid_grant`，而 session 刷新又救不回 RT。邮箱密码/`mailbox_token` 还会以**明文**进账号 extra（`core/credential_crypto.py:41-50` 的 `SENSITIVE_EXTRA_KEYS` 不含 `password`/`mailbox_token`），并经列表接口下发（`api/accounts.py:201-260` 只裁 token 键）。当前生产库 667 个 ChatGPT 账号的 `mail_provider` 全是 `icloud_local`/`icloud_remote`（其 extra 不含这些键），所以**尚未触发**；这是切到 Outlook 号池就会爆的 P0。
- **建议**：把邮箱凭据的合并改成白名单且换命名空间（如 `mailbox_password` / `mailbox_client_id` / `mailbox_refresh_token`），永远不要写 `client_id` / `refresh_token` 这两个账号字段；`client_id` 必须在 metadata 展开**之后**再赋值；补一条「Outlook 形状的 mailbox extra → 账号 extra 的 RT/client_id 不变」的单测。

### R-02 [P0] 换 RT 的两条路线把 `BaseMailbox` 当 `MailProvider` 传，邮箱验证码分支必抛 `AttributeError`

- **位置**：`platforms/chatgpt/registration_engine.py:296`（Codex）、`:317`（登录链）。
- **证据**：两处都是 `mail_provider=self.mailbox`。`self.mailbox` 是 `BaseMailbox`，只有 `wait_for_code(account, ...)`；协议层要的是 `MailProvider.wait_for_otp(email, timeout, issued_after)`（`mail_provider.py:89-106`）。实测：

  ```
  BaseMailbox.wait_for_otp: False
  BaseMailbox.wait_for_code: True
  ```

  触发点：`platform_rt.py:240-246`（登录链要邮箱码时）与 `codex.py:322` 的 `mail_provider.wait_for_otp(...)`；两处都被 `except Exception` 收成「登录链等邮箱验证码失败: 'XxxMailbox' object has no attribute 'wait_for_otp'」，然后返回 False。
- **影响**：注册链里只要 RT 路线撞上 `email_otp_verification`，RT 就**必然拿不到**，且日志看起来像"邮箱链路问题"。这正是 `a093ab4`/`7f6be46` 在补 RT 链修过的同一类坑（补 RT 传的是 `services/chatgpt_otp_mailbox` 造的真 provider），注册链只修了"属性不存在"（`:292-295` 的注释），没修"类型不对"。
- **建议**：`_build_flow` 把 `provider`（`MailboxProviderAdapter`）存到引擎上，`_acquire_rt_adapter` / `_run_login_rt` / `_run_codex_rt` 一律用它；加结构性测试：`AuthFlow` 收到的 `mail_provider` 必须 `isinstance(MailProvider)`。

### R-03 [P1] `route=login` 仍会跑 Codex：注册链"只走登录链"的承诺与实现不符

- **位置**：`platforms/chatgpt/protocol/auth_flow.py:276-303`（`_ensure_refresh_token`）、`:1197-1200`（`run_protocol_login` 末尾）、`platforms/chatgpt/login_refresh.py:172-184`（env 未关 Codex）。
- **证据**：`_ensure_refresh_token` 在注入路线失败后**无条件**执行 `if self._env_flag("OAUTH_CODEX_RT_EXCHANGE", "1"): self.oauth_codex_rt_exchange(...)`，没有看 `chatgpt_rt_route`；而 `registration_engine.py:250-253` 的 docstring 明确写「`login` → 只走登录链，不跑 Codex（那条路在这个场景白花时间）」。
  刷新链更直接：`login_refresh` 构造的 flow 没有 `acquire_rt`，`run_protocol_login` 在登录成功后因为 `result.refresh_token` 为空（密码登录本身不产 RT）→ `:1199-1200` 调 `_ensure_refresh_token` → 走 Codex。也就是说**每一次"刷新 Token"的登录兜底都会额外跑一次 Codex OAuth**，与设置无关。
- **影响**：① 选 `login` 的用户仍要付 Codex 的 add-phone 成本（配了接码会**真租号花钱**；没配则多 3 轮 authorize 刷新）；② Codex 若成功，会拿回一个 Codex 签发的 RT，被 `plugin.py:530-534` 以 platform client_id 落库 → 下一次刷新 `invalid_client`（见 R-25）。
- **建议**：把"是否允许 Codex 兜底"做成显式参数/环境（`OAUTH_CODEX_RT_FALLBACK`），由 `resolve_chatgpt_rt_route` 决定；`login_refresh` 在 `route=login` 时设 `OAUTH_CODEX_RT_EXCHANGE=0`（它只想要 AT）；注册链只在 `route=codex` 时才允许兜底。

### R-04 [P1] 补 RT 的"密码登录"策略不 warmup，`device_id` 在同一轮内前后不一致

- **位置**：`platforms/chatgpt/rt_backfill.py:371-384`（`_try_platform_login`）、`platforms/chatgpt/protocol/mixins/platform_rt.py:188-190`（`auth_oauth_init` 调用）、`platforms/chatgpt/protocol/mixins/signup.py:166-168`（取不到 cookie 就 `uuid4()`）。
- **证据**：`_try_platform_login` 只 `seed_device_id(self.device_id)`（`rt_backfill.py:377`），而 `seed_device_id` 明确"只记值、不种 cookie"（`auth_flow.py:306-324`）。随后 `_build_platform_authorize` 把 `result.device_id`（库里的值 A）写进 authorize URL 的 `device_id` 参数（`platform_rt.py:60`）；紧接着 `auth_oauth_init(auth_url)` 从 cookie 找不到 `oai-did` → `uuid4()` 覆盖 `result.device_id`（B），后续 `get_sentinel_token` / `authorize_continue` 全用 B。
- **影响**：同一轮登录里"URL 说设备 A、sentinel/continue 说设备 B"，是比"换设备"更明显的异常特征；且每次补 RT 都把 B 落库（`build_extra_patch` 的 `device_id`），库里的设备标识被反复改写，"复用 oai-did 降低风控"的目标失效。注册链因为 warmup 已种 cookie，不受影响。
- **建议**：`_try_platform_login` 先 `check_proxy()` + `warmup()`（或显式 `session.cookies.set("oai-did", seed, domain=".chatgpt.com")`）；`auth_oauth_init` 在 `result.device_id` 已有值时不要用 uuid 覆盖。

### R-05 [P2] platform 路线的邮箱验证码走了"新注册"发码顺序

- **位置**：`platforms/chatgpt/protocol/mixins/platform_rt.py:242`（`kickoff_otp_delivery("platform_rt_login")`）、`platforms/chatgpt/protocol/mixins/signup.py:455-500`。
- **证据**：`kickoff_otp_delivery` 只把含 `existing` / `passwordless_login` / `passwordless_signup` 的 mode 或 `self._is_existing_account` 当已有账号；`"platform_rt_login"` 都不匹配，而注册链刚建完号时 `_is_existing_account=False`（`signup.py:294`）。于是走 `send_passwordless_otp(...)` 优先的新注册分支 —— 而同一文件 `:464-468` 的注释明说这条会新建 challenge、让已有账号的旧码失效。
- **影响**：多打一次注定 409 的请求，极端情况下把刚发出的 OTP 作废，随后 resend 才恢复；表现为"第一次等码超时、重发才成功"。
- **建议**：进入 `acquire_rt_via_platform_login` 时设 `self._is_existing_account = True`，或把 mode 改成 `existing_platform_rt`。

### R-06 [P2] route 默认值的注释与代码相反（文档漂移）

- **位置**：`platforms/chatgpt/chatgpt_registration_mode_adapter.py:129-131` vs `:137`。
- **证据**：注释写「默认保持 `codex`（不改既有行为）」，下一行 `DEFAULT_CHATGPT_RT_ROUTE = RT_ROUTE_LOGIN`。补 RT 的空配置行为、注册链的抢跑判定都读这个常量，读注释的人会得出相反结论。
- **建议**：改注释；这类"默认值"注释最好由测试锁定（`test_chatgpt_platform_rt_route.py` 已锁行为，补一句 docstring 校验即可）。

---

## 2. 重试 / 回退语义

### R-07 [P1] `TaskInterruption` 被 4 处 catch-all 吞掉，"停止/跳过"变成普通失败

- **位置**：
  - `platforms/chatgpt/registration_engine.py:141-144`
  - `platforms/chatgpt/protocol/mixins/codex.py:501-503`
  - `platforms/chatgpt/protocol/mixins/platform_rt.py:255-258`
  - `platforms/grok/register_browser.py:573-575`
- **证据**：`TaskInterruption` 继承 `RuntimeError`（`core/task_runtime.py:12`）。上面四处都是 `except Exception` → 转成 `RegistrationResult(success=False)` / `return False` / `result["error"]`。对比同仓已经修过的两处：`rt_backfill.py:220-224`、`login_refresh.py:124-126` 都是 `except TaskInterruption: raise`。
  最典型的是 Grok：`_read_code` 内部三处 `except TaskInterruption: raise`（`:634/:642/:694`）写得很认真，但 `register_grok_via_browser` 最外层 `:573` 又把它吞成 `result["error"]="StopTaskRequested: ..."`，于是 `grok/plugin.py:731` 的 `except TaskInterruption: raise` **永远不可达**，点"停止"会被记成一次 failed（且 retryable）。
- **影响**：① 停止任务后当前号仍会走完回退链（Codex→登录→重试策略），最长多花几分钟；② 该号被记成 failed，`consumed=False` 时邮箱会被放回池子；③ ChatGPT 侧若在 `_salvage` 时已有凭证，会把"用户主动停止"记成 success。
- **建议**：所有跨协议边界（引擎、flow 包装、浏览器流程）统一在 `except Exception` 前加 `except TaskInterruption: raise`；加一个"stop during OTP wait"的回归测试。

### R-08 [P1] `_ensure_two_factor` 在 salvage 边界之外，2FA 段一炸就丢号

- **位置**：`platforms/chatgpt/registration_engine.py:138-150`。
- **证据**：`try` 只包住 `flow.run_register(provider)`；`self._ensure_two_factor(flow, provider)`（`:146`）在 try 之外，且 `_set_mailbox_status(provider, "used")`（`:149`）在它之后。`bind_totp_via_login` 内部虽然兜了大部分异常（`two_factor.py:191-220`），但 `_journal_secret` → `_write_secret_journal`（`services/chatgpt_two_factor.py:238-272`）会做文件 IO，`_notify_secret` 只兜回调异常（`two_factor.py:137-146`），构造 `AuthFlow` 的 `create_http_session` 也可能抛。
- **影响**：账号已在 OpenAI 侧创建、AT/ST 已在 `flow.result` 上，但异常从 `run()` 抛出 → `PlatformRegistrationService`（`modules/platforms/registration.py:46-51`）转成 `RuntimeError` → 任务记 failed；**邮箱状态没被标 used**，别名被放回池子，下次再领到同一地址时 OpenAI 回"已有账号"，白跑一轮。号本身（凭证）也丢了。
- **建议**：把 `_ensure_two_factor` 包进 `try/except` 并记 metadata；`_set_mailbox_status` 放到 `finally`，按"是否已创建账号"决定 used/failed（见 R-19）。

### R-09 [P1] 注册失败分类过粗：`register_password` 任意非 200 都回退 OTP；`login_password_verify` 任意非 200 都当密码错

- **位置**：`platforms/chatgpt/protocol/mixins/signup.py:584-597`（密码校验）、`platforms/chatgpt/protocol/auth_flow.py:646-652`（注册失败回退）。
- **证据**：`login_password_verify` 对 `resp.status_code != 200` 一律 `return {}`，注释只讨论了 401；429/500/403 同样被当成"这个号不能用密码登"→ 静默切 OTP。`register_password` 返回 False 后，`run_register` 直接把流程当"已有账号"处理：`kickoff_otp_delivery(...)` + 等码。
- **影响**：一次 429 会让整条注册链去等一个**不存在的账号**的验证码，白等一个 `OTP_TIMEOUT`（默认 180s）；错误信息最终是"OTP 超时"，真实原因（限流/5xx）完全丢失。这是本次审计里最典型的"静默降级"。
- **建议**：按状态码分类：401/403 → 走 OTP；429/5xx/网络 → 抛可重试异常（不要进 OTP 分支）；`register_password` 失败时先 `fetch_client_auth_session_dump` 判断账号是否真的存在，再决定是否走已有账号路径。

### R-10 [P2] OTP 首次验证失败的重试条件三条链不一致

- **位置**：`auth_flow.py:671`（新号，仅 `"401"`）、`:843`（已有账号，`"401"/"409"`）、`:1153`（协议登录，`"401"/"409"`）。
- **证据**：同一类失败（`verify_otp` 的 409 `invalid_state`）在新号分支会直接抛出，在另外两条会重发重试。
- **影响**：新号在 409 场景下少一次补救机会；而 409 恰恰是"发码后 state 被切走"的高频错误。
- **建议**：抽一个 `_is_retryable_otp_error(exc)`，三处共用。

### R-11 [P2] `otp_sent_at` 在"注册密码失败"回退分支里是发码**之后**取的

- **位置**：`auth_flow.py:646-652`，对照 `:637-639` 的注释。
- **证据**：`register_password` 成功分支先 `otp_sent_at = time.time()` 再 `send_otp()`，注释明确写"先发再取时间会让 otp_sent_at 晚于邮件时间戳，被 issued_after 过滤掉"；失败分支却是 `kickoff_otp_delivery(...)` → `otp_sent_at = time.time()`，正好踩自己写的坑。
- **影响**：Outlook 的 cutoff 是 `otp_sent_at - 2`（`core/mailboxes/channels/outlook/backends.py:109-111`），iCloud 是 `-30`，多数情况能兜住；但如果邮件在同一个秒内到达且 provider 用严格 `>` 比较，就会把刚发的码过滤掉，表现为"发码成功但等不到"。
- **建议**：时间戳移到发码调用之前（与成功分支一致）。

### R-12 [P2] OAuth 刷新成功但 AT 未通过校验时仍按成功返回并落库

- **位置**：`platforms/chatgpt/token_refresh.py:403-415`（OAuth 分支）、`platforms/chatgpt/plugin.py:512-523`（写库）。
- **证据**：session 分支会 `if result.verified: return`，未通过则继续试 OAuth；OAuth 分支直接 `oauth_result.success = True; self._verify(oauth_result); return oauth_result`，不看 `verified`。插件随后把 `access_token` 无条件写进 patch。
- **影响**：服务端偶发返回 200 + 一个打任何接口都 401 的 AT（代码注释自己记过这个实测），会覆盖库里原本可用的 AT；账号从"能用"变"不能用"，且状态仍显示正常。
- **建议**：OAuth 分支同样要求 `verified` 才 success；未通过时保留旧 AT，把 `verified=False` + 原因返回给调用方。

### R-13 [P2] Grok 登录协议刷新把所有异常归为 `retryable`

- **位置**：`platforms/grok/token_refresh.py:225-235`。
- **证据**：`except Exception` 一律 `kind="retryable"`；只有浏览器返回 `sso_rejected` 才判 permanent（`:241-250`）。账号被删/停用时浏览器通常抛异常或返回空 token，落不到 `sso_rejected` 分支。
- **影响**：已废的号永远不会被标失效，每轮批量刷新都重跑一次浏览器 device flow（每次几十秒），与 ChatGPT 侧 `looks_like_banned` 的处理不对称。
- **建议**：对异常文本跑 `looks_like_banned`/`sso_rejected` 同款判定；判不出时给 `unknown`，不要给 `retryable`。

### R-14 [P3] Sentinel/QuickJS 的"模块缺失"按可重试处理，会白烧邮箱

- **位置**：`platforms/chatgpt/protocol/sentinel.py:82-83`、`core/environment.py:67-92`。
- **证据**：`ImportError` 被包成 `RuntimeError("Sentinel QuickJS 模块缺失: ...")`；`is_environment_error_message` 只认 `"找不到 node 运行时"` 等少数文本，不认 QuickJS 模块/sdk.js 缺失。任务层因此按 retryable 重开整轮（`services/task_runners.py:320-334`），每轮都重新领一个邮箱。
- **影响**：部署缺 `quickjs`/`sdk.js` 时，每个号会烧掉 `1 + register_retry_times` 个别名，最后全部失败。
- **建议**：把 sentinel 的模块/资源缺失纳入环境错误白名单（`EnvironmentNotReadyError`）。

### R-15 [P3] 适配器重试提示与实际行为不符

- **位置**：`platforms/chatgpt/chatgpt_registration_mode_adapter.py:305-330`。
- **证据**：失败重试时日志固定写"换用下一个邮箱重试"，但 `context.email` 非空（用户指定邮箱）时传下去的是同一个地址；而且 `context.mailbox` 是同一个对象，只有 `get_email()` 会领新号 —— 指定邮箱场景下两次尝试用的是同一地址，重试没有意义。
- **建议**：`context.email` 非空时不要重试（或明确提示"同一邮箱重试"）。

---

## 3. 邮箱与接码

### R-16 [P1] 指定邮箱注册仍从号池领一个别名，并在成功后把它标成 `used`

- **位置**：`platforms/chatgpt/protocol/mailbox_adapter.py:80-88`、`platforms/chatgpt/registration_engine.py:143/149`。
- **证据**：`create_mailbox()` 无条件 `account = self._mailbox.get_email()`，然后 `address = self._fixed_email or account.email`；`self._account` 已被绑定到刚领到的别名。实测：

  ```
  address: fixed@x.com | pool get_email calls: 1 | bound account: pool1@x.com
  ```

  注册成功后 `_set_mailbox_status(provider, "used")` 把 `provider.account`（即 `pool1@x.com`）标成 used。
- **影响**：每次"指定邮箱"注册都会白烧一个号池别名（iCloud：对**当前平台**标 `used`，该地址不能再给本平台用、其它平台仍可；Outlook：`used` 直接移出池子）。用户指定 10 个邮箱，池子里就少 10 个本平台可用别名，且这些别名从未被使用过。
- **建议**：`fixed_email` 非空时跳过 `get_email()`（用 `FixedAddressProviderAdapter` 的思路，只做 baseline/收信）；`_set_mailbox_status` 只在真正领过号时调用。

### R-17 [P1] add-phone 未配置时返回原 URL 静默继续；2FA/刷新链根本没传 SMS 回调

- **位置**：`platforms/chatgpt/protocol/mixins/add_phone.py:384-396`、`platforms/chatgpt/registration_engine.py:431-441`、`platforms/chatgpt/login_refresh.py:155-160`。
- **证据**：`_handle_add_phone_via_env` 在既没有 `OPENAI_PHONE_NUMBER` 也没有可用 SMS 时只 `logger.warning(...)` 然后 `return continue_url or ""` —— 把**进入 add-phone 时的 URL 原样返回**。调用方（`auth_flow.py:862-865`、`:1171-1174`、`codex.py:338-343`）把它当"处理完成"继续走 redirect/session，最终失败在无关的 `get_auth_session` 上。
  另外：`bind_totp_via_login(...)`（注册引擎 `:431-441`）与 `login_refresh._build_flow()` 都没有传 `sms_callback`，即使全局配了接码，这两条链撞上 add-phone 也走"未配置"分支。
- **影响**：① 日志里只有一行 warning，最终错误是"未拿到 session/AT"，排查方向被带偏；② 配了接码的用户在"绑 2FA"和"刷新 Token"两条链上仍然过不了 add-phone，与"接码已启用"的预期不符。
- **建议**：未配置时抛一个带明确文案的 `RuntimeError`（或 `NonRetryableRegisterError`），让上层立即失败并提示"该号需要手机验证"；`bind_totp_via_login` / `login_refresh` 传入 `build_phone_callback(...)`。

### R-18 [P2] `_SMS_VERIFY_LOCK` 是进程级全局锁，跨整个租号周期且无超时

- **位置**：`services/sms/controller.py:89-95`（acquire）、`:193-201` / `:231-247`（release）。
- **证据**：锁在 `get_phone()` 里 `acquire()`，直到 `report_success()`/`cleanup()` 才释放；锁对象是门面里的单个 `threading.RLock`（`services/sms_service.py:51`），不是"按号码"或"按 activation"。
- **影响**：并发注册（`task_runners.py:339-343` 的线程池）里所有走 SMS 的号会被串行化：一个号最长 3 个号码 × 80s，其余线程全部阻塞在 `get_phone()`，表现为任务"卡住"；若线程在 acquire 之后被 `BaseException` 杀掉（非 `Exception`，`finally` 不执行），锁永不释放，后续所有 SMS 流程永久阻塞。跨进程（多 worker）时 `_SMS_CACHE` 文件（`services/sms/providers.py:342-372`）也没有文件锁，两个进程可能复用到同一个号码。
- **建议**：锁只保护缓存读写（临界区毫秒级），租号/等码不要持锁；`acquire(timeout=...)` + `try/finally`；跨进程用 `fcntl`/`filelock` 保护缓存文件。

### R-19 [P2] 邮箱状态记账不区分"号已创建但凭证没拿全"

- **位置**：`platforms/chatgpt/registration_engine.py:141-144`、`:149`；Grok 对应 `platforms/grok/plugin.py:775-790`。
- **证据**：`_set_mailbox_status(provider, "used" if registration.success else "failed")`；`registration.success` 只有在 `_salvage` 有凭证时才为 True。若账号已在 OpenAI 侧创建（`register_password` 成功）但后续 `get_auth_session`/OTP 失败，`_salvage` 没有凭证 → success=False → 邮箱标 `failed` → iCloud `release_alias_claim` 放回 available。
- **影响**：下一次领到同一地址时 OpenAI 回"已有账号"，流程转 OTP 登录（若密码已记下还能救，否则白跑）；这正是"一次性邮箱复用"最危险的形态 —— 地址已被消耗，池子却认为它可用。
- **建议**：`_on_password` 回调触发时就记一个 `account_created` 标志；失败时据此标 `used`（而不是 failed），或在 extra 里留 `chatgpt_partial_registration` 供下次识别。

### R-20 [P3] `login_refresh` 自抄了一份 OTP 超时解析

- **位置**：`platforms/chatgpt/login_refresh.py:186-194`。
- **证据**：`core/base_platform.py:104-112` 的 `resolve_mailbox_otp_timeout` docstring 明确说"这段优先级规则以前抄了四份……单点定义"，但 `login_refresh` 仍是手写循环（默认 180 一致，但键顺序/异常处理是另一份）。
- **影响**：以后改默认值/加键（例如 `otp_timeout_ms`）时这条链会静默漂移。
- **建议**：改为 `resolve_mailbox_otp_timeout(self.extra_config)`。

---

## 4. 验证码 / Turnstile solver

### R-21 [P2] solver 根本没有接进注册链：`mint_turnstile` 与 `captcha_solver` 都是死代码

- **位置**：`platforms/grok/turnstile_mint.py:243-270`、`core/base_platform.py:327-342`、`api/tasks.py:83`。
- **证据**：全仓（除 `scripts/grok_turnstile_mint.py` CLI 与测试）没有任何 `mint_turnstile(` 调用；`_make_captcha` 也没有调用点。`RegisterConfig.captcha_solver` 从 API 一路传到 `task_runners.py:98/233`，之后再没人读。Grok 注册实际靠 camoufox 过 CF，ChatGPT 靠 Sentinel QuickJS。
- **影响**：① 用户配置的 `captcha_solver`（yescaptcha/local_solver）不产生任何效果，属于"配置项说谎"；② 一旦 CF 策略变化需要显式 Turnstile token，没有可用的接线点；③ solver 失败处理（重试/降级）无从谈起 —— 这条链从未执行。
- **建议**：要么把 `mint_turnstile` 接进 `register_browser` 的 CF 失败分支（并把 `captcha_solver` 传下去），要么删掉 `captcha_solver` 配置与 `turnstile_mint`，避免误导。

### R-22 [P1] Grok 发码页分类把"任何未识别文本"当成"地址已消耗"，CF 拦截也会烧别名

- **位置**：`platforms/grok/register_browser.py:120-142`（`classify_send_code_page` 的兜底分支）、`:474-490`（调用点）。
- **证据**：兜底 `SEND_CODE_NO_CONFIRMATION` 固定 `consumes_address=True, retryable=False`；调用点据此把邮箱标 `used` 并抛 `NonRetryableRegisterError`（`grok/plugin.py:775-790`）。而"页面没出现确认文案"既可能是 x.ai 真拒绝，也可能是 CF 挑战页、网络半截、页面还没渲染完（代码自己在 `:419-447` 就为重载/等表单做了 3 轮）。
- **影响**：一次 CF 抖动就会永久消耗一个别名（iCloud `used` 不可再领），并且任务被标成"不可重试"，直接放弃该序号。
- **建议**：兜底分支改为"重试 N 次后仍无确认"才 `consumes_address=True`；把 CF 挑战页（`cf-challenge`/`Just a moment`）单独识别为环境错误（不消耗地址）。

### R-23 [P3] Grok 读码超时与读信失败不可区分

- **位置**：`platforms/grok/register_browser.py:696-702`。
- **证据**：`wait_for_code` 的 `TimeoutError` 被 `except Exception` 吞成日志 + `return ""`；iCloud 路径则把连续读信错误计数后继续轮询到 deadline（`:645-655`）。两者最终都报"未收到验证码"。
- **影响**：DB 坏/别名不存在这类"永远收不到"的故障要等满 240s，且错误文案指向"没收到码"。
- **建议**：把"读信异常"与"超时"分开返回（或抛不同异常），调用方据此决定是否立即失败。

---

## 5. 注册成功后凭证落库完整性

### 5.1 字段落库矩阵（ChatGPT，`RegistrationResult` → `Account` → `extra_json`）

| 字段 | 来源 | 落库位置 | 现状 |
|---|---|---|---|
| AT | `AuthResult.access_token` | `Account.token` + `extra.access_token` | ✅ |
| RT | `AuthResult.refresh_token` | `extra.refresh_token` | ⚠️ 可能被邮箱 RT 覆盖（R-01）；route=codex 时 client 归属错（R-24/R-25） |
| ST | `AuthResult.session_token` | `extra.session_token` | ✅ |
| IDT | `AuthResult.id_token` | `extra.id_token` | ✅（约 45% 账号有，取决于路线） |
| SSO | — | — | ChatGPT 无此字段；Grok 存 `token` + `extra.sso`（`grok/plugin.py:872-880`）✅ |
| totp | `flow.result.totp_secret` | `extra.totp_secret` | ✅（加密落盘） |
| device_id | `AuthResult.device_id` | `extra.device_id` | ✅（604/667 有；补 RT 的密码登录路线会漂，见 R-04） |
| client_id | 应为签发 RT 的 client | `extra.client_id` | ❌ 见 R-24/R-25 |
| account_id / workspace_id | `RegistrationResult.account_id/workspace_id` | `Account.user_id` / `extra.workspace_id` | ❌ 恒为空（R-26） |
| cookies | `AuthResult.cookie_header` | `extra.cookies` | ✅ |

### R-24 [P1] `client_id` 在三个结果类型里全部丢失，落库值永远来自"默认常量"

- **位置**：`platforms/chatgpt/registration_engine.py:48-66`（`RegistrationResult` 无 `client_id`）、`:69-89`（`from_auth_result` 不复制）、`platforms/chatgpt/chatgpt_registration_mode_adapter.py:370-373`、`platforms/chatgpt/rt_backfill.py:123-145`（`BackfillResult` 无 `client_id`）、`:425-435`（`_absorb` 不复制）、`platforms/chatgpt/token_refresh.py:29-80`（`TokenRefreshResult` 无 `client_id`）、`platforms/chatgpt/plugin.py:530-534`。
- **证据**：`AuthResult.client_id` 是存在的（`auth_flow.py:67`），但 `RegistrationResult.from_auth_result` 只搬 4 个 metadata 键；`getattr(result, "client_id", "")` 恒为 `""` → `resolve_client_id_for_patch("")` → `_default_client_id()` = `PLATFORM_CLIENT_ID`。补 RT 的 `build_extra_patch`（`services/chatgpt_rt_backfill.py:158-162`）同样恒为 `""`。刷新链的 `TokenRefreshResult` 没有该字段，`plugin.py:531` 恒取默认。
  另一个地雷：`run_register` 在拿到 RT 后会把 `result.client_id` 设成 `_oauth_client_id`（`auth_flow.py:958-961`），而 `_oauth_client_id` 的初值是占位符 `"YOUR_OPENAI_WEB_CLIENT_ID"`（`:182`），`signup()` 又会用 ChatGPT Web 的 authorize URL 覆盖它（`signup.py:109` + `codex.py:32-39`）—— 即便把字段接上，存进去的也会是**Web client id**，不是签发 RT 的 platform/Codex client。
- **影响**：当前 303 个账号的 `app_2SKx...`（platform）是"默认常量碰巧对了"；一旦 RT 由 Codex 路线签发（`route=codex` 或刷新链兜底），client_id 仍然是 platform → 下次 OAuth 刷新 `401 invalid_client`。生产库已有 1 个账号是 `app_EMoam...`（Codex），363 个账号完全没有 client_id。
- **建议**：① 三个结果类型都加 `client_id`，`from_auth_result`/`_absorb` 复制；② `_exchange_platform_code` 成功后显式 `self.result.client_id = client_id`；③ Codex 交换成功后 `self.result.client_id = self._oauth_client_id`（此时它已被 `_build_codex_authorize` 设为 Codex client，`codex.py:408`）；④ 不要把 Web client 当作 RT 的 client。

### R-25 [P1] `client_id` 缺省值三处不一致，且刷新路径永远用不到 `token_refresh` 的 platform 默认

- **位置**：`platforms/chatgpt/plugin.py:342-355`（默认 Codex `app_EMoam...`）、`services/chatgpt_sync.py:191`、`services/external_sync.py:128`（同样默认 Codex）vs `platforms/chatgpt/token_refresh.py:167`（默认 platform `app_2SKx...`）。
- **证据**：`plugin.py:355` 构造刷新用的 `AccountRecord` 时 `a.client_id = extra.get("client_id", "app_EMoam...")` —— 缺省值是 **Codex** client；`refresh_account` 把它直接传给 `refresh_by_oauth_token(client_id=account.client_id)`（`token_refresh.py:406-409`），而后者写的是 `client_id = client_id or self._oauth_client_id`（`:290`）。因为调用方**总是**给了一个非空值（Codex 默认），`self._oauth_client_id = PLATFORM_CLIENT_ID`（`:167`）这条"正确答案"永远不会生效。
- **影响**：`extra` 缺 `client_id` 的账号（生产库 363/667）刷新时用的是 Codex client；如果它的 RT 是 platform 签发的（本仓默认路线），必然 `401 invalid_client`。当前这 363 个恰好都没有 RT（`refresh_account` 不会进 OAuth 分支），问题被"没有 RT"掩盖；一旦补到 RT、或导入带 RT 的旧号，就会命中。`invalid_client` 又被 `apply_chatgpt_status_policy` 归为 `configuration` **不改状态**（`services/chatgpt_account_state.py:228-234`），账号会一直显示"正常"、每轮刷新白打一次上游。上传/同步侧同样默认 Codex，面板拿到的 client 与 RT 签发者可能不符。
- **建议**：`plugin.py:355` 的默认值改为 `resolve_client_id_for_patch(extra.get("client_id", ""))`（platform 默认）；`refresh_by_oauth_token` 不要静默忽略管理器配置的默认值（或在调用方缺值时传空）；上传/同步侧缺值就留空，不要注入 Codex 常量；对 `configuration` 增加"连续 N 次后提示修配置"的可观测信号。

### R-26 [P2] `account_id` / `workspace_id` 从未赋值

- **位置**：`platforms/chatgpt/registration_engine.py:55-56`、`:69-89`；`chatgpt_registration_mode_adapter.py:337`。
- **证据**：全协议层没有任何 `result.account_id = ...` / `result.workspace_id = ...`；`build_account` 把 `user_id=result.account_id` 写成空串，`extra.workspace_id` 也是空串。生产库 667 个账号的 `workspace_id` 全部为空。
- **影响**：依赖 `user_id`/`workspace_id` 的展示与面板字段只能事后从 AT/IDT 的 JWT 里推导（`status_probe.extract_chatgpt_account_id`），注册当场拿到的 `workspace_id`（`create_account`/`workspace_select` 里有）被丢掉。
- **建议**：在 `run_register` 收尾处从 JWT/`workspace_select` 响应补齐；`RegistrationResult` 已有字段，只差赋值。

### R-27 [P2] `refresh_token` 模式缺 RT 仍按 success 计数

- **位置**：`platforms/chatgpt/registration_engine.py:152-174`、`:516-546`。
- **证据**：`_note_missing_refresh_token` 只写 `metadata["partial"]=True` + 日志；`_salvage` 在"有 AT/ST 但缺 RT"时也返回 `success=True`（`:534-546`）。`PlatformRegistrationService` 只看 `result.success`（`modules/platforms/registration.py:47-52`），任务计数 +1。
- **影响**：用户勾了"有 RT"方案，任务显示成功，库里 RT 为空；后续批量刷新/补 RT 才发现，且这些号会被 `select_backfill_targets` 重新挑出来补（`only_missing_rt=True`），等于把成本推迟。
- **建议**：`partial=True` 时让 `RegistrationResult.success` 仍为 True（账号可用），但把 `retryable=False` + `partial` 透传到任务日志/统计里单独计数；或在 UI 上把"部分成功"单列。

### R-28 [P3] 落库形态：更新路径明文（跨报告引用）

- **位置**：`core/db/repository.py:160-162`、`:388-395`。
- **说明**：`merge_account_fields` 对非 `AccountModel` 输入返回 `json.dumps(extra)` 明文，`upsert` 更新分支用 `setattr(existing, "extra_json", ...)` 绕过 `AccountModel.__init__` 的加密钩子。这与 `docs/audit/2026-10-30/data-layer.md` 的 **D-02** 是同一条，本报告不重复展开；注册链（`save_account(account)`）正好走这条路径，重注册/覆盖已有账号时会把 AT/RT/ST/totp 明文写回。建议按 D-02 的修法处理。

---

## 6. 并发注册时的共享状态

### R-29 [P2] 指定邮箱 + `concurrency>1` 的判重是 TOCTOU

- **位置**：`services/task_runners.py:274-276`。
- **证据**：`if req.email and _api.account_already_registered(...)` 与真正 `_platform.register(...)` 之间没有任何锁/占位；N 个线程同时通过检查 → N 个线程用同一邮箱注册。唯一索引 `uq_accounts_platform_email`（`core/db/models_account.py:36-41`）只能保证最后一行，挡不住上游创建多个账号。
- **影响**：同一邮箱在上游被重复注册/登录，后写的凭证覆盖先写的；并发越高越容易触发。
- **建议**：`req.email` 非空时强制 `concurrency=1`，或用进程内 per-email 锁 + DB 占位行（`status=registering`）。

### R-30 [P2] Grok 发码节流在锁内 `sleep`，且只在进程内生效

- **位置**：`platforms/grok/plugin.py:76-92`。
- **证据**：`_throttle_send_code` 持 `_SEND_CODE_LOCK` 执行 `time.sleep(wait)`，`wait` 最长等于 `grok_send_code_min_interval`（注释建议 240s）；`time.sleep` 不响应 `task_control`，`plugin.py:733-737` 的 `except TaskInterruption` 在这段时间内不会触发。`_SEND_CODE_LAST_TS` 是模块级变量，多进程部署时每个进程各算一份，限流形同虚设。
- **影响**：点"停止"要等节流睡完；并发任务被串行化成"每 240s 一个"；多 worker 时又会一起撞 x.ai 限流。
- **建议**：锁只保护时间戳读写，`sleep` 放到锁外并分片 `checkpoint`；跨进程用文件锁或 DB 记录时间戳。

### R-31 [P2] SMS 全局锁与缓存（见 R-18）

- **位置**：`services/sms/controller.py:89-95`、`services/sms/providers.py:342-372`。
- **说明**：这是"并发注册共享状态"里影响最大的一处：锁的粒度是进程而不是号码，持锁时长是整个租号+等码周期。详见 R-18。

### R-32 [P3] `ProxyPool` 的成功/失败计数是无锁 read-modify-write

- **位置**：`core/proxy_pool.py:77-96`。
- **证据**：`report_success`/`report_fail` 各自开 session 读行、`+= 1`、commit；`get_next` 的 `self._index` 有锁，但计数没有。并发注册完成时同一代理的 `success_count` 会丢更新。
- **影响**：代理排序（`get_next` 按成功率）失真；`report_fail` 的"5 次失败停用"可能因丢更新而延迟。影响有限，但会让"注册标签代理"的均衡选择偏移。
- **建议**：用 SQL `UPDATE ... SET success_count = success_count + 1` 原子自增。

### R-33 [P3] `_get_env` 回退进程环境变量，"实例隔离"不彻底

- **位置**：`platforms/chatgpt/protocol/auth_flow.py:213-221`。
- **证据**：注释说"env_overrides 优先，回退进程环境变量"，所以任何全局导出的 `OTP_TIMEOUT` / `OAUTH_CODEX_RT_EXCHANGE` / `WEBUI_ALLOW_LOGIN` 都会影响所有并发 flow；而 `AuthFlow.__init__` 的注释（`:111-117`）宣称"进程全局环境一个字节都不动"。
- **影响**：单进程部署下通常无害；一旦有人为了调试 `export OAUTH_CODEX_RT_EXCHANGE=0`，所有任务都会静默改行为。
- **建议**：协议层只读 `env_overrides`（CLI 入口显式注入 `os.environ` 快照），或在启动时把需要的键固化进配置。

### R-34 [P3] iCloud 主号锁是进程内 `RLock`，且 `_ACCOUNT_LOCKS` 无界增长

- **位置**：`services/icloud/_locks.py:33-41`。
- **证据**：`_ACCOUNT_LOCKS.setdefault(int(account_id), threading.RLock())` 只在本进程有效；`claim_alias` 依赖它做"同一主号不并发领号"（`services/icloud/aliases.py:146`）。当前部署是单进程（`main.py:619` 的 `uvicorn.run` 无 workers），所以不触发；多 worker 时两个进程可能领到同一个 `available` 别名。
- **建议**：把锁换成 DB 条件更新（`UPDATE ... WHERE pool_status='available'`）或文件锁；`_ACCOUNT_LOCKS` 用 `WeakValueDictionary` 或按主号数量上限清理。

---

## 7. 执行器 / HTTP 层（与注册链直接相关）

### R-35 [P2] `ProtocolExecutor` 没有超时

- **位置**：`core/executors/protocol.py:34-40`。
- **证据**：`self.s.get(url, headers=..., params=...)` / `post(...)` 均未传 `timeout`；curl_cffi 的默认是"不超时"。协议层自己建的 session（`http_client.create_http_session`）在每个调用点都显式传 30s，但这个执行器没有。
- **影响**：使用 `executor_type=protocol` 的平台/脚本遇到黑洞代理会无限挂起，占住线程池 worker；任务停止也无法打断（没有 checkpoint）。
- **建议**：构造时接受 `timeout` 并传给每个请求；或直接复用 `create_http_session`。

### R-36 [P3] `PlaywrightExecutor._init` 启动失败会泄漏 playwright driver 进程

- **位置**：`core/executors/playwright.py:24-43`。
- **证据**：`self._pw = sync_playwright().start()` 之后 `chromium.launch(**launch_opts)` 若抛（浏览器未安装/代理参数非法），构造函数直接抛出，`self._pw.stop()` 永远不会被调用；对象也没机会进 `close()`。
- **影响**：每次失败泄漏一个 node driver 进程；批量重试会累积。
- **建议**：`_init` 包 try/except，失败时 `self._pw.stop()` 再抛。

### R-37 [P2] `http_client` 降级分支对 POST 自动重试，且 curl_cffi 分支忽略 `user_agent`

- **位置**：`platforms/chatgpt/protocol/http_client.py:127-165`。
- **证据**：`_HAS_CFFI=False` 时用 `requests.Session` + `Retry(total=3, status_forcelist=[429,500,502,503,504], allowed_methods=["HEAD","GET","POST"])` —— 会对 `POST /api/accounts/user/register`、`email-otp/send` 这类非幂等请求自动重发；curl_cffi 分支则只重试 TLS 错误（`:101-115`），且 `create_http_session(user_agent=...)` 的参数在 cffi 分支完全没用（只有 requests 分支 `:168` 使用）。
- **影响**：在没有 curl_cffi 的环境（或 import 失败）里，一次 429/5xx 会重复提交注册/发码，可能触发服务端 `max_check_attempts` 或重复建号；UA 参数不一致会让指纹与 UA 脱节。
- **建议**：`Retry` 的 `allowed_methods` 去掉 POST（或只对幂等端点单独重试）；cffi 分支把 `user_agent` 写进 `session.headers`。

---

## 8. 建议修复顺序

1. **凭证正确性（P0/P1）**：R-01（邮箱 RT 覆盖）→ R-02（mail_provider 类型）→ R-24/R-25（client_id 归属与默认值）。
2. **账号/邮箱不丢（P1）**：R-08（2FA 段丢号）→ R-16（指定邮箱烧别名）→ R-19（已创建号标 failed）。
3. **控制语义（P1）**：R-07（吞 TaskInterruption）→ R-09（失败分类）→ R-17（add-phone 静默放行）。
4. **路线一致性（P1）**：R-03（login 仍跑 Codex）→ R-04（device_id 漂移）→ R-05（OTP 分支）。
5. **成本/稳定性（P2）**：R-18/R-30/R-31（全局锁）→ R-22（Grok 烧别名）→ R-12（未校验 AT）→ R-35（无超时）。
6. **可观测/清理（P3）**：R-21（solver 死代码）→ R-06/R-20/R-26/R-27 → R-32/R-33/R-34/R-36/R-37。

---

## 9. 附录：验证记录

### 9.1 既有测试（全部通过，说明上述问题不在测试覆盖内）

```
$ .venv/bin/python -m pytest tests/test_chatgpt_registration_engine.py \
    tests/test_rt_route_reaches_protocol_flow.py tests/test_chatgpt_rt_backfill.py -q
73 passed in 2.14s
```

`tests/test_chatgpt_client_id_present.py` 只做字符串包含断言（`"client_id" in combined`），无法发现 R-24 的"字段存在但永远是空"。

### 9.2 探针 1：邮箱 metadata 覆盖账号 RT/client_id

```python
r = RegistrationResult(success=True, email="a@outlook.com", password="pw",
                       access_token="AT", refresh_token="RT_CHATGPT", session_token="ST")
r.metadata.update({"client_id": "MS_CLIENT_ID", "refresh_token": "MS_RT",
                   "password": "MS_PW", "mail_provider": "microsoft"})
ChatGPTRegistrationModeAdapter("refresh_token").build_account(r, "").extra
# → {'access_token': 'AT', 'refresh_token': 'MS_RT', 'session_token': 'ST',
#    'client_id': 'MS_CLIENT_ID', 'password': 'MS_PW'}
```

### 9.3 探针 2：`BaseMailbox` 没有 `wait_for_otp`

```
BaseMailbox.wait_for_otp: False
BaseMailbox.wait_for_code: True
MailboxProviderAdapter is MailProvider: True
```

### 9.4 探针 3：指定邮箱仍从池里领号

```
address: fixed@x.com | pool get_email calls: 1 | bound account: pool1@x.com
```

### 9.5 生产库只读抽样（`mode=ro`）

| 指标 | 值 |
|---|---|
| ChatGPT 账号总数 | 667 |
| 有 `refresh_token` | 301 |
| 有 `session_token` | 667 |
| 有 `id_token` | 300 |
| 有 `totp_secret` | 664 |
| 有 `device_id` | 604 |
| `client_id` 为空 | **363** |
| `client_id = app_2SKx...`（platform） | 303 |
| `client_id = app_EMoam...`（Codex） | 1 |
| `mail_provider` 分布 | `icloud_remote` 295 / `icloud_local` 221 / 空 151 |

### 9.6 未覆盖 / 明确排除

- RT 交换的 `mail_provider` 在"注册三条路 + 补 RT 密码登录 + 补 RT 会话复用"上的历史修复（`7f6be46` / `a093ab4`）已核对，未重复报告；本报告 R-02 是**注册引擎**这条尚未修的同类问题。
- `data/account_manager.db` 中 `chatgpt_token_refresh` 的历史错误（385 条 `TOTP 验证失败: 403 ... has been`）属于上游封号，不在本次范围。
- `docs/audit/2026-10-30/data-layer.md` 的 D-02（SQLite upsert 明文）与 R-28 是同一处，未重复展开。
