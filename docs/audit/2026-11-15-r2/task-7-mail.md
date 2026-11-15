# Task 7 — 邮箱域：渠道、号池、导入、OAuth
状态: 完成（第一版；所有结论均已实跑复现）

## 结论

范围可运行、可验证。**渠道注册表「未知 provider 静默回落」这条历史缺陷确实已修好**：
`provider=""` / `"typo"` / 纯空白三种输入都抛 `ValueError` 并列出可用渠道，没有落到任何默认渠道。
导入侧的同类问题也修好了（`MailImportRegistry.get` 对未知类型抛错）。

真实存在的问题集中在**错误分类**上：OAuth 换发失败**不分可重试与永久**（又同时把 429 这种
瞬态错误当成永久、把 `invalid_grant` 这种永久错误在 IMAP 路径上吞成"等码超时"）；
Outlook 三个 backend **一个都没接** `is_transient` / `max_consecutive_errors` 钩子，
而两个 iCloud 渠道都接了 —— 同一个框架里两套口径。另外 Outlook 号池**没有
`in_use` 回收**，任务一崩就永久少一个号，且错误提示指向"请导入新邮箱"（错的修法）。

已复现：4 Important / 3 Minor / 0 Critical。既有单测 45 条全绿（`tests/test_mail_imports_service.py`、
`test_outlook_mailbox_oauth.py`、`test_outlook_import_field_order.py`、`test_outlook_pool_selection.py`）。

## 发现

### T7-1 — Important — OAuth 换发失败不区分「可重试 / 永久」，且永久错误也跑满 12 次请求
`core/mailboxes/channels/outlook/mailbox.py:587-600`（只有 `invalid_grant` + `service abuse mode`
这一种组合被特判）、`:636-640`（其余一律收敛成 `oauth_token_failed`）；
`services/mail_imports/providers.py:127-149`（`ok=False` 即该行导入失败）。

- 复现：mock `requests.post`，让 token 端点分别返回 400 `invalid_grant`（永久）与 429（瞬态）。
  `.venv/bin/python` 注入假响应的脚本（见"复现命令"）输出：
  `invalid_grant -> oauth_token_failed | token requests: 12 | endpoints: 3`；
  `429 -> oauth_token_failed | token requests: 12`。
- 判据：`_token_endpoints()` 3 个端点 × `_oauth_scope_candidates()` 4 个 scope，全部试完才返回，
  期间没有任何基于错误码的短路。
- 影响：**导入时 429/网络抖动被当成永久失败**——该行不落库、被计入 `failed`，用户看到
  "可用性检测未通过"（无错误码），只能手工重贴；一批 1000 行、8 worker 时，坏行还会放大成
  最多 12000 次 token 请求。反向：真正的 `invalid_grant` 也只得到 `oauth_token_failed`，
  调用方无法据此把账号标失效。
- 修：`probe_oauth_availability` 按 `error` 字段分三档（`invalid_grant`/`invalid_client` →
  `reason="permanent"` 并立即 return；429/5xx/连接异常 → `reason="transient"` 且换端点前先退避）；
  导入侧只把 `permanent` 记 `failed`，`transient` 计入新字段 `retryable` 让前端重试。

### T7-2 — Important — `wait_for_code` 跨渠道不统一：永久错误在 IMAP 上变成"等码超时"，服务挂掉在 Outlook 上无法与"没收到码"区分
`core/mailboxes/channels/outlook/backends.py:208-212`（IMAP per-folder 全捕获后 `continue`）、
`:221-226` 与 `:448-452`、`:534-538`（三处 `_run_polling_wait` **均未传** `is_transient` /
`max_consecutive_errors`）；对照 `core/mailboxes/base.py:72-75`（钩子是可选参数）、
`modules/mail/icloud_local.py:382-392`、`modules/mail/icloud_remote.py:401-414`（两个 iCloud 渠道都传了
`is_transient=is_recoverable`、`max_consecutive_errors=4`、`is_fatal_busy=is_upstream_busy`）。

- 复现（全部注入假网络层，无真实连接）：
  - 永久错误 `invalid_grant`：IMAP 路径 `token_attempts=12`、最终 `TimeoutError 等待验证码超时 (12s)`；
    Graph 路径同一错误 `attempts=1`、立刻 `RuntimeError ... [oauth_token_failed]`。
  - 瞬态 `HTTP 503`：Graph 路径 `polls=0`、静默等满 12s 报 `TimeoutError`；
    IMAP 路径同样报 `TimeoutError`。
  - MailAPI backend 恒 500：`TimeoutError (9s)`，中途一次连接抖动后被吸收、随后成功取到码（这部分行为是对的）。
- 判据：框架的 `is_transient` 钩子表达"这一轮要不要再读一次"、`max_consecutive_errors` 表达"服务是否真挂了"。
  Outlook 三条 backend 一个都没接 —— 于是 IMAP/MailAPI **把认证失败、404、DNS 失败都吸收进轮询**，
  真正永久的问题表现为"没收到码"；Graph 反过来**一次瞬态就穿透整轮**（`poll_once` 内层 try 只覆盖
  每个 folder，`_get_oauth_access_token` 在 folder 循环之外，异常直接冲出 `poll_once` → `base.py:113` 因
  `is_transient is None` 原样抛出）。
- 影响：一次 30s 读超时/一次 token 端点抖动 = 判整轮注册失败（与 iCloud 修这条前的 80% 失败同因）；
  反之永久 `invalid_grant` 的账号被反复重试、直到超时才失败，账号本身永不标失效。
- 修：三个 backend 统一传钩子：`is_transient` 认 5xx/429/连接类异常，`max_consecutive_errors=4`
  与 iCloud 同口径；`_get_oauth_access_token` 的失败按 T7-1 的 `permanent` 直接抛致命错误，
  `transient` 才进容错循环。

### T7-3 — Important — 换发回来的 `refresh_token` 被丢弃：RT 轮换后账号必然 `invalid_grant`
`core/mailboxes/channels/outlook/mailbox.py:602-622`（成功分支只取 `access_token` / `expires_in`，
响应体里的 `refresh_token` 未读）、`:705-732`（缓存只写 access token）、
`core/db/outlook_repository.py`（无任何 `refresh_token` 回写路径）。

- 复现：mock token 端点返回 `{"access_token":"AT","expires_in":3600,"refresh_token":"NEW-RT-ROTATED"}`，
  `probe_oauth_availability` 返回键为 `['access_token','endpoint','expires_in','message','ok','reason','scope_label']`
  —— **不含** `refresh_token`。
- 判据：微软 v2 token 端点在部分场景会轮换 RT（旧值随即失效）。本项目从没把新值写回池。
- 影响：账号能正常用一段时间，之后每次换发都 `invalid_grant`，表现为"账号忽然全部失效"、
  且 T7-1 会把原因压成 `oauth_token_failed`，排查方向全错。属"静默丢账号"。
- 修：`probe_oauth_availability` 透出 `refresh_token`；`_get_oauth_access_token` 在有新值时
  `outlook_repository.set_fields(account_id, refresh_token=...)`。

### T7-4 — Important — Outlook 号池没有 `in_use` 回收，崩一次永久少一个号，且提示指向错误修法
`core/mailboxes/channels/outlook/mailbox.py:272`（领取即 `status="in_use"`，只有
`set_account_status`/`requeue_account` 能放回）、`:254-259`（池空时的提示文案）、
`:404-451`（`import_accounts_to_pool` 只动 `unpooled`）。全仓没有任何 outlook 侧的
`release_stale_claims` 等价物（iCloud 有：`services/icloud/aliases.py`）。

- 复现：导入 1 行 → `import_accounts_to_pool` → `get_email()` 领到 `a@outlook.com` →
  模拟任务崩溃（不调 `set_account_status`）→ `pool_status_summary()` 为
  `{'available': 0, 'in_use': 1, 'total': 1}` → 再次 `get_email()` 抛
  `RuntimeError 微软邮箱账号池为空，请先在设置页批量导入`。
- 影响：任何崩溃/超时/进程重启都会永久吃掉一行库存；用户按提示"再导入新邮箱"只会继续消耗。
  号池只出不进，且症状被误导成"没导入成功"。
- 修：给 `in_use` 加租约（复用 `last_used`，超过 N 分钟自动回 `available`），
  并在池空分支像 `unpooled_left` 那样先统计 `in_use` 数、给出准确提示。

### T7-5 — Minor — 删除/批删按大小写敏感精确匹配，存储却已小写化
`services/mail_imports/providers.py:440-447`、`:472-479`；
对照 `core/db/outlook_repository.py` 的 `add()`（`payload["email"] = ...strip().lower()`）
与 `services/chatgpt_otp_mailbox.py:270-307`（OTP 回填会按 `+` 前主地址兜底）。

- 复现：导入 `UPPER.Mixed@outlook.com` → 池里存成 `upper.mixed@outlook.com`；
  `delete(email="UPPER.Mixed@outlook.com")` 抛 `RuntimeError 未找到要删除的微软邮箱: UPPER.Mixed@outlook.com`；
  小写形式删除成功。`batch_delete` 传小写成功、传原始大小写会进 `errors`。
- 影响：面板从快照取的是小写值所以点得动；但 API/脚本/导出的原始串删除会假性"不存在"。
  同一个地址的 `+` 别名形态（`base+tag@`）也不能删到存成 `base@` 的行。
- 修：`delete`/`batch_delete` 用 `normalize_import_email` 归一，并复用
  `_find_outlook_row` 的 `+` 主地址兜底。

### T7-6 — Minor — 只认 `----` 分隔文本：CSV 表头/JSON/竖线一律逐行报错；`filename` 收下就丢
`services/mail_imports/providers.py:226-232`（`splitlines()` + 空行/`#` 过滤）、
`services/mail_imports/microsoft_import_rules.py:138-155`（只按 `----` 分段数判格式）、
`services/mail_imports/schemas.py:55-57`（`filename` 字段从未被读取）。

- 复现：`content="email,password,client_id,refresh_token\na@outlook.com,pw,cid,rt"` →
  `success=0, failed=2`，`errors=['行 1: 格式错误…', '行 2: 格式错误…']`；
  JSON 数组内容 → `failed=1`；竖线分隔 → `failed=1`；`filename="x.csv"` 不出现在 `meta` 里。
- 影响：**不会中断整批**（这点是对的：混入 2 行坏数据时 `success=1, failed=2, total=3`，
  错误按行聚合），但 CSV 表头被当成数据行计数，用户看到"失败 1 行"以为丢了一条真账号。
- 修：`filename` 或首行嗅探（含 `,` 且有 `email` 表头）走 CSV 解析并跳过表头；
  不支持的格式给出一次明确的整体 400，而不是 N 条逐行错误。

### T7-7 — Minor — `enabled=False` 导入的行能被"导入邮箱池"，状态变 available 却永远领不到
`core/mailboxes/channels/outlook/mailbox.py:404-451`（`import_accounts_to_pool` 只判 `status`，
不看 `enabled`）、`core/db/outlook_repository.py` 的 `find_claimable`（`eq("enabled", True)`）。

- 复现：`MailImportExecuteRequest(..., enabled=False)` 导入 1 行 → 快照 `enabled=False` →
  `import_accounts_to_pool([id])` → `pool_status_summary()` 显示 `available: 1` →
  `get_email()` 抛 `RuntimeError 微软邮箱账号池为空，请先在设置页批量导入`。
- 影响：汇总数说"有 1 个可用"，取号说"池为空"，两者矛盾；用户无法理解。
- 修：`import_accounts_to_pool` 顺带把 `enabled` 置 True（入池 = 可用），或在池空分支统计
  "available 但 disabled"并单独提示。

## hypothesis

- H1（未证）：`OutlookGraphMailboxBackend.wait_for_code` 的 `_token_refreshed` 标志整轮只置一次
  （`backends.py:322,398-399`），token 在 12 分钟后再次 401 时不会刷新，直接吞掉继续轮询，
  长 timeout 场景可能空转到时限。已在代码上确认，未构造多轮 401 时序复现，故不作缺陷计。
- H2（未证）：`probe_oauth_availability` 用 `self._proxy`，而导入时 `OutlookMailbox()` 是无参构造，
  批量可用性检测实际直连。若部署强依赖代理出网，导入会全批失败而注册时正常。需真实网络环境才能判定。
- H3（未证）：`_expand_records_with_aliases` 生成的别名（`providers.py:56-60`）没有做长度上限/字符校验，
  极长 local part 可能超出微软别名规则。未找到规则文档，未复现。

## 覆盖清单（渠道注册表 / 号池 / 导入 / OAuth 错误分类）

| 面 | 断言 | 证据 |
|---|---|---|
| 渠道注册表 | `""` / `"typo"` / `"  "` 均抛 `ValueError` 且列 5 个可用渠道，不回落默认 | 实跑：`未知邮箱提供商: ''；可用: outlook, microsoft, mail_import, icloud_local, icloud_remote` |
| 渠道注册表 | `"Outlook"` / `"outlook "` 归一后可构建；重复注册抛错；空名被跳过（无害） | 实跑 + `registry.py:52-66` |
| 渠道注册表 | 可选渠道懒加载失败会 `print` 而非静默（`registry.py:88-92`） | 代码审阅，无法在不破坏进程的情况下构造 ImportError |
| 导入注册表 | `""` / `"typo"` / `"Microsoft"` 抛 `不支持的邮箱导入类型`；`"outlook "` 是 microsoft 别名 | 实跑 |
| 号池凭据 | 缺 `client_id`/`refresh_token` 不崩：探针返回 `missing_oauth_credentials`，Graph 自动降级 IMAP | 实跑 4 种组合（缺 cid / 缺 rt / 无密码但 OAuth 全 / 全空） |
| 号池凭据 | 全空凭据仍会被领出并进入轮询，最终 `TimeoutError`，行停在 `in_use` | 实跑：`EMPTY-CREDS -> TimeoutError` + summary `in_use: 1` |
| `+` 别名 | 存储侧只做小写归一；OTP 回填有主地址兜底；删除没有 | 实跑删除大小写 + `chatgpt_otp_mailbox.py:270-307` |
| 导入 | 混入坏行不中断整批，错误按行聚合 | 实跑 `success=1 failed=2 total=3` |
| 导入 | 重复导入幂等：第二次 `success=0 failed=1`，池里不重复 | 实跑 + `test_microsoft_import_writes_through_the_outlook_repository` |
| 导入 | CSV/JSON/竖线不支持，表头当数据行计数 | 实跑（T7-6） |
| 导入 | `unpooled` 不参与取号；勾选后转 available | `test_imported_accounts_land_unpooled`、`test_claim_skips_unpooled_accounts`、`test_selected_accounts_become_claimable` |
| OAuth | `invalid_grant` 与 429 都返回同一个 `oauth_token_failed`，各 12 次请求 | 实跑（T7-1） |
| OAuth | 轮换 RT 不落库 | 实跑（T7-3） |
| OAuth | Graph 首次瞬态失败穿透；IMAP 永久错误吞成超时 | 实跑（T7-2） |
| wait_for_code | iCloud 两渠道接 `is_transient`/`max_consecutive_errors`/`is_fatal_busy`；Outlook 三 backend 全不接 | `icloud_local.py:382-392`、`icloud_remote.py:401-414` vs `backends.py:221,448,534` |
| 共享邮件页 | 主题/发件人/正文全转义、iframe `sandbox="allow-same-origin"` 无 `allow-scripts`、404/500 不泄漏细节、`no-store` | 实跑 TestClient：转义 True、`<script>` 不裸出、500 不含 mock 的 `SECRET-DB-DSN` |
| 既有测试 | 45 passed（mail_imports_service / outlook_oauth / import_field_order / pool_selection） | `.venv/bin/python -m pytest … -q` |

复现命令（全部只读、临时库、假网络层；生产库未触碰）：

- `.venv/bin/python -m pytest tests/test_mail_imports_service.py tests/test_outlook_mailbox_oauth.py tests/test_outlook_import_field_order.py tests/test_outlook_pool_selection.py -q`
- OAuth 分类：`mock.patch("requests.post")` 返回 400 `invalid_grant` / 429，打印 `len(calls)` 与 `reason`（结果见 T7-1）。
- 轮换丢弃：同一 patch 返回含 `refresh_token` 的 200 响应，检查 `probe_oauth_availability(...)` 返回键。
- 导入：`tempfile` + `create_engine("sqlite:///…")` + `platform_database_registry.configure({"outlook": ...})`，
  喂 CSV/JSON/混合行，断言 `summary` 与 `errors`（结果见 T7-5/T7-6）。
- 号池：同一临时库里 `import_accounts_to_pool` → `get_email()` → 跳过状态写回 → 再 `get_email()`（结果见 T7-4）。