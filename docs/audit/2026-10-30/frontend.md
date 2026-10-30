# frontend 审计

[MODE: SANDBOX]

- 仓库/基线：`/data/workspace/Register`，分支 `main`，HEAD `4680c8d`
- 范围：`frontend/src/`（`pages/`、`components/`、`lib/`、`api/`、`App.tsx`），共 48 个 `.ts/.tsx`，14 577 行
- 方式：只读静态阅读 + 只读 `GET` 实测（`http://127.0.0.1:8000`）+ 构建产物核对；未改任何源码
- 结论速览：**P0 × 5、P1 × 16、P2 × 19**
- 工具链：`npx tsc --noEmit` **0 error（exit 0，零输出）**；`npm run lint` **exit 1，39 problems（34 errors / 5 warnings），12 个文件**

## 0. 结论摘要

| 级别 | 编号 | 一句话 |
|---|---|---|
| P0 | F-01 | 勾选账号后「导出」实际导出**全部筛选结果**（含明文密码）——UUID 被 `Number()` 吞掉 |
| P0 | F-02 | 同一根因让「补 RT / 刷新 Token / 上传到面板（所选）」**必然失败**，「同步所选」必然提示「请先选择」 |
| P0 | F-03 | 任务历史只取 50 条却按 20/页做客户端分页，`total=1661` 中 1611 条**永远不可达** |
| P0 | F-04 | 「删除选中 N 个」账号**一点即删、无二次确认**（单次最多 1000 条，不可恢复） |
| P0 | F-05 | 关闭「访问密码保护」/「双因素」**一键生效、无确认** |
| P1 | §1–§5（16 条） | 竞态、无 catch 的静默失败、重复请求、错误态=空态、契约字段被忽略、图标按钮无名字等 |
| P2 | §1–§5（19 条） | 死代码、四套时间格式化、a11y 细节、长列表、打包副作用等 |

> 已修项（**不重复报**，本次仅复核）：59 → 5 个 chunk、空闲预取、`/api/tasks` 的 ETag —— 见 §7。

---

## 1. 数据获取：重复请求 / 竞态 / 无谓全量拉取

### F-01 [P0] 选中账号的 UUID 被 `Number()` 转成 NaN，导出「所选」退化成导出「全部筛选」

- **位置**：`frontend/src/pages/Accounts.tsx:1469-1471`、`frontend/src/components/AccountExportModal.tsx:63-77`
- **证据**：
  ```tsx
  // Accounts.tsx:1469
  selectedIds={Array.from(selectedRowKeys)
    .map((value) => Number(value))
    .filter((value) => Number.isInteger(value) && value > 0)}
  ```
  账号主键自 T1.0 起是 UUID（实测 `GET /api/accounts?platform=chatgpt&page_size=1` → `"id":"c831c3b8-3ed2-469c-9706-b774febf803d"`），`Number("c831…") = NaN` → 被 `filter` 全部丢掉 → `selectedIds = []`。
  `AccountExportModal.tsx:63` 于是 `scope = scopeChoice ?? (selectedIds.length > 0 ? 'selected' : 'filtered')` 判成 `'filtered'`；`AccountExportModal.tsx:74` 的 `selectionKey.split(',').map(Number)` 即使走到 `'selected'` 也只会得到 `[NaN,…]`（JSON 序列化成 `null`），被后端 `normalize_account_ids` 丢弃。
  后端 `api/accounts.py:451-462`：`if body.account_ids: … else: accounts = _filtered_accounts(...)` —— **空数组 = 导出全部**。
  而菜单文案是 `导出 (3)`（`Accounts.tsx:1160`），用户以为自己只导 3 条。
- **影响**：数据外泄级误操作（整库邮箱+密码被导出/分享），且用户无从察觉。
- **建议**：删掉所有 `Number()` 强转，`selectedIds` 直接用 `string[]`；`AccountExportModal` 的 props 类型改 `string[]`；后端 `ExportTextRequest.account_ids` 已是 `list[str|int]`，无需改。顺手把 `AccountExportModal.tsx:74` 的 `split(',').map(Number)` 改为 `split(',')`（id 里不含逗号）。

### F-02 [P0] 同一根因：三个「按所选执行」的批量动作必然失败，第四个必然误报「请先选择」

- **位置**：`frontend/src/pages/Accounts.tsx:598-600`（同步状态）、`670-672`（上传到面板）、`721-723`（补 RT）、`764-766`（刷新 Token）
- **证据**：四处都是
  ```tsx
  body.account_ids = Array.from(selectedRowKeys)
    .map((value) => Number(value))
    .filter((value) => Number.isInteger(value) && value > 0)
  ```
  而 `scope = selectedRowKeys.length > 0 ? 'selected' : 'all'`（`Accounts.tsx:664`、`710`、`755`）—— 只要用户勾了行，`scope==='selected'`，但 `account_ids` 恒为 `[]` 且不带 `all_filtered`。
  后端 `services/chatgpt_account_selection.py:113-115`：`ids` 为空且 `all_filtered` 为假 → `raise ValueError("请提供 account_ids，或指定 all_filtered=true")` → 400。
  「同步所选」更直观：`Accounts.tsx:601-603` 的守卫把它变成 `message.warning('请先选择要同步的账号')` —— 用户明明勾了行。
- **影响**：账号页 4 个主要批量入口在「选中」路径上 100% 不可用；错误文案还指向用户操作。
- **建议**：同 F-01（去掉 `Number()`），并在发请求前断言 `account_ids.length === selectedRowKeys.length`；把「选中/全部」的判定集中到一个 `buildScopeBody()`，避免 4 处复制。

### F-03 [P0] 任务历史：服务端分页能力完全没用上，1661 条里只有 50 条可达

- **位置**：`frontend/src/pages/TaskHistory.tsx:54`（写死 `page:'1', page_size:'50'`）、`TaskHistory.tsx:218`（`pagination={{ pageSize: 20 }}`）
- **证据**：`GET /api/tasks/logs?page=1&page_size=50` 实测 `total=1661, items=50`；后端 `api/tasks.py:636-656` 支持任意 `page/page_size`。前端把 50 条塞进 Table 再**客户端**分 20/页 → 只有 3 页，第 4 页起为空；标题「共 1661 条」（`TaskHistory.tsx:156`）与可达数据不符。
- **影响**：审计/排障时看不到 30 天前的失败记录；用户以为「历史被删了」。
- **建议**：Table 改服务端分页（`pagination={{ current, pageSize, total, onChange }}`，`load` 依赖 `page/pageSize`），或至少 `page_size` 跟随 Table 的 `pageSize` 并把 `total` 传给 Table。

### F-04 [P0] 「删除选中 N 个」账号无二次确认

- **位置**：`frontend/src/pages/Accounts.tsx:1165-1179`（菜单项 `danger: true`）→ `Accounts.tsx:460-469`（直接 POST）
- **证据**：
  ```tsx
  { key: 'delete', icon: <DeleteOutlined />, label: `删除选中 ${selectedRowKeys.length} 个`, danger: true }
  …
  else if (key === 'delete') { void handleBatchDelete() }   // 无 Popconfirm / modal.confirm
  ```
  单条删除有 `Popconfirm`（`Accounts.tsx:1063-1073`），批量删除反而没有。后端上限 1000 条（`api/accounts.py:690-691`）。
- **影响**：一次误点 = 最多 1000 个账号不可恢复；与同仓 `Proxies.tsx:527-538`、`TaskHistory.tsx:165-175`、`ICloud.tsx:841-852` 的确认习惯不一致。
- **建议**：套 `modal.confirm`，正文写清「将删除 N 个账号（含密码/Token），不可恢复」，并要求输入数量或勾选「我确认」。

### F-05 [P0] 关闭「访问密码保护」与「双因素认证」一键生效，无确认

- **位置**：`frontend/src/pages/Settings.tsx:353-365`（`handleDisableAuth`）、`Settings.tsx:419-427`（`handleDisableTotp`）
- **证据**：两个 handler 都是 `await apiFetch('/auth/disable' | '/auth/2fa/disable', {method:'POST'})` 直接执行，按钮 `danger` 但无 `Popconfirm`/`modal.confirm`；对比同仓「清理日志」是有确认的（`TaskHistory.tsx:86-109`）。
- **影响**：安全姿态降级只需一次误点，且没有任何「刚刚发生了什么」的强提示（只有一条 toast）。
- **建议**：加 `modal.confirm`，要求重新输入当前密码；关闭 2FA 时额外提示「之后仅凭密码即可登录」。

### F-06 [P1] 详情抽屉竞态：连点两行，先发后到的响应会覆盖当前账号

- **位置**：`frontend/src/pages/Accounts.tsx:434-449`
- **证据**：`openDetail` 先 `setCurrentAccount(record)`，再 `await apiFetch(...)` 后无条件 `setCurrentAccount(normalizeAccount(full))`；没有请求序号、没有 `AbortController`、没有比对 `record.id`。
- **影响**：A→B 快速切换时抽屉标题是 B、内容可能是 A（含 A 的密码/Token/2FA 密钥），复制操作会把 A 的凭证贴到 B 的上下文。
- **建议**：`const reqId = ++detailReqRef.current`，回包时 `if (reqId !== detailReqRef.current) return`；或 `AbortController` 取消上一个请求。

### F-07 [P1] 列表请求既无竞态保护，也无 `catch`：旧响应覆盖新筛选 + 失败完全静默

- **位置**：`Accounts.tsx:321-347`、`Proxies.tsx:102-132`、`RunningTasks.tsx:106-124`、`Dashboard.tsx:95-107`、`TaskHistory.tsx:51-67`、`Settings.tsx:570-584`
- **证据**：六个页面的 `load()` 都是 `setLoading(true); try { await apiFetch(...); setX(data) } finally { setLoading(false) }` —— **没有 catch**。`useEffect(() => { load() }, [load])` 里也没有 `.catch`。
  - 竞态：`Accounts` 的 `load` 依赖 `[currentPlatform, search, filterStatus, filterPlusStatus, createdAtStart, createdAtEnd, page, pageSize]`（`Accounts.tsx:343`），任一项变化就发新请求；先发的慢响应会覆盖后发的快响应（切平台/连点翻页可复现）。
  - 静默：接口 500/断网 → unhandled promise rejection，`loading` 归 false，表格保留旧数据，**没有任何 message**。
  - 全仓只有 `TaskLogPanel.tsx:139` 用了 `AbortController`，列表页一个都没有。
- **影响**：筛选结果与筛选条件不一致（用户按「banned」筛却看到 registered 行）；后端故障时界面看起来「正常但空/旧」。
- **建议**：抽 `useListQuery(fetcher, deps)`：内部 `AbortController` + 序号 + `catch → message.error` + `error` state；或在每个 `load` 里 `try/catch` 并把错误渲染成 `<Alert>`（参考 `MailServicePage.tsx:161-166` 的 `loadFailed` 写法，那一页是全仓唯一做对的）。

### F-08 [P1] 重复请求：同一份数据在启动/切页时被拉多次

| 位置 | 重复内容 | 证据 |
|---|---|---|
| `App.tsx:136` + `App.tsx:225` | `GET /api/auth/status` | 挂载时 `ProtectedLayout` 拉一次，`AppContent` 再拉一次 |
| `RegisterTaskPage.tsx:134` + `:262` | `GET /api/config` | 两个 effect 都在挂载时执行；第二个还在每次切平台时再拉整份配置（只读 `${platform}_executor` 一个键） |
| `PanelManagement.tsx:154-169` vs `App.tsx:237-243` | `GET /api/integrations/panels` | 侧栏已拉过，页面再拉一次 |
| `Proxies.tsx:105-109` | `GET /proxies` + `/proxies/balance` + `/config` | 一次 `load()` 三请求；add/del/batchDel/toggle/balance 后各调一次 `load()`（`:152,163,178,204,283`） |
| `Settings.tsx:571` + `PanelConfigPanel.tsx:269` | `GET /config` | 「全局配置」页与「面板配置」tab 各拉一次 81 键配置 |
| `Accounts.tsx:518,521` | `GET /config` + `/platforms` | 每次点「注册」都重新拉（`/platforms` 有 promise 缓存，`/config` 没有） |

- **建议**：`/config`、`/platforms`、`/auth/status`、`/integrations/panels` 走一个带 TTL 的模块级缓存（`lib/platforms.ts:115` 的 `capabilitiesPromise` 已是正确范式，复制它）；`Proxies` 的 `load()` 拆成 `loadProxies()` / `loadBalance()` / `loadCfg()`，按动作只刷需要的部分。

### F-09 [P1] 无谓全量拉取：单页请求体积与字段都远超所需

| 接口 | 实测体积 | 问题 |
|---|---|---|
| `GET /api/accounts?page_size=50` | **242 653 B** | 单行 3 436 B，其中 `token` 占 **1 990 B（58%）**；列表只用它做 AT 列的兜底（`Accounts.tsx:423`），`_slim_account_row`（`api/accounts.py:260-305`）只瘦了 `extra_json` 和 `password`，没动 `token` |
| `GET /api/icloud/aliases` | **712 390 B / 1 941 条** | `ICloud.tsx:106-130` 每次挂载、每次切主号筛选都全量重拉；筛选是纯客户端（`ICloud.tsx:173-183`），注释也承认「一次性拉全量」 |
| `GET /api/tasks` | **374 349 B / 837 条** | `RunningTasks` 有运行中任务时 2.5 s 一次（ETag 已挡住重复体，但排序/解析仍全量） |
| `GET /api/config` | 81 键 | `Proxies` 只为 2 个键、`RegisterTaskPage` 只为 1 个键就整份拉 |

- **建议**：`/accounts` 列表加 `fields=` 或把 `token` 也换成 `has_access_token` + `at_iat/at_exp`（前端 `getAccessToken` 已优先读 `extra`，只有 grok/旧数据才落到列）；`/icloud/aliases` 加分页 + `keyword` 服务端筛选；`/config` 加 `keys=` 查询参数（后端 `CONFIG_KEYS` 白名单现成）。

### F-10 [P1] `Proxies`：检测失败后按钮永久转圈；启停失败完全静默

- **位置**：`frontend/src/pages/Proxies.tsx:207-214`（check）、`Proxies.tsx:202-205`（toggle）
- **证据**：
  ```tsx
  const check = async () => {
    setChecking(true)
    await apiFetch('/proxies/check', { method: 'POST' })   // 抛错 → 后面两行永不执行
    setTimeout(() => { load(); setChecking(false) }, 3000)
  }
  const toggle = async (id) => { await apiFetch(...); load() }   // 无 catch、无 loading
  ```
  后端 `api/proxies.py:196-199` 是 `background_tasks.add_task(proxy_pool.check_all)` —— 立即返回，真正检测在后台。
- **影响**：一次网络抖动 → 「检测全部」按钮永久 `loading`，只能刷新页面；启停失败时开关看起来「没反应」。
- **建议**：`try/catch/finally` + `message.error`；检测改为轮询 `/proxies`（或 `/proxies/balance`）直到 `last_checked` 变化，而不是固定 3 s 猜测。

### F-11 [P2] `Proxies` 单代理上限：每敲一个字符就 `PUT /config` + 弹一次「已保存」

- **位置**：`frontend/src/pages/Proxies.tsx:461-466` → `Proxies.tsx:292-309`
- **证据**：`onChange={(value) => saveBalanceCfg(balanceEnabled, Number(value) || 0)}`，`saveBalanceCfg` 里 `await apiFetch('/config', {method:'PUT'})` + `message.success('均衡配置已保存')`。
- **影响**：输入 `100` 触发 3 次写库 + 3 条 toast；清空输入框会先写 `0`（= 不限），中间态被持久化。
- **建议**：改受控本地 state + `onBlur`/防抖 600 ms 保存，成功提示合并。

### F-12 [P2] `RegisterTaskPage` 的轮询定时器不会停，也不会随组件卸载清理

- **位置**：`frontend/src/pages/RegisterTaskPage.tsx:230-243`
- **证据**：`const interval = setInterval(async () => { const t = await apiFetch(...); … clearInterval(interval) }, 2000)` —— 回调内无 `try/catch`，一次失败就再也不会 `clearInterval`；`setInterval` 的 id 没有存进 ref，组件卸载时无人清理。
- **影响**：网络抖动后每 2 s 无限打接口 + 对已卸载组件 `setState`；离开页面后仍在后台跑。
- **建议**：`useEffect` 里起轮询并 `return () => clearInterval(id)`；回调 `try/catch` 且失败计数超阈值后停止并提示。

---

## 2. 前后端契约

### F-13 [P1] `PUT /api/config` 的 `ignored` 字段没有任何前端读取 → 拼错键仍提示「保存成功」

- **位置**：`frontend/src/pages/Settings.tsx:617`、`frontend/src/components/mail/MailServicePage.tsx:206`、`frontend/src/pages/Proxies.tsx:294`
- **证据**：后端 `api/config.py:115-118` 明确把被丢弃的未知键收集起来，`api/config.py:196-200` 返回 `{"ok": true, "updated": [...], "ignored": [...]}`，注释写着「前端可据此提示『这些字段没生效』」。三处调用方都是 `await apiFetch(...)` 后直接 `message.success('保存成功')`，响应体被丢弃。
- **影响**：字段名写错/后端白名单漏加时，用户得到「保存成功」但配置没生效 —— 这正是后端注释里描述过的线上事故形态。
- **建议**：`const res = await apiFetch(...)` 后检查 `res.ignored?.length`，非空则 `message.warning` 列出「未生效的字段」，而不是无条件「保存成功」。

### F-14 [P1] `apiFetch` 对 FastAPI 422 的 `detail` 数组直接 `new Error(array)` → 用户看到 `[object Object]`

- **位置**：`frontend/src/lib/utils.ts:35-44`
- **证据**：`throw new Error(json.detail || text)`。实测 `GET /api/accounts?page=abc` → 422 `{"detail":[{"type":"int_parsing","loc":["query","page"],"msg":"…"}]}`，`String(array)` = `"[object Object]"`。
  可达路径：`Accounts.tsx:1275-1281` 的注册数量用 `<Input type="number" min max>`（HTML `min/max` 不参与 antd 校验），填 `20000` 直接 422；`handleRegister` 又没有 `catch`（见 F-22），于是**连 `[object Object]` 都看不到**。
- **建议**：在 `apiFetch` 里判断 `Array.isArray(json.detail)`，把每项的 `loc.join('.')` 与 `msg` 拼成可读串；同时给 `count/concurrency` 加 `rules={[{ type: 'number', min: 1, max: MAX_REGISTER_COUNT }]}`。

### F-15 [P1] 批量删除账号忽略 `{deleted, not_found}` → 删 0 条也报「批量删除成功」

- **位置**：`frontend/src/pages/Accounts.tsx:460-469`
- **证据**：`await apiFetch('/accounts/batch-delete', …); message.success('批量删除成功')`，不读响应。后端 `api/accounts.py:683-712` 返回 `{deleted, not_found, total_requested}`。同仓 `Proxies.tsx:169-200` 就弹了结果明细，`TaskHistory.tsx:77-80` 也读了 `not_found`。
- **影响**：配合 F-16（跨平台残留选择）会出现「提示成功、实际一条没删」，用户以为已清理。
- **建议**：照抄 `Proxies.batchDel` 的结果弹窗（成功/未找到条数 + id 列表）。

### F-16 [P1] 切平台不清 `page` / `selectedRowKeys`：越界空页 + 跨平台选择残留

- **位置**：`frontend/src/pages/Accounts.tsx:288-290`（`if (platform) setCurrentPlatform(platform)`，无其他重置）、`Accounts.tsx:1252-1255`（`rowSelection` 受控但从不主动清空）
- **证据**：`/accounts/chatgpt` 第 20 页 → 点侧栏切到 `/accounts/grok`（390 条，8 页）→ `page` 仍是 20，`GET …&page=20` 返回空 `items`，界面空白且分页器停在 20。
  勾选几行后切平台，`selectedRowKeys` 保留旧平台的 UUID；此时点「删除选中」会把 A 平台 id + `platform=B` 发给后端 → `deleted: 0`（再叠加 F-15 仍提示成功）。
- **建议**：平台变化时 `setPage(1); setSelectedRowKeys([])`；或在 `load` 里若 `items.length === 0 && page > 1` 自动回落到最后一页。

### F-17 [P2] 任务类型标签不认 `action:*`，界面直接显示原始英文 id

- **位置**：`frontend/src/lib/taskKinds.ts:20-29`、`RunningTasks.tsx:236-243`
- **证据**：`KINDS` 只有 `manual/api/schedule/backfill_rt/bind_2fa/refresh_token/auto_refresh/chatgpt2api_sync`；实测 `GET /api/tasks` 里存在 `source: "action:backfill_refresh_token"`、`"action:probe"`、`"action:check_plus_trial"` 等 11 条 → `taskKindMeta` 回落 `{label: key}`，卡片上显示 `action:backfill_refresh_token`。
- **建议**：`taskKindMeta` 先 `source.startsWith('action:')` 时查一张动作表（`lib/panelComparison.ts` 的 `PanelActionKey` 可复用），未知值再回落。

### F-18 [P2] 日志面板把 `task.source` 强转成 `TaskKind`，`action:*` 任务的统计文案被标成「注册」

- **位置**：`frontend/src/pages/RunningTasks.tsx:421`（`kind={(logTask?.source || 'register') as TaskKind}`）、`frontend/src/components/TaskLogPanel.tsx:25-35,74`
- **证据**：`KIND_TEXT[kind] || KIND_TEXT.register` —— 未知 kind 静默落到「注册」，于是补 RT/探测任务的日志页显示「注册成功：N / 已注册：N」。
- **建议**：`kind` 改用 `taskKindMeta` 归一后的 key，`KIND_TEXT` 补 `batch_action` 一套通用文案。

### F-19 [P2] `POST /accounts` 同邮箱是 upsert，前端一律提示「添加成功」

- **位置**：`frontend/src/pages/Accounts.tsx:471-481`；后端 `api/accounts.py:363-395`
- **证据**：后端 `existing is not None` 时更新并返回既有行（无 `created/updated` 标记），前端无法区分新增与覆盖。
- **影响**：手工录入一个已存在的邮箱会静默覆盖其密码/Token，用户以为「新加了一条」。
- **建议**：后端返回 `{created: bool}` 或在响应头带 `X-Upserted`；前端据此提示「已存在，已更新」。

### F-20 [P2] 备份导出/导入绕开 `apiFetch`，401 不会跳登录

- **位置**：`frontend/src/components/settings/DataBackupPanel.tsx:86`、`:140`
- **证据**：两处用原生 `fetch`（因为响应是 ZIP / 请求体是原始字节），只加了 `Authorization`，没有 `X-Panel-Auth-Required` 的 401 处理逻辑（`lib/utils.ts:28-34`）。
- **影响**：token 过期时导出得到 `{"detail":"令牌已过期…"}` 的 toast，页面不跳登录，用户反复重试。
- **建议**：把 401 判定抽成 `handleAuthFailure(res)`，两处复用。

### F-21 [P2] 前端注释与后端实际契约不一致（列表 `token` 并未裁掉）

- **位置**：`frontend/src/pages/Accounts.tsx:426-432`（注释：「列表响应已把 token 正文裁掉」）、`Accounts.tsx:423`（仍读 `record.token`）
- **证据**：实测单行 `token` 字段 1 990 B 仍在；`api/accounts.py:260-305` 只处理 `extra_json` 与 `password`。
- **影响**：后来者按注释理解会误判「列表不含凭证」，安全评审结论会错。
- **建议**：要么后端真裁（配 `has_access_token`），要么改注释并说明「列镜像 AT 仍随列表下发，grok 列是 SSO」。

---

## 3. 交互缺陷

### F-22 [P1] 危险动作缺 `catch`：失败时用户得不到任何反馈（8 处）

- **位置**：`Accounts.tsx:451-458`（删除）、`460-469`（批量删除）、`471-481`（新增）、`514-568`（注册）、`572-583`（详情保存）；`TaskHistory.tsx:69-83`（批量删除历史）；`Settings.tsx:587-636`（保存配置）；`RegisterTaskPage.tsx:167-229`（提交注册）
- **证据**：全部是 `try { … } finally { setLoading(false) }`，**没有 `catch`**。以 `handleRegister` 为例：`await apiFetch('/tasks/register')` 抛 422 → `finally` 复位 loading → 异常冒泡成 unhandled rejection → 弹窗原地不动、无 toast。
- **影响**：最常见的后端校验失败（数量越界、缺邮箱服务、代理不可用）表现为「点了没反应」。
- **建议**：统一 `catch (e) { message.error(e instanceof Error ? e.message : '操作失败') }`；可抽 `runAction(fn, {success, error})` 高阶函数，顺带统一 loading。

### F-23 [P1] 错误态与空态同形：接口挂了显示「暂无数据」

- **位置**：`Dashboard.tsx:234-245`（`Empty description="暂无账号数据"`）、`RunningTasks.tsx:398-400`（`Empty description="暂无任务记录"`）、`Accounts.tsx:1246-1263`（Table 默认 `暂无数据`）、`TaskHistory.tsx:209-219`（同）
- **证据**：`load` 没有 `error` state（F-07），失败后 `data` 保持 `[]`/旧值 → 渲染分支与「真的没有数据」完全一致。
- **影响**：后端 500 时用户以为「账号被清空了」，是本类审计里最容易被误判为数据丢失的形态。
- **建议**：`load` 增加 `error` state，`error ? <Alert type="error" … action={<Button onClick={load}>重试</Button>}/> : data.length ? … : <Empty/>`。

### F-24 [P1] 长列表：代理表 `pagination={false}` 全量渲染；列定义每次渲染重建

- **位置**：`Proxies.tsx:540-550`（`pagination={false}`，无 `virtual`）、`Proxies.tsx:311`、`Accounts.tsx:820`、`TaskHistory.tsx:112`
- **证据**：代理表一次渲染全部行；`const columns = [...]` 在组件体内定义（未 `useMemo`），父组件每次 `setState`（输入代理地址、切筛选、动作后刷新）都生成新数组 → antd Table 全表重算。
- **影响**：代理上千条时首屏卡顿；`Accounts` 50 行 × 11 列每次筛选都整表 diff。
- **建议**：`pagination={{ pageSize: 50, showSizeChanger: true }}`（或 `virtual` + `scroll.y`）；`columns` 用 `useMemo` 并把它依赖的 handler 用 `useCallback` 固定。

### F-25 [P1] 收银台链接在定时器回调里 `window.open`，会被浏览器拦截

- **位置**：`frontend/src/pages/RegisterTaskPage.tsx:238-240`
- **证据**：`setInterval(async () => { … t.cashier_urls.forEach(url => window.open(url, '_blank')) }, 2000)` —— 不是用户手势，Chrome/Safari 默认拦截多个弹窗。
- **影响**：「注册成功自动打开收银台」在主流浏览器上基本不生效，用户以为没生成链接。
- **建议**：改成页面内的链接列表（`<a target="_blank" rel="noreferrer">`）+「全部打开」按钮（用户手势）。

### F-26 [P1] 长任务日志无上限、O(n²) 追加

- **位置**：`frontend/src/components/TaskLogPanel.tsx:247`（`setLines(prev => [...prev, line])`）
- **证据**：SSE 每行一次浅拷贝；无 `MAX_LINES` 截断。服务端 `api/tasks.py:738-777` 把内存里的 `logs` 全量逐行推送，`core/task_runtime.py` 的日志列表没有 `maxlen` —— 长跑任务可持续输出上千行。
- **影响**：长任务页面越来越卡（每次新行复制整个数组 + 全量重渲染），移动端尤其明显。
- **建议**：保留环形缓冲（`prev.length > 2000 ? prev.slice(-1500) : prev` 再 concat），或把行渲染改成 `useMemo` + 虚拟列表。

### F-27 [P2] 「检测全部」用固定 3 s 猜测后台任务完成，无进度反馈

- **位置**：`Proxies.tsx:207-214`；后端 `api/proxies.py:196-199` 是后台任务
- **证据**：`setTimeout(() => { load(); setChecking(false) }, 3000)` —— 与真实检测时长无关。
- **影响**：代理多时 3 s 后刷新到的还是旧的成功/失败计数，用户重复点「检测全部」。
- **建议**：轮询 `/proxies` 的 `last_checked` 最大值，或后端返回 `job_id` + 进度端点。

### F-28 [P2] 「停止任务」单击即停，无确认

- **位置**：`frontend/src/components/TaskLogPanel.tsx:326-335`
- **证据**：`onClick={handleStopTask}` 直接 `POST /tasks/{id}/stop`（`:118-132`），按钮 `danger` 但无 `Popconfirm`。
- **影响**：查看日志时误点会中断正在跑的批量任务（已成功的号不受影响，但剩余队列停止）。
- **建议**：`Popconfirm title="停止后剩余账号不再执行，确认？"`。

### F-29 [P2] Solver 状态卡：5 s 轮询无可见性判断；重启无确认/无 catch；失败显示「未运行」

- **位置**：`frontend/src/pages/Settings.tsx:252-278`
- **证据**：`window.setInterval(checkSolver, 5000)` 无 `document.hidden` 判断；`restartSolver` 只有 `await apiFetch('/solver/restart')`（无 catch、无 loading、无确认）；`checkSolver` 的 `catch { setRunning(false) }` 把「请求失败」显示成「未运行」。
- **建议**：`catch → setRunning(null)` + 显示「状态未知」；轮询加 `visibilitychange` 暂停；重启加确认与 loading。

### F-30 [P2] 登录页硬编码暗色主题

- **位置**：`frontend/src/pages/Login.tsx:210`（`<ConfigProvider theme={darkTheme}>`）
- **证据**：`AppContent` 用 `localStorage.theme` 切明暗（`App.tsx:177-179,219-222`），登录页不读。
- **影响**：亮色用户被登出后看到一张暗色卡片，视觉割裂；也是「主题」这个单一真相源被绕过的例子。
- **建议**：`theme={localStorage.getItem('theme') === 'light' ? lightTheme : darkTheme}`。

### F-31 [P2] 移动端导航/表格滚动的补丁式可访问性实现过于脆弱

- **位置**：`App.tsx:279-320`（`MutationObserver(document.body, {childList:true, subtree:true})` + 每帧 `querySelectorAll`）
- **证据**：为了给 `.ant-table-content/.ant-table-body` 补 `tabindex/role/aria-label`，对整页 DOM 变更做全量扫描；`RunningTasks` 2.5 s 一次重渲染、`TaskLogPanel` 每行一次 `setState` 都会触发调度。
- **影响**：长会话/大表格下这是持续的 CPU 税；且依赖 antd 内部 class 名，升级即失效。
- **建议**：把这段逻辑收进一个 `<ScrollableTable>` 包装组件（或 `Table` 的 `onHeaderRow` + ref），只在表格挂载/数据变化时跑一次。

---

## 4. 死代码 / 未使用组件 / 重复实现

### F-32 [P2] `lib/chatgptRegistrationMode.ts` 有一半是死代码

- **位置**：`frontend/src/lib/chatgptRegistrationMode.ts:11-33`
- **证据**：全仓仅 `lib/chatgptTokenPlan.ts:10-13` 引用它的两个常量与类型；`CHATGPT_REGISTRATION_MODE_STORAGE_KEY`、`DEFAULT_CHATGPT_REGISTRATION_MODE`、`normalizeChatGPTRegistrationMode`、`loadChatGPTRegistrationMode`、`saveChatGPTRegistrationMode` 零引用（「Token 方案」合并后遗留）。
- **建议**：删掉 5 个死导出，只留两个常量 + 类型；或整文件并入 `chatgptTokenPlan.ts`。

### F-33 [P2] 六处时间格式化并存，语义还不一致

| 实现 | 位置 | 语义 |
|---|---|---|
| `formatLocalTime` / `parseTimeValue` | `lib/time.ts:42-84` | 无时区串按 UTC 解析，再转本地 + 标注 `UTC+8`（唯一正确的口径） |
| `formatSyncTime` | `lib/accountFormat.ts:47-51` | `new Date(value).toLocaleString()`，无时区串按**本地**解析 |
| `formatCreatedAt` | `lib/accountFormat.ts:53-63` | 同上，拆成 date/time 两行 |
| `formatDateTime` / `formatRelativeTime` | `lib/icloud.ts:80-104` | 同上 + 相对时间 |
| 内联 `new Date(text).toLocaleString('zh-CN')` | `pages/TaskHistory.tsx:118` | 同上 |
| `formatDuration` / `toUnixSeconds` | `pages/RunningTasks.tsx:62-90` | 自己一套 epoch 解析 |

- **证据**：`lib/time.ts` 只被 `PanelComparisonPanel.tsx:11` 使用（1 处），其余页面各写各的。
- **影响**：同一条 naive UTC 时间在不同页面相差一个时区（后端 `api/panel_comparison._iso` 已归一成 UTC，但账号/日志接口给的是带 `+00:00` 的串，风险主要在 `formatSyncTime` 这类入口）；维护时改一处漏三处。
- **建议**：全部收敛到 `lib/time.ts`（`formatLocalTime` 已处理「无时区=UTC」），`formatDuration` 也搬进去并加单测。

### F-34 [P2] 过度导出：一批只在本文件使用的符号被 `export`

- **位置**：`lib/finishedTasks.ts:10,16,21`（`FinishedTask`/`FINISHED_PAGE_SIZE`/`needsAttention`）、`lib/pollBackoff.ts:14,16`（两个 `POLL_INTERVAL_*`）、`lib/secretConfig.ts`（`SECRET_SET_SUFFIX`）、`lib/registerRetry.ts`（`MAX_REGISTER_RETRY_TIMES`）等
- **证据**：脚本扫描：全仓 248 个导出符号，其中 **73 个**在定义文件之外零引用（59 个是类型/常量，可接受；**14 个**是小写函数/常量 —— 多数属过度导出，如 `needsAttention`、`parsePlatformField` 仅在本文件内使用；真死代码如 `formatCountryIdList`，全仓只有定义处一行）。
- **影响**：轻微 —— 增加公共 API 面，重构时不敢删。
- **建议**：非必要不 `export`；类型可保留（跨文件契约有用）。

### F-35 [P2] 打包副作用：`pages` 单组让路由级 `lazy()` 实际失效（不是重报 59→5）

- **位置**：`frontend/vite.config.ts:29-48`（`{ name: 'pages', test: /src[\\/]pages[\\/]/ }`）、`frontend/src/App.tsx:24-46`（11 个 `lazy()` + 注释「首屏只付它自己那一份」）
- **证据**：`static/assets` 实际产物只有 5 个 JS：`index 10.7 KB`、`pages 201.8 KB`、`vendor-react 229.8 KB`、`vendor-antd 1 140.4 KB`、`runtime 0.7 KB`（gzip 合计 484 KB，其中 antd 347 KB）。所有页面被合进**一个** `pages` chunk → 首屏仍要下载 `Accounts`(1723 行)/`ICloud`(928)/`PanelComparison`(889) 的全部代码，`lazy()` 只省了「执行」不省「下载」。
- **影响**：首屏 JS 1.51 MB（gzip 473 KB），其中业务页面 200 KB 是全量；弱网首屏 TTI 受损。
- **建议**：保留 59→5 的收敛目标，但把 `pages` 组拆成 `pages-core`（Dashboard/Login/Accounts）与 `pages-rest`（其余），或按 `PAGE_LOADERS` 的 key 用 `manualChunks` 分 3 组；同时把 `App.tsx:24-27` 的注释改成与现状一致（现在写的是「首屏只付它自己那一份」，已不成立）。

### F-36 [P2] 重复实现：`localStorage` 令牌读写绕过 `lib/utils`

- **位置**：`Settings.tsx:342`（`localStorage.setItem('auth_token', …)`）、`Settings.tsx:357`（`removeItem`）、`Login.tsx:29,49`（`setToken` 已用，正确）
- **证据**：`lib/utils.ts:4-14` 已提供 `getToken/setToken/clearToken`，键名硬编码在两处。
- **建议**：统一走 `setToken/clearToken`，键名只留一个常量。

---

## 5. 可访问性与键盘操作

### F-37 [P1] 纯图标按钮没有可访问名

- **位置**：`Proxies.tsx:397-402`（启停代理，只有 `SwapLeft/RightOutlined`）、`Proxies.tsx:410`（删除，`Popconfirm` 内的 `DeleteOutlined`）、`Accounts.tsx:194-199`（行内「更多」，只有 `MoreOutlined`）
- **证据**：这三个 `<Button icon={…}/>` 没有 `aria-label`、没有 `title`、没有可见文字；读屏只念「button」。对比 `TaskHistory.tsx:203`、`PageHeader.tsx:51` 都正确加了 `aria-label`。
- **建议**：`aria-label={record.is_active ? '禁用该代理' : '启用该代理'}`、`aria-label="删除代理"`、`aria-label="更多操作"`。

### F-38 [P1] 代理标签只能鼠标点击（`<Tag onClick>` 不可聚焦）

- **位置**：`Proxies.tsx:332-342`
- **证据**：`<Tag color={…} onClick={() => toggleTag(record, key)}>` —— antd `Tag` 渲染 `<span>`，无 `role="button"`、无 `tabIndex`、无 `onKeyDown`。
- **影响**：键盘用户无法给代理打「注册/绑定」标签，而这是「均衡绑定」的前置条件（`Proxies.tsx:470-478` 的告警会一直存在）。
- **建议**：改用 `Tag.CheckableTag`（本页 `:504` 已经在用）或加 `role="button" tabIndex={0} onKeyDown={e => (e.key==='Enter'||e.key===' ') && toggleTag(...)}`。

### F-39 [P2] 开关/数值输入/可勾选标签没有可访问名，且部分不可键盘操作

- **位置**：`Proxies.tsx:452-456`（`<Switch>` 无 `aria-label`）、`Proxies.tsx:461-466`（`<InputNumber>` 无 `aria-label`）、`Proxies.tsx:504-513`（`<Tag.CheckableTag>`）、`Proxies.tsx:332-342`（`<Tag onClick>`，见 F-38）
- **证据**：antd `Switch` 渲染 `<button role="switch">`、`InputNumber` 渲染 `<input role="spinbutton">`，二者都靠旁边的 `<span>按负载均衡</span>` / `<span>单代理上限</span>` 表达语义，但没有 `aria-labelledby`/`aria-label`，读屏朗读为空名控件。
  `Tag.CheckableTag` 的源码（`node_modules/antd/es/tag/CheckableTag.js:41-44`）是 `<span onClick={handleClick}>` —— 无 `tabIndex`、无 `role`、无键盘事件，与 F-38 同源。
- **影响**：键盘用户无法在「添加代理」时选择标签（只能接受默认标签），读屏用户听到两个无名开关。
- **建议**：`<Switch aria-label="按负载均衡绑定" />`、`<InputNumber aria-label="单代理绑定上限" />`；标签选择改用 `Checkbox.Group`（antd 原生支持键盘）或在 `Tag` 上补 `role/tabIndex/onKeyDown`。

### F-40 [P2] 筛选控件只有 placeholder，没有可访问名；分页缺 `showTotal`

- **位置**：`Accounts.tsx:1209-1243`（4 个 `Select`/`DatePicker` 只有 `placeholder`）、`Proxies.tsx:496-501`（地区输入）、`TaskHistory.tsx:177-185`
- **证据**：antd `Select` 的 `placeholder` 不进入 `aria-label`，读屏朗读为「combobox」；`DatePicker` 同理。搜索框是唯一加了 `aria-label` 的（`Accounts.tsx:1202`）。
- **建议**：逐个加 `aria-label="状态筛选"` / `"Plus 试用筛选"` / `"开始时间"` / `"结束时间"`；分页加 `showTotal`（形如「1-50 / 共 1661 条」）。

> 正面：`App.tsx:488` 有 skip-link，`index.css:599-601` 有 `:focus-visible` 描边，`App.tsx:279-295` 给可横向滚动的表格补了 `tabindex/role=region`，`PageHeader.tsx:51` 汉堡按钮有 `aria-label`。这几处不用改。

---

## 6. 工具链真实输出

### 6.1 `npx tsc --noEmit`

```
$ cd frontend && npx tsc --noEmit
（无输出）
exit code = 0
```

**0 error、0 warning、0 行输出。** 类型层面是干净的（这也说明本报告的多数问题不是「类型能拦住」的那类：`any` 把契约错误放行了）。

### 6.2 `npm run lint`

```
$ npm run lint        # eslint .
✖ 39 problems (34 errors, 5 warnings)
exit code = 1
```

- 涉及文件：**12 个**
- 按规则：`@typescript-eslint/no-explicit-any` **30**、`react-hooks/exhaustive-deps` **5**、`react-hooks/set-state-in-effect` **2**、`react-refresh/only-export-components` **1**、`no-empty` **1**
- 按文件：

| 文件 | 条数 | 主要规则 |
|---|---|---|
| `src/pages/Accounts.tsx` | 15 | `no-explicit-any` ×15（`columns: any[]`、`accounts: any[]`、`currentAccount: any`） |
| `src/pages/Settings.tsx` | 10 | `no-explicit-any` ×8、`no-empty`(326)、`set-state-in-effect`(271) |
| `src/App.tsx` | 3 | `exhaustive-deps`(147)、`no-explicit-any`(230)、`set-state-in-effect`(249) |
| `src/pages/Login.tsx` | 2 | `no-explicit-any`(32,51) |
| `src/components/settings/MailImportPanel.tsx` | 2 | `exhaustive-deps`(284,620) |
| `src/pages/Dashboard.tsx` | 1 | `no-explicit-any`(92) |
| `src/pages/RunningTasks.tsx` | 1 | `no-explicit-any`(250) |
| `src/pages/RegisterTaskPage.tsx` | 1 | `no-explicit-any`(78) |
| `src/pages/PanelManagement.tsx` | 1 | `exhaustive-deps`(169) |
| `src/components/settings/PanelConfigPanel.tsx` | 1 | `exhaustive-deps`(328) |
| `src/components/settings/ConfigPanels.tsx` | 1 | `react-refresh/only-export-components`(160) |
| `src/components/account/AccountDetailParts.tsx` | 1 | `no-explicit-any`(91) |

值得注意的两条 `set-state-in-effect`：`App.tsx:249`（切路由时 `setNavOpen(false)`，React 官方建议在渲染期调整或改用派生 state）与 `Settings.tsx:271`（`checkSolver()` 在 effect 内同步触发 setState）。这两条本身不是 bug，但 `App.tsx` 已经在 `:261-265` 用「渲染期调整」正确解决了同类问题，两处写法不一致。

### 6.3 构建产物（只读核对，未重新 build）

```
static/assets/index-*.js            10 739 B   (gzip   3 834)
static/assets/pages-*.js           201 839 B   (gzip  59 880)
static/assets/vendor-react-*.js    229 841 B   (gzip  72 732)
static/assets/vendor-antd-*.js   1 140 405 B   (gzip 347 182)
static/assets/rolldown-runtime-*.js    716 B   (gzip     459)
合计 1 583 540 B / gzip 484 087 B，JS 文件 5 个
```

与「59 → 5 个 chunk」的修复一致；但见 F-35：`pages` 单组让 11 个页面的 `lazy()` 只剩「延迟执行」意义。

---

## 7. 已修项复核（不重复上报）

| 既有修复 | 复核结果 | 证据 |
|---|---|---|
| 打包 59 → 5 个 chunk | ✅ 生效 | `static/assets/*.js` 恰好 5 个；`vite.config.ts:29-48` 的 `codeSplitting.groups` |
| 空闲预取 | ✅ 生效 | `App.tsx:202-217`（`requestIdleCallback` + `setTimeout` 兜底，`PAGE_LOADERS` 共用 `import()` 缓存） |
| `/api/tasks` ETag | ✅ 生效 | `curl -D -` 返回 `etag: "381ace…"` + `cache-control: no-cache`；同参数二次请求由浏览器缓存层返回 304 并复用 200 体 |

> 附带说明：`main.tsx:13` 开了 `StrictMode`，开发模式下每个 effect 会执行两次 —— 本文的「重复请求」结论均已排除 StrictMode 因素（对比的是不同 effect / 不同组件，不是同一 effect 的双跑）。

---

## 8. 建议修复顺序

1. **今天**：F-01/F-02（UUID `Number()` 强转：`Accounts.tsx` 5 处 + 导出弹窗 1 处）—— 数据外泄与批量功能全废，改动最小、收益最大。
2. **今天**：F-04/F-05（批量删除、关闭密码/2FA 的二次确认）—— 纯 UI，零风险。
3. **本周**：F-03（任务历史服务端分页）、F-13/F-15/F-16（契约字段与平台切换状态）、F-22（统一 `catch`）。
4. **本周**：F-06/F-07（竞态 + 错误态），抽 `useListQuery` 一次性覆盖 6 个页面。
5. **下周**：F-09（列表瘦身/分页）、F-24（长列表）、F-33（时间格式化收敛）、F-37~F-39（a11y）。
6. **随手**：F-32/F-34/F-36（死代码与重复实现）、F-35（chunk 分组 + 注释订正）、lint 的 30 个 `any`（先补 `Account`/`TaskSnapshot`/`ProxyRow` 三个类型，能一次消掉 20+）。

---

## 附：证据与口径

- 静态阅读：`frontend/src` 全部 48 个文件；重点页面逐行读完（`Accounts.tsx` 1723 行、`Settings.tsx` 705、`Proxies.tsx` 554、`RunningTasks.tsx` 425、`TaskHistory.tsx` 223）。
- 只读实测（`127.0.0.1:8000`，仅 GET）：
  - `GET /api/accounts?platform=chatgpt&page=1&page_size=1` → 单行 3 436 B，`token` 1 990 B，`id` 为 UUID
  - `GET /api/accounts?platform=chatgpt&page=1&page_size=50` → 242 653 B
  - `GET /api/tasks/logs?page=1&page_size=50` → `total: 1661`
  - `GET /api/tasks` → 837 条 / 374 349 B，含 11 条 `source: action:*`
  - `GET /api/icloud/aliases` → 1 941 条 / 712 390 B，`pool.total: 1941`
  - `GET /api/proxies` → 5 条 / 926 B；`GET /api/proxies/balance` → `bindable_count: 0`、`bound_accounts: 1057`
  - `GET /api/accounts?page=abc` → 422，`detail` 为数组（用于验证 F-14）
  - `GET /api/accounts/stats` → `{total: 1057, by_platform: {grok: 390, chatgpt: 667}, by_status: {registered: 618, banned: 421, expired: 13, invalid: 5}}`
- 未执行任何写操作（POST/PUT/PATCH/DELETE 一律未调用）；未修改源码；唯一写入文件为本报告。
