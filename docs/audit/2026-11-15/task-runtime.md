# Lane: 任务运行时与生命周期

基线: `fca2eec` + 工作树 dirty
方法: 控制器取证（lane 6 子代理在 17 分钟内未落盘即被中断，未留下任何结论）。

## 结论

被审计到的主链收尾路径**基本健全** —— 已经吸取了那次 Broken-pipe 事故的教训。
发现 1 条 Minor 的不一致，无 Critical。

## 发现

### TASK-1 [Minor] `finish()` 的保护不一致：批量动作分支有 `finally`，注册分支没有

- 位置:
  - `services/task_runners.py:1194`（批量动作**成功路径**：`finish()` 在
    `try/finally` 里，日志写失败也照样结束任务）
  - `services/task_runners.py:864`、`services/task_runners.py:900`
    （注册分支的两处 `finish()` **裸调**，没有 `try`）
- 复现: 读代码即可确认；不需要运行时复现（见下方"为何只判 Minor"）。
- 证据: 同一文件里两种写法并存 —— 1234 行的注释明说"日志写失败不能把
  finish() 一起带走（F-03）"，而 864 行的同类调用没有这层保护。
- 影响: 若 `finish()` 自身抛异常，`cleanup()` 被跳过、任务停在 `running`。
  **但实际风险低**：`TaskStore.finish`（`core/task_runtime.py:364-386`）只做
  内存字段赋值，无 I/O、无序列化，唯一可能的异常是 `task_id` 不存在时的
  `KeyError` —— 那属于调用方已犯的错（任务一定先 `create` 过）。
- 为何只判 Minor: 按"可复现性"标准，我无法构造出 `finish()` 抛异常的
  现实路径。**因此不给 Critical/Important**，只作为一致性缺陷记录。
- 建议: 把 864 / 900 两处 `finish()` 包进 `try/except` + `logger.exception`，
  与 1194 行统一；这属于低成本防御，不要为此改 `finish()` 的语义。

## 已经正确的部分（避免误改）

- `except` 分支的收尾（`services/task_runners.py:850-880`）把
  `_api.log` / `finish` / `persist_task_snapshot` **逐个** try 包住，注释写明了
  "收尾路径自己绝不能再抛"。这是对照 1194 行的正确范式。
- `core.console.guard_stdout` 存在并在 `lifespan` 里安装（`main.py`），
  针对的就是那次 `Broken pipe`。

## 未审计（本 lane 未完）

lane 6 子代理被中断，以下面**未覆盖**：

- 停止/跳过能否打断阻塞中的 socket 读（只有 `_sleep_with_checkpoint` 走 Event 唤醒，
  长请求没有验证）。
- 跨任务的模块级可变状态（熔断器、来源认领集合、缓存）是否会互相污染 ——
  注意 `_FAILED_SOURCES` 按任务键、熔断器按服务+主号键这两处本会话已改过，
  但**未做全量普查**。
- 计数漂移（`registered = success + skipped + errors`）在并发下的实际一致性。
- 任务日志的持久化是否原子（进程被杀时能否留下半截日志）。
