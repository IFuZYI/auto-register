# Task 9 — 前端：页面动作接线与状态映射
状态: 完成

范围：`frontend/src/pages/`、`frontend/src/lib/`、`frontend/src/components/`、
`frontend/scripts/run_*.mjs`、`frontend/src/api/*.ts`。**只读审计**：除本报告外未修改任何
仓库文件；临时脚本放在 `/tmp/t9/`（不入库）。

执行证据三件套：

1. `cd frontend && npx tsc -b` → **exit 0**（前端能构建，无类型错误）。
2. `cd frontend && for f in scripts/run_*.mjs; do node "$f"; done` → **29/29 exit 0**，
   合计 355 条断言全绿（占比见 T9-4：其中 16 个脚本是文本正则断言，不是可执行行为断言）。
3. 本次新增的两条机械检查（临时脚本，未入库）：
   - `/tmp/t9/match3.py`：从 `src/**/*.ts(x)` 抽出 **116 个 `apiFetch(...)` 调用点**（方法 +
     路径），与 `api/*.py` + `main.py` 的 **114 条 FastAPI 路由**（110 + 4）做（method, path）比对；
   - `/tmp/t9/check_status_parity.py`：把 `core/base_platform.py::AccountStatus` 的 5 个
     枚举值与前端每一处状态清单逐项比对。

## 结论

**路由接线是干净的。** 116 个前端调用点逐一比对后端路由表：**0 条调用不存在的路由、
0 条方法不匹配、必填参数无缺失**。模板路径（`${kind}` / `${panelKey}` / `${workflow}`）里
只有 `/sms/${kind}` 需要取值域证明（`kind: 'balance' | 'countries'` → 两条路由都在）。

**问题集中在「界面承诺的动作」与「实际发生的动作」之间的缝，不集中在路由表：**

- 批量删除的勾选集合会**跨平台残留**：确认文案写「删除选中 N 个」，实际发给后端的是
  `platform=<新平台>` + `<旧平台>的 N 个 id`，后端 `deleted: 0`，前端仍弹「已删除 N 个账号」
  （T9-1，已用 TestClient 复现 `200 {"deleted":0,"not_found":[...3]}`）。
- **10 处异步动作失败时界面上什么都不会发生**（T9-2）：既没有 toast，也没有行内错误 ——
  因为 `apiFetch` 对非 2xx 一律 reject（已复现），而这些处理函数外层没有任何 `catch`。
  其中最贵的是「全局配置保存」与「邮箱设置保存」：保存失败后按钮弹回、页面不动，
  用户以为已保存。
- 「访问密码」状态读取失败被 `catch {}` 吞掉，页面把 `null` 渲染成 **「未启用」**
  （T9-3）—— 这是把「读不到」误报成「没设密码」，属于安全相关的错误陈述。
- 剩下的「验证缺口」本身是 Important（T9-4）：29 个脚本里 16 个是 `readFileSync` + 正则，
  没有一条断言「每个异步处理函数必须有可见失败通道」，所以上面 10 处从来没被拦住。

## 发现

### T9-1 · Important · 批量删除：勾选跨平台残留，确认文案与实际删除集合不一致，且成功提示谎报

- **file:line**：`frontend/src/pages/Accounts.tsx:551-575`（`handleBatchDelete`，
  第 566 行发 `platform: currentPlatform` + `ids: Array.from(selectedRowKeys)`，
  第 568 行无条件 `message.success(\`已删除 ${selectedRowKeys.length} 个账号\`)`）；
  勾选状态 `frontend/src/pages/Accounts.tsx:254`（`selectedRowKeys` 只在第 569 行被清空，
  `currentPlatform` 变化时**没有**清空 —— 平台切换只走 `useEffect` 第 308-310 行）。
- **复现**（`/tmp/t9/probe.py`，临时 SQLite 库 + `TestClient`，未动生产库）：
  1. chatgpt 平台库插 3 行；
  2. 模拟「在 chatgpt 页勾 3 行 → 切到 grok 页 → 点删除选中 3 个」；
     `POST /api/accounts/batch-delete {"ids":[3 个 chatgpt uuid],"platform":"grok"}`。
- **实测**：
  ```
  POST batch-delete wrong platform -> 200 {"deleted": 0, "not_found": [3 ids], "total_requested": 3}
  DELETE wrong platform           -> 404 {"detail": "账号不存在"}      # 单删那条路径是可见的
  POST batch-delete right platform -> 200 {"deleted": 2, "not_found": []}
  ```
  前端逻辑不看响应体，所以 200 + `deleted: 0` = **「已删除 3 个账号」**，同时清空勾选
  → 用户没有任何补救线索。同类端点 `Proxies.batchDel`（`Proxies.tsx:210-232`）与
  `TaskHistory.handleBatchDelete`（`TaskHistory.tsx:96-105`）都展示了 `not_found`
  计数，**只有账号页不展示**，三处反馈不一致。
- **机制佐证**：antd `Table` 的 `checkStrictly` 默认 `true`
  （`node_modules/antd/es/table/hooks/useSelection.js:52,138` → `derivedSelectedKeys =
  mergedSelectedKeys`），数据源变化**不会**剪掉不在当前 `dataSource` 里的 key；
  组件实例在 `/accounts/:platform` 参数变化时被 React Router 复用，state 全保留。
- **blast radius**：所有多平台账号页（chatgpt / grok / kiro）。批量删除不可恢复，且
  误导方向是「以为删掉了、其实一个没删」—— 用户下一步往往去别处找这批号，或对另一批
  号重复操作。若某平台 id 空间与平台库重叠（`core/db/repository.py:895-928` 的
  `enforce_platform` 分支是唯一保护），出错方向会从「什么都没删」变成「删错库」，
  不过本次复现是前者。
- **fix**：`currentPlatform` 变化时 `setSelectedRowKeys([])`（与 `TaskHistory.tsx:224-232`
  换平台即清勾选的做法对齐）；`handleBatchDelete` 读响应体，`deleted !== ids.length` 时
  按 `not_found` 报 warning（照抄 `Proxies.tsx:219-232` 的结果弹窗），只有
  `deleted > 0` 才说「已删除」。

### T9-2 · Important · 10 处异步动作失败后界面上没有任何反馈（静默失败簇）

- **共同机制（已复现）**：`frontend/src/lib/utils.ts:20-46` 的 `apiFetch` 对 `!res.ok`
  一律 `throw new Error(json.detail || text)`。`/tmp/t9/apifetch_probe.mjs` 实测：
  ```
  THROWS: db is locked                 # 500 → reject
  THROWS: 未知的导出格式: x            # 400 → reject
  ```
  而这些处理函数外层**没有 `catch`**，所以 reject 变成无人接管的 Promise；所用容器
  （antd `Popconfirm` / `Modal`、rc-field-form 的 `onFinish`、原生 `onClick`）都不会为
  失败显示任何东西：
  - `node_modules/antd/es/_util/ActionButton.js:47-62`：reject 时只 `setLoading(false)`，
    然后 `return Promise.reject(e)` —— 该返回值被丢弃，无 UI；
  - `node_modules/antd/es/modal/components/NormalOkBtn.js`：`onClick={onOk}`，返回值直接丢弃；
  - `node_modules/rc-field-form/es/useForm.js:859-869`：`onFinish(values)` 只被同步
    `try/catch` 包住，async 处理函数的 rejection 逃出 try。
- **枚举**（`grep`+AST 扫描，`/tmp/t9/silent.py`；67 个"含 api 调用的 async 函数"里
  12 个无 catch，去掉 2 个纯数据函数后如下）：

  | # | file:line | 处理函数 | 触发入口 | 失败时用户看到 |
  |---|---|---|---|---|
  | 1 | `pages/Settings.tsx:770` | `save`（全局配置保存） | 按钮 `:894` | 无（`try/finally`，无 catch） |
  | 2 | `components/mail/MailServicePage.tsx:173` | `save`（邮箱设置保存） | 按钮 `:240` | 无（`try/finally`，无 catch） |
  | 3 | `pages/Accounts.tsx:542` | `handleDelete` | `Popconfirm :1514` | 无 |
  | 4 | `pages/Accounts.tsx:580` | `handleAdd` | `Modal onOk :2020` | 无 |
  | 5 | `pages/Accounts.tsx:623` | `handleRegister` | `Form onFinish :1799` | 无（弹窗不关、无提示） |
  | 6 | `pages/TaskHistory.tsx:93` | `handleBatchDelete` | `Popconfirm :214` | 无 |
  | 7 | `pages/Proxies.tsx:239` | `toggle`（代理启停） | `onClick :497` | 无 |
  | 8 | `pages/Settings.tsx:412` | `restartSolver` | `onClick :444` | 无 |
  | 9 | `pages/RegisterTaskPage.tsx:176` | `submit`（开始注册） | `Form onFinish :342` | 无 |
  | 10 | `pages/RegisterTaskPage.tsx:240` | `pollTask` | `setInterval :242` | 无（且定时器不复位） |

  另有两处**有意为之**、不计为缺陷：`TaskHistory.tsx:131` / `RunningTasks.tsx:211`
  的 `await load().catch(() => {})`（先成功后刷新，注释已说明）；`App.tsx:225`
  的 chunk 预取 `catch(() => {})`；`App.tsx:526` 的退出登录 `catch(() => undefined)`
  （`finally` 里必清本地会话，注释已说明）。
- **blast radius**：直接命中「保存配置」「删账号」「开始注册」这三条最高频路径。
  #1/#2 尤其麻烦：`PUT /api/config` 会因未知键、口令空串、SQLite 类型等原因失败
  （`api/config.py:107-140`），失败后表单看起来"已经保存过了"，下一次再点保存时
  用户会以为上次的改动已生效。
- **fix**：统一给这些入口补 `catch` + `message.error`（照 `Accounts.tsx:571-575`、
  `Proxies.tsx:156-160` 既有写法）；#10 另需在 catch 里 `clearInterval` 并复位
  `polling`，否则失败后界面永远停在「正在注册」。

### T9-3 · Important · 「访问密码」状态读取失败被吞掉，渲染成「未启用」（安全相关的错误陈述）

- **file:line**：`frontend/src/pages/Settings.tsx:468-473`
  （`loadStatus` 里 `catch {}`）；渲染处 `:608-620`：`status?.has_password ? 已启用 : 未启用`。
  `status` 初值 `null`（`:457`），所以**任何**读取失败都落到 else 分支。
- **复现**：`apiFetch('/auth/status')` 在 401/500 时 reject（见 T9-2 的 probe）；此时
  `setStatus` 从不执行，`status === null`，页面渲染
  `<Tag color="default">未启用</Tag>`，并且**不显示**「关闭密码保护」按钮 —— 于是
  用户既看不到真实状态，也失去了关闭入口，还得到一个"面板没有密码保护"的错误结论
  （`api/auth.py:312-318` 表明该端点就是唯一状态源）。
- **blast radius**：面板安全门的状态显示。运维据此判断"要不要设密码"时会被直接误导；
  同时这也是 T9-2 里唯一**读出错误结论**（而不只是"没提示"）的一处。
- **fix**：`catch` 里写入一个 `statusLoadError` state，渲染成
  「状态未知（读取失败：…）＋ 重试」而不是「未启用」；与 `MailServicePage.tsx:159-164`
  的 `loadFailed` 处理保持同一口径。

### T9-4 · Important · 验证缺口：29 个脚本里 16 个是正则断言，没有任何检查覆盖「处理函数必须有失败通道」

- **file:line**：`frontend/scripts/`（29 个文件）。机械分类（`await import(` 判定）：
  **13 个可执行行为断言**（rolldown 打包 `src/lib/*.ts` 后真实调用纯函数，如
  `run_account_format_checks.mjs` 的 53 条、`run_pool_status_checks.mjs` 的 35 条），
  **16 个是对源码文本做 `readFileSync` + 正则**（`run_silent_failure_checks.mjs`、
  `run_batch_capability_checks.mjs`、`run_authorized_bulk_checks.mjs`、
  `run_all_actions_are_tasks_checks.mjs` 等）。
- **后果（可复现）**：`scripts/run_silent_failure_checks.mjs` 自述"界面不能对失败保持沉默"，
  但它只钉住了 2 个代码块（`handleDetailSave`、`handleBatchDelete`，见该文件第 20-30 行），
  于是 `node scripts/run_silent_failure_checks.mjs` → `{"checked":11,"failures":[]}`
  的同时，T9-2 的 10 处静默失败**一个都没被发现**。同理，仓库里**不存在**任何
  「前端 `apiFetch` 调用 ↔ 后端路由表」的契约检查；本次的 116×114 比对是临时脚本
  （`/tmp/t9/match3.py`），没有被 CI 固化 —— 所以"路由接线正确"目前只是本次结论，
  不是可持续的护栏。
- **blast radius**：这些脚本给了"已覆盖"的错觉；`package.json` 的 `check`
  脚本（`for f in scripts/run_*.mjs; do node "$f" || exit 1`) 会全绿，于是新增一个
  没有 catch 的处理函数、或删掉一条后端路由，都不会让 CI 变红。
- **fix**：把本次两条机械检查固化为 `scripts/run_route_contract_checks.mjs`
  （解析 `src/**/*.ts(x)` 的 `apiFetch` 与 `api/*.py` 的装饰器，做 method+path 比对）
  和 `scripts/run_async_failure_checks.mjs`（AST 级：每个含 `apiFetch` 的 async 函数
  必须在自身或最近的调用点有 `catch` + 可见提示）；正则断言逐步换成行为断言。

### T9-5 · Minor · 两处清理后的列表刷新失败被吞掉（与 T9-2 分开，因为成功路径已提示）

- **file:line**：`frontend/src/pages/TaskHistory.tsx:131`、`frontend/src/pages/RunningTasks.tsx:211`
  （`await load().catch(() => {})`）。清理本身成功并已 toast，刷新失败只让列表停留在旧数据。
- **blast radius**：低 —— 用户会看到"已清理 N 条"但列表还列着那些行；再点一次会得到
  `deleted: 0`，仍在可理解范围。注释已说明这是有意的取舍，故只记为 Minor。
- **fix**：失败时补一句 `msg.warning('清理成功，但列表刷新失败，请手动刷新')`。

### T9-6 · Minor · iCloud 登录会话取消失败静默

- **file:line**：`frontend/src/components/icloud/ICloudLoginModal.tsx:89`
  （`cancelICloudLogin(login.login_id).catch(() => {})`）。
- **blast radius**：低 —— 只在关闭弹窗时清理服务端会话；失败会留下一个悬挂的
  login-session（`DELETE /api/icloud/login-sessions/{id}` 存在且可用）。用户主动"取消
  登录"却仍留会话，属于清理不彻底而非数据风险。
- **fix**：`catch` 里 `void` 记一条 `message.warning('已关闭弹窗，但服务端会话未能取消')`，
  或延长 session TTL 作为兜底。

## hypothesis

### H-1 · AuthorizedAccounts 状态筛选缺 `limited`（未证实可达）

`frontend/src/pages/AuthorizedAccounts.tsx:702-707` 的筛选项只有
`registered/invalid/banned/expired` 四个值，机械比对输出：

```
backend AccountStatus values: ['banned', 'expired', 'invalid', 'limited', 'registered']
src/pages/Accounts.tsx: 10 occurrences -> [registered, expired, invalid, limited, banned] ×2 块  ✅
src/pages/AuthorizedAccounts.tsx: 4 occurrences -> [registered, invalid, banned, expired]
  MISSING states: ['limited']
```

**为什么只是 hypothesis**：`limited` 由 `services/chatgpt_account_state.py:26` 与
`core/base_platform.py:42` 定义，但 AWS / Kiro 的探测策略
（`platforms/aws/plugin.py:311-326`、`platforms/kiro/plugin.py:546-560`）只会落
`registered/banned/invalid`，面板同步（`services/panel_status_sync.py:85-86`）也只挂在
chatgpt / grok 插件上。**没有找到让 aws 行出现 `status=limited` 的写入路径**，所以这是
"清单不全"，未必是可达缺陷。若将来 AWS 侧引入限流落库，这个筛选会立刻变成漏筛。
建议顺手补齐第五项（零风险的对称性修复）。

### H-2 · Kiro 导出跨平台勾选会 400（与 T9-1 同源，但方向可见）

`frontend/src/pages/Accounts.tsx:992-994` 把 `selectedRowKeys` 原样当作 `account_ids`
发给 `/accounts/export-text`（`format: 'kiro_auth_token'`）。若勾选是在切换到 kiro 页
之前留下的（同 T9-1 的机制），后端 `api/accounts.py:556-560` 会以
`400 没有可导出的 Kiro 账号（筛选结果为空？）` 拒绝 —— 这次**有**可见 toast
（`Accounts.tsx:1023`），所以只记为 hypothesis：错误方向是安全的（不导出），
但错误文案会误导（"筛选结果为空"而实际是勾选跨平台）。

### H-3 · 其余页面状态渲染未做全量枚举（未发现缺陷）

`Dashboard.tsx:19-28` 的 `statusColor`、`Dashboard.tsx:171`、`Accounts.tsx:1286-1297`
（表格状态列）、`Accounts.tsx:1059`（恢复预览，`accountStatusMeta`）、`Accounts.tsx:2174`（详情提示）、
`PanelComparisonPanel.tsx:210` 全部走 `lib/accountFormat.ts:173-190` 的
`accountStatusMeta`，五个值齐备且文案一致（正常/过期/失效/限流/禁用）。唯一一处
渲染原始枚举的是 `Accounts.tsx:2370` 的 `<Tag>{status || '异常'}</Tag>`，但那里的
`status` 是 grok 的 `extra.probe_status`（HTTP 码或 `refresh_ok`），**不是**账号状态
枚举，注释（`:1431-1433`）已说明取意。不构成缺陷。

## 覆盖清单（按钮→路由 / 状态映射 / 批量作用域 / 失败反馈）

### 1. 按钮 → 后端路由（116 个调用点 × 114 条路由）

| 判定 | 数量 | 说明 |
|---|---|---|
| 路由存在且方法匹配 | 112 | 含所有页面主按钮 |
| 模板路径需取值域证明 | 1 | `Settings.tsx:347` `POST /sms/${kind}`，`kind: 'balance' \| 'countries'`（`:343` 的联合类型）→ `POST /api/sms/balance`(:87)、`POST /api/sms/countries`(:105) 均存在 |
| 路径经变量间接 | 3 | `TaskHistory.tsx:119`→`/tasks/logs/cleanup`、`RunningTasks.tsx:200`→`/tasks/runs/cleanup`（两条 POST 路由都在）、`api/icloud.ts:145` 的 `post()` 泛型助手（`apiFetch(path,...)`）、`lib/utils.ts:20` 的 `apiFetch` 定义本身 |
| **不存在的路由** | **0** | — |
| **方法不匹配** | **0** | — |
| **必填参数缺失** | **0** | 按 pydantic 必填字段核过：`RegisterTaskRequest.platform`（3 处调用都带）、`BatchActionTaskRequest.platform+action_id`（5 处都带）、`BatchDeleteRequest.ids`（2 处都带）、`AccountCreate.platform+email+password`（`handleAdd` 带 `...values` 且表单 `rules: required`）、`ProxyCreate.url` / `ProxyBulkCreate.proxies` / `ProxyBatchDelete.ids` |

另 6 个**原生 `fetch`** 调用（不走 `apiFetch`，因此不在 116 之内）也逐条核过，全部命中
既有路由：`App.tsx:148,246` `GET /api/auth/status`、`Login.tsx:26,50`
`POST /api/auth/login` / `POST /api/auth/verify-totp`、`DataBackupPanel.tsx:86,140`
`GET /api/backup/export`（二进制 ZIP）/ `POST /api/backup/import`（`Content-Type:
application/zip`，非 multipart —— 与 `api/backup.py:57` 的约定一致）。SSE 流
`TaskLogPanel.tsx:200` `GET /api/tasks/{id}/logs/stream?since=` 带 `Authorization` 头，路由存在。

复现命令：

```bash
cd /data/workspace/Register/frontend
npx tsc -b                       # exit 0
python3 /tmp/t9/match3.py        # TOTAL 116 BAD 1（仅 /sms/${kind}，见上表）
python3 /tmp/t9/rawfetch.py      # 6 条原生 fetch，全部命中既有路由
```

代表性「按钮 → 路由」抽样（每条都在这 112 条里）：注册账号
（`RegisterTaskPage.tsx:219` → `POST /api/tasks/register`）、单账号动作
（`Accounts.tsx:129` → `POST /api/tasks/batch-action`，`account_ids:[id]`）、批量检测
（`:793`）、刷新 Token（ChatGPT 走 `/tasks/refresh-token` `Accounts.tsx:956`，其它平台走
`/tasks/batch-action` `:961` 起）、补 RT（`Accounts.tsx:852`）、补 2FA（`Accounts.tsx:910`）、恢复异常号
（`:1047` 预览 + `:1077` 执行，均在 `Accounts.tsx`）、代理启停（`Proxies.tsx:240` → `PATCH /proxies/{id}/toggle`）、
备份导出/导入（`DataBackupPanel.tsx:86,140` → `GET /backup/export`、`POST /backup/import`）、
面板推送（`PanelComparisonPanel.tsx:678` → `POST /integrations/panels/{k}/push`）。

### 2. 状态标签映射（后端 5 值：registered/expired/invalid/banned/limited）

| 渲染点 | 映射来源 | 五值齐备 |
|---|---|---|
| 账号表格状态列 `Accounts.tsx:1289` | `accountStatusMeta` | ✅ |
| 详情「本地状态」下拉 `Accounts.tsx:2165-2169` | 字面量清单 | ✅ |
| 状态筛选 `Accounts.tsx:1702-1706` | 字面量清单 | ✅ |
| 恢复预览 `Accounts.tsx:1059` | `accountStatusMeta` | ✅ |
| 工作台状态色 `Dashboard.tsx:19-28` + 标签 `:171` | `accountStatusMeta` + 自有色表 | ✅ |
| 授权账号树 `AuthorizedAccounts.tsx:139,128,166` | `accountStatusMeta` | ✅ |
| 授权账号状态筛选 `AuthorizedAccounts.tsx:702-707` | 字面量清单 | ❌ 缺 `limited`（H-1） |
| 面板对比 `PanelComparisonPanel.tsx:210` | `accountStatusMeta` | ✅ |

`lib/accountFormat.ts:173-190` 是唯一标签源（正常/过期/失效/限流/禁用），**未发现任何
页面渲染原始英文枚举**；旧的「已注册/已过期/已封禁」文案已彻底清除（`trial`/`subscribed`
也只在能力清单与注释里出现，不在状态映射里）。

### 3. 批量作用域（`selected` vs `all_filtered`）

| 入口 | 提示文案 | 实际发送 | 一致 |
|---|---|---|---|
| 批量删除 `Accounts.tsx:555`（文案）`/`:566`（ids） | `删除选中的 N 个账号？` | `ids: Array.from(selectedRowKeys)` + `platform: currentPlatform` | 数量一致、**集合可能跨平台**（T9-1） |
| 批量检测（probe）`Accounts.tsx:1536-1551`（文案）`/`:757-781`（body） | `批量检测所选账号 (N)` / `批量检测当前筛选 (total)` | `account_ids: [N 个字符串]` / `all_filtered:true` + 5 个筛选键 | ✅ |
| Plus 试用 `:1544-1551` | `检测所选 Plus 试用资格 (N)` / `检测当前筛选 Plus 试用资格 (total)` | 同上 | ✅ 且非 chatgpt 时 `return`（`Accounts.tsx:745`） |
| 补 RT `:1866-1867`（文案）、`:836-846`（body） | `处理所选 N 个账号` / `处理当前筛选的 total 个账号（本页缺 RT m 个）` | 同 selected/all 分支 | ✅ |
| 刷新 Token `:1929-1930`（文案）、`:934-956`（body） | `处理所选 N` / `处理当前筛选的 total` | 同分支 + 平台无关动作 id | ✅ |
| 补绑 2FA `Accounts.tsx:1977-1979`（文案）、`:890-910`（body+请求） | `将处理所选 N 个账号中尚未绑定 2FA 的…` | 同分支 + `only_missing_2fa:true` | ✅ |
| Kiro 导出凭据 `:1017-1021`（文案）、`:992-993`（body） | `已导出 N 个账号的凭据` / `已导出当前筛选的 Kiro 凭据` | `account_ids` / 5 个筛选键 | 数量一致、集合可能跨平台（H-2） |
| AWS 授权页批量 `AuthorizedAccounts.tsx:325-352` | `批量测活` / `批量刷新凭据`（该页无行选择） | 恒 `all_filtered:true` + `email`/`status` | ✅ 与「作用域=筛选结果」的页面语义一致 |
| 代理批量删除 `Proxies.tsx:207-232` | `确认删除选中的 N 条代理？` (`:648`) | `ids: selectedRowKeys.map(Number).filter(isFinite)` | ✅ 且展示 `not_found` |

`selected` 分支的 id 归一都正确用了 `String(value)`（不是 `Number`，UUID 不会被过滤成
空集）—— `:766-767`、`:839-840`、`:938-939` 三处注释点名的 F-01/F-02 回归已修复。

### 4. 失败反馈（每个异步动作是否有可见失败提示）

| 动作 | 失败可见 | 证据 |
|---|---|---|
| 列表加载（Accounts / Proxies / RunningTasks / TaskHistory） | ✅ | `Accounts.tsx:366-372` + 常驻 `Alert :1755-1764`、`Proxies.tsx:156-160`、`RunningTasks.tsx:130-134`、`TaskHistory.tsx` 的 `loadError` |
| 详情加载 / 详情保存 | ✅ | `Accounts.tsx:534-537`（`detailLoadError`）、`:700-702`（`message.error`） |
| 批量动作（检测/补RT/刷新/2FA/恢复/导出/删除） | ✅ | `:806-812`、`:858-860`、`:971-973`、`:917-919`、`:1084-1086`+`:1089-1090`、`:1022-1024`、`:571-575` |
| 动作下拉、平台列表、面板列表 | ✅ | `Accounts.tsx:382-393`、`App.tsx:250-260`+`run_silent_failure_checks` ④ |
| iCloud 账号/别名加载、生成、删除、启停 | ✅ | `ICloud.tsx:126-128`、`withBusy:140-151` |
| 邮箱设置读取失败 | ✅ | `MailServicePage.tsx:159-164` → `:227-234` 行内「重新读取设置」 |
| 代理增删、启停（批量）、检测、复活、标签 | ✅（启停单条除外，见 T9-2 #7） | `Proxies.tsx:191`、`:202`、`:235`、`:273`、`:332`、`:382`、`:403` |
| **全局配置保存 / 邮箱设置保存 / 单删账号 / 手动新增 / 开始注册 / 注册轮询 / 任务历史批量删除 / 代理单条启停 / Solver 重启** | **❌ 静默** | **T9-2 表（10 处）** |
| 访问密码状态读取 | ❌ 且**读出错误结论** | T9-3 |
| 退出登录（服务端吊销失败） | ✅（有意） | `App.tsx:526-529` 注释 + `finally` 必清本地 |

### 5. 脚本执行记录（29/29 exit 0）

| 脚本 | exit | checked | 证明了什么 |
|---|---|---|---|
| `run_account_format_checks.mjs` | 0 | 53 | 真实执行 `lib/accountFormat.ts` 的纯函数（打包后 import），断言格式化与状态元数据 |
| `run_pool_status_checks.mjs` | 0 | 35 | 真实执行号池状态纯函数 |
| `run_panel_filter_checks.mjs` | 0 | 36 | 面板过滤纯函数 |
| `run_task_kind_checks.mjs` | 0 | 20 | 任务种类映射纯函数 |
| `run_dashboard_data_checks.mjs` | 0 | 18 | 工作台数据装配 |
| `run_panel_filter_driven_checks.mjs` | 0 | 14 | 面板过滤驱动 |
| `run_batch_capability_checks.mjs` | 0 | 13 | **文本断言**：批量菜单由能力推导、走 `/tasks/batch-action`、`selected` 优先 |
| `run_chatgpt_token_plan_checks.mjs` | 0 | 13 | 真实执行 token plan 纯函数 |
| `run_maintenance_keys_checks.mjs` | 0 | 13 | 真实执行维护开关归一 |
| `run_form_hint_checks.mjs` | 0 | 12 | 表单提示文本 |
| `run_log_cleanup_checks.mjs` | 0 | 12 | 真实执行日志保留期纯函数 |
| `run_silent_failure_checks.mjs` | 0 | 11 | **文本断言 11 条**：只覆盖 2 个处理函数块 + 平台/面板列表 + `/mail` 路由（见 T9-4） |
| `run_authorized_bulk_checks.mjs` | 0 | 9 | 文本断言：AWS 授权页批量入口与 `all_filtered` |
| `run_panel_unavailable_display_checks.mjs` | 0 | 9 | 真实执行面板不可用展示 |
| `run_poll_backoff_checks.mjs` | 0 | 9 | 真实执行轮询退避 |
| `run_proxy_release_checks.mjs` | 0 | 9 | 文本断言：解绑走 `PATCH .../proxy` |
| `run_panel_upload_entry_checks.mjs` | 0 | 8 | 文本断言：上传入口 |
| `run_accounts_list_token_checks.mjs` | 0 | 7 | 文本断言：列表不下发 token 正文 |
| `run_finished_cap_checks.mjs` | 0 | 7 | 真实执行已完成任务数量上限 |
| `run_panel_actions_by_state_checks.mjs` | 0 | 7 | 真实执行面板动作按状态 |
| `run_grok_batch_default_checks.mjs` | 0 | 6 | 文本断言：`batch_default` 优先 |
| `run_panel_action_mapping_checks.mjs` | 0 | 6 | 文本断言：上传不复用 sync 映射 |
| `run_all_actions_are_tasks_checks.mjs` | 0 | 6 | 文本断言：单动作也走任务端点 |
| `run_detail_completeness_checks.mjs` | 0 | 5 | 文本断言：详情字段完整性 |
| `run_account_row_field_checks.mjs` | 0 | 4 | 文本断言：行字段 |
| `run_import_format_checks.mjs` | 0 | 4 | 文本断言：导入格式 |
| `run_probe_task_checks.mjs` | 0 | 4 | 文本断言：探测走任务 |
| `run_kiro_derived_paging_checks.mjs` | 0 | 3 | 文本断言：派生账号分页 |
| `run_dashboard_tasks_checks.mjs` | 0 | 2 | 真实执行任务汇总 |

（13 行为断言 + 16 文本断言 = 29；文本断言的空转风险见 T9-4。）