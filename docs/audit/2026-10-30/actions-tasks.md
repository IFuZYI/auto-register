# actions-tasks 审计

[MODE: SANDBOX]

- 仓库：`/data/workspace/Register`，分支 `main`
- 基线：审计执行于 `4680c8d`；**复核于 `9630980`**（该提交把 F-00 的修复、回归测试与四份审计报告一并入库）
- 解释器：`.venv/bin/python`（3.12）
- 生产库：`data/account_manager.db`（只读；`mode=ro` 打开，本报告未写入任何一行）
- 审计方式：静态通读 + 只读 SQL 抽样 + 4 个可复现探针（能力对差 / 跳过语义 / 取消开销 / 临时库复活），本审计未修改任何源码
- **基线说明**：正文行号以 `4680c8d` 为准。`9630980` 上唯一的行号漂移是 `api/actions.py` 自 `:262` 起 +4 行（F-00 的注释 + `str()`），逐条核对见第 10 节。**F-00 在 `9630980` 上已修复**，本条保留为缺陷记录与回归依据。

范围（10 个文件，共 4829 行）：

| 文件 | 行数 |
|---|---|
| `api/tasks.py` | 833 |
| `api/actions.py` | 465 |
| `services/task_service.py` | 201 |
| `services/task_runners.py` | 1065 |
| `services/task_store_io.py` | 439 |
| `services/action_execution.py` | 292 |
| `core/capabilities.py` | 106 |
| `core/task_runtime.py` | 385 |
| `core/scheduler.py` | 210 |
| `services/chatgpt_maintenance.py` | 833 |

---

## 0. 结论摘要

| # | 维度 | 判定 | 最严重问题 |
|---|---|---|---|
| 0 | 动作分发（批量） | **已修复**（`9630980`；修复前为必现 500） | `api/actions.py:263`（旧）对 UUID 主键做 `int()` → 现 `:267` 已改 `str()` |
| 1 | 17 动作 → 6 能力 | **映射完整**（0 落空、0 孤儿），但「17」是 17 个 (平台,动作) 声明、唯一 id 只有 16 个；Grok 的 `SYNC_STATUS` 能力有映射却**不落状态** | F-05 / F-06 |
| 2 | 停止 / 跳过 / 线程池取消 | **部分生效**：停止是协作式的，`chatgpt2api_sync` 任务完全不可停；一次「跳过当前」会跳过**全部在飞账号**；取消循环 O(n²) | F-07 / F-08 / F-09 |
| 3 | 重试与计数 | **不会重复计数，但会漏计且两套账**：`registered=success+skipped+errors` 掩盖未处理；运行中 `skipped` 恒 0；生产库里 `skipped > total` | F-11 / F-12 / F-13 |
| 4 | 快照持久化 / 重启恢复 | **能恢复，但有三处硬伤**：收尾段无 try → 任务永久 running；DELETE 后 GET 会**复活**；孤儿识别依赖内存（多进程会误杀） | F-03 / F-04 / F-14 |
| 5 | 批量并发 / 限速 / 单次上限 | **上限有，但不一致**：register 10000/并发 200 有校验；其余任务 `concurrency` **无上界**；限速闸门持锁睡眠，并发被序列化；同步批量端点无任何限速 | F-15 / F-16 / F-17 |
| 6 | 长任务资源占用 | **明确风险**：长任务直接占用 anyio 的 40 线程池；每个日志行全量重写快照；SSE 在事件循环里同步写库；列表每 2.5s 0.4s/404KB | F-01 / F-02 / F-19 |

一句话：**动作层（映射）是干净的，批量分发路径上唯一的必现 500（F-00）已在 `9630980` 闭环；任务层（控制、计数、持久化）的骨架正确而收尾路径不设防，几处「异常 → 永久 running」和「一次点击跳过一批」会在真实批量场景里放大。**

---

## 1. 17 个动作 id → 6 个能力

### 1.1 实测映射（脚本枚举两个插件的 `get_platform_actions()` 后与登记表对差）

| 平台 | 动作 id | 能力 | 插件实现 |
|---|---|---|---|
| chatgpt | `probe_local_status` | PROBE | `platforms/chatgpt/plugin.py:358` |
| chatgpt | `check_plus_trial` | PROBE | `:378` |
| chatgpt | `sync_cliproxyapi_status` | SYNC_STATUS | `:397` |
| chatgpt | `sync_sub2api_status` | SYNC_STATUS | `:645`（面板分支） |
| chatgpt | `sync_chatgpt2api_status` | SYNC_STATUS | `:645` |
| chatgpt | `refresh_token` | REFRESH | `:420` |
| chatgpt | `backfill_refresh_token` | BACKFILL | `:551` |
| chatgpt | `bind_2fa` | BIND_2FA | `:573` |
| chatgpt | `upload_cpa` | UPLOAD | `:610` |
| chatgpt | `upload_sub2api` | UPLOAD | `:623` |
| chatgpt | `upload_chatgpt2api` | UPLOAD | `:633` |
| grok | `probe` | PROBE | `platforms/grok/plugin.py:443` |
| grok | `probe_refresh` | PROBE | `:486` |
| grok | `refresh_token` | REFRESH | `:560` |
| grok | `refresh_oauth`（已弃用，转发 `refresh_token`） | REFRESH | `:623` |
| grok | `sync_grok2api_status` | SYNC_STATUS | `:631` |
| grok | `upload_grok2api` | UPLOAD | `:650` |

对差结果（实测）：

```
chatgpt 11  grok 6   声明对 17   唯一 id 16   登记表 16
orphan in registry: set()         declared missing: set()
```

**结论**：没有动作落不到能力，也没有登记了却没声明的孤儿；`tests/test_capability_registry.py` 双向钉住了这一点（实跑 7 passed）。

### 1.2 发现

**F-00（P0，已在 `9630980` 修复；保留为缺陷记录）批量动作分发对 UUID 主键做 `int()` → `POST /actions/chatgpt/sync_cliproxyapi_status/batch` 必然 500**

- 位置：`api/actions.py:263`（`4680c8d`）→ 修复后位于 `:267`（`9630980`）

```python
sync_result = sync_results.get(int(acc_model.id or 0), {})
```

- 证据：
  - `AccountModel.id` 是 UUID 字符串（`core/db/models_account.py:45-51`，`default_factory=lambda: str(uuid.uuid4())`）；生产库实测 `3a436b47-86e5-4023-842f-6ac38a6d98b8`（`select id, platform, email from accounts limit 3`）。
  - 复现（临时替换批量同步实现，不触网）：

```
RAISED: ValueError invalid literal for int() with base 10:
        '3a436b47-86e5-4023-842f-6ac38a6d98b8'
```

  - 即便 id 是纯数字，键类型也对不上：`sync_chatgpt_cliproxyapi_status_batch` 用 `getattr(account, "id")` 原样做键（`services/cliproxyapi_sync.py:682-693`），`int("123") != "123"` → 永远取到 `{}` → `is_sync_ok({})` 恒 False（`services/cliproxyapi_sync.py:324-335`），整批被记成失败。
  - 该端点有真实调用方：对比页「同步远端状态」（`frontend/src/components/settings/PanelComparisonPanel.tsx:549`，按平台分发 `POST /actions/{plat}/{actionId}/batch`）。
- 影响：CPA 面板的「同步远端状态」对任何账号都直接 500（`ValueError` 未被 `_execute_batch_cliproxy_sync` 捕获，冒泡到 FastAPI）。这是本次审计唯一的**必现 P0**。
- **复核状态（`9630980`）**：已修复。该提交把 `int(acc_model.id or 0)` 改为 `str(acc_model.id or "")`（现 `api/actions.py:267`），并新增 `tests/test_batch_cliproxy_sync_uuid.py`（3 条：UUID 不抛、缺结果算失败、两账号结果不串）。本审计实跑该文件 **3 passed**，连同能力/任务层回归共 **87 passed**（第 10 节）。`_execute_batch_panel_status` 的 `str(...)` 与它现在同形，兄弟不一致这条信号消失。
- 建议（剩余项）：`_execute_batch_cliproxy_sync` 内对 `sync_results` 的键做一次 `{str(k): v}` 归一，防止上游再返回非字符串键；`services/cliproxyapi_sync.py` 的 `dict[int, ...]` 注解仍是同一历史假设的另一半，只改 `actions.py` 会把错误留在类型契约里。

**F-05（P2）「17 个 id」实际是 16 个 id —— 映射键是 `action_id` 而非 `(platform, action_id)`，平台级差异无法表达**

- 位置：`core/capabilities.py:5`（docstring「17 个（ChatGPT 11 + Grok 6）」）、`core/capabilities.py:57-76`（16 个键）、`api/actions.py:290`（「不用再硬编码 17 个动作 id」）
- 证据：`refresh_token` 同时被 ChatGPT（`platforms/chatgpt/plugin.py:183`）与 Grok（`platforms/grok/plugin.py:412`）声明，字典只有一份 `"refresh_token": Capability.REFRESH`（`core/capabilities.py:64`）。两平台当前恰好同能力，所以今天不出错；但 `capability_of()` 只收 id（`core/capabilities.py:79-81`），一旦某个平台需要不同策略（例如 Grok 的 `refresh_token` 要落「SSO 被拒」，ChatGPT 要落「RT 失效」），这张表表达不了。
- 影响：新增平台或让两平台对同名动作分道时，只能再退回 `if platform == ...` 字符串判断 —— 正是能力层要消灭的东西。
- 建议：要么把键改成 `(platform, action_id)` 并保留 `capability_of(action_id)` 作为「无平台上下文」的兜底，要么在 docstring 明确写「16 个 id / 17 个平台声明」，并在 `tests/test_capability_registry.py` 加一条 `len(ACTION_CAPABILITIES) == len(declared_unique)` 的断言，避免「17」继续漂移。

**F-06（P1）Grok 的 SYNC_STATUS 有映射但不落状态；`_apply_grok_status` 里还有一条死分支**

- 位置：`services/action_execution.py:200-206`（`sync_cliproxyapi_status` 分支）、`services/action_execution.py:173-215`（整张 id 分派表）、`platforms/grok/plugin.py:408-437`（Grok 已不声明 `sync_cliproxyapi_status`）
- 证据：
  - Grok 声明的同步动作是 `sync_grok2api_status`，`_apply_grok_status` 里**没有**这个分支 → 走完 `status_reason = ""` → 不写 `status`；
  - 它有的 `sync_cliproxyapi_status` 分支对 Grok 永远不可达（该 id 只由 ChatGPT 声明），是 CPA 下线（2026-10-27）后的残留；
  - 与之配套的 `apply_grok_status_policy(remote_state=...)`（`services/grok_account_state.py:82`）因此**从未被喂过值**；
  - 同一函数对 `refresh_oauth` 也没有分支：老脚本调 `refresh_oauth` 时插件内部转发到 `refresh_token`（`platforms/grok/plugin.py:623-627`），凭证刷新成功，但 applier 按**原始 action_id** 分派 → 不落状态。
- 影响：Grok 面板同步「成功」只写 `sync_statuses.grok2api`，账号 `status` 不变；远端已失效的号仍显示正常，用户以为同步起作用了。
- 建议：把 `_apply_grok_status` 改成与 `_apply_chatgpt_status` 一致的**按能力分派**（`capability_of(action_id) is Capability.SYNC_STATUS` → 读 `data.sync` / `data.remote_state`），删掉 `sync_cliproxyapi_status` 死分支；`refresh_oauth` 要么在 applier 里归一到 `refresh_token`，要么在插件返回值里带 `normalized_action_id`。

**F-06b（P3，一致性）`UPLOAD_ACTIONS` 与 `UPLOAD_SYNC_WRITERS` 差一个 `upload_grok2api`**

- 位置：`services/action_execution.py:49-54`（`UPLOAD_ACTIONS` 4 个）vs `services/action_execution.py:287-292`（`UPLOAD_SYNC_WRITERS` 只有 3 个 ChatGPT 面板，查表点 `:73`）
- 证据：`upload_grok2api` 的上传账由插件自己写进 `account_extra_patch.sync_statuses.grok2api`（`platforms/grok/plugin.py:1001-1014`），再经 `_apply_extra_patch` 合并，所以**功能不缺**；两表不对称本身是可接受的（一张是「是不是上传动作」，一张是「谁有本地写入器」）。**复核更正（`9630980`）**：原报告称 `record_upload_sync` 的 docstring 写着「grok 的 upload_sub2api 也收进来」——现 `:69-71` 已改成「`upload_cpa` 对 ChatGPT 与 Grok 都有效；sub2api / chatgpt2api 当前只在对应平台声明」，与代码一致，**这条过期注释不再成立**。
- 建议：仅剩一致性建议 —— 若要让两张表自洽，可在 `UPLOAD_SYNC_WRITERS` 里给 `upload_grok2api` 注册 `record_grok2api_sync_result`，与插件路径二选一；否则在 `UPLOAD_SYNC_WRITERS` 上补一行注释说明「grok 走插件自写」，免得下一个人以为是漏登记。

---

## 2. 任务控制（停止 / 跳过）与线程池取消

### 2.1 生效矩阵（逐路径核对）

| 任务类型 | 停止 | 跳过当前 | 说明 |
|---|---|---|---|
| `register` | ✅ 协作式 | ⚠️ 一次跳过全部在飞 | `services/task_runners.py:209` 先 `start_attempt()` 再排队 |
| `batch-action` / `probe_status` / `refresh_token` | ✅ | ⚠️ 同上 | `services/task_runners.py:551` |
| `backfill_rt` / `bind_2fa` | ✅ 最彻底 | ⚠️ 同上 | 控制器一路传到等码循环（`services/chatgpt_rt_backfill.py:79-103`） |
| `auto_refresh` | ✅ | ⚠️ 跳过的是**下一个**账号 | `services/chatgpt_maintenance.py:498-500`，`checkpoint()` 不带 attempt_id |
| `chatgpt2api_sync` | ❌ **完全无效** | ❌ | `services/chatgpt_maintenance.py:750` 拿到 control 就丢了 |
| 卡死任务（runner 已死） | ❌ 返回 200，无人消费 | ❌ | 见 F-03 |

### 2.2 发现

**F-07（P1）一次「跳过当前账号」会跳过全部在飞账号（并发 > 1 时）**

- 位置：`core/task_runtime.py:89-95`（`request_skip_current` 把 `_active_attempt_ids` **全量**并入 skip 集合）、`services/task_runners.py:551-552`（`start_attempt()` 在 `_wait_turn()` **之前**）、`services/task_runners.py:209-210`（注册任务同样）
- 证据（纯内存复现，5 个并发 attempt）：

```
active: 5   skip_targets: 5
skipped attempts: [(1, 'SkipCurrentAttemptRequested'), (2, ...), (3, ...), (4, ...), (5, ...)]
```

- 影响：`concurrency=5` 时点一次「跳过」→ 5 个账号被登记为 `skipped`，用户看到「跳过 5 个」但只想跳 1 个；`concurrency=50 + delay_seconds=5` 时一次点击废掉 50 个号（这些号根本没开始跑）。
- 建议：`request_skip_current` 只标记**最近一个** attempt（`max(_active_attempt_ids)`），或让前端把 `attempt_id` 带上来；排队中的 worker 不应在 `_wait_turn` 之前就 `start_attempt()`（改为拿到起跑权后再登记）。

**F-08（P1）停止后的取消循环是 O(n²)，大任务上「停一下」要烧几十秒到几分钟 CPU**

- 位置：`services/task_runners.py:368-373`、`services/task_runners.py:642-647`

```python
for f in as_completed(futures):
    ...
    if stopped or control.is_stop_requested():
        stopped = True
        for pending in futures:          # ← 每次完成都重扫全部 future
            if pending is not f:
                pending.cancel()
```

- 证据（实测 `Future.cancel()` 的真实开销，含内部锁）：

```
1000 futures -> 1.751 s
3000 futures -> 38.035 s
```

  按平方外推，`MAX_REGISTER_COUNT = 10000`（`api/tasks.py:60`）在停止瞬间约需 **数分钟**纯 CPU 扫描。
- 影响：点停止后任务长时间停在 `running`（`finish()` 还没执行），日志不再更新，用户以为卡死；同时这段时间占着一个 anyio 线程（见 F-01）。
- 建议：把 cancel 循环移到 `as_completed` **外面**，用 `stopped_once` 标志只跑一次；Python 3.9+ 直接 `pool.shutdown(wait=False, cancel_futures=True)`；循环里 `break` 掉 `as_completed`（配合 `pool.shutdown` 的 `wait=True` 在退出 `with` 时收口）。

**F-09（P2）`chatgpt2api_sync` 任务的停止按钮是摆设**

- 位置：`services/chatgpt_maintenance.py:750`（`task_id, _control = _open_auto_task(...)`，`_control` 之后再未使用）；对比 `run_auto_refresh_pass` 的 `services/chatgpt_maintenance.py:498-500`（有 `control.checkpoint()`）
- 证据：全文件 `control` 只出现在 475/477/498/500/638/656/750 行，`run_chatgpt2api_auto_sync` 内零次 `checkpoint`。
- 影响：`POST /tasks/{id}/stop` 返回 200（`api/tasks.py:626-...` 只设标志），任务继续推完，最后 `_close_auto_task(status="done")`（`:787`）。
- 建议：在推送循环里接 `control.checkpoint()`（`services/panel_push.py` 的逐账号回调），或在 `_open_auto_task` 后显式 `control.checkpoint()`；若确实不支持停止，端点应返回 409 而不是假装成功。

**F-10（P2）线程池「取消」只对未启动的 future 有效；运行中的线程靠协作，且池线程非 daemon 会阻塞进程退出**

- 位置：`services/task_runners.py:341` / `:613`（`ThreadPoolExecutor`）、`:343` / `:615`（`with` 块等待全部完成）
- 证据：`concurrent.futures.ThreadPoolExecutor` 的 worker 是非 daemon 线程，解释器退出时 `_python_exit` 会 join 它们；`with` 退出时 `shutdown(wait=True)`。因此：
  - 停止一个 200 并发、每号要等 120s 验证码的注册任务，`finish()` 最迟要等**当前这批**全部返回；
  - `uvicorn` 重启会卡在等待在跑账号上（无超时）。
- 影响：重启/部署窗口不可控；「停止」在 UI 上表现为长时间无变化。
- 建议：给单账号执行加 deadline（`future.result(timeout=...)` 不够，需要协作式中断 + 网络层超时）；进程退出时用 `pool.shutdown(wait=False)` + 显式 `os._exit` 兜底，或把池线程改为自管理的 daemon 线程。

---

## 3. 失败重试与计数（success / skipped / failed）

### 3.1 计数口径（实测核对）

| 位置 | 写法 | 结论 |
|---|---|---|
| `services/task_runners.py:359-367`、`:633-640` | `registered = success + skipped + len(errors)` | 把「已处理」定义为三桶之和，**未处理的账号不进任何桶** |
| `core/task_runtime.py:311-326` | `update_counters(success=, registered=)` | **没有 `skipped` 参数** → 运行中 `skipped` 恒为 0 |
| `services/chatgpt_maintenance.py:611-614` | `registered=ok+failed`，`skipped=exec_skipped`，`total=len(to_run)` | 停止时 `total > registered+skipped` |
| `services/chatgpt_maintenance.py:750-761` | `total=pushed+failed`，`skipped=skipped`（全量面板跳过） | **`skipped` 可以远大于 `total`** |

**是否重复计数**：逐路径核对后**没有发现同一账号被计两次的路径** —— `_do_one` 每次调用只返回一个 `AttemptResult`（`services/task_runners.py:547-608`），主循环按 `outcome` 只进一个桶（`:356-362`）；注册任务的重试循环 `break` 在首个非 FAILED 结果（`:170-171`），`save_task_log` 也只写一条。**问题在漏计和口径不一致，不在重复。**

### 3.2 发现

**F-11（P2）生产库里 `skipped > total`，计数自相矛盾**

- 位置：`services/chatgpt_maintenance.py:750-761`
- 证据（只读 SQL：`select count(*) from task_runs where skipped > total` → **614 / 837 行命中（73%）**，不是个别脏数据）：

```
('chatgpt2api_sync_1793015081417', total=278, success=278, registered=278, skipped=338, 'done')
('chatgpt2api_sync_1793014447182', total=10,  success=10,  registered=10,  skipped=606, 'done')
meta_json: {"pushed": 278, "failed": 0, "deleted": 0, "skipped": 338}
```

  `total` 只算 `pushed+failed`，`skipped` 却是整轮面板对比的跳过数（338/606 个「已是最新」的账号）。任务详情页会显示「共 10 个，跳过 606 个」。
- 建议：`total = pushed + failed + skipped`（或把 `skipped` 只放 `meta`，计数器里写 0），并在 `_close_auto_task` 里加一条 `assert success+skipped+len(errors) <= total` 的日志告警。

**F-12（P2）运行中 `skipped` 永远是 0，`registered` 却已含跳过 —— 进度条与计数不同源**

- 位置：`core/task_runtime.py:311-326`（无 `skipped` 形参）、`services/task_runners.py:359-364`（调用处只传 `success`/`registered`）
- 证据：`update_counters` 签名只有 `success` / `registered`；`skipped` 只在 `finish()`（`core/task_runtime.py:322-344`）一次性写入。
- 影响：任务跑到一半时 `registered=10, success=8, skipped=0`，用户看不出「跳过 2 个」；SSE 每秒推的也是这份快照（`api/tasks.py:753-760`）。
- 建议：`update_counters` 增加 `skipped` 形参并透传。

**F-13（P2）两套账：`task_logs` 与 `task_runs` 覆盖范围不同，跳过/异常路径不写历史**

- 位置：`services/task_runners.py:557-558`（批量任务里「账号不存在」只 `log` 不写 `save_task_log`）、`:605-606`（通用 except 只返回 failed，不写历史）、`:1043-1045`（绑 2FA 的 `already_bound` 跳过同样不写历史）、`:191-198`（注册任务重试耗尽才写一条 `failed`）
- 证据：`_do_one` 的三条 `return`（skipped / stopped / failed-by-exception）里只有 `_do_one_round` 的成功/失败/跳过分支会 `_api.save_task_log`（`services/task_runners.py:290`、`:300-305`、`:193-198`）；`_run_account_batch_task` 的 `_do_one` 全程不写 `save_task_log`，历史页因此比任务计数少。
- 影响：「任务历史」页与「任务运行」页对同一批账号给出不同的成功/失败数；排查时按历史找不到失败账号。
- 建议：把 `save_task_log` 收敛到 `_do_one` 的 `finally`（按 `AttemptResult.outcome` 写一条），删掉 `_do_one_round` 里的分散写入。

**F-13b（P2）`save_task_log` 是「一个账号一个线程」的 fire-and-forget，退出即丢**

- 位置：`services/task_service.py:99-107`
- 证据：`threading.Thread(target=_write, daemon=True).start()`，无 join、无异常捕获、无队列。
- 影响：1000 账号任务 = 1000 个短命线程；进程退出/重启时 daemon 线程被直接杀掉 → 历史行静默丢失；SQLite 写锁竞争时异常被线程吞掉。
- 建议：改为单写线程 + `queue.Queue`（或在 `_do_one` 里同步写，反正已经在工作线程里）。

---

## 4. 任务快照持久化与进程重启恢复（孤儿任务）

### 4.1 现状

- 写点：`_create_task_record`（`api/tasks.py:262-271`）→ `persist_task_snapshot`；每条日志（`services/task_service.py:89-96`）；每次计数（`services/task_runners.py:362-367`）；结束（`:400-411` / `:669-680`）。
- 重启恢复：`_finalize_orphan_tasks`（`services/task_store_io.py:350-381`）把库里 `pending/running` 且**内存 store 里没有**的行改成 `stopped` + 追加一行系统日志。
- 触发时机：**懒触发** —— `GET /tasks`（`api/tasks.py:809`）、`GET /tasks/{id}`（`:785`）、SSE（`:741`）、stop/skip（`:616/627`）、delete（`:823`）、`/runs/cleanup`（`:731`）。
- 没有断点续跑：重启后一律标 stopped（设计如此，`services/task_store_io.py:387-389` 有说明）。

### 4.2 发现

**F-03（P1，最严重）收尾段没有 try：`finish()` 之前的任何异常都会让任务永久 `running`**

- 位置：
  - `services/task_runners.py:55-57`（`control_for` / `mark_running` / `persist_task_snapshot` 在 `try` **之前**）
  - `services/task_runners.py:400-411`（`except` 之后的收尾段：`_api.log(summary)` → `finish()` → `persist` → `cleanup()`，全程无保护）
  - `services/task_runners.py:465-467`、`:668-680`（批量任务同构）
  - `services/task_store_io.py:181-188`（`persist_task_snapshot` 只兜 `snapshot()`，`_upsert_task_run` 的异常直接上抛）
  - `services/task_service.py:89-96`（`log()` 里 `persist_task_snapshot(task_id)` 无 try）
- 证据：`log()` → `persist_task_snapshot()` → `_upsert_task_run()` → `Session(current_engine())` 写 SQLite；并发写时 `database is locked` / `TimeoutError` 会直接抛出。`services/task_runners.py:374-376` 的注释已经写明「收尾路径自己绝不能再抛，否则 finish() 会被整个跳过」，但那只保护了 `except` 块内部，**没保护 except 之后的正常收尾**。
- 旁证：`api/tasks.py:51-58` 的常量注释记录了同一个事故形态 ——「内存涨到 863MB、CPU 37.6%，`DELETE` 因「运行中」被 409 拒绝 —— 用户没有任何 UI 手段清掉它」。`MAX_REGISTER_COUNT` 只是把触发概率压低，**没有修机制**。
- 影响链：
  1. 任务在内存里永远 `pending/running`；
  2. `_finalize_orphan_tasks` 只处理「内存里没有」的行（`services/task_store_io.py:367-368`），**不会**救它；
  3. `DELETE /tasks/{id}` 对 `pending/running` 返回 409（`api/tasks.py:827`）→ 删不掉；
  4. `stop` 只设标志，没有 runner 消费 → 永远停不下来；
  5. `has_active()`（`core/task_runtime.py:231-244`）一直为真 → `services/data_bundle.py:573` 的导入守卫**永久拒绝**导入数据包。
- 建议：把「`mark_running` → 主体 → `finish`」整体包进 `try/finally`，`finally` 里用裸 `try` 兜底调用 `finish(status="failed", error="收尾异常")`；`persist_task_snapshot` 内部 catch 并 `logger.warning`，绝不让持久化异常冒泡进任务线程。

**F-04（P1）`DELETE` 之后 `GET` 会把任务「复活」**

- 位置：`api/tasks.py:822-831`（只删 DB 行，**没有** `_task_store.remove(task_id)`）、`services/task_store_io.py:429-432`（`_get_task_snapshot` 在内存记录存在时会 `persist_task_snapshot` 回写）
- 证据（临时库复现，未触碰生产库）：

```
before delete: row exists = True
after  delete: row exists = False
GET snapshot -> done
after  GET:    row exists = True   <-- 复活
```

- 影响：用户删掉一个已结束任务，刷新列表又出现；`stream_logs`（`api/tasks.py:748-752`）每 0.5s 还会持续回写。
- 建议：`delete_task` 里 `_task_store.remove(task_id)`（`RegisterTaskStore` 目前没有该方法，需补一个只删已结束记录的 `remove`）；同时让读路径不写库（`_get_task_snapshot` 去掉 persist）。

**F-14（P2）孤儿识别只看本进程内存 —— 多进程部署会误杀别的 worker 的任务**

- 位置：`services/task_store_io.py:366-369`（`if _store().exists(row.id): continue`）
- 证据：`_store()` 是进程内单例（`services/task_service.py:44-47`）。当前 `main.py:619` 是单进程 `uvicorn.run(...)`，所以**今天不触发**；一旦按常规做法加 `--workers N` 或 gunicorn，worker B 的每次 `GET /tasks` 都会把 worker A 正在跑的任务标成 `stopped`。
- 建议：给 `task_runs` 加 `owner_pid` / `heartbeat_at` 列，只收尾「心跳超时」的行；或在文档里把「必须单进程」写成硬约束。

**F-15（P2）快照写入无重试、无节流：每条日志一次全量 upsert**

- 位置：`services/task_service.py:89-96` → `services/task_store_io.py:124-188`
- 证据：`_upsert_task_run` 每次 `SELECT` 整行 + 重写 `logs_json` / `errors_json` / `meta_json` / `control_json` 全部字段。生产库实测：单任务 `logs_json` 最大 **41,753 字节**，`errors_json` 最大 **37,132 字节**；837 行合计 **12,084,870 字节**（约 12 MB）。
- 影响：一个 278 账号的 `chatgpt2api_sync` 任务 = 278 次全量重写（约 5–6 MB 写入）；与 F-01 的 SSE 写叠加后 SQLite 写锁竞争明显。
- 建议：日志改为 append-only 表（`task_log_lines`），或至少用 dirty 标记 + 1s 合并写；`errors` 只存前 N 条 + 总数。

---

## 5. 批量动作的并发与限速、单次上限

### 5.1 上限现状

| 入口 | 单次上限 | 并发上限 | 限速 |
|---|---|---|---|
| `POST /tasks/register` | `count ≤ 10000`（`api/tasks.py:73`） | `concurrency ≤ 200`（`:74`） | `register_delay_seconds`，全局闸门 |
| `POST /tasks/batch-action` | 选号层 1000（`services/chatgpt_account_selection.py:22,115-116`） | **无**（`api/tasks.py:190`） | `delay_seconds=1` 默认，全局闸门 |
| `POST /tasks/probe-status` | 1000 | **无**（`api/tasks.py:210`） | 同上 |
| `POST /tasks/refresh-token` | 1000 | **无**（`api/tasks.py:160`） | 同上 |
| `POST /tasks/backfill-rt` | 1000 | **无**（`api/tasks.py:118`） | 同上 |
| `POST /tasks/bind-2fa` | 1000 | **无**（`api/tasks.py:140`） | 同上 |
| `POST /actions/{platform}/{action}/batch` | 1000（`api/actions.py:168`） | 串行（`api/actions.py:397`） | **无** |

### 5.2 发现

**F-16（P2）除注册任务外，所有任务的 `concurrency` 没有上下界**

- 位置：`api/tasks.py:118, 140, 160, 190, 210`（均为裸 `concurrency: int = 1`）；`services/task_runners.py:613`（`max(1, min(int(concurrency or 1), max(total, 1)))`）
- 证据：`RegisterTaskRequest.concurrency` 有 `ge=1, le=200`（`api/tasks.py:74`），其余 5 个请求模型没有 `Field` 约束。传 `concurrency=1000` → `ThreadPoolExecutor(max_workers=1000)`。
- 影响：
  - 1000 个线程同时跑，每个 `_do_one` 会开 2–3 个 `platform_session`（`services/task_runners.py:517`、`:709` 等），而连接池是 **5 + 10 = 15**（实测 `QueuePool size=5 overflow=10 timeout=30`）→ 第 16 个线程等 30s 后抛 `TimeoutError`，被 `_do_one` 的 `except Exception` 记成**账号失败**（`services/task_runners.py:605-606`），污染成功率；
  - 每个注册线程还可能持有浏览器进程，内存不可控。
- 建议：统一 `Field(default=1, ge=1, le=8)`（上限取连接池的 1/2），并在 runner 里 `min(concurrency, pool_size)` 兜底。

**F-17（P2）限速闸门「持锁睡眠」：并发被序列化成 1/delay，排队线程纯占资源**

- 位置：`services/task_runners.py:533-545`（`_wait_turn`：`with start_gate:` 内部 `time.sleep`）、`services/task_runners.py:213-226`（注册任务同款：`with start_gate_lock:` 内 `_sleep_with_control`）
- 证据：

```python
with start_gate:
    control.checkpoint(...)
    remaining = max(0.0, next_start_time - time.time())
    while remaining > 0:
        time.sleep(chunk)          # ← 持锁睡
    next_start_time = time.time() + delay_seconds
```

- 影响：
  - `concurrency=5, delay=5` 时，起跑速率恒为 1 个/5 秒，5 个线程里 4 个在等锁 —— 并发只体现在「已起跑的号同时在跑」，对吞吐没有线性帮助；
  - 排队线程在 `with` 上阻塞期间**无法** `checkpoint`（好在拿到锁的第一句就是 checkpoint，停止能快速排空）；
  - 1000 个账号 + `delay=5` 的任务至少要 83 分钟才能起跑完，期间线程全部驻留。
- 建议：锁内只做「算下一个时间片 + 推进 `next_start_time`」，`sleep` 放到锁外（用 `Condition.wait(timeout)` 更佳）。

**F-18（P2）同步批量端点：串行、无超时、整个请求期间占着数据库连接**

- 位置：`api/actions.py:397-419`（`for acc_model in accounts:` 逐号 `_execute_platform_action`）、`api/actions.py:129-168`（上限 1000）
- 证据：端点 `def` 是同步函数，`session: Session = Depends(platform_session_from_path)` 在整个请求期间不释放；1000 个账号 × 单号 probe（数秒）≈ 小时级请求。
- 影响：一个用户点「全选 → 批量测活」就占掉 1/15 的连接数小时；十几个并发即耗尽连接池，全站 30s 超时。
- 建议：该端点只保留「小批量（≤50）」或直接改为 302 到 `/tasks/batch-action`；至少加 `asyncio.wait_for` 级别的整体 deadline 与分批提交。

---

## 6. 长任务的资源占用（线程 / Session / 内存）

**F-01（P1）长任务直接占用 anyio 的 40 线程池 —— 40 个任务就能让全站同步端点停摆**

- 位置：`api/tasks.py:289`（register）、`:354`（backfill）、`:441`（batch-action）、`:496`（probe）、`:551`（bind-2fa）、`:601`（refresh-token）；`api/accounts.py:720`（`scheduler.check_accounts_valid`）
- 证据：Starlette 的 `BackgroundTasks` 对同步函数走 `run_in_threadpool`，而 anyio 默认线程限流器是 **40**（实测 `current_default_thread_limiter().total_tokens == 40`，starlette 1.7.0）。任务函数本身是「跑完整个任务才返回」的同步函数，注册任务可持续数小时。
- 影响：40 个并发长任务（或长任务 + 慢请求）之后，**所有** `def` 端点（账号列表、配置、代理…）全部排队；`/tasks` 轮询也会被拖死，形成「越忙越看不到进度」。
- 建议：任务统一走独立执行器（`threading.Thread(daemon=True)` 已有分支，`api/tasks.py:284-288` 在 `background_tasks is None` 时就是这么做的），`BackgroundTasks` 只留给秒级作业。

**F-02（P1）SSE 在事件循环里同步写库 + 每 0.5s 全量复制日志**

- 位置：`api/tasks.py:739-770`（`async def stream_logs` 内直接调用 `_persist_task_snapshot(task_id)` 与 `_task_store.snapshot(task_id)`）
- 证据：`stream_logs` 是 `async def`，生成器里的 `_persist_task_snapshot` 是**阻塞**的 SQLite 全量 upsert（`services/task_store_io.py:124-188`），每 0.5s 一次/每个连接；`snapshot()` 还会 `list(record.logs)` 复制整份日志（`core/task_runtime.py:346-352`）。
- 影响：3 个浏览器标签页 = 6 次/秒的阻塞写 + 全量序列化，全部发生在 **asyncio 事件循环线程**上 → 所有 HTTP 请求（包括健康检查）一起卡顿；任务日志越长越慢（41KB 日志实测）。
- 建议：SSE 只读不写（持久化交给任务线程的节流写），`snapshot` 改为增量 `since` 读；必须写时用 `await run_in_threadpool(...)`。

**F-19（P2）`GET /tasks` 每 2.5s 一次全表扫描 + 404 KB 序列化**

- 位置：`api/tasks.py:790-820`、`services/task_store_io.py:240-330`
- 证据（837 行实测）：

```
rows: 837   time: 0.131 s（warm）/ 0.402 s（cold）   payload: 404.3 KB
include_logs=true: 0.897 s, 15,676.8 KB
```

  另外 `_finished_status_filter`（`status IS NULL OR status NOT IN (...)`）走的是 **SCAN**，`ix_task_runs_status` 用不上。
- 影响：每个打开「任务运行」页的客户端每 2.5s 消耗一个线程 0.13–0.4s；`?include_logs=1` 单次 15.7 MB，浏览器与 SSH 隧道都会被拖垮。
- 建议：列表分页（`limit/offset`）+ `since` 增量；`include_logs` 直接下线或强制 `limit ≤ 50`；把 `NOT IN` 改成 `status IN ('done','failed','stopped')` 以命中索引。

**F-20（P2）每个任务一个线程池 + 每账号 2–3 个 Session + 每个成功注册一个自动上传线程**

- 位置：`services/task_runners.py:341/613`（池）、`:517/709/778/787/950/959/1029`（每账号短 Session）、`services/task_service.py:169-184`（`auto_upload_integrations` 每号起一个线程）
- 证据：`auto_upload_integrations` 用 `threading.Thread(daemon=True)` 且无上限、无 join。
- 影响：1000 个成功注册 → 1000 个并发上传线程（每个都在打外部面板 API），叠加 F-16 的 1000 线程池，峰值线程数可达 2000+。
- 建议：自动上传走一个固定大小的 worker 队列（`ThreadPoolExecutor(max_workers=4)` 全局单例）。

**F-21（P3）`errors` 无上限，随任务规模线性膨胀并进入内存快照**

- 位置：`services/task_runners.py:361`、`:635`（`errors.append(result.message)`）、`core/task_runtime.py:322-344`（整份 `list(errors)` 拷贝进 record）
- 证据：生产库 `errors_json` 最大 37,132 字节；`_list_persisted_tasks` 的注释也记录了「errors 占 4.49 MB（96%）」（`services/task_store_io.py:246-249`）。
- 建议：只保留前 200 条 + `error_count`；详情页按需从 `task_logs` 查。

**F-22（P3）调度线程串行跑长 job，且失败不推进 `last_run_at` → 每 60s 重试一次全量扫描**

- 位置：`core/scheduler.py:144-166`（`job.runner()` 在调度线程内同步执行；异常分支不更新 `last_run_at`）
- 证据：`chatgpt_auto_refresh` 的 runner 会扫全部 chatgpt 账号（生产库 667 个）并按 3s 间隔串行执行最多 5 个（`services/chatgpt_maintenance.py:283,396-500`），一轮可达 5 分钟以上；期间 `chatgpt2api_auto_sync` 被阻塞。
- 影响：两个周期任务互相拖累；一个持续抛异常的 job 会每分钟重扫全表。
- 建议：每个 job 独立线程 + 单飞锁（`max_instances=1`）+ 指数退避；至少把异常分支的 `last_run_at` 推进到 `now + backoff`。

**F-23（P3）`task_id` 用毫秒时间戳，同毫秒会撞**

- 位置：`api/tasks.py:281`（`task_{int(time.time()*1000)}`，另外 `:335/422/479/532/584` 五个任务端点同款）、`services/chatgpt_maintenance.py:646`（`_open_auto_task` 同款）
- 影响：同一毫秒内两个注册请求 → 后者覆盖前者的内存记录/DB 行（`_upsert_task_run` 是 upsert）。
- 建议：`f"task_{time.time_ns()}_{secrets.token_hex(3)}"`。

**F-24（P3）`core/scheduler.py` 的 docstring 已过期**

- 位置：`core/scheduler.py:21-23`（「尚无服务注册任务」），实际 `services/chatgpt_maintenance.py:805-813` 注册了 `chatgpt_auto_refresh` 与 `chatgpt2api_auto_sync`。
- 建议：改文档；顺便在 `registered_jobs()` 输出里带上 interval，便于运维核对。

---

## 7. 问题清单（按优先级）

| ID | 级别 | 位置 | 一句话 | 建议动作 |
|---|---|---|---|---|
| F-00 | **P0 → 已修复** | `api/actions.py:263`（旧） | UUID 主键被 `int()`，批量同步状态必 500；`9630980` 已改 `str()` 并附 3 条回归 | 已闭环，仅剩类型注解归一 |
| F-01 | P1 | `api/tasks.py:289,354,441,496,551,601` | 长任务占用 anyio 40 线程池 | 任务改用独立线程/队列 |
| F-02 | P1 | `api/tasks.py:748-752` | SSE 在事件循环里同步全量写库 | 只读 SSE + 节流写 |
| F-03 | P1 | `services/task_runners.py:55,400-411,465,668-680` | 收尾段无 try → 永久 running | `try/finally` 兜底 finish |
| F-04 | P1 | `api/tasks.py:822-831` + `services/task_store_io.py:429-432` | 删除后 GET 复活 | 删内存记录 + 读不写库 |
| F-06 | P1 | `services/action_execution.py:200-206` | Grok SYNC_STATUS 不落状态 + 死分支 | 按能力分派 |
| F-07 | P1 | `core/task_runtime.py:89-95` | 一次跳过全部在飞账号 | 只跳最近一个 attempt |
| F-08 | P1 | `services/task_runners.py:368-373,642-647` | 取消循环 O(n²) | 移出循环 / `cancel_futures=True` |
| F-09 | P2 | `services/chatgpt_maintenance.py:750` | chatgpt2api_sync 不可停 | 接 checkpoint 或 409 |
| F-10 | P2 | `services/task_runners.py:341,613` | 非 daemon 池线程阻塞重启 | deadline + 自管理线程 |
| F-11 | P2 | `services/chatgpt_maintenance.py:750-761` | `skipped > total` | `total` 含 skipped |
| F-12 | P2 | `core/task_runtime.py:311-326` | 运行中 skipped 恒 0 | 增加形参 |
| F-13 | P2 | `services/task_runners.py:557-558,605-606` | 两套账、跳过不写历史 | 收敛到 finally |
| F-13b | P2 | `services/task_service.py:99-107` | 每账号一个线程写历史 | 单写线程 + 队列 |
| F-14 | P2 | `services/task_store_io.py:366-369` | 多进程会误杀任务 | heartbeat / 单进程约束 |
| F-15 | P2 | `services/task_store_io.py:124-188` | 每条日志全量 upsert | append-only / 合并写 |
| F-16 | P2 | `api/tasks.py:118,140,160,190,210` | concurrency 无上界 | `Field(le=8)` |
| F-17 | P2 | `services/task_runners.py:533-545` | 持锁睡眠，并发被序列化 | 锁外 sleep |
| F-18 | P2 | `api/actions.py:397-419` | 同步批量串行、无超时、占连接 | 改任务化 |
| F-19 | P2 | `api/tasks.py:790-820` | 列表 0.4s/404KB 每 2.5s | 分页 + 增量 |
| F-20 | P2 | `services/task_service.py:169-184` | 每号一个自动上传线程 | 固定线程池 |
| F-05 | P2 | `core/capabilities.py:5,57-76` | 「17」实为 16 个唯一 id | 改文档或改键 |
| F-21 | P3 | `services/task_runners.py:367` | errors 无上限 | 截断 + 计数 |
| F-22 | P3 | `core/scheduler.py:144-166` | 调度线程串行 + 失败热重试 | 独立线程 + 退避 |
| F-23 | P3 | `api/tasks.py:281` | 毫秒级 task_id | ns + 随机后缀 |
| F-24 | P3 | `core/scheduler.py:21-23` | 文档过期 | 更新 |
| F-06b | P3 | `services/action_execution.py:69-71` | 过期注释（upload_sub2api） | 更新 |

---

## 8. 建议修复顺序

1. ~~**先确认 F-00**~~ —— **已闭环（`9630980`）**：`int()` → `str()` 已提交，附 `tests/test_batch_cliproxy_sync_uuid.py`（3 条，实跑 3 passed）。残留项只有把 `services/cliproxyapi_sync.py` 的 `dict[int, ...]` 注解归一。**修复顺序从第 2 项（F-03）开始。**
2. **再堵「永久 running」**（F-03 + F-04 + F-15）：这三条决定了「任务停不下来 / 删不掉 / 重启后状态错」的全部用户可见症状，改动集中在 `task_runners` 的两个收尾段与 `task_store_io` 的 upsert，风险低、收益最大。
3. **再修控制语义**（F-07 + F-08 + F-09）：一次跳过一批是「误伤账号」，O(n²) 是「点了没反应」，两个都会直接毁掉批量任务的可信度。
4. **然后摘掉线程池炸弹**（F-01 + F-16 + F-20）：把长任务移出 anyio 池、给 `concurrency` 加上界，这两件事做完，F-18 的连接池耗尽也会自然缓解。
5. **最后做计数与展示的一致性**（F-11 + F-12 + F-13 + F-19 + F-02）：`total/skipped` 口径、运行中 skipped、两套账、列表体积、SSE 写库 —— 都是「不改也能用，但用户会怀疑数据」的问题。

回归测试建议（新增，不改现有）：
- `test_batch_cliproxy_sync_uuid.py`：用 UUID 主键的账号跑 `_execute_batch_cliproxy_sync`，断言不抛 `ValueError` 且 `ok` 与 `sync_results` 对齐（钉住 F-00）—— **工作区已有同名文件（非本次审计产出），实跑 3 passed，待提交**。
- `test_skip_single_attempt.py`：并发 3 个 attempt，`request_skip_current()` 后断言只有 1 个被跳过。
- `test_task_finish_on_persist_error.py`：monkeypatch `persist_task_snapshot` 抛错，断言任务最终状态不是 `running`。
- `test_delete_then_get.py`：删除后 GET 断言 404（钉住 F-04）。

---

## 9. 附录：关键证据与复现命令

### 9.1 动作/能力对差

```bash
.venv/bin/python - <<'PY'
from core.base_platform import RegisterConfig
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.grok.plugin import GrokPlatform
from core.capabilities import ACTION_CAPABILITIES
c=[a["id"] for a in ChatGPTPlatform(config=RegisterConfig()).get_platform_actions()]
g=[a["id"] for a in GrokPlatform(config=RegisterConfig()).get_platform_actions()]
print(len(c), len(g), "pairs", len(c)+len(g), "unique", len(set(c)|set(g)), "registry", len(ACTION_CAPABILITIES))
print("orphan", set(ACTION_CAPABILITIES)-(set(c)|set(g)), "missing", (set(c)|set(g))-set(ACTION_CAPABILITIES))
PY
# → 11 6 pairs 17 unique 16 registry 16 / orphan set() / missing set()
```

### 9.2 跳过语义（F-07）

```python
from core.task_runtime import RegisterTaskControl
c = RegisterTaskControl()
ids = [c.start_attempt() for _ in range(5)]
c.request_skip_current()
for i in ids:
    try: c.checkpoint(attempt_id=i)
    except Exception as e: print(i, type(e).__name__)
# → 1..5 全部 SkipCurrentAttemptRequested
```

### 9.3 取消循环开销（F-08）

```python
from concurrent.futures import Future
import time
for n in (1000, 3000):
    fs=[Future() for _ in range(n)]; t=time.time()
    for f in fs:
        for p in fs:
            if p is not f: p.cancel()
    print(n, round(time.time()-t,3))
# 首次（4680c8d 机器状态）：1000 1.751s / 3000 38.035s
# 复核（9630980 机器状态）：1000 4.898s / 3000 18.392s  ← 绝对值随机器负载浮动，量级与超线性不变
```

### 9.4 DELETE 复活（F-04，临时库，不触碰生产库）

```python
# 建临时 sqlite → create → finish → delete row → _get_task_snapshot
# before delete: True / after delete: False / GET -> done / after GET: True
```

### 9.5 生产库只读抽样

```sql
-- 837 行，12 MB 的 logs+errors
select sum(length(logs_json)+length(errors_json)) from task_runs;   -- 12084870
select id, length(logs_json), length(errors_json) from task_runs
 order by length(errors_json) desc limit 3;
-- chatgpt2api_sync_1792814416608 | 41753 | 37132 | done

-- skipped > total
select id,total,success,registered,skipped,status from task_runs
 where skipped > total limit 3;
-- chatgpt2api_sync_1793015081417 | 278 | 278 | 278 | 338 | done
-- chatgpt2api_sync_1793014447182 |  10 |  10 |  10 | 606 | done
```

### 9.6 运行时环境事实

```
anyio default thread limiter tokens: 40      # Starlette BackgroundTasks 的并发上限
engine pool: QueuePool size=5 overflow=10 timeout=30.0
starlette 1.7.0
```

---

## 10. 复核记录（HEAD `9630980`）

`9630980` 把 F-00 的修复、`tests/test_batch_cliproxy_sync_uuid.py` 与四份报告一并入库。本节记录在该提交上**逐条复核**的结果：哪些仍成立、哪些已闭环、哪些引用需要更正。

### 10.1 复核结论

| 项 | 结果 |
|---|---|
| F-00 | **已修复**：`api/actions.py:267` 为 `sync_results.get(str(acc_model.id or ""), {})`；`grep -n "int(acc_model.id" api/actions.py` 无命中；回归 3 条实跑通过 |
| F-01 / F-02 / F-03 / F-04 | **全部复现**：`BackgroundTasks` → anyio 40 线程池；SSE 生成器内 `_persist_task_snapshot`（`api/tasks.py:751`）；`finish()` 前无 `try/finally`（`services/task_runners.py:401-411`）；DELETE 后 GET 复活（10.3 实测） |
| F-05 / F-06 | **复现**：17 个平台声明 / 16 个唯一 id / 登记表 16；`_apply_grok_status`（`services/action_execution.py:173`）无 `sync_grok2api_status` 分支，`:200` 的 `sync_cliproxyapi_status` 分支对 Grok 不可达（`capability_of("sync_grok2api_status") == sync_status` 已验证） |
| F-06b | **部分更正**：docstring 过期那半已不成立（见 10.4） |
| F-07 / F-08 | **复现**：5 并发 → 一次 `request_skip_current()` 命中 5 个 attempt；取消循环超线性（10.3） |
| F-09 ~ F-24 | **静态复核通过**，行号见 10.2；`skipped > total` 的规模从「最新 12 行」扩大到 **614/837 行（73%）**，比原判断更严重 |
| 生产库现状 | 837 行任务、`done` 825 / `stopped` 12、**无 `running`/`pending` 残留**（说明当前没有卡死任务，F-03 是「一旦触发就永久」的潜在缺陷而非现存故障） |

### 10.2 行号核对（`9630980`）

`api/actions.py` 因 F-00 的注释 + `str()` 自 `:262` 起整体 **+4 行**，其余文件行号不变。已核对的关键锚点：

```
api/actions.py:181  def _execute_batch_panel_status      （循环 199，无 try）
api/actions.py:234  def _execute_batch_cliproxy_sync     （循环 262，str() 267）
api/actions.py:402  通用批量循环（try 在 403）           （原报 :398-415 → 现 :402-419）
api/actions.py:296  def list_actions                     （「17 个动作」docstring 300，原 :290）
core/task_runtime.py:89   request_skip_current
core/task_runtime.py:231  has_active
core/task_runtime.py:307  update_counters（无 skipped 形参；原报 :311）
core/task_runtime.py:322  finish
services/task_runners.py:55-57   control_for / mark_running / persist（try 之前）
services/task_runners.py:368-373 取消循环（O(n²)）
services/task_runners.py:401-411 收尾段（_api.log → finish → persist → cleanup）
services/task_store_io.py:124 / 181 / 240 / 350 / 429   upsert / persist / list / 孤儿收尾 / 读路径
services/task_store_io.py:366-367  `if _store().exists(row.id): continue`
api/tasks.py:822-831  delete_task（无 store.remove，409 在 :826）
services/chatgpt_maintenance.py:750 / 753 / 761   `_control` 丢弃 / total=pushed+failed / skipped
core/scheduler.py:144-166  调度线程内 runner() + 失败不推进 last_run_at（:162 注释自证）
services/action_execution.py:287-292  UPLOAD_SYNC_WRITERS（3 个，查表点 :73）
```

### 10.3 复核实跑（本次）

```bash
cd /data/workspace/Register
.venv/bin/python /root/.hermes/cache/scratch/probe_actions_tasks.py
# === 1. capability diff ===
# 11 6 pairs 17 unique 16 registry 16
# orphan set() missing set()
# grok sync capability: [('sync_grok2api_status', 'sync_status')]
# === 2. skip semantics ===
# active: 5  skip_targets: 5  [(1..5, 'SkipCurrentAttemptRequested')]
# === 3. cancel loop cost ===
# 1000 4.898 / 3000 18.392

.venv/bin/python /root/.hermes/cache/scratch/probe_delete_revive.py     # 临时 sqlite，非生产库
# before delete: row exists = True
# after  delete: row exists = False
# GET snapshot -> done
# after  GET:    row exists = True   <-- 复活
# in-memory store still has record: True

.venv/bin/python -m pytest tests/test_batch_cliproxy_sync_uuid.py \
  tests/test_capability_registry.py tests/test_task_runtime.py \
  tests/test_task_store_io.py tests/test_register_task_controls.py \
  tests/test_batch_action_task.py -q
# 87 passed, 1 warning, 14 subtests passed in 15.67s
```

### 10.4 引用更正（原报告 → 复核）

1. **F-06b 的「过期注释」不成立**：`services/action_execution.py:69-71` 现在写的是「`upload_cpa` 对 ChatGPT 与 Grok 都有效；sub2api / chatgpt2api 当前只在对应平台声明」，与代码一致。剩下的只是 `UPLOAD_ACTIONS`（4）与 `UPLOAD_SYNC_WRITERS`（3）的数目不对称，功能由 grok 插件自写补齐。
2. **F-11 的量级被低估**：不是「最新 12 行命中」，是 **614/837 行**。`skipped` 记的是整轮面板对比的跳过数，而 `total` 只算 `pushed+failed` —— 口径错误是系统性的，不是个别历史脏数据。
3. **F-08 的绝对值不可复现**：同一段代码两次测量差 2–4 倍（1.751→4.898 / 38.035→18.392），说明该探针受机器负载影响大。**结论应建立在代码结构（每个 future 完成都重扫全部 future = O(n²)）而不是单次秒数上**；引用时用「1000 个 future 秒级、10000 个数量级到分钟」这种表述更稳。
4. **F-00 的「建议」已执行一半**：提交与回归测试都做了；剩 `services/cliproxyapi_sync.py` 的 `dict[int, ...]` 注解（`:570/577/665/667` 一带）与 `{str(k): v}` 归一。

### 10.5 复核未覆盖

- 未重跑 `chatgpt2api_sync` / `auto_refresh` 的真实调度（会触网打面板）；F-09、F-22 仍是静态结论。
- 未做并发压测（F-16 的 1000 线程 → 连接池 15 → `TimeoutError` 是算术推导 + 池参数实测，不是压测结果）。
- 生产库仍只读；本次仅新增两条 `count(*)` 级别的统计（837 行、614 行 `skipped>total`、状态分布），未读取任何凭据字段。

---

*本报告只读产出：唯一写入的文件是 `docs/audit/2026-10-30/actions-tasks.md`；`data/account_manager.db` 全程以 `mode=ro` 打开。*
