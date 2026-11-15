# Task 5 — iCloud 域：主号、别名池、收件、远程服务

状态: 完成（4 条 Important + 2 条 Minor + 1 条正面结论 + 2 条 hypothesis；末尾列未验证清单）

范围仅：`api/icloud.py`、`services/icloud/**`、`modules/mail/icloud_*.py`、`platforms/icloud/**`（含必要的仓储/调用点交叉引用）。

## 结论

整体设计是**成熟且被测试严密保护的**：四态流转的每个转移都有条件 UPDATE（`WHERE pool_status=…`），
领取独占是真实的线程互斥（我用独立脚本 + 真实 SQLite 复现了「只有一个赢」，不是 mock 断言），
租约续约/超时回收/失败释放三条路径都活着且有测试。收件参数在渠道层已被专门修过（`OTP_READ_LIMIT=20`、
`DEFAULT_READ_DAYS=2`），避开了实测的 15s 悬崖与 10 年 SEARCH。

发现的问题集中在**边缘接口与账实一致性**上。最重要的一类是 `pool_status` 与 `status`
两个正交维度被当成一个用，导致「统计条说可领、实际永远领不到」的僵死号（T5-3 / T5-4，均已复现）。

- **T5-3 / T5-4（最要紧）**：`set_alias_active(False)` 与 `import_aliases_to_pool` 都不校验行的
  `status`。前者让「停用持有中的别名 + 2h 回收」把号推进 `pool_status=available` + `status=disabled`；
  后者让「导入一批含停用的别名」直接产生同一状态。两者都能被池页面的按钮触发，结果都是
  统计条计入「可领取」而 `claim_alias` 永远跳过 —— 用户看到「池里明明有号，注册却说没有」。
- 其次是 **T5-2**：Grok / AWS 路径不传 `error`，上游网关故障被记成地址失败，3 次后别名永久退出候选，
  **且没有任何 API/UI 能复位** `consecutive_failures`（实测：手工改回 `available` 也无效）。
- 然后是 **T5-1**：`/aliases/{id}/messages` 与 `/accounts/{id}/messages` 的默认 `limit=50`
  和前端固定传 50，正好落在实测的 15s 悬崖上；API 层虽然把 `warning` 回给了前端，但**前端从不读它**，
  降级读成 0 封时用户/UI 都无从知道「这不是真的没信」。

## 发现

### T5-1（Important）API/FE 读信默认 `limit=50`，正落在 15s 悬崖；`warning` 无任何消费方

- **file:line**
  - `api/icloud.py:321` `def account_messages(account_id, limit: int = 50, ...)`
  - `api/icloud.py:440` `def alias_messages(alias_id, limit: int = 50)`
  - `frontend/src/api/icloud.ts:325` 前端固定 `limit=50`（注释还写着「所以取服务端默认的 50」）
  - 上游预算实现参照 `platforms/icloud/remote_client.py:350`（`days` 上限）与 `platforms/icloud/remote_client.py:348`（`limit` 夹到 ≤50）
- **复现（实测，走本地 HTTP 双端，不碰真实上游）**
  ```
  PYTHONPATH=. .venv/bin/python /tmp/t5_repro2.py     # 见「复现脚本」一节
  limit= 5 -> 上游收到 limit=5 days=2 预算=max(15, 1.5)s
  limit=10 -> 上游收到 limit=10 days=2 预算=max(15, 3.0)s
  limit=20 -> 上游收到 limit=20 days=2 预算=max(15, 6.0)s
  limit=50 -> 上游收到 limit=50 days=2 预算=max(15, 15.0)s
  ```
  即：`limit=50` 时预算**正好**顶到 `max(15s, 15s)=15s` 的悬崖；`limit≤50` 全部走 15s 地板，
  真正的悬崖在 ≥15s。`limit<15`（5/10）则是另一个已知坏区间（上游按 `offset+limit` 截候选 → 丢信）。
- **`warning` 消费点审计（问题 2 的答案）**：上游 `method`/`warning` 在服务层被消费的代码点只有 **1 个**
  （`services/icloud/remote.py:798-804`，把 `method != imap` 归一成 `warning` 文本）。
  往外传的出口有 2 个（`api/icloud.py:330-337`、`api/icloud.py:449-452` 都塞进 payload）。
  但**前端 0 个消费点**：`frontend/src/api/icloud.ts:325` 只 `return data?.items ?? []`，`warning` 被丢掉；
  `AliasInboxDrawer.tsx:26` 拿到的只有列表。所以「降级→0 封」在 UI 上等价于「没收到信」。
- **爆炸半径**：别名收件箱页面（用户手动看码）+ `GET /accounts/{id}/messages`。
  自动化等码链路不受影响（渠道用的是 `limit=15` / `OTP_READ_LIMIT=20`，见 T5-4 的正面结论）。
- **修复**：API 默认值从 50 降到 20（与 `OTP_READ_LIMIT` 同档），前端显式传 20 或改用服务端默认；
  `listICloudAliasMessages` 把 `warning` 一起返回，抽屉里在列表为空且 `warning` 非空时显示
  「上游降级，本次读取不可信」而不是「暂无邮件」。

### T5-2（Important）Grok / AWS 路径的 `failed` 不带 `error` → 上游故障记在地址头上，且计数无法复位

- **file:line**
  - `platforms/grok/plugin.py:962` `setter(account, status)`（无 `error`）
  - `platforms/aws/plugin.py:274` `recorder(account, status)`（无 `error`）
  - 对照：`platforms/chatgpt/registration_engine.py:246-252` 会先试 `error=error` 再回退
  - 消费端 `services/icloud/aliases.py:291-304`：`error is None` → 直接 `mark_failed()`（累加）
  - 永久跳过：`core/db/icloud_alias_repository.py:764`（SQLite）/ `:733`（JSON）`consecutive_failures < 3`
- **复现**
  ```
  PYTHONPATH=. .venv/bin/python /tmp/t5_repro5.py
  上游故障 3 次后: available consecutive_failures= 3
  可领候选: 0
  带 error=upstream_unavailable 一次后: 0 available     # 传了 error 就不计数（正确）
  ```
  以及 `/tmp/t5_repro3.py`：
  ```
  after failure 3: pool_status=available consecutive_failures=3
  find_candidates 命中数: 0
  pool_summary: {'available': 1, ...}        # 统计条仍显示「可领取 1」—— 账实不符
  手工 set_alias_pool_status('available') 后候选数: 0
  claim_alias -> 抛错（池内一个可用号都没有 → 走生成路径烧额度）
  ```
- **关键点**：`mark_failed(reset=True)` 唯一的生产调用点是 `claim_alias`（`services/icloud/aliases.py:215`），
  而 `claim_alias` 只从**候选集**里挑；候选集已经排除了 ≥3 次失败的行 → **没有任何路径能把计数减回去**。
  `set_alias_pool_status` 只改 `pool_status`（`aliases.py:375`），不碰计数。这是**不可逆的单向门**。
- **爆炸半径**：Grok 与 AWS 注册（`icloud_local` 与 `icloud_remote` 两个渠道共用同一钩子）。
  一次网关抖动（`services/icloud/upstream_gate.py` 记录的正是这种）连撞 3 轮，就会静默把整池好号判死；
  Apple 每小时只给 5 个新地址。统计条仍显示 available，用户看不出问题。
- **修复**：两条调用点对齐 ChatGPT 引擎（先传 `error=`，`TypeError` 再回退）；
  另外给 `set_alias_pool_status` 增加「显式放回 available 时清零 `consecutive_failures`」。
  （后者是防呆：用户已经在池页面做了「我要再用它」的明确动作。）

### T5-3（Important，**已复现**）停用持有中的别名 → 静默进入「available 但永远领不到」的僵死状态

- **file:line** `services/icloud/aliases.py:610-613`（`set_alias_active` 只改 `status`，**不碰 `pool_status`**）、
  `api/icloud.py:412`（`POST /aliases/{id}/deactivate` 直接调它，不校验池状态）、
  回收/取号的交叉条件 `core/db/icloud_alias_repository.py:757`（`find_candidates` 要求 `status == active`）
- **复现** `/tmp/t5_repro7.py`（真实 SQLite，非 mock）：
  ```
  停用后: {'status': 'disabled', 'pool_status': 'in_use'}
  回收数: 1                                    # release_stale_claims 把它放回 available
  回收后: {'status': 'disabled', 'pool_status': 'available'}
  find_candidates 命中: 0                      # 但 status=disabled → 永远不进候选
  pool_summary: {'available': 1, ...}          # 统计条/池页面显示「可领取 1」
  ```
  两步都需要（停用 + 2h 回收），我把 `updated_at` 手工老化 3 小时跳过等待，其余全走真实代码路径。
- **为什么是漏洞而不是「按设计」**：`aliases.py:578-583` 的 docstring 说停用的语义是
  「地址还在 Apple 那边保留，随时能恢复」，**只承诺挡住转发，从没说要把它从池子里摘掉**；
  而 `set_alias_active` 的 `pool_status` 保持 `in_use` 会让这条行在 2 小时内既不可领也不可回收，
  2 小时后变成 `available` 却依然不可领 —— 一个会被统计条计入「可领取」、但 `claim_alias` 永远跳过的号。
- **爆炸半径**：池页面「停用」按钮（单个或批量）。误停用一批 → 它们全部显示「已入池」且计数正常，
  注册时却领不到，用户只会看到「没有可用邮箱」→ 回落到生成，烧掉每小时 5 个的额度。
  `pool_summary` 是用户唯一的可见账本，它在这里说了假话。
- **修复**：`set_alias_active(False)` 时若 `pool_status == in_use`，要么一并 `unpool`（更符合「停用即移出」直觉），
  要么拒绝停用并提示「该地址正在被任务使用」；同时 `pool_summary` 应把 `status != active` 的行从
  `available` 计数里排除（或单列一个「已停用」档）。两者选一即可消除「统计说可领、实际领不到」。

### T5-4（Important，**已复现**）同一类账实不符的另一入口：`import_aliases_to_pool` 不校验 `status`

- **file:line** `services/icloud/aliases.py:407-443`（`_set_pool_status_many`，条件只有 `id` + `pool_status`）、
  `api/icloud.py:385`（`POST /aliases/import-to-pool`）
- **复现** `/tmp/t5_repro8.py`：
  ```
  import 结果 changed: [1] | pool_status: available
  行状态: {'status': 'disabled', 'pool_status': 'available'}
  find_candidates: 0
  pool_summary: {'available': 1, ...}          # 统计说 1 个可领，候选 0 个
  ```
  把一个 `status=disabled` + `pool_status=unpooled` 的别名导入号池，它会被标成 `available` 并计入统计，
  但 `find_candidates` 要 `status=active`（`icloud_alias_repository.py:757`），它永远领不到。
- **与 T5-3 的关系**：**同一个根因**（`pool_status` 与 `status` 两个维度被当成一个用），两个入口各自可达。
  修 T5-3 时应当同修：`_set_pool_status_many` 的 `match` 加上 `status: active`，不命中的进 `skipped`。
- **爆炸半径**：池页面「导入邮箱池」批量按钮。用户勾选一批（含此前停用的）→ 统计条数字虚高，
  实际可领数偏低；这批号既不会被生成分支复用，也不会报错。

### N1（正面结论，不是缺陷）渠道层读信参数已经避开了两个坏区间

- `modules/mail/icloud_remote.py:48` `OTP_READ_LIMIT = 20`；`services/chatgpt_otp_mailbox.py:30` `ICLOUD_FETCH_LIMIT = 20`；
  `platforms/grok/register_browser.py:1416` `limit=15`；`services/icloud/remote.py:726` `DEFAULT_READ_DAYS = 2`。
- **实测验证**：`/tmp/t5_repro2.py` 打印的 `days=2` 证明 `fetch_remote_messages` 的默认窗口确实只有 2 天
  （不是 3650），且 `limit` 原样透传到上游、未被 `remote_client.py:348` 夹改。
- `limit=15`（Grok 浏览器路径）是**能读到信的最小档位**，预算 `max(15, 4.5)=15s` —— 正好在悬崖边沿，
  比 `OTP_READ_LIMIT=20`（预算同为 15s 地板）没有额外余量，但没有越过。列为 **T5-5（Minor，观测项）**。

### T5-5（Minor）`limit=15` 与 `limit=20` 的预算同为 15s 地板，两个数字并存无统一出处

- **file:line** `platforms/grok/register_browser.py:1416`（15）vs `modules/mail/icloud_remote.py:48`（20）
- **实测**：两者上游预算都是 `max(15, limit*0.3)` = 15s（15→4.5、20→6.0，都被 15 地板压住）。
- **风险**：`modules/mail/icloud_remote.py:41-48` 的注释明确说「20 = 实测能稳定读到信的最小档位」，
  而 Grok 浏览器路径用 15 —— 恰好是该注释里「limit=15/20/25/30/50 → 2 封」的**下边界**。
  实测能读到，但没有余量；若上游 `need` 计算方式微调（例如多算一个文件夹），15 会先掉出去。
- **修复**：把 15 提到共享常量（`services/icloud/_locks.py` 已有 `DEFAULT_MESSAGE_LIMIT`），Grok 路径改成 20。

### T5-6（Minor）`fetch_alias_messages` / `fetch_account_messages` 静默丢弃 `method`/`warning`

- **file:line** `services/icloud/messages.py:26-29`、`:135-136`（`_method, _warning = ...` 后直接丢）
- **复现** `/tmp/t5_repro6.py`（本地 Web-only 主号）：
  ```
  detailed: method= web_api | warning= 未配置 IMAP 应用专用密码，已回退 Web API
  plain（本地渠道 _messages() 用的）-> [] = 无任何降级信号
  ```
  远程分支同理：`/tmp/t5_repro_warning.py` 输出
  `degraded fetch_alias_messages -> 0 msgs; 调用方看不到任何降级信号`。
- **为什么仍算 Minor**：远程渠道走的是 `_messages_detailed`（`modules/mail/icloud_remote.py:226`，已正确消费）；
  但**本地渠道** `modules/mail/icloud_local.py:214` 用的是不带 detailed 的 `_messages()`，降级不可见。
- **修复**：本地渠道 `_messages` 改用 `fetch_account_messages_detailed`，与远程渠道同口径。

### hypothesis

- （原 H1 已升级为 T5-3 / T5-4，不再是 hypothesis。）
- **H2（低）** `renew_alias_claim` 用 `update_where(match={"id", "pool_status"}, values={})` 传空 `values`，
  只靠 `update_where` 里的 `payload.setdefault("updated_at", now)` 续命（`core/db/icloud_alias_repository.py:228`）。
  JSON 分支能工作（我实测 `renew` 后 `updated_at` 前进），但语义依赖了一个「副作用」——
  若有人给 `update_where` 加 `if not values: return 0` 的短路（很自然的优化），租约续约会**静默失效**且无测试报警。
- **H3（低）** `_PREVIEW_CACHE` 的 LRU 是「按 dict 插入顺序淘汰第一个非空桶」，`set()` 里已经 `pop` 再插入刷新顺序，
  但 `_evict_oldest` 遍历的是 `self._data.items()` 的顺序 —— 跨服务淘汰时总是先淘汰**最早建桶的服务**，
  而不是全局最旧的那条。500 条上限下影响很小，仅记录。

## 覆盖清单（别名四态流转 / 领取独占 / 租约 / 收件参数 / 远程降级）

### 1. 别名四态流转（`unpooled / available / in_use / used`）

| 转移 | 触发点 | 回滚/反向 | 验证方式 |
|---|---|---|---|
| → `unpooled`（新行） | `upsert_from_sync`（`icloud_alias_repository.py:804` JSON / `:846` SQLite）、模型默认值 `models_platform.py:111`、迁移默认 `migrations.py:312` | `import_aliases_to_pool`（`aliases.py:395`，条件 UPDATE `from=unpooled`） | `test_icloud_alias_pool.py:411`（只动 unpooled，in_use/used 被跳过）、`:457`（unpool 只动 available）**已跑绿** |
| `unpooled → available` | `import_aliases_to_pool` | `unpool_aliases`（`aliases.py:404`） | 同上；`:441` 幂等性（第二次 changed 为空）**已跑绿** |
| `available → in_use` | `claim_alias`（`aliases.py:206-208`，`set_fields`） | `release_alias_claim`（`aliases.py:289`，先校验 `pool_status==in_use`） | `test_icloud_alias_pool.py:102/123/181`；我另用真实线程复现独占（见 2）**已跑绿** |
| `in_use → used` | `mark_alias_used`（`aliases.py:260-266`，同时并 `used_platforms`） | **无自动回滚**（docstring 明说单向）；仅 `set_alias_pool_status(id,"available")` 手工 | `test_icloud_alias_pool.py:161-171`（mark used → 手工 available → 能再领）**已跑绿** |
| `in_use → available`（超时） | `release_stale_claims` → `release_stale`（`icloud_alias_repository.py:257`，条件 UPDATE `WHERE pool_status='in_use' AND updated_at < cutoff`） | — | `test_icloud_alias_pool.py:283/344`；续约测试 `:550-552/582-583/601/617-626`**已跑绿** |
| `in_use → available`（失败） | `release_alias_claim`（`aliases.py:289`），并按 `error` 决定是否 `mark_failed` | — | `test_icloud_alias_pool.py:181`、`test_icloud_alias_failure_accounting.py:106`（上游故障不计数）/`:134`（真实失败计数）**已跑绿** |

**能到达但回不去的状态（回答「有没有路回不去」）**：
1. `consecutive_failures >= 3` —— `pool_status` 回得去，但候选集回不去（T5-2，**已复现**）。
2. `used` —— 有意单向（设计明确），且有 `set_alias_pool_status` 手工出口；**不算缺陷**。
3. `status=disabled` + `pool_status=available`（T5-3 / T5-4，**已复现**）：统计条计入 `available`，
   但 `find_candidates` 要求 `status=active` → 永远领不到，且没有任何按钮能把它变回可领
   （`set_alias_pool_status` 只改 `pool_status`，`import_aliases_to_pool` 也只改 `pool_status`）。
   **没有一个按钮能单独救它**：`/tmp/t5_repro9.py` 实测重新激活后是
   `{'status': 'active', 'pool_status': 'in_use'}` → `find_candidates` 仍为 0，
   还得再等 2 小时回收（或手工 `set_alias_pool_status`）。即「停用再启用」= 该号再冻结 2 小时。
4. `consecutive_failures >= 3` 与上面 3 叠加时，两个维度都锁死（T5-2 + T5-3）。

### 2. 领取独占 —— 是真的并发，不是 mock

- 既有测试 `tests/test_icloud_alias_pool.py:651` `test_concurrent_claims_never_hand_out_the_same_alias`：
  4 个 `threading.Thread` + `threading.Barrier(4)`，池里只放 1 个号，断言
  `claimed.count("race@icloud.com") == 1` 且 `len(claimed)==len(set(claimed))`。这是**真并发**（有调度窗口），
  但作者已在注释里诚实标注「靠调度窗口，不保证每次撞上竞态」，并配了确定性补充
  `tests/test_icloud_claim_holds_the_lock.py:28`（断言「读池子时锁已被持有」）。
- **我的独立复现**（`/tmp/t5_repro4.py`，2 线程 + 真实 SQLite + Barrier）：输出
  `claimed: ['race@icloud.com'] | errors: ['ICloudError']`、`唯一发号: True`、`落库状态: [('race@icloud.com','in_use')]`。
  即：**恰好一个赢**，输的那个落到生成分支并因缺少 HME 会话而抛 `ICloudError`（符合预期）。
- **前提约束**：互斥是进程内 `RLock`（`services/icloud/_locks.py:33`），`docs/MAINTENANCE.md:147 §1.6`
  明确要求单进程部署；`docker-compose.yml`/`main.py:625` 均无 `--workers`。跨进程会双发 —— 这是已文档化的已知限制，不算缺陷。

### 3. 租约（renew / timeout / release on failure）

- **renew**：`aliases.py:312-330`，条件 UPDATE 只碰仍 `in_use` 的行。调用点 3 处：
  `modules/mail/icloud_local.py:333`、`modules/mail/icloud_remote.py:343`、`platforms/grok/register_browser.py:1397`
  （Grok 那条用 `time.monotonic()`，注释说明防 NTP 回拨）。三者都 60s 节流。
- **timeout**：`release_stale_claims(older_than_minutes=120)`；调用点 `icloud_local.py:119`、`icloud_remote.py:136`（取号前兜底）。
- **release on failure**：`set_account_status("failed", error=...)` → `release_alias_claim`。
- **`in_use` 泄漏路径审计**（逐条看调用点）：
  - 进程崩溃 → 2h 回收 ✅（`test_icloud_alias_pool.py:283`）
  - 上游故障 → 放回且不计数 ✅（`test_icloud_alias_failure_accounting.py:106`）
  - Grok/AWS 不传 `error` → 放回但**计数**，3 次后永久跳过 ❌（T5-2）
  - `set_alias_active(False)` → 先停在 `in_use`+`disabled`，2h 后变 `available`+`disabled` 且**永久不可领**（T5-3，已复现）
  - `delete_alias` → 行删除，无泄漏 ✅
  - `mark_alias_used` 抛异常被吞（`icloud_local.py:206-207` `except Exception: log`）→ 停在 `in_use`，2h 回收 ✅（延迟但不永久）

### 4. 收件参数 → 上游 IMAP 信封预算

上游预算公式 `max(15s, limit*0.3s)`，cap 45s（`services/icloud/remote.py:719-725` 的注释 + `remote_client.py:33-40`）。
**我逐调用点算了预算**：

| 调用点 | limit | days | 预算 | 判定 |
|---|---|---|---|---|
| `api/icloud.py:321` `/accounts/{id}/messages` 默认 | 50 | 2 | **15s（悬崖）** | ❌ T5-1 |
| `api/icloud.py:440` `/aliases/{id}/messages` 默认 | 50 | 2 | **15s（悬崖）** | ❌ T5-1 |
| `frontend/src/api/icloud.ts:325` 前端固定 | 50 | 2 | **15s（悬崖）** | ❌ T5-1 |
| `modules/mail/icloud_remote.py:48` `OTP_READ_LIMIT` | 20 | 2 | 15s（地板，有余量） | ✅ |
| `services/chatgpt_otp_mailbox.py:30` `ICLOUD_FETCH_LIMIT` | 20 | 2 | 15s | ✅ |
| `platforms/grok/register_browser.py:1416` | 15 | 2 | 15s（下边界） | ⚠️ T5-5 |
| `modules/mail/icloud_local.py:227/354` `_messages(limit=50)` | 50 | — | 本地 IMAP，无此预算 | ⚠️ 见下 |
| `services/icloud/remote.py:726` `DEFAULT_READ_DAYS` | — | **2**（原 3650） | 不再 SEARCH 10 年 | ✅ 改善已落地 |
| `platforms/icloud/remote_client.py:348-350` | 夹 `[1,50]` | 夹 `[0,3650]` | 客户端护栏 | ✅（但 3650 仍可达，需显式传） |

**`days=3650` 现状**：`fetch_remote_messages(days=...)` 有参数但**生产调用点 0 个传非默认值**
（`services/icloud/remote.py:750` 是唯一透传处，`fetch_remote_messages` 的调用方
`messages.py:75` 不传 `days`）。所以「3650 天 SEARCH」已从热路径移除；风险仅在于未来有人显式传。

**`modules/mail/icloud_local.py:227/354` 的 `limit=50`**：本地渠道走 IMAP（`platforms/icloud/mailbox.py`），
**没有**上游信封预算这一层，50 是本机 IMAP 的 `limit`。不对应本任务的预算公式，列为观察项而非缺陷。

### 5. 远程服务降级

- **`permission_denied` → 只读回退**：`platforms/icloud/remote_client.py:239-247`，仅在 `code == "permission_denied"`
  时回退 `_read_only_probe`（`GET /api/accounts` 列表里能读到即算活）。**4 条测试覆盖**（`test_icloud_remote_client.py:462-500`）：
  `test_falls_back_to_read_only_probe_when_check_is_denied`（断言真的走了 `GET /api/accounts` 而非重打 `/check`）、
  `test_denied_check_does_not_survive_as_an_error`、`test_other_check_failures_still_raise`（只对权限降级）、
  `test_read_only_probe_reports_a_missing_account`、`test_read_only_probe_surfaces_a_broken_service` **全部已跑绿**。
  这是本切片里覆盖最好的降级路径。
- **`method`/`warning` 消费点统计（问题 5 的直接答案）**：
  - 生产代码里**消费**上游 `method`/`warning` 的点：**1 个** —— `services/icloud/remote.py:798-804`
    （`upstream_method != "imap"` → 合成降级 warning，并保留 `upstream_warning`）。
  - **转出**到 HTTP 响应的点：2 个（`api/icloud.py:330-337`、`:449-452`）。
  - **真正据此改变行为的点**：1 个 —— `modules/mail/icloud_remote.py:226-231` 的 `_messages_detailed`，
    它把非空 `warning` 当 `degraded=True`，再在 `poll_once` 里抛 `upstream_error`（`:365-372`），
    让容错循环继续等而不是判定「没有码」。**这是唯一一个「消费并行动」的点**。
  - **丢弃点**：`services/icloud/messages.py:26-29`、`:135-136`（T5-6）；`frontend/src/api/icloud.ts:325`（T5-1）。
  - 结论：**降级信号在等码主链路上被正确消费（1 个点，且有测试）；在人工看码页面链路上被两处丢弃。**

## 跑过的测试（证据）

```
.venv/bin/python -m pytest tests/test_icloud_alias_pool.py tests/test_icloud_remote_service.py \
  tests/test_icloud_remote_client.py tests/test_icloud_otp_read_scope.py tests/test_icloud_local_mailbox.py \
  tests/test_icloud_claim_holds_the_lock.py tests/test_icloud_remote_mailbox.py \
  tests/test_icloud_alias_failure_accounting.py tests/test_icloud_api.py -q
→ 182 passed, 1 warning in 77.35s
```
覆盖：四态流转、领取独占（并发 + 确定性两版）、续约/回收、远程降级、只读探活回退、
HTTP 双端下的 limit/days 透传、号池记账钩子。

## 复现脚本（均在 `/tmp`，未写入仓库；运行方式 `PYTHONPATH=/data/workspace/Register .venv/bin/python <脚本>`）

| 脚本 | 复现内容 |
|---|---|
| `/tmp/t5_repro_warning.py` | 远程降级时 `fetch_alias_messages` 丢 `warning`（真实 HTTP 双端 `degrade_to_web_api`） |
| `/tmp/t5_repro2.py` | 4 档 `limit` 的实际上游参数与预算计算；失败计数可被 `claim_alias` 复位 |
| `/tmp/t5_repro3.py` | 3 次失败 → 候选 0、统计条仍显示 available、手工改回也无效 |
| `/tmp/t5_repro4.py` | 2 线程 + Barrier + 真实 SQLite：恰好一个 `claim_alias` 赢 |
| `/tmp/t5_repro5.py` | Grok/AWS 形态（不传 `error`）连 3 次上游故障 → 别名永久不可领；传 `error` 则不计数 |
| `/tmp/t5_repro6.py` | 本地 Web-only 主号：detailed 有 `web_api`+warning，plain 版本全丢 |
| `/tmp/t5_repro7.py` | 停用 `in_use` 别名 → 回收后 `available`+`disabled`，候选 0、统计条仍算可领 |
| `/tmp/t5_repro8.py` | `import_aliases_to_pool` 把 disabled 行标成 `available`，候选 0、统计条算可领 |
| `/tmp/t5_repro9.py` | 停用后再重新激活仍卡在 `in_use`（还要再等 2h） |

## 最小复现配方（T5-3 / T5-4，可直接粘进 pytest 或用 -c 跑）

```
from services import icloud_service as S
from core.db.icloud_alias_repository import icloud_alias_repository as R
# 造一行 status=disabled + pool_status=unpooled，然后：
S.import_aliases_to_pool([1])
assert (R.get(1)["status"], R.get(1)["pool_status"]) == ("disabled", "available")
assert S.pool_summary()["available"] == 1        # 统计条说「可领取 1」
assert len(R.find_candidates(account_id=1)) == 0 # 实际候选 0 —— 矛盾
```
我实跑输出：`import: [1] -> disabled available` / `summary.available = 1 | find_candidates = 0`。
同样的不对称出现在 `set_alias_active`：`find_candidates` 的 WHERE 同时要求 `status == active`，
而那两处 UPDATE **只带 `id`（+`pool_status`）**。完整脚本见 `/tmp/t5_repro7.py`、`/tmp/t5_repro8.py`。

## 未验证

- **T5-3 的真实时间路径**：我用「手工把 `updated_at` 老化 3 小时」代替真等 2 小时；回收本身走的是
  生产 `release_stale_claims()`。时间判定逻辑本身未在真实时钟下跑过（但 `test_icloud_alias_pool.py:258/344` 覆盖了）。
- **JSON 后端 vs SQLite 后端**的全部四态流转：我的复现脚本全跑 SQLite。补跑了
  `tests/test_icloud_alias_repository_backends.py`（→ 42 passed，含 10 subtests）：它用 `_SqliteFixture` /
  `_ApiFixture` 双后端跑 `upsert / list_aliases / set_pool_status / get / find_available / delete` **6 个方法**，
  但**没有覆盖** `update_where` / `release_stale` / `find_candidates` / `mark_failed` / `mirror_remote` /
  `upsert_from_sync` —— 也就是条件 UPDATE 那套并发纪律的 JSON 分支（`icloud_alias_repository.py:230-237`）
  **无对等测试**。这是**验证缺口**（Important 级缺口，但我没有独立复现出跨后端行为差异，故不单列缺陷）。
- **`platforms/icloud/mailbox.py`（本地 IMAP 收件）**、`platforms/icloud/web_mail.py`（Web 收件）、
  `platforms/icloud/login.py`、`srp.py`、`cookies.py`：本任务的「收件参数」只查了预算相关的 `limit`/`days`，
  这三个文件的协议正确性未验证（超出 12 分钟预算）。
- **`upstream_gate.py` 的熔断分级**（服务级 vs 主号级）：读了代码与注释，未实跑故障注入。
- **`generate_remote_alias`**：`services/icloud/remote.py:651` 存在，但 `generate_alias`（本地路径）
  明确拒绝远程账号（`aliases.py:492-497`）。两者并存没有调用冲突，但我**没有验证** `generate_remote_alias`
  是否还有生产调用点（grep 未发现，可能是死代码）。
- **真实上游行为**：全部结论基于本地 HTTP 双端 + 题面给的实测数据；未向 `icloud-hme.fuzy.site` 发过任何请求。