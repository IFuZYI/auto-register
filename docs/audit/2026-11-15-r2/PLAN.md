# 全量功能逻辑验证计划（2026-11-15 第二轮）

## Spec（绑定的权威）

用户要求：**全量验证各功能逻辑，确保不出现任何 BUG**。

这句话在工程上只能落成两条可判定的性质（"零 BUG"不可证，不写进去充数）：

1. **每个功能面都有可执行的验证**：不是"读代码看起来对"，而是
   **调用它、断言它的输出/副作用**。找不到验证手段的功能面，必须明确列出，
   不得默认为"没问题"。
2. **每条发现的缺陷都有复现**：不可复现的只记 `hypothesis`，不得进入修复波。

判定"全量"的边界，用**入口枚举**代替"扫完所有文件"：
- 4 个平台的注册入口
- 13 个 HTTP API 路由组（`api/` 下 13 个模块）
- 任务系统的 8 种任务类型（`api/tasks.py` 的路由）
- 后台周期任务 13 个（启动日志 `[OK] 周期任务: [...]`）
- 前端 12 个页面

这就是"全量"的可数清单。每一个都必须被至少一条 lane 覆盖。

## 第一轮（docs/audit/2026-11-15）的教训 —— 本轮据此改打法

上一轮 10 条 lane 里 8 条零产出：子代理在"全量面 + 每条必须复现"下
17 分钟仍未落盘。**本轮的硬约束**：

- 每个子代理**开局先创建报告文件并写入骨架**（标题 + 空表），再开始干活。
  这样即使被中断，也有部分产物，不会像上轮那样全白。
- 每个任务**只给一个窄面**（一个平台 / 一组 API / 一类任务），
  不给"全仓"。任务多但小。
- 明确时限：**单任务 12 分钟内必须完成第一版报告**（宁可少报，不许不落盘）。

## Global Constraints（全任务适用）

- **只读**。不得修改生产代码、不得改测试、不得写生产库。唯一允许写的是
  报告文件 `/data/workspace/Register/docs/audit/2026-11-15-r2/<task>.md`。
- 每条发现编号 `<PREFIX>-<n>`，带：严重度、`file:line`、**可执行的复现方式**、
  影响面、建议修法。
- **可复现才算缺陷**。构造不出复现 → `hypothesis`，且不得给 Critical/Important。
- 不得读取或输出：`.env`、`data/secrets/`、任何真实凭据/代理口令/账号邮箱。
- **不得对真实上游发请求**（`icloud-hme.fuzy.site`、`auth.openai.com` 等）。
  用 `tests/` 下的双端（`remote_hme_double.py` 等）或自己起本地假服务。
- 报告不粘贴超过 10 行的代码块，给 `file:line`。
- 发现"某个功能面根本没有任何验证手段"是**合法且重要的结论**，
  记为 `Important` 级"验证缺口"，不要为了凑数去编造缺陷。

## Task 1 — 任务系统：8 种任务类型的生命周期与计数

范围：`api/tasks.py`（8 个路由）、`services/task_runners.py`、`services/task_service.py`、
`core/task_runtime.py`、`services/task_store_io.py`。

必查（每一条都要能指到具体代码或跑出结果）：
1. 8 种任务类型各自：启动 → 计数 → 收尾，`success/skipped/errors/registered`
   是否自洽（`registered == success + skipped + len(errors)`）。
2. 停止/跳过的传播：`stop_requested` 能否打断长睡；`active_attempts` 是否归零。
3. 任务永久 `running` 的路径（`finish()` 被跳过 / `cleanup()` 前抛异常）。
4. 快照持久化：`persist_task_snapshot` 失败时任务状态是否仍正确落库。

## Task 2 — 账号域 API：列表/详情/批量动作的语义

范围：`api/accounts.py`、`api/actions.py`。

必查：
1. 分页/筛选/排序：`page`、`page_size`、日期区间、状态筛选、平台筛选
   —— 组合起来是否与"前端看到的总数"一致。
2. 批量动作的 `ids` 解析：字符串 UUID、空列表、含不存在的 id、含跨平台 id。
3. `action` 白名单：不存在的 action 是否 400 而不是 500。
4. 列表响应是否下发凭据（密码/token/cookies）。

## Task 3 — 凭据域 API：导入/导出/备份/回填

范围：`api/backup.py`、`api/contribution.py`、`services/account_export.py`、
`services/data_bundle.py`。

必查：
1. 导出的每种格式（CSA/CPA/2API/Agent2API/JSON/CSV）字段是否与目标导入器匹配。
2. 备份包含哪些字段；是否包含**解密密钥**；导入是否校验来源。
3. 回填（contribution）的鉴权与幂等。

## Task 4 — 代理域：池、探测、均衡、绑定

范围：`api/proxies.py`、`core/proxy_pool.py`、`core/proxy_utils.py`、
`services/panel_proxy_binding.py`。

必查：
1. `get_next` 选路：全部停用时返回什么；标签为空时的回退是否合理。
2. `report_fail/success` 的停用与复活阈值；一次失败是否过度惩罚。
3. 均衡（rebalance）dry-run 与实际执行的差异；是否可逆。
4. 探测（check_all）的并发与超时；部分失败时的结果聚合。
5. 绑定切换（register → bind）是否可能读到过期快照。

## Task 5 — iCloud 域：主号、别名池、收件、远程服务

范围：`api/icloud.py`、`services/icloud/`、`modules/mail/icloud_*.py`、`platforms/icloud/`。

必查：
1. 别名池四态流转（`unpooled/available/in_use/used`）：每个转移的触发条件与回滚。
2. 领取独占：两个 worker 同时 `claim_alias` 是否真的只有一个拿到。
3. 租约：续约、超时回收、失败释放；`in_use` 泄漏的可能路径。
4. 收件：`limit`/`days` 的取值与上游预算的关系（**本会话已实测**：limit<15 会丢信、
   limit=50 顶到 15s 悬崖；`days=3650` 会 SEARCH 上千封）。
5. 远程服务：API key 权限不足时的降级路径；`method/warning` 的消费点。

## Task 6 — 面板域：注册表、同步、对比、推送

范围：`services/panel_*.py`、`api/integrations.py`、`api/platforms.py`。

必查：
1. 13 个面板的注册表字段是否齐全（key/url_key 唯一、能力声明）。
2. 同步（push/pull/enroll）的方向语义与幂等；"登记"闸门是否会活锁。
3. 对比表：本地 vs 远程的时间判定（`iat` 缺失时的回落）。
4. 批量同步的并发与失败聚合。

## Task 7 — 邮箱域：渠道、号池、导入、OAuth

范围：`modules/mail/`、`core/mailboxes/`、`api/mail_imports.py`、`api/outlook.py`、
`api/shared_mail.py`、`services/mail_imports.py`。

必查：
1. 渠道注册表：未知 provider 是否明确报错（而不是静默落到某个默认渠道）。
2. 号池：Outlook/微软号的凭据字段完整性；`+` 别名归一。
3. 导入：CSV/JSON 格式变体、重复导入幂等、错误行聚合。
4. OAuth：refresh token 换发失败的错误分类（可重试 vs 永久）。

## Task 8 — 后台周期任务：13 个 job 的注册与执行

范围：`services/local_maintenance.py`、`services/chatgpt_maintenance.py`、
`services/panel_status_sync.py`、`services/log_cleanup.py`、`services/cliproxyapi_sync.py`、
`main.py` 的 scheduler 注册段。

必查：
1. 启动日志里的 13 个 job 与代码里的注册是否一一对应（不多不少）。
2. 每个 job 的"间隔为 0 = 不跑"语义是否一致；非法间隔是否被夹到合法范围。
3. job 内异常是否会杀死整个 scheduler。
4. 开关（配置键）是否真的控制对应 job；白名单是否完整。

## Task 9 — 前端：12 个页面的动作接线与状态映射

范围：`frontend/src/pages/`、`frontend/src/lib/`、`frontend/src/components/`、
`frontend/scripts/`（已有可执行检查脚本）。

必查：
1. 每个页面的每个按钮 → 后端路由：是否真的存在、方法是否匹配、参数是否齐全。
   （用 `frontend/scripts/run_*.mjs` 的可执行检查 + 路由表比对）
2. 状态标签映射：后端四值（registered/expired/invalid/banned）在前端是否一致。
3. 批量动作的 `selected` vs `all_filtered` 分支：前端传的 id 集合与提示文案一致。
4. 失败反馈：每个异步动作是否都有可见的失败提示（禁止静默）。

## Task 10 — 平台插件：4 个平台的动作与注册模式声明

范围：`platforms/*/plugin.py`、`core/registry.py`、`core/base_platform.py`。

必查：
1. 每个平台的 `get_platform_actions` 声明的动作是否与实现一致（存在、scope 正确）。
2. `registration_modes` 声明与运行时行为一致。
3. `check_environment` 钩子：缺依赖时是否在分配资源**之前**拦截。
4. 平台注册表：未知平台、重复注册、插件加载失败的处理。

## Task 11 — 汇总与修复波

汇总 Task 1–10 的 Critical/Important，按可复现性排序，逐条：复现 → 修复（先失败测试）
→ 冻结全量 → 记录。产出 `docs/audit/2026-11-15-r2/SUMMARY.md`。

## 执行裁决（Ruling）

- **Ruling**：Task 1–10 并行只读（互不写同一文件、不改代码），Task 11 串行修复。
  理由：同上一轮。代价：两条 lane 可能报同一处，汇总去重。
- **Ruling**：任务按**功能面**切而不是按代码目录切，因为用户要的是"功能逻辑验证"
  而不是"文件覆盖率"。代价：跨面的横切缺陷（如双后端）可能被两个面各看到一半。
- **Ruling**：允许 lane 报"验证缺口"（某功能面完全没有验证手段）作为 Important。
  理由：这比编造一条假缺陷更有价值。代价：修复波可能包含"补测试"而不是"修 bug"。