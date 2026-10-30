# proxy 审计

[MODE: SANDBOX]

审计对象：`core/proxy_pool.py`、`core/proxy_utils.py`、`core/models/proxy.py`、`core/db/proxy_repository.py`、`api/proxies.py`、`services/task_runners.py` 代理相关段、`migrations/versions/d4e7f1a25b90_add_proxy_tags.py`、`tests/test_proxy_tags_balance.py`。

方法：静态审阅 + 只读真库（`data/account_manager.db`，`mode=ro`）+ 真库副本上的可写实验（`/root/.hermes/cache/scratch/proxy_audit/`）+ 单文件测试。**真库未被写入**；所有 apply / 并发实验都在副本上执行。

结论先行：标签解析、单线程 `rebalance` 的 dry-run 不写库、单线程 dry-run 计划与 apply 计划一致，这几条是成立的。但**跨平台写回、并发写回、升级迁移、空池绑定**存在 4 个 P0；另有 9 个 P1 级边界/安全问题。当前真库的 5 个代理 `tags` 全为空，正好落在最危险的“空绑定池回退”分支上。

---

## 0. 结论速览

| 审计项 | 结论 | 最严重发现 |
|---|---|---|
| (1) 标签语义 | ❌ 多个边界错误 | P0-1/P0-2 跨平台写错号；P1-1 空池绑未打标签代理；P1-2 注册/绑定语义互斥 |
| (2) 均衡 | ⚠️ 单线程一致，并发/耗时/事务有问题 | P0-3 快照覆盖并发字段；P1-3 并发无预留；P2-7 1057 个号 35.5s / 1057 次 commit |
| (3) 换绑 | ❌ `rebind` 与 `resolve_for_account` 会互相覆盖 | P0-1/P0-2；P1-9 无候选静默失败；P2-1 归一化不一致 |
| (4) 代理异常判定 | ❌ 误判与漏判都明显 | P1-4 `"407"` 子串误判、配置错误误判；漏 `ERR_NAME_NOT_RESOLVED`/`502` |
| (5) URL 归一化 | ❌ 各比较路径不统一 | P2-1 仅 `socks5→socks5h`；仓储精确匹配 vs 池归一化匹配；重复行导致健康信号分叉 |
| (6) 安全 | ❌ 明文代理串出现在日志/接口/前端 | P1-10 真库已有 59 条任务日志含完整 `user:pass` |

---

## 1. 现场基线（只读真库）

以下均为 `data/account_manager.db` 的只读查询结果，用于判断风险是否真实存在：

- `proxies` 5 行，`tags` **全部为空**：
  - `id=2..5` 为 `socks5://***:***@9.142.43.158:5328` 等 4 个在用出口；`id=6` 为 `success_count=0/fail_count=0` 的未使用出口。
  - `alembic_version = d4e7f1a25b90`（说明当前库是手工/部署流程跑过 Alembic 的，见 P0-4）。
- `accounts` 1057 行，**1057 行都有 `extra_json.register_proxy`**，分布为 290 / 266 / 262 / 239（4 个代理），第 5 个未使用。
- 账号侧存的是 `socks5h://...`，代理表存的是 `socks5://...`——同一物理代理两种写法，正好压中 P2-1。
- 670 个不同邮箱中，**387 个同时存在于 chatgpt 和 grok 两个平台**；按 `created_at` 取每个邮箱最新一行，**286 个最新行是 grok**。这使 P0-1/P0-2 的“按 email 写回”不是理论风险，而是近一半共享邮箱会踩中。
- `task_runs.logs_json` 中 **59 条任务日志**含完整代理串（例：`使用代理: socks5h://***:***@9.142.194.72:6740`），见 P1-10。

---

## 2. P0 发现

### P0-1 `_persist_register_proxy()` 硬编码 chatgpt：Grok 任务不写回，或写进同邮箱的 ChatGPT 号

- 位置：`services/task_runners.py:510-531`（硬编码在 `:517` 与 `:520`），调用点 `:507`；`_run_account_batch_task` 的 `platform` 参数在 `:442-451`。
- 证据：
  - 函数体固定 `platform_session("chatgpt")` + `AccountModel.platform == "chatgpt"`；`_load_account_fields()` 的 docstring 在 `:417-419` 明确说“这里原来**两处**硬编码 chatgpt……Grok 的批量动作每个号都报账号不存在”，但 10 行后的写回路径又把同一个 bug 写了一遍。
  - 在副本上构造“同邮箱 chatgpt + grok，grok 更新”的数据后，复刻 `:517-522` 的查询：对 grok 邮箱，`_persist_register_proxy` 会更新 **chatgpt** 行；真实数据中 387 个共享邮箱、286 个最新行是 grok。
  - 对 Grok-only 邮箱，查询返回 `None`，`:523-524` 直接 `return`，**静默不写**；而调用方 `:498-506` 仍然打印“已更新账号的代理字段”。
- 影响：Grok 批量任务（`_run_batch_action` 在 `:900-909` 传 `platform=platform`）每次复用都会重新挑代理但永不落库，下一次又换出口；共享邮箱则直接改坏 ChatGPT 号的 `register_proxy`。
- 建议：写回必须按 `(platform, email)` 或更好的 `account_id` 定位；把 `_persist_register_proxy(email, proxy)` 改成 `_persist_register_proxy(account_id, platform, proxy)`，并在写回后校验 `row.platform == platform`。禁止任何跨平台按 email 的写路径。

### P0-2 `rebind()` 按 email 全平台取 `rows[0]`：同邮箱双平台会改错号

- 位置：`core/proxy_pool.py:190-213`（`rows` 在 `:197-201`，`account = rows[0]` 在 `:204`，`upsert` 在 `:212`）。
- 证据：
  - `account_repository.list_all_accounts()` 跨所有平台库合并，并按 `created_at` 倒序（`core/db/repository.py:1197`）；`rebind()` 只按 `email.lower()` 过滤，取**最新**一行。
  - 副本实验：同邮箱 chatgpt 旧行 + grok 新行，调用 `rebind(email)` 后改的是 **grok** 行；真实数据 286/670 个邮箱的最新行是 grok。
  - 调用点 `services/task_runners.py:586` 只传 `fields.get("email")`，没有传 `platform`；`fields` 本身是按 `account_id + platform` 读出来的（`:414-439`），信息在调用点被丢掉了。
- 影响：ChatGPT 批量任务在代理异常时可能把 Grok 号的绑定改掉；Grok 任务同理。与 P0-1 叠加后，两个平台的绑定会互相踩。
- 建议：`rebind(account_id, platform)`；若必须按 email，至少加 `platform` 过滤，并在 `rows` 多于一行时拒绝/记录告警，而不是默认取第一行。

### P0-3 `rebalance` / `rebind` 用整行快照 `upsert` 写回：覆盖并发字段，且会复活已删除账号

- 位置：`core/proxy_pool.py:235-262`（`accounts` 快照在 `:235`，循环内 `get_extra`/`set_extra`/`upsert` 在 `:239-262`）；`core/proxy_pool.py:209-212`（rebind）；`core/db/repository.py:337-437`（`upsert` 的全字段 `setattr` 在 `:388-395`，插入分支在 `:397-434`）；`core/db/repository.py:128-204`（`merge_account_fields` 的 `extra_json`/password/token/status 全量来源在 `:152-198`）。
- 证据（副本实验，1057 个真实账号）：
  1. **覆盖并发字段**：`rebalance(dry_run=False)` 运行到 t=1.0s 时，把某个尚未处理账号的 `password` 改成新值、`extra_json` 写入 `{"marker": ...}`；rebalance 结束后该行 `password` 回到快照里的旧密码（重新加密后的密文）、`marker` 消失，`register_proxy` 变成 rebalance 的目标。原因是 `upsert` 把快照里的 `password/token/status/cashier_url/extra_json` 全部 `setattr` 回写。
  2. **复活已删除账号**：rebalance 运行到 t=1.0s 时删除一个尚未处理的账号；结束后同一 email 又以**新的 UUID** 重新出现（总数仍是 1057）。因为 `upsert` 找不到 `(platform,email)` 就走插入分支（`repository.py:397-434`）。
  3. 时间窗很大：1057 个账号全量 apply 实测 **35.53s**（见 P2-7），足够覆盖多次并发写。
- 影响：均衡/换绑不是“只改一个键”，而是用 0.4–35 秒前的整行快照覆盖账号；任务线程刚写入的 token、2FA 密钥、状态、手工备注都可能被回滚；用户删除的号会被重新创建。
- 建议：写回只做 `UPDATE accounts SET extra_json = json_set(extra_json, '$.register_proxy', ?) WHERE id = ? AND platform = ?`（或 `save` 只更新 `extra_json`），并在 `WHERE` 里带 `updated_at` 做乐观锁；行不存在时跳过并记日志，**绝不能用 upsert 做“更新”**。

### P0-4 升级路径没有把 `tags` 列接进自动迁移：按文档升级后启动即 `no such column: proxies.tags`

- 位置：`migrations/versions/d4e7f1a25b90_add_proxy_tags.py:31-35`（只在 Alembic 里 `add_column`）；`core/db/__init__.py:122-161`（`init_db()` 先 `create_all`，再 `run_migrations()`）；`core/db/migrations.py:760-791`（`run_migrations` 只跑手写迁移，**没有 tags**）。
- 证据：
  - 在副本上删除 `proxies.tags` 模拟老库，调用 `init_db()` 后 `pragma table_info(proxies)` 仍然没有 `tags`；随后 `select(ProxyModel)` 直接抛 `OperationalError: no such column: proxies.tags`。
  - `docs/MAINTENANCE.md:102-114` 明确写“`init_db()` 在每次启动时跑 `run_migrations()`……迁移是幂等的，不需要手工执行 SQL”，并警告“列还没补上时整条语句直接抛 `no such column`，启动就炸”。实际行为与文档相反：Alembic 迁移不在 `init_db()` 的调用链里。
  - 当前真库 `alembic_version=d4e7f1a25b90`，说明线上是有人手工跑过 Alembic 的；但任何**已有旧库、按“自动迁移”文档升级**的部署都会踩（旧库没有 tags，`init_db` 不会补）。空库由 `create_all` 直接建出 tags，不走这条路径，因此问题只在“老库升级”这个最常见场景暴露。
- 建议：二选一——(a) 在 `core/db/migrations.py` 增加幂等的 `_ensure_proxy_tags_column()`（`PRAGMA table_info` 判断后 `ALTER TABLE`），排在所有 `select(ProxyModel)` 之前；(b) 在 `init_db()` 里显式执行 `alembic upgrade head`（或部署脚本强制先跑）。补一条“老库无 tags → init_db → 列存在且可查询”的测试。

---

## 3. P1 发现

### P1-1 bind 空池/全禁用时静默绑定未打 `bind` 标签的代理，且“被删”分支会永久保留死代理

- 位置：`core/proxy_pool.py:341-364`（空池回退 `:352-355`，不可用回退 `:360-364`）；`core/proxy_pool.py:285-301`（`is_available` 对“不在池里”返回 `True`，`:299-301`）；`core/proxy_pool.py:100-106`（`list_bindable` 只取 active + bind）。
- 证据：
  - `resolve_for_account` 在 `bindable` 为空时走 `_pick_fallback` → `get_next()`（无 tag），返回任意 active 代理，且 `bool(chosen)` 使 `should_bind=True`，调用方会把它写进 `register_proxy`。
  - 副本实验（清空所有 tags）：`resolve_for_account("", fallback_provider=None)` 返回 `("", False)`；把 `saved` 设为已删除的 `socks5h://deleted-proxy:1@10.9.9.9:1080`，返回 **原值、False**——因为 `is_available` 把“不在池里”当手工代理，已删除的池代理永远不被替换；而 `rebalance` 在无 bind 池时直接拒绝（`:231-232`），所以没有任何自动恢复路径。
  - 单代理/全禁用：`list_bindable` 过滤掉 inactive，只剩 `saved` 且其 inactive 时 `pick_bindable(exclude=saved)` 返回 `None`，随后走 fallback，可能绑定一个未打标签的 active 代理；若没有其它 active，则保留死代理。
- 影响：模型注释 `core/models/proxy.py:66-68` 说“空列表 = 两个用途都不参与”，迁移注释 `d4e...:11-13` 说“既不能注册也不能绑定”；实际代码在空池时把这类代理绑给账号，语义自相矛盾。当前真库 5 个代理 tags 全空，第一次复用未绑定账号就会踩中。
- 建议：把“绑定回退”变成显式策略：`bind` 池为空时**不要**写 `register_proxy`（返回 `chosen, False` 或保留 `saved`），并记录 warning；若要兼容存量，增加 `proxy_bind_fallback_to_untagged` 配置并默认关闭。已删除的 `saved` 应可识别（`find_by_url` 返回 None 且 URL 不是调用方手工传入）→ 允许替换/清空，而不是当手工代理保留。

### P1-2 注册回退把 `bind`-only 代理当注册出口；注册又把 register 代理记成“绑定”，首次复用即换 IP

- 位置：`services/task_runners.py:120-125`（`_tagged or [全部 active]`）；`services/task_runners.py:284-285`（注册成功写 `account.extra["register_proxy"] = _proxy`）；`core/proxy_pool.py:343-350`（bindable 非空时不在 bindable 的 saved 被替换）。
- 证据：
  - 注册预取：没有 `register` 标签时，`_prefetched_proxies` 取**全部** active，包括只打了 `bind` 的代理；`get_next(tag=register)` 的回退逻辑 `core/proxy_pool.py:58-65` 同样如此。于是“只给绑定用”的代理会被注册任务消耗。
  - 注册成功把 `_proxy`（可能是 `register`-only）写进 `register_proxy`；下一次复用 `resolve_for_account` 发现它不在 `bindable_urls`，直接换一个 bind 代理并写回。注册注释 `task_runners.py:276-279` 说“之后复用优先回到同一个出口 IP”，在“注册池/绑定池分开”的标签配置下并不成立。
- 影响：出口 IP 在“注册 → 首次复用”之间必然变化，正是该功能想避免的；bind-only 代理被注册任务占用，绑定池容量被悄悄消耗。
- 建议：注册选路时排除 `bind`-only（`register` 标签优先；回退时至少排除 `bind`-only）；注册写 `register_proxy` 时只在代理同时带 `bind` 时写，或把“注册出口”和“绑定出口”分成两个字段；如果产品上就是要分开，删除“优先回到注册出口”的承诺并改注释。

### P1-3 绑定决策没有预留/锁：并发批量会把同一批号堆到同一个代理，两个 rebalance 会把 501 个号写 1002 次

- 位置：`core/proxy_pool.py:150-188`（`counts = self.bound_counts()` 在 `:165`，`random.choice` 在 `:188`）；`core/proxy_pool.py:215-263`（`counts` 只读一次在 `:234`，循环内只本地 `+=1` 在 `:257-258`）；调用点 `services/task_runners.py:493-507`。
- 证据：
  - 副本实验：8 个线程同时 `resolve_for_account(saved_untagged, fallback_provider=None)`，**8/8 都选中同一个代理**（`9.142.43.133:5303`），因为每个线程都在写回前读同一份 `bound_counts`，没有原子“占位”。
  - 两个线程同时 `rebalance(dry_run=False)`：各自都返回 `changed=501`，合计 **1002 次写**对应 501 个账号；耗时 21s（单线程 13.5s）。本次因目标选择是确定性的（`min(count,url)`）最终分布仍为 529/528，但写放大 2×；一旦与 `pick_bindable` 的随机 tie-break 或 `resolve_for_account` 交错，最终分布会漂移。
  - `rebalance` 的 `counts` 是快照，运行 35s 期间别的任务写入的绑定完全不可见；文档 `:222` 说“每次分配都重新读一次计数”与代码不符。
- 影响：并发注册/补号时“均衡”退化为“同时挑最轻的那个”，批量越大越容易堆叠；两个管理员同时点“执行均衡”会双倍写库。
- 建议：把绑定计数落到独立表/列（`proxy_bindings` 或 `proxies.bound_count`），用 `UPDATE ... SET bound_count = bound_count + 1 WHERE id = ? AND bound_count = ?` 做原子占位；或对 `rebalance`/`pick_bindable` 加进程级/数据库级互斥（SQLite `BEGIN IMMEDIATE`），并在 `resolve_for_account` 写回前重新读一次计数。

### P1-4 `looks_like_proxy_error()` 误判/漏判都明显，一次误判就永久摘掉健康代理并级联换绑

- 位置：`core/proxy_utils.py:207-234`（markers 在 `:209-221`，子串匹配在 `:231-234`）；`core/proxy_pool.py:265-283`（`report_proxy_error` 直接 `is_active=False`）；调用点 `services/task_runners.py:310-323`、`:577-596`。
- 证据（直接调用结果）：
  - 误判为真：`"账号 #1407 状态异常"` → `True`（`"407"` 是裸子串）；`"no proxy configured"` → `True`（`"proxy"`）；`"Browser does not support socks5 proxy authentication"` → `True`（这是浏览器配置错误，不是代理不健康）。
  - 漏判为假：`"net::ERR_NAME_NOT_RESOLVED"`（代理 DNS 失败常见形态）、`"502 Bad Gateway"`（代理侧 502）、中文 `"连接被拒绝"`/`"无法连接"`（不含“代理”二字）。
  - `report_proxy_error` 无阈值、无二次确认，直接把 `is_active=False`；如果该代理同时带 `bind`，所有绑定它的账号在下一次任务里都会 `resolve_for_account` → 换绑，形成一次误判引发的大规模出口漂移。
- 影响：一个带 `407` 的账号 ID/验证码/时间戳就能摘掉一个好代理；一个配置错误（Chromium 不支持 socks5 认证）也会被当成代理故障。
- 建议：用结构化错误码/异常类型优先（Playwright `ERR_PROXY_*`、requests `ProxyError`、HTTP 407）；文本匹配改成词边界/正则（`\b407\b`、`\bproxy\b`），并排除“配置/不支持/未配置”类短语；`report_proxy_error` 先进“suspect”状态，连续 2 次或 `check_all` 复核后才 `is_active=False`。

### P1-5 健康状态不可区分：`report_success` 会复活手工停用的代理，`report_fail` 对有成功历史的代理永不摘除

- 位置：`core/proxy_pool.py:77-96`（`report_success` 在 `:82` 无条件 `is_active=True`；`report_fail` 的条件在 `:93` 要求 `success_count == 0`）；`core/proxy_pool.py:400-422`（`check_all` 对成功项调用 `report_success`）。
- 证据：
  - 副本实验：手工把 `id=2` 设为 `is_active=0`，调用 `report_success(url)` 后 `is_available` 变回 `True`。
  - `report_fail` 的 `p.success_count == 0` 意味着一个曾经成功过 1 次的代理，之后连续失败 1000 次也不会被自动停用；`check_all` 也走 `report_fail`，所以死代理会一直留在池里。
  - `check_all` 遍历全部代理（包括手工停用的），成功即复活——无法区分“自动摘除”和“管理员手工停用”。
- 影响：管理员手动停用的代理会被任意一次成功/检测悄悄启用；有历史成功的死代理永远不会被自动清理，注册/绑定会持续命中它。
- 建议：增加 `disabled_reason`/`auto_disabled` 字段，`report_success` 只自动复活 `auto_disabled` 的行；`report_fail` 改用“连续失败计数”（成功时清零），不要依赖历史 `success_count`；`check_all` 对 `manual_disabled` 只报告不复活。

### P1-6 注册预取是快照：`report_proxy_error` 摘掉后，重试仍可能随机选中同一个死代理

- 位置：`services/task_runners.py:107-137`（预取在 `:109-125`，`random.choice` 在 `:133-134`）；错误处理 `:310-323`（日志说“下一轮换出口”）。
- 证据：`_prefetched_proxies` 在任务开始时构建一次；`report_proxy_error(_proxy)` 只改数据库的 `is_active`，不会从该列表移除；`_get_proxy()` 仍从旧列表 `random.choice`。只有一个代理时重试命中率 100%；5 个代理时每轮仍有 20% 概率命中同一个死代理。
- 影响：日志承诺“下一轮换出口”但实际可能原地重试；批量任务反复失败，浪费时间。
- 建议：代理报错后从 `_prefetched_proxies` 删除该 URL（加锁），或每轮重新 `get_next(tag=register)`（预取只作为缓存，出错即失效）；日志改为“已摘除 X，下一轮重新选路”。

### P1-7 `rebalance` 完全忽略 `proxy_balance_enabled` 与 `proxy_balance_max_per_proxy`

- 位置：`core/proxy_pool.py:215-263`（整个函数没有调用 `_balance_settings`）；对比 `core/proxy_pool.py:175-188`（`pick_bindable` 读配置并执行 cap）。
- 证据：`rebalance` 只读 `list_bindable` / `bound_counts`；没有 `config_store` 访问。把 `proxy_balance_max_per_proxy=100` 时，1057 个号仍会被平均分到 2 个代理（实测 529/528），远超上限；把 `proxy_balance_enabled=0` 时，`rebalance` 仍会执行均衡，而 `pick_bindable` 会改为纯随机。
- 影响：同一个“均衡配置”开关对自动选路和手动均衡行为不一致；用户关了均衡仍会被“均衡绑定”改写绑定，或设置上限后被无视。
- 建议：`rebalance` 开头读 `_balance_settings()`；`enabled=0` 时返回 `reason="均衡已关闭"`（或改为随机分配并说明）；`cap>0` 时按 cap 分配，剩余账号保留原绑定/报告无法分配。

### P1-8 `rebalance` 会搬走手工/一次性代理；`resolve_for_account` 的“不在池=可用”在 bind 池非空时失效

- 位置：`core/proxy_pool.py:285-301`（`is_available` 对不在池返回 True）；`core/proxy_pool.py:343-350`（bindable 非空时先替换）；`core/proxy_pool.py:238-262`（rebalance 只跳过 `current in bindable` 的账号）。
- 证据：
  - 副本实验：`is_available("http://manual:pass@9.9.9.9:8080")` 返回 `True`，但同一值传给 `resolve_for_account`（bind 池有 2 个代理）返回一个新 bind 代理且 `should_bind=True`——docstring `:333` 的“原代理不在池里 → 视为可用”在 bindable 非空时根本走不到。
  - `rebalance` 对 `current` 不在 `bindable` 的账号一律改到目标；手工给某个号指定的特殊代理会被“均衡绑定”覆盖。
- 影响：用户为单号手工指定的一次性代理无法保留；`is_available` 的语义与 `resolve` 的实际决策矛盾，排障时容易误判。
- 建议：给手工绑定加标记（`register_proxy_manual=true` 或单独字段），`resolve`/`rebalance` 跳过；或者把“不在池”分成“手工指定”和“已删除”两种，后者允许替换。

### P1-9 `rebind` 无候选时静默返回原值；已删除代理 + 空 bind 池会永久卡死

- 位置：`core/proxy_pool.py:206-208`（`chosen` 为空直接 `return old, old`）；调用方 `services/task_runners.py:586-592` 只在 `new != old` 时打日志；`core/proxy_pool.py:299-301`（不在池=可用）。
- 证据：副本实验：清空所有 tags，`saved` 设为不存在的代理，`resolve_for_account` 返回原值/False；`rebalance` 在无 bind 池时返回 `reason`（`:231-232`），不会修。`rebind` 在 `pick_bindable` 返回 None 时也没有日志。
- 影响：代理被删除/全部禁用后，账号会一直使用一个不存在的出口，每次任务失败，但日志里只有“使用代理: ...”，没有“无法换绑”的告警；用户以为绑定还在工作。
- 建议：`rebind` 无候选时记录 warning（含 email/platform/old）并返回明确状态；`resolve_for_account` 对“池里找不到且不是手工传入”的 saved 返回 `("", False)` 或允许清空；`rebalance` 在无 bind 池时也报告“N 个账号绑定已失效”，而不是只返回 reason。

### P1-10 安全：完整代理串（含账号密码）进入任务日志、接口响应和前端

- 位置：`services/task_runners.py:259`（注册日志 `使用代理: {_proxy}`）、`:320`（`已摘除 {_proxy}`）、`:568`（批量任务 `使用代理: {account_proxy}`）；`services/task_service.py:81-96`（日志写入 task_store + snapshot）；`api/proxies.py:37-39`（GET 返回 `ProxyRecord.url`）、`:128-173`（balance 返回 `url`）、`:176-192`（`from`/`to`）；前端 `frontend/src/pages/Proxies.tsx:313-316`（表格直接渲染 url）、`:271-274`（均衡预览显示 `item.to`）；`frontend/src/pages/Accounts.tsx:1697-1699`（详情显示并可复制 `registerProxy`）。
- 证据：
  - 真库 `task_runs.logs_json` 有 59 条含完整 `socks5h://***:***@...` 的记录（只读 SQL 统计）。
  - `redact_proxy_url` 已存在（`core/proxy_utils.py:109-145`），但上述三处日志没有使用；`_resolve_proxy_for_account` 的日志用了 redact（`:500`/`:505`），说明团队知道该怎么做，只是漏了这几条。
  - GET `/api/proxies` 与 `/api/proxies/balance` 返回原始 `url`；前端代理表格、账号详情、均衡确认弹窗都明文展示，账号详情还提供“复制代理”按钮。
- 影响：任何能看任务日志/面板/备份的人都能拿到代理账号密码；日志会进入 `task_runs`、快照、数据导出包，事后清理困难。
- 建议：所有日志统一走 `redact_proxy_url`；任务日志入库前做一次脱敏（或只存 `***:***@host:port`）；API 返回 `url_masked` + 仅在显式“编辑/查看”接口返回原文；前端默认掩码，复制按钮需二次确认。对历史 59 条日志做一次清洗/轮换代理密码。

---

## 4. P2 发现

### P2-1 URL 归一化只覆盖 `socks5→socks5h`，各比较路径不一致；重复行会分叉健康信号并让 balance 双计

- 位置：`core/proxy_utils.py:89-106`（只处理 legacy 与 `socks5`，其余原样返回）；`core/db/proxy_repository.py:55-73`（`find_by_url` 精确匹配）；`core/proxy_pool.py:23-42`（`_find_by_url` 精确 → 归一化精确 → 全表扫描）；`api/proxies.py:150-170`（按归一化 key 取计数，但 `items[].url` 是原文）。
- 证据：
  - `normalize_proxy_url("HTTP://u:p@Host.Example:8080")` 与 `"http://u:p@host.example:8080"` 返回不同字符串；`"http://u:p@host:8080/"` 与无尾斜杠也不同。
  - 副本实验：已有 `socks5://...@9.142.43.158:5328`，`proxy_repository.find_by_url("socks5h://...@9.142.43.158:5328")` 返回 `None`，随后 `add` 成功插入 `id=7` 的重复行；把重复行设为 inactive 后，`is_available(归一化 URL)` 变成 `False`（精确命中 inactive 的重复行），尽管原行仍 active。
  - 重复行存在时，`api/proxies.py:152` 对两行都取同一个归一化 `stat`，`:170` 的 `bound_accounts` 会把同一批账号**加两次**；`bindable_count` 也会多算。
- 影响：用户从账号详情复制 `socks5h://` 到“添加代理”就会造出重复行；健康计数被拆到两行，`report_proxy_error` 可能只摘掉其中一行；balance 页面数字虚高。
- 建议：引入唯一的 `canonical_proxy_url()`（scheme/host 小写、去尾斜杠、legacy 归一、`socks5→socks5h`），**写入时**落 canonical，比较时一律 canonical；给 `proxies.url` 加 canonical 唯一索引；迁移现有行并合并重复。`find_by_url` 改用 canonical 查询，不再精确匹配。

### P2-2 `ProxyRepository.get_many` 两种后端返回不同类型；`save` 是整行写

- 位置：`core/db/proxy_repository.py:43-53`（`list_all` 返回 `ProxyRecord`）vs `:163-181`（`get_many` SQLite 分支 `:178` 直接返回 `ProxyModel`，JSON 分支 `:170-174` 返回 `ProxyRecord`）；`save` 在 `:104-143`。
- 证据：`api/proxies.py` 的 `toggle`/`update_proxy_tags` 拿到 `ProxyModel` 时给 `tags` 赋 `list`（字段声明是 `str`），依赖 `save` 内部再 `proxy_tags_csv` 纠正；JSON 后端则拿到 `ProxyRecord`。`save` 的 JSON 分支 `:110-122` 把 url/region/is_active/success_count/fail_count/last_checked/tags 全量写回，若传入的是旧快照会覆盖并发更新。
- 影响：两条后端行为不同；任何未来调用方按 `ProxyRecord` 使用 `get_many` 的返回值都会在 SQLite 下拿到 `str` 类型的 tags。
- 建议：`get_many` 统一返回 `ProxyRecord`（SQLite 分支也 `from_model`）；`save` 改为字段级 patch（只写调用方明确改的字段），或至少不要用旧快照覆盖计数。

### P2-3 `ProxyRecord.to_dict()` 丢 `id`/`tags`，测试却声称“round trips every column”

- 位置：`core/models/proxy.py:105-113`（`to_dict` 只有 url/region/success_count/fail_count/is_active/last_checked）；`tests/test_proxy_record.py:17-31`（只断言 5 个字段 + last_checked）。
- 证据：`ProxyRecord(url=..., tags=["bind"], id=7).to_dict()` 没有 `id`/`tags`；`from_dict(to_dict())` 后 tags 变成 `[]`、id 变成 `None`。当前生产路径没有用 `to_dict` 做持久化，但它是公开的“记录 → dict”接口，导出/日志/未来 JSON 适配器一旦使用就会静默丢标签。
- 建议：`to_dict` 补 `id`/`tags`；测试补 `tags`/`id` 的 round-trip，别再用“every column”这种与实现不符的描述。

### P2-4 API 边界：单条添加不 trim/不归一、批量逐条 commit、删除不处理绑定、toggle 无确认

- 位置：`api/proxies.py:42-49`（`add_proxy` 只 `find_by_url` 精确查重，`ProxyRepository.add` 在 `core/db/proxy_repository.py:86/92` 存 `str(url)` 不 `strip`）；`:52-65`（bulk 每条 `add` 都 commit）；`:68-74`（删除代理不检查/不清理账号绑定）；`:97-105`（toggle 一键停用，无确认/影响面提示）。
- 证据：`find_by_url` 会 `strip` 查询值，但 `add` 存原串；添加 `" http://x "` 后按 `"http://x"` 再查不到，形成重复。bulk 1000 条 = 1000 次 commit（与 rebalance 相同的 27ms/commit 量级）。删除/停用一个 bind 代理会让所有绑定账号在下次任务里换绑，接口不返回影响账号数。
- 建议：写入前 `strip` + canonical；bulk 用单事务/批量 insert；删除/停用前返回 `bound_total` 并要求 `force=true`；toggle 在 UI 上显示“将影响 N 个账号”。

### P2-5 `check_all` 串行 8s × N + 依赖外部 httpbin：1000 个代理最坏 2.2 小时

- 位置：`core/proxy_pool.py:400-422`（`for p in proxies` 串行，`timeout=8`，目标 `https://httpbin.org/ip`）。
- 证据：1000 个代理 × 8s = 8000s ≈ 2.2h；`BackgroundTasks` 只保证不阻塞响应，不保证并行；httpbin 故障时所有代理都会 `report_fail`（对有成功历史的代理又不会停用，见 P1-5）。
- 建议：线程池并发（如 10–20）+ 总超时预算；探活目标改为可配置的本地/自建 echo；区分“代理失败”和“探测目标失败”。

### P2-6 `bound_counts` 文档说“1000 个号 ~50ms、不进热路径”，实测 0.54–1.1s，且每个账号复用都会走

- 位置：`core/proxy_pool.py:108-128`（docstring `:114-115`）；调用链 `core/proxy_pool.py:343-350` → `:348` → `:162-165`。
- 证据：真库副本上 `bound_counts()` 实测 1.107s（首次）/0.541s（后续）；`list_all_accounts()` 0.69s。`resolve_for_account` 对“saved 未打标签/已删除/空”的账号每次都要 `list_bindable` + `is_available` + `pick_bindable`（内部再 `list_bindable` + `bound_counts`），实测慢路径 0.541s/账号；1057 个这样的账号 ≈ 9.5 分钟纯 DB 扫描。只有 saved 是 active bind 代理时快路径 0.010s。
- 建议：把绑定计数做成 `proxies.bound_count` 列或 `proxy_bindings` 表，用 SQL `GROUP BY` 一次算；`resolve_for_account` 缓存一次请求内的 `bindable`/`counts`；不要在每账号路径上解密 1057 份 `extra_json`。

### P2-7 `rebalance` 是同步 HTTP + 每账号一次 commit：1057 个号实测 35.5s，无事务/进度/锁

- 位置：`core/proxy_pool.py:235-262`；`api/proxies.py:176-192`；`core/db/repository.py:361-363`（`upsert` 每次新建 session）、`:435-437`（关闭）。
- 证据：1057 个全不合规账号：dry-run 0.42s / **apply 35.53s**，SQLAlchemy 事件计数 501 个号时 **501 次 UPDATE + 501 次 commit**；1057 个号同理 1057 次。HTTP 请求期间没有进度，客户端超时/刷新后看不到“已处理多少”；进程中断会留下部分已改、部分未改的状态（重跑可收敛，但用户不知道）。
- 建议：改成后台任务（复用 `task_store` 的进度/停止机制）返回 `job_id`；每个平台库一个事务分批提交（如 100/批）；加互斥锁防止双击并发；响应只返回摘要，明细走任务日志。

### P2-8 解密异常无隔离：一条坏 `extra_json` 就让 balance/rebalance 500

- 位置：`core/proxy_pool.py:121`（`account.get_extra()`）、`:239`（同）；`api/proxies.py:142`（balance 也调用 `bound_counts`）。
- 证据：副本上把一条真实账号的 `sso` 改成 `v1:AAAA` 后，`bound_counts()` 与 `rebalance(dry_run=True)` 都抛 `CredentialKeyError: 凭据密文长度不足`；整个代理页/均衡端点 500，无法只跳过坏号。
- 建议：`bound_counts`/`rebalance` 对每个账号 `try/except (CredentialKeyError, ValueError)`，记录 email/id 并计入 `skipped_errors`，不要中断全局；API 返回 `error_accounts` 列表。

### P2-9 `rebalance` 对空 email/platform 没有预校验：dry-run 能过，apply 中途 `ValueError`

- 位置：`core/proxy_pool.py:238-262`；`core/db/repository.py:352-355`（`upsert` 缺 platform/email 直接 `raise ValueError`）。
- 证据：`rebalance` 的 dry-run 只做内存计算，不调用 `upsert`；apply 到该账号时才抛 `ValueError`，前面的账号已经逐个 commit，形成部分完成。真实库当前无空 email/platform（只读查询 0 行），但导入/同步可能产生。
- 建议：`rebalance` 开头过滤 `not email or not platform` 并计入 `skipped_invalid`；apply 外层 `try/except` 记录并继续，返回失败清单。

### P2-10 `ProxyRecord.__repr__` 带完整 URL（低）

- 位置：`core/models/proxy.py:115-116`。
- 证据：`repr(record)` 输出 `<ProxyRecord id=2 url='socks5://user:pass@host:port'>`；任何 `logger.debug("%r", record)` 或异常上下文都会泄漏。当前代码没有直接这样打，但属于“默认不安全”的表示。
- 建议：`__repr__` 用 `redact_proxy_url(self.url)`。

### P2-11 `region` 字段在标签/绑定/均衡路径完全不参与选路

- 位置：`core/proxy_pool.py:100-106`（`list_bindable` 不读 region）、`:150-188`（`pick_bindable` 不读）、`:215-263`（`rebalance` 不读）；`services/task_runners.py:137`/`:480` 调 `get_next` 时也不传 region。
- 证据：`get_next(region=...)` 本身支持过滤（`core/proxy_pool.py:55-56`），但核心调用点都没有传；`api/proxies.py` 的 `ProxyCreate.region` 只落库/展示。给代理设置 `US`/`SG` 后，绑定和均衡不会按账号地区筛选，UI 的“地区”是装饰字段。
- 建议：要么在 `list_bindable`/`pick_bindable`/`rebalance` 增加 `region` 维度（账号有 `region` 列），要么从 UI 移除该字段避免误导；如果产品上地区只用于展示，明确写进注释。

---

## 5. 逐条回答

### (1) 标签语义：register 回退 / bind 分支

**register 回退**

| 分支 | 行为 | 判定 |
|---|---|---|
| 有 `register` 标签的 active 代理 | 只在其中选（`core/proxy_pool.py:58-61`；预取 `task_runners.py:120-124`） | ✅ 正确 |
| 没有 `register` 标签，但有 active 代理 | 回退到**全部 active**，包含 `bind`-only 和空标签（`:62-65`、`task_runners.py:125`） | ⚠️ 会把 bind-only 当注册出口（P1-2） |
| 空池 | `get_next` 返回 `None` → 直连；无显式“直连”告警（`:66-67`） | ⚠️ 可用，但用户不知道没走代理 |
| 全部 disabled | 同空池 → 直连 | ⚠️ 同上 |
| 单代理 | 正常返回该代理 | ✅ |
| 代理被删除（任务运行中） | 预取列表是快照，仍会继续用已删除的 URL | ⚠️ P1-6 |

**bind 分支**

| 分支 | 行为 | 判定 |
|---|---|---|
| 已绑定，且 saved 在 bind 池且 active | 原样返回，不写库（`:346-347`） | ✅ |
| 已绑定，saved 没有 `bind` 标签 | 从 bind 池挑最轻的替换（`:348-350`） | ✅（但手工代理也会被替换，P1-8） |
| 已绑定，saved 已删除，bind 池非空 | 替换并写回（`:348-350`） | ✅ |
| 已绑定，saved 已删除，bind 池为空 | `is_available` 把“不在池”当手工代理 → 保留死代理（`:299-301`、`:357-358`） | ❌ P1-1/P1-9 |
| 未绑定，bind 池非空 | 挑最轻的，`should_bind=True` | ✅ |
| 未绑定，bind 池为空 | fallback 到任意 active 代理并写回（`:352-355`） | ❌ P1-1 |
| bind 池全部 disabled | 同空池 → fallback 到未打标签代理，或保留死代理 | ❌ P1-1 |
| 单代理，active 且就是 saved | 保留 | ✅ |
| 单代理，inactive / 与 saved 相同但不可用 | `pick_bindable` 排除后返回 None → fallback 可能绑未打标签代理 | ❌ P1-1 |
| 单代理，saved 已删除、该代理 active | 选中它并写回 | ✅ |

### (2) 均衡：dry-run 与 apply、并发、耗时、事务

- **dry-run 与 apply 是否一致**：单线程、同一快照下**一致**。副本 501 个待迁移账号，dry-run `changed` 列表与 apply `changed` 列表逐项相等（`dry==apply: True`）；dry-run 0.595s / 0 次 UPDATE，apply 13.467s / 501 次 UPDATE + 501 次 commit。但 apply 是**重新计算**的，不绑定 dry-run 的计划：用户在确认弹窗停留期间若有任务改绑，apply 的 `changed` 会与预览不同；API 没有 plan token/version（`api/proxies.py:176-192`）。
- **并发会不会把同一个号挪两次**：会。两个并发 `rebalance` 各自返回 501、合计 1002 次写；`pick_bindable`/`resolve_for_account` 也没有占位，8 并发 8/8 选中同一代理。`rebalance` 与 `resolve`/`rebind` 同时写同一账号时，后写覆盖先写（P0-3/P1-3）。
- **1000+ 耗时**：1057 个全不合规账号 apply 实测 **35.53s**、1057 次 commit；501 个 13.47s。dry-run 0.42–0.60s。`bound_counts` 0.54–1.1s；`resolve_for_account` 慢路径 0.54s/账号（1057 个 ≈ 9.5 分钟）；`rebind` 0.57s/次。
- **事务边界**：没有整体事务；`account_repository.upsert(account)` 每次新建 session 并 commit（`repository.py:361-363`/`:435-437`）。中途失败/进程退出会留下部分完成；重跑可收敛，但 API 不告诉用户进度和失败清单（P2-7）。
- **额外不一致**：`rebalance` 不读 `proxy_balance_enabled` / `proxy_balance_max_per_proxy`，而 `pick_bindable` 读（P1-7）。

### (3) 换绑：`rebind()` 与 `resolve_for_account()` 是否互相打架

会，且有三层冲突：

1. **定位冲突**：`rebind` 按 email 全平台取最新行（P0-2）；`_persist_register_proxy` 固定 chatgpt（P0-1）。同一邮箱双平台时，两者可能改不同行；Grok 任务里 `resolve` 决定“已更新”，`_persist` 实际 no-op，下一次 `resolve` 又换一个出口（sticky 绑定完全失效）。
2. **写回冲突**：`rebind` 用整行快照 `upsert`（`proxy_pool.py:209-212`），`rebalance` 同样（`:259-262`）；`_persist_register_proxy` 只改一个键但仍是 read-modify-write（`task_runners.py:525-529`）。三者没有锁/版本号，交错时后写覆盖先写；`rebalance` 的 35s 窗口尤其大。实测：rebalance 期间写入的 password/marker 被回滚，删除的账号被以新 UUID 复活（P0-3）。
3. **语义冲突**：`resolve` 的“不在池=可用/手工代理保留”（`:333`）与“bindable 非空就替换不在 bind 池的 saved”（`:343-350`）矛盾；`rebalance` 又把不在 bind 池的 saved 全部搬走（P1-8）。手工代理、已删除代理、register-only 代理在三条路径上的处理各不相同。
4. **失败日志/回滚**：`rebind` 无候选返回 `(old, old)` 且不打日志（`:206-208`；调用方 `:587` 只在变化时打）；`resolve` 无候选返回 `saved, False`，也不打“无法换绑”。`rebind` 的 `upsert` 异常会被 `_do_one` 的 `except Exception` 吞成“账号异常”（`task_runners.py:604-606`），原始失败原因丢失。

### (4) `looks_like_proxy_error` 的误判/漏判

- **误判**（会把好代理摘掉）：`"407"` 裸子串命中任何含 407 的账号 ID/验证码/时间戳（实测 `"账号 #1407"` → True）；`"proxy"` 命中 `"no proxy configured"`、`"Browser does not support socks5 proxy authentication"`（配置错误）；`"etimedout"`/`"econnreset"`/`"failed to connect"` 是通用网络错误，目标站重置也会命中。
- **漏判**（不会换绑，白跑）：`net::ERR_NAME_NOT_RESOLVED`（代理 DNS 失败常见）、`502 Bad Gateway`（代理侧 502）、中文 `"连接被拒绝"`/`"无法连接"`（不含“代理”）、`ERR_CONNECTION_CLOSED`。
- **放大效应**：`report_proxy_error` 一次命中就 `is_active=False`（`proxy_pool.py:281`），且该代理若带 `bind`，所有绑定账号下次任务都会换绑；在下次成功上报/“检测全部”之前它会一直停用。误判成本远高于注释里说的“摘掉 + 换一次出口”。

### (5) URL 归一化（socks5 vs socks5h）每条比较路径是否一致

不一致，至少有 5 条路径：

1. **写入口**：`ProxyRepository.add` 原样存 `str(url)`（`proxy_repository.py:86/92`）；`api.add_proxy` 只用精确匹配查重（`:45`）。`socks5://` 与 `socks5h://` 可同时入库（实测插入重复行 id=7）。
2. **读入口**：`ProxyRepository.find_by_url` 精确匹配（`:67-69`），不归一；`ProxyPool._find_by_url` 精确 → 归一化精确 → 全表扫描（`:23-42`），两条路径结论不同（实测 repository 返回 None，pool 能找到）。
3. **计数**：`bound_counts` 用 `normalize_proxy_url` 做 key（`:121`），`api/proxies.py` 的 `known`/`stat` 也用归一化（`:150-152`），但 `items[].url` 是原文；重复行会双计（P2-1）。
4. **绑定选择**：`bindable_urls` 归一化（`:345`）、`pick_bindable` 归一化排除（`:166-170`），但返回的是 `row.url` 原文；`rebind` 把原文写回（`:210`），`resolve` 返回归一化的 saved（`:336`），同一账号的写法会在 `socks5://`/`socks5h://` 之间来回变。
5. **健康上报**：`report_success`/`report_fail`/`report_proxy_error` 都先归一化再 `_find_by_url`（`:79`/`:89`/`:272-276`），但 `_find_by_url` 精确命中可能命中重复行中的 inactive 行，导致“摘错行”（P2-1）。

另外 `normalize_proxy_url` 只处理 `socks5` 与 legacy 四/五段格式，**不统一 scheme/host 大小写、不去尾斜杠、不处理 IPv6 无括号**；`HTTP://Host:8080` 与 `http://host:8080` 被视为两个代理。

### (6) 安全：代理串（含账号密码）是否出现在日志/接口/前端

是，三条路径都有：

- **日志**：`task_runners.py:259/320/568` 明文；真库 `task_runs.logs_json` 已有 59 条完整 `user:pass` 记录；日志会进 `task_store`、快照和数据导出包。
- **接口**：`GET /api/proxies` 返回原始 `url`（`api/proxies.py:37-39`）；`GET /api/proxies/balance` 返回原始 `url`（`:155-164`）；`POST /api/proxies/balance` 返回 `from`/`to` 原始 URL（`:186-192`）。
- **前端**：代理表格直接渲染 `url`（`Proxies.tsx:313-316`）；均衡预览显示 `item.to`（`:271-274`）；账号详情显示并可复制 `registerProxy`（`Accounts.tsx:1697-1699`）。
- 已有的 `redact_proxy_url` 只在 `task_runners.py:500/505/590` 和 Grok turnstile 日志里用了，覆盖不全。

---

## 6. 测试评估

已跑（单文件，conftest 使用临时库，不碰真库）：

- `tests/test_proxy_tags_balance.py`：14 passed（2.55s）。
- `tests/test_account_proxy_reuse.py`：19 passed（1.56s）。
- `tests/test_account_proxy_rebind_endpoints.py`：4 passed（3.73s）。

测试的优点是 conftest 把 `DATABASE_URL`/`DATA_DIR` 指到临时目录（`tests/conftest.py:40-56`），不会污染真库；标签解析、`get_next` 标签过滤、`resolve` 的四条基本路径、`rebalance` 的 6 账号小样本都有覆盖。

但新功能的边界基本没覆盖：

- **没有 `rebind()` 的任何测试**（`tests/test_proxy_tags_balance.py` 全文无 `rebind`）；P0-2 的跨平台取行、P1-9 的无候选静默都不会被发现。
- **没有 `_persist_register_proxy` 的 platform 测试**；`test_account_proxy_rebind_endpoints.py:54-57` 的 `_add_proxy` 默认 `tags=""`，所以端到端测试仍在走“无标签回退”的旧路径，P0-1 的 Grok 写回完全没被触达。
- **空池/全禁用/单代理/被删**：只测了“bind 池非空 + saved 无标签/已删除”，没有测 bind 池为空、全部 disabled、单代理被排除、已删除 + 空池永久保留（P1-1/P1-9）。
- **dry-run 与 apply 的一致性**：`test_dry_run_changes_nothing`（`:167-172`）只比较 `URL_UNTAGGED` 一个计数，没有比较两次 `changed` 列表；`test_rebalance_moves_only_non_compliant_and_balances`（`:174-183`）只断言 6 个测试账号的 `abs(a-b)<=1`，没有 1000+、并发、cap、enabled、手工代理、空 email 的用例。
- **异常判定**：没有任何 `looks_like_proxy_error` 的用例，P1-4 的 `"407"` 误判/`ERR_NAME_NOT_RESOLVED` 漏判没有回归保护。
- **URL 归一化**：没有 `socks5://` vs `socks5h://` 重复添加、`find_by_url` 精确/归一化差异、重复行健康分叉的测试（P2-1）。
- **迁移**：没有“老库无 tags → init_db → 列存在”的测试；`test_migration_index_parity.py` 只覆盖空库 Alembic，不覆盖 `init_db` 路径（P0-4）。
- **`ProxyRecord`**：`test_round_trips_every_column` 名字说 every column，但只断言 5 个字段，`id`/`tags` 都没测（P2-3）。

---

## 7. 建议修复顺序

1. **P0-1/P0-2（跨平台）**：所有代理写回改为 `(account_id, platform)` 定位；`rebind` 增加 `platform` 参数；补 Grok/共享邮箱的端到端测试。
2. **P0-3（快照覆盖/复活）**：写回改为只更新 `extra_json.register_proxy` 的字段级 UPDATE + `updated_at` 乐观锁；行不存在时跳过，禁止 upsert。
3. **P0-4（迁移）**：把 `_ensure_proxy_tags_column()` 加进 `core/db/migrations.py`，或让 `init_db()` 执行 Alembic；补老库升级测试。
4. **P1-1/P1-2（标签语义）**：明确“空 bind 池是否允许绑未打标签代理”，默认不允许；注册回退排除 bind-only；注册写绑定前检查 `bind`。
5. **P1-3/P1-7/P2-6/P2-7（均衡/并发/性能）**：绑定计数落表/列 + 原子占位；`rebalance` 读配置、后台任务、分批事务、互斥锁；`bound_counts` 改成 SQL 聚合。
6. **P1-4/P1-5/P1-6（健康判定）**：结构化错误码 + 词边界；`disabled_reason` 区分手工/自动；预取列表出错即失效。
7. **P1-10（安全）**：日志统一 redact，清洗历史 59 条；API 返回 masked，前端默认掩码。
8. **P2**：canonical URL + 唯一索引；`get_many` 统一 `ProxyRecord`；补测试。

---

## 8. 复现实验与限制

- 真库只读：`sqlite3.connect("file:data/account_manager.db?mode=ro", uri=True)`；未执行任何 INSERT/UPDATE/DELETE。
- 可写实验：先把真库 `shutil.copyfile` 到 `/root/.hermes/cache/scratch/proxy_audit/audit.db`，同时复制 `data/secrets`（否则 `get_extra` 会因缺密钥报 `CredentialKeyError`），再通过 `DATABASE_URL`/`DATA_DIR` 指向副本运行。
- 计时环境：本机 SQLite 3.45.1，Python 3.12，`.venv`；数值是单机实测，生产磁盘/负载不同会有差异，但 27ms/commit、0.54s/1057 行解密的量级可复现。
- 未测：真实 Playwright/Chromium 的代理错误文本（只做了字符串级判定）、真实网络下的 `check_all`、JSON 存储后端（只审了代码路径）。
