# Task 8 — 后台周期任务：13 个 job 的注册与执行

状态: 完成（第一版，全部结论均有可复现命令）

范围: `services/local_maintenance.py`、`services/chatgpt_maintenance.py`、
`services/panel_status_sync.py`、`services/log_cleanup.py`、`services/cliproxyapi_sync.py`、
`core/scheduler.py`、`main.py` 的 scheduler 注册段。

验证方式：不是读代码。真实 import 两个自注册模块、用 spy 包住 `register_job` 数调用点、
用临时 `DATA_DIR`/`DATABASE_URL` 起一次性 SQLite 逐项开关矩阵、用亚秒 tick 的真
`Scheduler()` 线程观察触发与异常隔离、真实执行 `run_log_cleanup()`。

## 结论

**注册面是干净的；执行面有一个必现的 Critical。**

1. 13 个 job 与启动日志**完全相等**（双向），13 次 `register_job` 调用、13 个唯一名字、
   零重复（T8-0 基线，见覆盖清单）。
2. **`log_auto_cleanup` 在 SQLite 后端 100% 失败**：`run_log_cleanup` 把
   `core.db.get_session`（FastAPI 依赖，裸生成器）当上下文管理器用 →
   `TypeError: 'generator' object does not support the context manager protocol`。
   任务因此**永远清不掉任何一条历史**，并且失败不推进 `last_run_at`，
   生产上每 60s 重试一次、每 60s 打一行错误，永不停止（T8-1，Critical）。
3. 该分支后面还叠着第二个 bug：即使修好 #1，naive cutoff 会让 SQLModel 直接
   `StatementError`（T8-2，Important）。
4. 异常隔离本身是**过关的**：runner 抛异常不会杀死调度线程，其它 job 继续触发
   （已构造验证）。但「失败即每 tick 重试」「last_run_at 不持久化」「单线程串行」
   三个既有设计带来 T8-3 / T8-4 / T8-5 三条 Important。
5. 计数：**Critical 1**（T8-1）、**Important 5**（T8-2/3/4/5/6）、**Minor 2**（T8-7/8）、
   另有 1 条基线结论（T8-0，非缺陷）与 3 条 hypothesis（H1/H2/H3，未复现故不定级）。

## 发现

### T8-0（基线，非缺陷）13 个 job 与日志逐字相等

- 证据命令：`.venv/bin/python -c "import services.chatgpt_maintenance, services.local_maintenance; from core.scheduler import registered_jobs; print(len(registered_jobs()), registered_jobs())"`
  → `13 ['aws_auto_probe','aws_auto_refresh','chatgpt2api_auto_sync','chatgpt_auto_probe','chatgpt_auto_refresh','cpa_auto_sync','grok2api_auto_sync','grok_auto_probe','grok_auto_refresh','kiro_auto_probe','kiro_auto_refresh','log_auto_cleanup','sub2api_auto_sync']`，
  与 brief 里的 live log **逐字相同**。
- 调用点计数（spy 包 `register_job`，见 T8-6 复现脚本）：`call sites total: 13 unique: 13 duplicates: {}`，
  且 `set(registered_jobs()) ^ set(调用序列) == set()` —— 双向相等。
- 三个注册点：`services/local_maintenance.py:671-675`（`log_auto_cleanup`）、
  `services/local_maintenance.py:695-705`（`PLATFORM_ACTIONS` 4 平台 × 2 = 8 条）、
  `services/chatgpt_maintenance.py:551-555`（`chatgpt2api_auto_sync`）+
  `services/chatgpt_maintenance.py:567-572`（`sub2api/cpa/grok2api` 3 条）。
  1+8+1+3 = 13。`panel_status_sync.py` / `log_cleanup.py` / `cliproxyapi_sync.py`
  **不注册任何 job**（`grep -rn "register_job" ` 只命中上面两个模块）——它们是被
  调用方，不是注册方，符合 brief 的怀疑方向。
- 名字唯一性有保障：`core/scheduler.py:92-93` 同名覆盖写，所以重复注册不会报错、
  只会**静默覆盖**（少一条日志也看不出来）。当前无重复，但这条静默语义是 T8-6 的成因。

### T8-1（Critical）`log_auto_cleanup` 在 SQLite 后端必现崩溃，任务永不生效，且每 60s 重试

- 位置：`services/local_maintenance.py:648`（`from core.db import get_session`）+
  `services/local_maintenance.py:655`（`with get_session() as session:`）。
  根因：`core/db/base.py:121` 的 `get_session()` 是 **FastAPI 依赖生成器**
  （`def get_session() -> Iterator[Session]: ... yield session`），没有
  `@contextmanager`；全仓只有这一处把它当上下文管理器用
  （`grep -rn "with get_session()"` 仅 1 条命中，就是这里）。
- 复现（临时库，不碰生产）：
  `DATA_DIR=$(mktemp -d) DATABASE_URL=sqlite:///$DATA_DIR/t.db PYTHONPATH=. .venv/bin/python -c "import services.local_maintenance as lm; print(lm.run_log_cleanup())"`
  → `TypeError: 'generator' object does not support the context manager protocol`。
- 通过真实调度器复现（亚秒 tick 放大观察）：
  `python` 起 `Scheduler()` + `_loop_interval_seconds=0.2`，把 `log_auto_cleanup` 置为到期
  → 0.9 秒内打出 **4 行** `[Scheduler] 周期任务 log_auto_cleanup 错误: 'generator' object does not support the context manager protocol`。
  生产 tick = 60s（`core/scheduler.py:114`），即**每分钟一条错误、永不停止**。
- 为什么生产必然走这条分支：`data/account_manager.db` 存在（33 MB，2026-11-14 19:37），
  `.venv/bin/python -c "from core.storage import resolve_backend_name, sqlite_db_exists; print(resolve_backend_name(db_exists=sqlite_db_exists()))"`
  → `sqlite`。JSON 后端才会在 `services/local_maintenance.py:641-646` 提前 return
  （`cleanup_json` 返回非 None）；SQLite 分支就是本条。
- 附带后果（调度器语义）：`core/scheduler.py:177-182` 失败时**不推进** `last_run_at`，
  所以「永久失败」= 每 tick 重试 = 每分钟一次无意义的 SQL 会话构造 + 一行 print。
- 爆炸半径：`task_logs` 永不清理 → 库无界增长（这也是 `log_auto_cleanup` 存在的唯一目的）；
  日志/任务历史页的保留期承诺（`log_cleanup_keep_days`）完全失效；错误行污染服务日志。
  不影响其它 12 个 job（异常被 `core/scheduler.py:177` 捕获）。
- 修复：改成 `with Session(current_engine()) as session:`（与 `services/log_cleanup.py:49`
  同一写法），或给 `core/db/base.py` 的 `get_session` 加 `@contextmanager`
  —— 后者影响所有 `Depends(get_session)` 调用点，不建议；首选前者，并且
  真正复用 `services/log_cleanup.py::cleanup_task_logs` 而不是再写一份 SQL。
- 修复后必须补测试：现有 74 个相关测试（`tests/test_local_maintenance.py`、
  `tests/test_task_runtime.py`）全绿，却**没有一条执行过 `run_log_cleanup`**
  （`grep -rn "run_log_cleanup" tests/` 零命中；`tests/test_log_cleanup.py` 只测
  `services/log_cleanup.py` 与前端选项）。这是本条能潜伏到今天的原因。

### T8-2（Important）同一函数里叠着第二个必现 bug：naive cutoff 会让 SQL 直接抛

- 位置：`services/local_maintenance.py:654`（`naive_cutoff = cutoff_dt.replace(tzinfo=None)`）
  + `:656`（`TaskLog.created_at < naive_cutoff`）。
  `TaskLog.created_at` 是 aware 列（`core/db/models_infra.py:30`，SQLModel `DateTime`
  带时区校验），传 naive 值直接 `ValueError: Datetime values must have timezone information`。
- 复现（绕过 #1，直接用真实 `Session` 跑同一段 SQL）→
  `naive cutoff DELETE FAILED: StatementError (builtins.ValueError) Datetime values must have timezone information`。
- 也就是说 #1 修好后这条会立刻接棒，任务**仍然**不工作。两条必须一起修。
- 反证：`services/log_cleanup.py:65-74`（人工清理端点）用 aware cutoff + `Session`，
  跑得通 —— 同一需求已有一份正确实现，定时任务这份是重写的劣化版。
- 修复：删掉 `.replace(tzinfo=None)`，或用 `NaiveDatetime` 列注解；顺手对齐 T8-1 建议
  （直接调用 `cleanup_task_logs(keep_days)`）。

### T8-3（Important）`last_run_at` 只在内存里 → 重启即清零，"间隔"实际变成"自进程启动起算"

- 位置：`core/scheduler.py:62`（`last_run_at` 是 dataclass 字段，无持久化）+
  `core/scheduler.py:124-129`（`start()` 把所有 job 的 `last_run_at` 顶到 `now`）。
- 「重启不瞬间触发」这个诉求**达成**了（已构造验证：`start()` 后 0.9s 内 0 次触发，
  `last_run_at` 被顶到当前时间）。但它同时意味着**重启把计时器清零**。
- 复现（纯算术，无 sleep，跑 30 小时虚拟时间）：每 5 小时重启一次、间隔 6h 的 job
  → `fired in 30h with restart every 5h: 0`。即**重启周期短于 job 间隔时，该 job 永不执行**。
- 爆炸半径：`log_auto_cleanup` 默认 24h，`docs/MAINTENANCE.md:42,71` 明确要求改代码后
  `systemctl restart`。任何一天内重启过一次的部署，24h 清理**永远不跑**；
  6h 的 probe/refresh 在一天重启 4 次以上的部署里同样永不执行。
  这是静默失效（除 T8-1 那条错误外没有任何提示）。
- 修复：把 `last_run_at` 落库（configs 或一张 `job_state` 表），`start()` 读回来而不是
  无条件覆盖；若要保留"不瞬间触发"，用 `min(now, stored + interval)` 之类的折中，
  并对"已超期"的 job 只补跑一次而不是跳过。

### T8-4（Important）间隔从"开始跑"算，不是文档/UI 承诺的"跑完"算 → 长轮次会背靠背连跑

- 位置：`core/scheduler.py:171-176`。`now` 在 `:153`（tick 开始）取，`job.runner()`
  跑完后 `:176` 写回的仍是那个 `now` —— 即 `last_run_at` = **本轮开始时刻**。
  但 UI 与文档的口径是"以距上次**跑完**算"：
  `frontend/src/components/settings/MaintenancePanel.tsx:390,578`、
  `services/local_maintenance.py:666`、`tests/test_local_maintenance.py:272`。
- 复现（把时钟压成 1:1000 比例，用真 `Scheduler()` 线程）：
  runner 执行 0.35s、间隔 0.3s、tick 0.06s → `rounds: 2 gaps(s): [0.4]`，
  第二轮几乎无缝接上。纯逻辑复现更直接：4 个合成 tick 每次 `_run_due_jobs` →
  `runs so far = 1,2,3,4`，即**每个 tick 都跑一轮**。
- 生产可达性：`MIN_INTERVAL_SECONDS = 300`（`services/local_maintenance.py:94`），
  `PER_ACCOUNT_DELAY_SECONDS = 3.0`（`:365`）。probe "all" 模式 100 个号 ≈ 300s+，
  200 个号 ≈ 600s → 轮次时长 > 最小间隔 → 下一 tick（60s 后）判到期 → **立刻连跑下一轮**，
  上游被连续打、出口 IP 进风控正是 `:92-94` 注释想避免的事。
  `chatgpt2api_auto_sync` 固定 600s（`services/chatgpt_maintenance.py:248`）同理。
- 修复：runner 结束后写 `job.last_run_at = time.time()`（跑完时刻），而不是 tick 时刻；
  同时给"这一轮已在进行中"加标记，避免上一轮未结束又被调度（当前串行执行，
  单靠 `last_run_at` 只能事后补偿）。

### T8-5（Important）单线程串行：一个慢/挂住的 runner 阻塞全部 13 个 job，`stop()` 也无法中断

- 位置：`core/scheduler.py:160-182`（`for job in jobs: job.runner()`，主循环线程里同步调用）。
- 复现 A（阻塞）：`slow` runner `sleep(1.5)`、`fast` runner 立即返回，两者 interval 都是 1s、tick 0.2s
  → 实测 `[('slow_start', 1.01), ('slow_end', 2.51), ('fast', 2.51)]`：
  `fast` 在 `slow` 结束后才第一次拿到执行权（延迟 1.5s）。
- 复现 B（无法中断）：runner 正在 `sleep(12)` 时调 `stop()`
  → `stop() took 5.2s; loop threads still alive: 1`。
  `stop()` 的 `join(timeout=self._loop_interval_seconds + 5)`（`:148`）只能等，不能取消；
  进程退出时这条 daemon 线程会被直接杀死在任务中途。
- 爆炸半径：13 个 job 共用一个线程。任何一个 job 里的无超时网络调用
  （维护类任务会打上游/面板）→ 其余 12 个 job 全部停摆，且**没有任何告警**
  （`[Scheduler]` 只有错误 print，没有"某 job 超时"）。`stop()` 迟缓会让 lifespan
  关停多等 65s（`60+5`），systemd 下表现为重启变慢。
- 修复方向（按代价排序）：runner 加 per-job 超时/看门狗；或至少把每次 `runner()` 丢进
  一次性线程并 `join(timeout)`，超时记一条明确日志；`stop()` 用 `thread.join(0)`
  或缩小 timeout，不要等于整个轮询周期。
- 注意：这条**不是**异常隔离问题 —— 异常隔离本身是好的（见下）。

### T8-6（Important，验证缺口）没有任何测试钉住"13 个 job 的精确集合"

- 现状：`core/scheduler.py:96-99` 有 `registered_jobs()`，但测试只做**子集**断言：
  `tests/test_local_maintenance.py:326-338` 只覆盖 `log_auto_cleanup` + 4 平台 × 2；
  `tests/test_chatgpt_auto_maintenance.py:269-276` 只断言 `chatgpt2api_auto_sync`、`sub2api_auto_sync`
  两条。**`cpa_auto_sync` / `grok2api_auto_sync` 的注册无用例**，
  也没有任何用例断言 `len(registered_jobs()) == 13` 或集合相等。
- 后果（这为什么是 Important）：`core/scheduler.py:92-93` 同名**静默覆盖**，
  加上注册点分散在 3 处、其中 1 处在模块级 `for` 循环里，删掉/改错一条
  （例如把 `:567` 的元组删一个面板、或 `PLATFORM_ACTIONS` 少一个平台）
  在 CI 里**全绿**，只有启动日志的 13 变 12 —— 而启动日志没人盯。
- 复现（证明当前是对的、但没有护栏）：spy 脚本统计调用点与集合差异 →
  `call sites total: 13 unique: 13 duplicates: {}`，`set diff (registered-logged): []`。
- 建议用例（一句话就能钉死）：断言 `registered_jobs()` 精确等于那 13 个名字的字面量集合，
  再单独断言 `PANEL_AUTO_SYNC` 的每个 key 都有一个 `*_auto_sync` 注册 ——
  两处都用 `assertEqual(sorted(...), [...])`，任何增删都必须同步改测试。

### T8-7（Minor）`*_probe_mode` / `*_refresh_scope` 为空时 job 静默不跑，但 runner 自己会把空值当默认

- 位置：`services/local_maintenance.py:350-351`（mode 不在白名单 → 0）、
  `:358-359`（scope 不在白名单 → 0）对比 `services/local_maintenance.py:498-500`
  （`run_auto_probe` 里 `... .lower() or "sample"`）与 `:536-538`（`or "failed"`）。
- 复现：只写开关、不写 mode → `aws_auto_probe (mode empty) -> 0`；补上
  `aws_probe_mode=sample` → `21600`。scope 同构（`0` → `21600`）。
- 判断：对**越界值**不跑是刻意的（`:349` 注释，合理）。但"键不存在/空串"和"填了非法值"
  被合成同一档，而 runner 侧对空值的默认是 `sample` / `failed` —— 门与执行器对同一份
  配置给出两种解释。通过 UI 保存时 `TEXT_DEFAULTS` 会补默认值
  （`MaintenancePanel.tsx:167-177`），所以正常路径不触发；但直接用
  `PUT /api/config {"data":{"aws_auto_probe_enabled":"1"}}` 就会得到一个**永久静默的 job**。
- 爆炸半径：单平台单条规则静默失效，无日志（`due()` 只 debug 级）。修复：空值走默认值，
  只有**非空且越界**才返回 0。

### T8-8（Minor）4 条面板同步 job 的"已配置"判据不一致：chatgpt2api 查密钥，其余只查地址

- 位置：`services/chatgpt_maintenance.py:314-320`（`get_chatgpt2api_auto_sync_interval_seconds`
  同时要求 `chatgpt2api_api_url` **和** `chatgpt2api_api_key`）对比
  `:274-292`（`get_panel_auto_sync_interval_seconds` 只查 `panel["url_key"]`，不查 `secret_key`）。
- 复现：只配 url、不配密钥 → `chatgpt2api url-only -> 0`，但
  `sub2api_auto_sync interval with url but no key: 600`（`PANELS_BY_KEY["sub2api"]["secret_key"]
  == "sub2api_api_key"`，确实缺）。
- 爆炸半径：sub2api/cpa/grok2api 在"地址配了、密钥忘了"时每 10 分钟跑一轮必然失败的推送，
  每轮一条 `[panel 维护] 同步异常`/`远端读取失败` 日志，且不建任务记录（
  `services/chatgpt_maintenance.py:471` 只在有动作时建）→ 用户看不到，日志被淹。修复：统一查 `secret_key`。

## hypothesis

- **H1**：T8-5 的"挂住"最坏情形确实存在 —— 如果 `execute_action` 走的 HTTP 客户端
  没有显式 timeout，一次上游无响应就会让整个调度器停摆到进程重启。本轮只验证了
  "慢 runner 阻塞其它 job"（可复现），未逐一核查每个平台 action 的 timeout 设置，
  故不定级。（下一步：`grep -rn "timeout=" platforms/*/plugin.py services/cliproxyapi_sync.py`。）
- **H2**：`_seen_jobs` 只用名字判"新任务"（`core/scheduler.py:167-170`），
  加上 `register_job` 同名覆盖会把 `_Job` 重建为 `last_run_at=0.0` ——
  运行期同名重注册（`importlib.reload`、或同一模块被两条 import 路径加载）会让
  该 job **立刻触发一次**，绕过"不瞬间触发"的约定。正常进程生命周期内不可达，未构造成功。
- **H3**：`run_log_cleanup` 的 JSON 分支（`:641-646`）在 JSON 后端可用，但
  `cleanup_json` 对 naive/aware 混用的行是 `except TypeError: continue`（
  `core/db/task_log_repository.py:124-126`）—— 那批脏行永远清不掉，属已知取舍，未定级。

## 覆盖清单（13 个 job 逐条：注册 / 间隔语义 / 异常隔离 / 开关）

共同项（对 13 条都成立，已实测）：
- **注册**：`register_job(name, interval_seconds=<callback>, runner=<callable>)`，
  13 条均为 interval 型，无遗留 `daily_at` 任务（`core/scheduler.py:25-27` 记录已撤）。
- **间隔语义**：`0`（含负数/NaN/`""`/解析失败）→ `due()` 返回 0 → **本次不跑**，
  这是 13 条统一的"关闭"通道（`core/scheduler.py:76-78`）；间隔回调抛异常也按 0 处理
  （`:73-75`，有用例 `tests/test_task_runtime.py:152`）。
  合法范围内的解析钳制 `[300, 2592000]` 只由 `parse_interval` 提供（`:132-135`），
  **对 12 个 `*_auto_probe/refresh/cleanup` 生效**，但**不作用于 4 个 `*_auto_sync`**
  ——它们的间隔是常量 600s，不经 `parse_interval`，因此不存在非法值可夹（见下表备注）。
- **异常隔离**：runner 抛异常 → `except` 捕获、`last_run_at` 不推进、循环继续
  （`core/scheduler.py:174-182`）。构造验证：`boom` 抛异常 3 次、`good` 正常跑 1 次、
  线程 `is_alive()` 为 True —— **单 job 异常不会杀死 scheduler**。
  代价：永久失败的 job 会按 tick 频率（60s）无限重试并打错误行（T8-1 即此形态）。
- **开关**：13 条全部由配置键门控；13 条在"全关"基线下 interval 全为 0
  （`baseline(all off): 0 of 13 nonzero`），逐一开启只唤醒自己（无串扰，
  `tests/test_local_maintenance.py:334` 的 lambda 晚绑定坑已被 `_k=_p` 默认参数规避）。
  全部所需键都已在 `core/config_schema.py` 登记（脚本比对：`MISSING from config_schema: []`，
  共 130 个键），且前端 `BOOLEAN_KEYS` 覆盖（`MaintenancePanel.tsx:140-148` 用
  `${prefix}_auto_probe_enabled` / `${prefix}_auto_refresh_enabled` 模板 + `log_cleanup_enabled`
  + `PANEL_CARDS.map(c => c.switchKey)`，四个 `*_auto_sync_enabled` 亦在
  `PanelConfigPanel.tsx:177-181`）。**无"渲染了但控制不了"的开关。**

| # | job | 注册点 | 间隔语义 / 钳制 | 异常隔离 | 开关（配置键 → 实测） | 结论 |
|---|---|---|---|---|---|---|
| 1 | `chatgpt_auto_probe` | `local_maintenance.py:695-700` | `parse_interval(chatgpt_probe_interval)`，夹 [300, 2592000]；实测 `21600` | 单号失败在 `_run_actions` 内吞（`:475`），整轮异常到 scheduler 兜底 | `chatgpt_auto_probe_enabled` + `chatgpt_probe_mode∈{sample,all}`；单开 `chatgpt_auto_probe` ✅ | 通过（mode 空值坑见 T8-7） |
| 2 | `chatgpt_auto_refresh` | `local_maintenance.py:701-705` | 同上，`chatgpt_refresh_interval`；实测 `21600` | 同上；`_pull_from_panels`/`_push_to_panels` 各自 try（`:530-533`,`:555-558`） | `chatgpt_auto_refresh_enabled` + `chatgpt_refresh_scope∈{failed,all,random}` ✅ | 通过（同上） |
| 3 | `grok_auto_probe` | `:695-700` | 同构，`grok_probe_interval`；实测 `21600` | 同上 | `grok_auto_probe_enabled` + `grok_probe_mode` ✅ | 通过 |
| 4 | `grok_auto_refresh` | `:701-705` | 同上；实测 `21600` | 同上 | `grok_auto_refresh_enabled` + `grok_refresh_scope` ✅ | 通过 |
| 5 | `aws_auto_probe` | `:695-700`（`PLATFORM_ACTIONS["aws"]`，2026-11-15 新增平台） | 同上；实测 `21600` | 同上 | `aws_auto_probe_enabled` + `aws_probe_mode` ✅（空 mode → 0，T8-7） | 通过 |
| 6 | `aws_auto_refresh` | `:701-705` | 同上；实测 `21600` | 同上 | `aws_auto_refresh_enabled` + `aws_refresh_scope` ✅ | 通过 |
| 7 | `kiro_auto_probe` | `:695-700` | 同上；实测 `21600` | 同上 | `kiro_auto_probe_enabled` + `kiro_probe_mode` ✅ | 通过 |
| 8 | `kiro_auto_refresh` | `:701-705` | 同上；实测 `21600` | 同上 | `kiro_auto_refresh_enabled` + `kiro_refresh_scope` ✅ | 通过 |
| 9 | `log_auto_cleanup` | `local_maintenance.py:671-675` | `parse_interval(log_cleanup_interval)`，默认 `24h`，夹 [300, 2592000]；实测 `86400` | runner 抛异常 → scheduler 兜住（线程不死），但**每次都抛** | `log_cleanup_enabled` + `log_cleanup_keep_days`（`clamp_days` 夹 [1,3650]）✅ 开关本身有效 | **Critical（T8-1/T8-2）** |
| 10 | `chatgpt2api_auto_sync` | `chatgpt_maintenance.py:551-555` | 常量 600s，**不经 `parse_interval`**（无用户可填间隔 → 无需钳制）；实测 `600` | `run_panel_auto_sync` 内 `push()` 的 try（`:446-456`）→ 返回空结果不抛 | `chatgpt2api_auto_sync_enabled` **且** url+key 均非空 ✅（`:314-320`） | 通过 |
| 11 | `sub2api_auto_sync` | `chatgpt_maintenance.py:567-572` | 常量 600s，同上；实测 `600` | 同上 | `sub2api_auto_sync_enabled` + **仅** url ✅（缺密钥仍跑，T8-8） | 通过（判据不一致 T8-8） |
| 12 | `cpa_auto_sync` | `chatgpt_maintenance.py:567-572` | 常量 600s；实测 `600` | 同上 | `cpa_auto_sync_enabled` + 仅 url ✅ | 通过（T8-8） |
| 13 | `grok2api_auto_sync` | `chatgpt_maintenance.py:567-572` | 常量 600s；实测 `600` | 同上 | `grok2api_auto_sync_enabled` + 仅 url ✅ | 通过（T8-8） |

注册之外的三个被调模块（非 job，供 cross-check）：
- `services/panel_status_sync.py`：纯函数（`classify_remote_status` / `build_status_update` /
  `sync_panel_status_batch`），被 `platforms/*/plugin.py` 与 `api/actions.py` 调用，**无后台线程**。
- `services/log_cleanup.py`：人工清理端点用（`api/tasks.py:908,926`），
  与 job 9 是**两份实现**（正确的那份 vs T8-1 那份），见 T8-2。
- `services/cliproxyapi_sync.py`：上游调用库，无调度逻辑。

### 复现清单（全部只读，不碰生产库）

1. T8-0 集合相等 + 调用点计数：临时 `DATA_DIR` 起一次性进程，用 spy 包 `register_job` 后 import 两个模块。
2. T8-1/T8-2：`run_log_cleanup()` 直接调用 + 真实 `Scheduler()` 亚秒 tick 观察重试。
3. T8-3：虚拟时钟算术（`fired in 30h with restart every 5h: 0`）+ `start()` 前后 `due()` 对比。
4. T8-4：合成 tick 4 次 `_run_due_jobs` + 1:1000 比例真线程复现。
5. T8-5：`slow`/`fast` 双 job 时序打印 + runner 在跑时 `stop()` 计时并数存活 `_loop` 线程。
6. 开关矩阵：13 条逐条开启、断言"只唤醒自己"；`config_schema` 键集合差集为空。
7. 既有测试：`.venv/bin/python -m pytest tests/test_local_maintenance.py tests/test_task_runtime.py -q` → `74 passed, 8 subtests passed`。