# 任务必须能被停止

等码是整条链最长的一段。停止/跳过必须在 0.25 秒内生效，否则用户点了没反应。

## 重构约束

- 新增轮询必须走 `_sleep_with_checkpoint` / `_run_polling_wait`
- 不许裸 `time.sleep(长秒数)`
- `finish_attempt` 必须在 finally 里执行

## 证据（8 块，自动抽取自源码注释）

> 原始索引见 `_index.json`。改对应代码前逐块读完 —— 这些分支不是冗余，是故障换来的。

### `platforms/grok/register_browser.py:584-597`

~~~
等验证码：iCloud 走 `icloud_service`，其它渠道走渠道自己的 `wait_for_code`。

    为什么两条路：iCloud 本地渠道的 `fetch_alias_messages(alias_id)` 是按别名
    过滤的专用接口，读得比通用 `wait_for_code` 更准；而 Outlook/tempmail 等
    渠道收码必须带 `account_id`（inbox token），只有邮箱地址读不到 —— 所以
    优先复用上层已经拿到的渠道账号对象。

    `task_control` 给定时每轮轮询都 checkpoint 一次：这条路径默认要等 240 秒，
    不给停止/跳过留入口的话，用户在面板上点「停止」得干等满超时（评审发现）。

    **控制流异常必须穿透**：`StopTaskRequested`/`SkipCurrentAttemptRequested`
    继承自 RuntimeError，被下面任何 `except Exception` 捕获都会让「停止」变成
    一次普通失败（评审实测复现）。所以内层 `except` 一律先放行 TaskInterruption。
    
~~~

### `services/chatgpt_maintenance.py:395-404`

~~~
跑一轮扫描：排计划 / 执行到点的刷新。

    每轮最多执行 `max_accounts` 个（避免高并发），串行 + `delay_seconds`
    间隔；结果按账号落库（`record_auto_refresh_result` 的语义）。

    **有实际执行**时创建任务记录（source=`auto_refresh`）——「任务运行」页
    可见、日志可回看、支持停止/跳过（用户要求 2026-10-07）；空轮不建记录。

    返回 `{scanned, scheduled, due, executed, ok, failed, skipped}`。
    
~~~

### `services/task_runners.py:344-346`

~~~
        # 收尾路径自己绝不能再抛：这里本来就在处理异常，若 _api._log 再抛一次，
        # finish() 会被整个跳过，任务永远停在 running —— 实测过的
        # `[Errno 32] Broken pipe` 事故正是这样：日志只有两行、DB 状态卡死。
~~~

### `services/task_runners.py:603-605`

~~~
        # 收尾路径自己绝不能再抛：这里本来就在处理异常，若 _api._log 再抛一次，
        # finish() 会被整个跳过，任务永远停在 running —— 实测过的
        # `[Errno 32] Broken pipe` 事故正是这样：日志只有两行、DB 状态卡死。
~~~

### `services/task_store_io.py:216-224`

~~~
「已结束任务」的 SQL 过滤条件。

    **必须显式带上 `IS NULL`**：SQL 里 `NULL NOT IN (...)` 求值为 NULL 而不是
    TRUE，只写 `notin_` 会让 NULL 行在「运行中」与「非运行中」两条查询里都不
    命中，从列表里静默消失。

    抽成函数是为了让测试能检查**生产代码真正用的**那个条件 —— 早先的测试在
    测试里重写了一遍同样的 WHERE，结果变异掉生产代码它照样绿（实测）。
    
~~~

### `services/task_store_io.py:232-247`

~~~
列出所有任务快照。

    `include_logs=False`（默认）时，**已结束**任务的 `logs` 与 `errors`
    都不下发。实测两轮：

    - 第一轮：253 个任务、415 KB，其中 52% 是历史日志；
    - 第二轮（日志裁掉后）：仍有 5.5–6.1 MB，其中 **`errors` 占 4.49 MB
      （96%）**，最大单条 37 KB，而 715 个任务里**运行中 0 个**。

    列表页对 `errors` 只用 `.length` 显示「✗ 失败 N」，明细在单条详情里看。

    两个字段共用一个开关是刻意的：拆成 `include_logs` + `include_errors`
    会得到四种组合，而没有任何调用方需要「只要其中一个」。

    仍在跑/等待的任务**保留**两者：那是实时面板要用的，裁掉就没得看了。
    
~~~

### `services/task_store_io.py:282-287`

~~~
    # **SQL 层就不取大字段**：`logs_json` / `errors_json` 在 763 行上合计
    # 十几 MB，`SELECT *` 会先全读进内存再在 Python 里清空 —— 实测查询本身
    # 86–518ms、序列化再 200–320ms。只取轻量列后降到 42–82ms（快 6–10 倍）。
    #
    # 要全量时（`include_logs=True`）才走 `SELECT *`：那条路是「点开看明细」，
    # 用户主动触发的，慢一点可以接受。
~~~

### `services/task_store_io.py:342-343`

~~~
    # **JSON 后端走自己的路径**：数据在 `task_runs.json`，扫 SQLite 的
    # `task_runs` 表会 `no such table`（实测，让 JSON 部署起不来）。
~~~
