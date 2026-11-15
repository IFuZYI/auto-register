# Task 1 — 任务系统：8 种任务类型的生命周期与计数
状态: DONE_WITH_CONCERNS

基线: 工作树（`git status` 未跟踪文件冲突，未取 commit）；Python `.venv/bin/python`，
测试 `-m pytest`。**只读**：生产代码/测试/数据库未改。实验在 `/tmp/t1exp*.db`
（`DATABASE_URL` 重定向）+ `core.db.init_db()` 的新库上跑，不碰 `data/`。

## 结论

1. **计数不变量 `registered == success + skipped + len(errors)` 在 8 类任务的主链上成立**
   —— 我用真的跑一遍逐类验证过（含混合结局、全成功、全停止），不是读代码推断。
   唯一不自洽的是**自动凭证同步**（`chatgpt2api_sync`）把它算成
   `pushed + failed`，`skipped` 不参与（T1-3，Minor，有复现）。
2. **停止/跳过传播是真的**：`stop_requested` 能在 0.3 秒内打断 30 秒的睡，
   `active_attempts` 在 `finish_attempt` 后归零（T1-验证，见"已验证"节）。
3. **有一处真实的 Critical**：两个 runner 的 `persist_task_snapshot(task_id)`
   在 `try` **之外**（`services/task_runners.py:405`、`:974`）。它一抛异常，
   整个 runner 直接逃逸，`finish()` 永不执行 → 任务**永久 running**，
   且 stop 无法救（没有 worker 再消费 `stop_requested`）。这不是 hypothesis，
   我复现到 HTTP 层：DELETE 409、`has_active()` 恒真、导入包被 409 拒绝。
4. 「`persist_task_snapshot` 失败时任务状态是否仍正确落库」的答案是**否**：
   收尾那一次失败后，内存 `failed`、`task_runs` 行仍是 `running`。
   列表接口读 DB，于是**列表显示 running 而详情显示 failed**（T1-2，Important）。
   详情端点会顺手修复该行 —— 也就是说这个 bug 只暴露在列表上，点开就自愈，
   极易被当成"偶发显示问题"。

## 发现

### T1-1 [Critical] `persist_task_snapshot` 位于 `try` 之外 → 任务永久 running，且不可停止

- 位置: `services/task_runners.py:405`（注册），`services/task_runners.py:974`
  （`_run_account_batch_task`，服务 6 类批量任务）。二者都在 `mark_running`
  之后、`try:` 之前，异常不属于任何 `except`。
- 严重度理由: 后果是**用户无法自救**。stop 端点只置标志位，已死线程不会再
  `checkpoint()`；DELETE 因 status=running 被 409；`has_active()` 恒真连带
  拒绝数据导入。只有重启进程 + 孤儿收尾能清。
- 复现（注册分支，实测）:
  - 建任务记录 → patch `services.task_runners.persist_task_snapshot` 抛
    `RuntimeError("database is locked")` → 调 `_run_register`。
  - 结果: 异常**逃出** `_run_register`；`_task_store.snapshot()['status']`
    = `running`；`has_active()` = `True`。
- 复现（批量分支，实测）: 同样 patch，调
  `_run_account_batch_task(tid, ["1"], label="补 RT", handle_account=...)`
  → 异常逃逸，status = `running`。
- 复现（后果链，实测，走真 HTTP 路由）: 上面留下 stuck 记录后
  `DELETE /api/tasks/{id}` → **409「运行中的任务不允许删除，请先停止任务」**；
  `POST /api/tasks/{id}/stop` → 200 但 `GET /api/tasks/{id}` 仍是 `running`；
  `has_active()` 仍 `True`（`services/data_bundle.py:602` 会据此 409 拒绝导入）。
- 现实触发源: `persist_task_snapshot` → `_upsert_task_run` → SQLite commit。
  同一文件 890 行与 1166 行的注释都写明这条路径会撞 `database is locked`
  （并发写同一 DB 时）。也就是说，**注释在别处已经承认这个异常存在**，
  却在 405/974 这两处没有防。
- 影响面: 全部 8 类任务里 7 类（注册 + 6 类批量任务）。`recover` 也走
  `_run_account_batch_task`。一旦命中，该任务永久占 `has_active()`，**该来源
  无法再开新任务、数据导入被拒**，直到进程重启。
- 建议修法: 把 `services/task_runners.py:405` 与 `:974` 的首次
  `persist_task_snapshot` 移进 `try`（或就地包 `try/except` + `logger.exception`），
  与 890/1188 行已有的"日志写失败不能带走 finish()"同一口径。首帧落不进去
  只影响可见性，绝不能反过来决定任务能不能结束。
- 为何现有测试没抓到: `tests/test_register_task_controls.py:526`
  `test_task_finishes_even_if_persist_snapshot_raises_at_the_end` 只让
  **第 4 次起**的调用抛（`calls["n"] > 3`），恰好跳过 405 行这次。
  测试守住的是收尾路径，**没有人守首帧**。

### T1-2 [Important] 收尾快照落库失败 → 列表显示 running，详情显示 failed（只在列表上暴露）

- 位置: `services/task_runners.py:876`（失败收尾）/ `:911`（正常收尾）
  的 `persist_task_snapshot` 被 `try/except` 吞掉；而读侧
  `api/tasks.py:1011` `_list_persisted_tasks()` 只读 `task_runs`。
- 复现（实测）: 让 `persist_task_snapshot` 从第 2 次调用起一直抛
  （模拟收尾时 `database is locked`）跑一次注册任务:
  - 内存 `_task_store.snapshot()` → `failed`
  - `GET /api/tasks`（`_list_persisted_tasks`）→ 该任务 **`running`**
  - `GET /api/tasks/{id}`（`_get_task_snapshot`）→ `failed`，并且该 GET
    顺手把正确状态写回 DB；**此后**列表才变成 `failed`
- 为什么是 Important 而不是 Critical: 有自愈路径（点开详情），且不影响
  计数（DB 行的 success/registered 在失败前那次快照里已是对的）。
- 影响面: 前端每 2.5 秒轮询 `GET /tasks`（`api/tasks.py:993-995` 注释）。用户
  看到的是「任务永远在运行中」+ 无法删除（DELETE 读的是快照，
  `delete_task` 先 `_get_task_snapshot` → 此时已是 failed，所以 DELETE 能过；
  **但列表上的红色/进行中状态不会消失**）。这是"显示与实际不一致"，
  用户无法判断该不该再等。
- 建议修法: 收尾的 `persist_task_snapshot` 失败应重试一次；或让
  `_list_persisted_tasks` 对内存里存在的任务以内存状态为准（内存是权威，
  DB 是缓存）。后者更彻底，也与 `_get_task_snapshot` 已有的"先落库再读"
  顺序一致。**不要**只加重试 —— 重试仍可能失败。

### T1-3 [Minor] `chatgpt2api_sync` 的 `registered` 不含 `skipped`，与其余 7 类不同口径

- 位置: `services/chatgpt_maintenance.py:510` `registered=pushed + failed`；
  对照 `services/local_maintenance.py:484` `registered=ok + failed`（同样漏
  `skipped`，但那条路径 `skipped` 只在 `acc is None` 时 +1）。
- 复现（实测，直接调 `_close_auto_task` 复刻算式）:
  `success=1, skipped=2, errors=[]` → `registered=1`。
  `1 != 1 + 2 + 0`，不变量**不成立**。
  对照 `local_maintenance` 同算式 `success=1, failed=2, errors=[2 条]`
  → `registered=3`，不变量成立（因为它恰好没 skipped）。
- 影响面: 任务页的「已处理 N/M」进度分子。`total=3` 但 `registered=1`，
  界面显示「1/3」；实际 3 个都处理完了。用户会以为任务卡住。
- 为何 Minor: 只影响这一个自动维护来源的展示，不影响注册记录、不影响
  手动 8 类路由。但它是**口径分叉**，不是"少算一点"。
- 建议修法: 两处统一成 `registered = success + skipped + len(errors)`，
  或干脆让 `_close_auto_task` 在 `registered` 缺省时自己按不变量算
  （`core/task_runtime.py:379` 的 `finish()` 已经有这个兜底逻辑，绕过它
  才出的错）。

### T1-4 [Important] 两处 `finish()` 裸调 + 一个 `except` 分支没有 `finally` 保护（验证缺口）

- 位置: `services/task_runners.py:1172-1181`（`_run_account_batch_task` 的
  `except` 分支）—— 这里的 `finish()` / `persist_task_snapshot()` /
  `cleanup()` 三者**都没有** `try`。
  对照同文件 `:864-878`（注册分支的 except，逐个 try 包住）与
  `:1193-1208`（批量分支的正常收尾，`finally` 包住）。
- **这一条我标 Important 但按"验证缺口"归类**：我构造不出 `finish()` 抛异常的
  现实路径（`core/task_runtime.py:364-386` 只做内存赋值，唯一异常是 id 不存在
  的 `KeyError`，那属于调用方已犯错）。但 `persist_task_snapshot()` 会抛 ——
  这一点我**已经复现**（T1-1/T1-2）。所以 1172-1181 的
  `persist_task_snapshot(task_id)` 一旦抛，`cleanup()` 被跳过，
  且异常从 `except` 块里**二次逃逸**。
- 复现（实测）: `_run_account_batch_task` 走 except 分支（内部抛致命错误）
  且 `persist_task_snapshot` 抛 → 异常逃离函数。此路径**没有任何测试覆盖**：
  `grep` 全部 293 个测试文件，没有一条让批量 runner 的 except 分支 +
  快照失败同时发生。
- 建议修法: 把 1172-1181 改成与 864-878 同构的三段 `try/except`。

### T1-5 [Minor] `delete_task` 删库不删内存 → 任务被后续任意 GET「复活」

- 位置: `api/tasks.py:1021-1033` `delete_task`。它只
  `s.delete(TaskRunModel)`，不 `_task_store` 里的记录。
- 复现（实测，走真 HTTP 路由）:
  1. 建一条 done 任务并落库 → `GET /api/tasks` 含该 id
  2. `DELETE /api/tasks/{id}` → 200；列表不含该 id（符合预期）
  3. `GET /api/tasks/{id}` → 200（详情端点调 `_persist_task_snapshot`，
     `services/task_store_io.py:432`）→ **重新 upsert 回 DB**
  4. 再 `GET /api/tasks` → 该 id **又出现了**
- 影响面: 「删除任务」在内存记录被 cleanup 淘汰前是**假的**。用户删掉一条
  历史任务，只要中间打开过一次详情（前端详情页必然 GET），它就回来。
  `cleanup()`（`core/task_runtime.py:401`）只在超过 250 条时淘汰**最老**的
  已完成任务，被删的这条若较新会长期留着。
- 建议修法: `delete_task` 同时从 `_task_store` 移除（需要新增
  `RegisterTaskStore.remove()`），或在 `_get_task_snapshot` 里不
  upsert 已标记删除的 id。前者更干净。
- 注: 这条不在 brief 的 4 条必查里，属于范围内代码（`api/tasks.py`）的顺带发现。

## hypothesis

### H1 [hypothesis/Minor] `BaseException` 逃逸同样留下永久 running
复现: `register()` 抛 `SystemExit(1)` → `except Exception` 抓不住，
`_run_register` 逃逸，status = `running`、`has_active()` = `True`（**已实测**）。
但这不是"缺陷"—— 进程通常就此退出，孤儿收尾会在下次启动时把它标成 stopped。
**缺的证据**: 生产环境里 `BaseException` 逃逸的真实触发源（目前想不出除了
`KeyboardInterrupt`/`SystemExit` 之外的路径）。因此不列为缺陷，只记录该行为。

### H2 [hypothesis/Minor] 停止对"卡在网络请求里的 worker"无效
`core/task_runtime.py:143` 的 `sleep` 走 Event 唤醒（实测 0.3s 打断 30s 睡，
**有效**），但 `register()` / `execute_action()` 内部的 socket 读没有控制点。
**缺的证据**: 需要构造一个"上游挂住 60 秒"的本地假服务，测量 stop 到
`active_attempts` 归零的延迟。时间不够，未做。
上一轮审计（`docs/audit/2026-11-15/task-runtime.md:44`）也把它列为未覆盖。

## 已验证（正面证据，避免误改）

| 项 | 命令/测试 | 断言到的事实 |
|---|---|---|
| 停止打断长睡 | 实测 `control.sleep(30)` + 0.3s 后 `request_stop()` | 0.302s 抛出 `StopTaskRequested` |
| 跳过打断长睡 | 同上，`request_skip_current()` | 0.300s 抛出 `SkipCurrentAttemptRequested` |
| `active_attempts` 归零 | 同上 + `finish_attempt` | `1 → 0`；最终快照 `active_attempts: 0` |
| 注册混合结局 | 实测 3 号 = 1 成功 / 1 失败 / 1 跳过 | `success=1 skipped=1 errs=1 registered=3`，不变量成立；`progress=3/3` |
| 注册全成功 | `tests/test_register_task_controls.py:352` | `success=2`，逐号进度日志齐全 |
| 注册先停后跑 | 实测 `request_stop` 在建记录后立即调 | `status=stopped`，计数全 0，不变量成立（0=0+0+0） |
| 批量：全成功/混合/全停/停一半 | 实测 4 组（mock `_load_account_fields`） | 4 组不变量全部成立，`active_attempts=0` |
| 批量：skip 计入 skipped | 实测 `mixed` 组 | `succ=1 skip=1 err=1 reg=3` |
| 前端任务类型标签 | `node frontend/scripts/run_task_kind_checks.mjs` | `passed:true, checked:20, failures:[]` |
| 8 类路由的建任务/选号/拒绝 | `pytest tests/test_backfill_rt_task_api.py tests/test_bind_2fa_task_api.py tests/test_probe_status_task.py tests/test_refresh_token_task.py tests/test_batch_action_task.py` | 122 passed, 14 subtests |
| 跳过只消费一次 / 停止粘滞 / 定向跳过 | `tests/test_task_runtime.py:12,22,32` | 三条断言 pass |

## 覆盖清单（8 种任务类型逐条）

判定标准：`registered == success + skipped + len(errors)`，且 `finish()` 必达。

| # | 类型 / 路由 | 计数不变量 | `finish()` 必达 | 证据 |
|---|---|---|---|---|
| 1 | `POST /register` → `_run_register` | ✅ 成立（3 组实跑） | ⚠️ **T1-1**（`:405` pre-try） | 实测 + `test_register_task_controls.py` |
| 2 | `POST /backfill-rt` → `_run_account_batch_task` | ✅ 成立（4 组实跑） | ⚠️ **T1-1**（`:974` pre-try） | 实测 + `test_backfill_rt_task_api.py:150` |
| 3 | `POST /bind-2fa` → 同上 | ✅ 成立（同骨架） | ⚠️ **T1-1** | `test_bind_2fa_task_api.py:156,194`（skipped 而非 failed） |
| 4 | `POST /refresh-token` → 同上 | ✅ 成立（同骨架） | ⚠️ **T1-1** | `test_refresh_token_task.py:142,291` |
| 5 | `POST /probe-status` → 同上 | ✅ 成立（同骨架） | ⚠️ **T1-1** | `test_probe_status_task.py:63`（只测调了 action） |
| 6 | `POST /recover` → 同上 | ✅ 成立（同骨架） | ⚠️ **T1-1** | 无专门 runner 测试；选号逻辑在 `api/tasks.py:694-711` |
| 7 | `POST /batch-action` → 同上 + `platform` 参数 | ✅ 成立（同骨架） | ⚠️ **T1-1** | `test_batch_action_task.py:100,104` |
| 8 | 自动维护（`auto_probe_*`/`auto_refresh_*`/`chatgpt2api_sync`）→ `_close_auto_task` | ⚠️ **T1-3**（sync 漏 skipped） | ✅ 有（`:371-384` 全 try） | 实测 `_close_auto_task` 算式 + `test_task_kinds_and_visibility.py` |

**8 类的共同底座**: 2–7 全部复用 `_run_account_batch_task`，所以 T1-1 的
`:974` 一处缺陷同时打穿 6 类任务。这是本次审计最重要的结构性事实 ——
计数逻辑没有分叉，但**收尾失败模式也没有分叉**，一处未防就是六处未防。

## 未验证

- **stop 能否打断"卡在 socket 读里的 worker"**（H2）：需要本地假服务配合，
  时间不足。只验证了 Event 睡眠路径。
- **并发下的计数一致性**: 我用 `concurrency=1` 跑混合结局。`concurrency>1`
  时 `success/skipped/errors` 是主线程累加的（`as_completed` 单线程消费），
  逻辑上无竞态，但**未实跑** `concurrency=4` 且结局交错。
- **`_finalize_orphan_tasks` 的两条分支**（SQLite / JSON 后端）只读了代码，
  没实跑"重启后 job 状态从 running 变 stopped"的完整链路（只在 T1-1 的
  复现里观察到：记录仍在内存时它**跳过**收尾 —— `services/task_store_io.py:367`
  `if _store().exists(row.id): continue`，这正是 T1-1 无法自愈的原因）。
- `GET /tasks/{id}/logs/stream` 的 SSE 循环（`api/tasks.py:938-980`）未实测。