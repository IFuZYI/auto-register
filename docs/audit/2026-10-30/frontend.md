# frontend 审计

[MODE: SANDBOX]

- 仓库/基线：`/data/workspace/Register`，分支 `main`，HEAD **`e6f8670`**（审计起点 `4680c8d`）。本轮期间仓库被并行修复推进了 4 次：`c93b71f` → `a3470d3` → `e6f8670` → `519c821`（最后一次只改 `api/proxies.py`/`services/*`，未触及 `frontend/`）。`e6f8670` 就是照上一轮本报告的 F-01~F-05 修的；**本文所有行号按 `e6f8670`（2026-10-30 18:52 UTC）复核，对 `519c821` 仍成立**
- 范围：`frontend/src/`（`pages/`、`components/`、`lib/`、`api/`、`hooks/`、`App.tsx`、`main.tsx`）、`vite.config.ts`、`index.html`、`scripts/run_*.mjs`、构建产物 `static/`
- 方式：只读静态阅读 + 只读 `GET` 实测（`http://127.0.0.1:8000`）+ 在 `/root/.hermes/cache/scratch` 的 HEAD 快照里跑 `tsc -b` / `npm run build`（不覆盖仓库 `static/`，不动线上服务）；未改任何源码
- 结论速览：**P0 × 0（上轮 5 条已落地 4 条，剩 1 条未闭环 → F-01）、P1 × 17、P2 × 17**
- 一句话：**上一轮的 5 条 P0 里有 4 条真修好了；第 5 条只修了一半，而新加的守卫测试恰好测不到漏掉的那一半。**

## 0. 上轮 P0 的落地复核（不重复上报已修项）

| 上轮编号 | 现状 | 证据 |
|---|---|---|
| F-01 导出所选 = 导出全库 | ⚠️ **未闭环**：父组件已改 `String`，导出弹窗第 77 行仍 `map(Number)` → 现在不是「导出全部」，而是 **422 直接失败**（见 §1） | `Accounts.tsx:1492-1494` 已是 `String(value)`；`AccountExportModal.tsx:77` 仍是 `.map(Number)` |
| F-02 所选批量动作全废 | ✅ 已修（4 处 `Number` → `String`） | `Accounts.tsx:612`、`:687`、`:741`、`:787` 四处均 `String(value)` |
| F-03 任务历史第 51 条不可达 | ✅ 已修（服务端分页到位） | `TaskHistory.tsx:52-67` 的 `page/pageSize` 进 state，`:227-238` 把 `total` 交给 Table |
| F-04 批量删除无确认 | ✅ 已修 | `Accounts.tsx:461-480` 已套 `Modal.confirm`（`danger` + 「不可恢复」文案） |
| F-05 关密码/关 2FA 无确认 | ✅ 已修 | `Settings.tsx:354-377`、`:434-454` 均改为 `modal.confirm` |

> 更早的既有修复（**不重复上报**）：打包 59 → 5 个 chunk、空闲预取、`/api/tasks` 的 ETag —— 本轮复核仍生效，见 §8.4。
> `a3470d3` 落地后，账号列表**真的**不再下发 `token`/`password` 正文（改为 `has_token`/`has_password`），上一轮 F-21 随之关闭；前端 `getAccessToken` 已按 `has_token` 兜底（`Accounts.tsx:412-425`）。

---

## 1. 导出链路：上轮 P0 的残留（唯一一条「修了但没修完」）

### F-01 [P1] 导出弹窗仍把 UUID 转成 `Number`，勾选后「导出所选」必然 422

- **位置**：`frontend/src/components/AccountExportModal.tsx:71-78`（`ids: … .map(Number)`）、`:96-97`（`body.account_ids = request.ids`）；对照 `frontend/src/pages/Accounts.tsx:1492-1494`（已改成 `String`）
- **证据**：
  ```tsx
  // AccountExportModal.tsx:70-77
  const selectionKey = selectedIds.join(',')
  const request = useMemo(() => ({
    filters: JSON.parse(filtersKey) as AccountExportFilters,
    ids: selectionKey ? selectionKey.split(',').map(Number) : [],   // ← UUID → NaN
  }), [filtersKey, selectionKey])
  ```
  勾选 3 行时父组件传 `["c831c3b8-…","…"]` → `Number("c831c3b8-…") = NaN` → `JSON.stringify` 把 `NaN` 序列化成 `null` → 请求体 `{"account_ids":[null,null,null]}`；后端模型是 `account_ids: list[str | int] = []`（`api/accounts.py:69`），实测 pydantic 对 `[None]` 报 `Input should be a valid string` → **422**。
  于是「导出所选」这一分支 100% 失败，弹窗只弹一条 `生成导出内容失败: …`（`AccountExportModal.tsx:111-115`），用户只能改选「当前筛选」才能导出。
  更糟的是新加的守卫测试只断言类型字符串、不检查这一行，**测试是绿的**：
  ```python
  # tests/test_frontend_uuid_and_confirm_guards.py:38-40
  def test_export_modal_accepts_string_ids(self):
      source = _read("components", "AccountExportModal.tsx")
      self.assertIn("(string | number)[]", source, "导出对话框的 selectedIds 类型要能装 UUID")
  ```
  菜单文案仍然是 `导出 (3)`（`Accounts.tsx:1183`），点下去却报错。
- **影响**：上轮定为 P0 的功能（按勾选导出）仍然不可用；而且「类型放宽了、值还是 Number」这种半修状态最危险 —— 下一个人很容易把父组件的 `String` 改回 `Number` 来「对齐类型」，从而复活「导出全库」。
- **建议**：`ids: selectionKey ? selectionKey.split(',') : []`（id 里不含逗号，不需要再转）；`ids` 类型写 `string[]`。守卫测试改成断言 **`AccountExportModal.tsx` 里不出现 `map(Number)`**，而不是断言出现了某个类型字面量。

---

## 2. 交付与构建（本轮新增）

### F-41 [P1] 全站没有 ErrorBoundary：任何一个渲染期异常 = 整页白屏

- **位置**：`frontend/src/main.tsx:15-19`（`createRoot(...).render(<StrictMode><App/></StrictMode>)`）；全仓 `grep -rn 'ErrorBoundary|componentDidCatch|onUncaughtError' src/` → **0 命中**
- **证据**：`main.tsx` 只有 `createRoot` + `render`，没有 `onUncaughtError`，`App.tsx` 里也没有 class 边界组件。React 19 的默认行为是「未捕获的渲染错误 → 卸载整棵树」，页面上什么都不剩（`#root` 变空），只有控制台有栈。
  可达路径举例：`Accounts.tsx:338` `(data.items || []).map(normalizeAccount)`（`data` 若是数组则 `.items` 为 `undefined`，`|| []` 兜住了）；但 `PanelComparisonPanel`、`ICloud` 这类深链路里 `data.xxx.map` 没有统一兜底，后端契约一变就是白屏。
- **影响**：单页异常 → 整个控制台不可用（含侧栏），用户只能刷新；且没有任何「出错了 / 重试」入口。
- **建议**：`createRoot(el, { onUncaughtError: (e) => … })` 上报 + 在 `<Routes>` 外包一层边界组件（`getDerivedStateFromError` → `<Result status="error" extra={<Button onClick={() => location.reload()}>重载</Button>}/>`）。`React.lazy` 的 chunk 加载失败也走这条路，收益很大。

### F-42 [P1] `static/` 与进程启动的竞态：构建期间启动 → 整个前端 404（本轮实测复现）

- **位置**：`main.py:556-560`（`if os.path.isdir(_static_dir): app.mount("/assets", …)` + `@app.get("/{full_path:path}")` 兜底都挂在这个 `if` 里）、`frontend/vite.config.ts:13-14`（`outDir: '../static'` + `emptyOutDir: true`）
- **证据**：本轮真实时间线（同一天、同一台机）：
  ```
  18:42:xx  uvicorn 启动（ps: 337367, STARTED 18:42）
  18:46     npm run build 完成，static/ 被 emptyOutDir 清空后重建（index.html mtime 18:46）
  18:49     实测：GET /            → 404 {"detail":"Not Found"}（22 B）
            GET /accounts/chatgpt → 404
            GET /assets/index-CR2x9rLV.js → 404      ← 连静态资源挂载都没有
  18:50:57  服务重启（ps: 346400）
  18:51     GET / → 200，/assets/*.js → 200
  ```
  机制：`vite build` 先 `emptyOutDir` 删掉 `static/`；如果服务正好在这个窗口里 import `main.py`，`os.path.isdir(_static_dir)` 为假 → assets 挂载与 SPA 兜底**永远不会注册**（`--workers 1`、无 `--reload`），直到下一次重启。
- **影响**：发版/重建那一刻重启服务 = 整站 404（API 正常，UI 全无），排查时极易误判成「构建坏了」。本轮就真实发生了 4 分钟以上。
- **建议**：路由**无条件注册**，把存在性判断挪到请求期：`app.mount("/assets", StaticFiles(directory=..., check_dir=False))`，`spa_fallback` 里 `os.stat` 失败时返回 503 + 「前端产物缺失，请先 `npm run build`」。这样构建窗口内最坏是 503 而不是「路由不存在」。

### F-43 [P2] API 的 JSON 响应全程不压缩：`/api/tasks` 每次 374 KB 原文过网

- **位置**：`main.py:374-560`（`_GzipStatic` 只包 `/assets`，API 路由没有任何压缩）；对照 `frontend/src/pages/RunningTasks.tsx:132-153`（2.5 s 轮询）
- **证据**：
  ```
  $ curl -H 'Accept-Encoding: gzip, br' -D - -o /dev/null /api/tasks
  HTTP/1.1 200 OK
  content-length: 374349          ← 没有 content-encoding
  gzip -9 后：23 501 B（6.3%）
  /api/accounts?page_size=50：160 233 B → gzip 13 515 B（8.4%）
  ```
- **影响**：走 SSH 隧道/公网时，列表页与轮询的传输量是必要值的 12~16 倍；这是「切页慢」里除 chunk 之外的另一半成本。
- **建议**：给 API 挂 `GZipMiddleware(minimum_size=1024)`（或让反代压）。`_GzipStatic` 的注释担心 `content-length` 丢失，那是静态资源的顾虑；JSON 走中间件即可，`vary: Accept-Encoding` 记得保留。

### F-44 [P2] `npx tsc --noEmit` 是空跑：它一直在骗人，真门禁是 `tsc -b`

- **位置**：`frontend/tsconfig.json:1-7`（`"files": []` + 只有 `references`）
- **证据**：solution 风格的 tsconfig 在**非 build 模式**下只编译 `files` 里列出的文件（空集）→ 永远绿：
  ```
  $ npx tsc --noEmit ; echo $?
  0                      # 零输出
  $ npx tsc -b ; echo $?  # 同一棵树（a3470d3）
  src/pages/Accounts.tsx(424,20): error TS2339: Property 'has_token' does not exist
  2
  ```
  也就是说 `a3470d3` 把 `npm run build` 弄坏了（`build` = `tsc -b && vite build`），而 `tsc --noEmit` 依旧 exit 0 —— 这正是上一轮报告写「类型干净」的原因，也是这条 P2 值得单列的原因。
- **影响**：任何以 `tsc --noEmit` 为准的本地/CI 门禁都是假绿；类型错误会一路带到 `vite build` 才炸（或被 `any` 吞掉）。
- **建议**：`package.json` 增加 `"typecheck": "tsc -b"`，CI 用 `npm run build`；README/审计口径里把 `tsc --noEmit` 换成 `tsc -b`。

### F-45 [P2] `npm run check` 是红的，而且失败时一个字都不打印

- **位置**：`frontend/package.json:11`（`for f in scripts/run_*.mjs; do node "$f" > /dev/null || exit 1; done`）、`frontend/scripts/run_panel_upload_entry_checks.mjs:17-19`
- **证据**：
  ```
  $ npm run check
  exit code = 1          # 没有任何输出
  $ node scripts/run_panel_upload_entry_checks.mjs
  {"checked":7,"failures":["没有调用既有的 /actions/{platform}/{action_id}/batch 端点"]}
  ```
  该断言仍要求账号页调用 `/actions/{platform}/{action_id}/batch`，而「上传到面板」早已统一走 `/tasks/batch-action`（`Accounts.tsx:700-712`）。
- **影响**：20 个源码级守卫里 1 个过期 → 整条 `check` 常红 → 大家不再跑它，剩下 19 个也一起失效。`> /dev/null` 让失败原因不可见，是本条最坏的部分。
- **建议**：断言改成 `/tasks/batch-action` + `action_id: \`upload_${…}\``；`package.json` 去掉 `> /dev/null`（这些脚本本来就只输出一行 JSON），或改成 `node "$f" || { echo "FAIL $f"; exit 1; }`。

### F-46 [P2] 任务历史切平台时打两次请求（旧页码一次 + 归位后一次）

- **位置**：`frontend/src/pages/TaskHistory.tsx:55-76`
- **证据**：`load` 依赖 `[platform, page, pageSize]`（`:67`），而「切平台归位第 1 页」是另一个 effect（`:74-76` `setPage(1)`）。点侧栏换平台时：`platform` 先变 → `load()` 立刻用**旧 page** 发一次请求 → 再 `setPage(1)` → 第二次请求。第 2 页以后切平台会稳定多打一次。
- **建议**：把 `setPage(1)` 并进平台的 `onChange`（和 `Accounts` 的筛选一致），或 `load` 里 `page` 从 ref 读、`platform` 变化时先归位再取数。

---

## 3. 数据获取：竞态 / 静默失败 / 无谓全量（上轮结论复核，仍在）

### F-06 [P1] 详情抽屉竞态：A→B 快切会把 A 的凭证显示在 B 的标题下

- **位置**：`frontend/src/pages/Accounts.tsx:435-450`
- **证据**：`openDetail` 先 `setCurrentAccount(record)` 占位，`await apiFetch('/accounts/{id}')` 回来**无条件** `setCurrentAccount(normalizeAccount(full))`；没有请求序号、没有 `AbortController`、没有比对 `id`。
- **影响**：抽屉里是 A 的密码 / AT / RT / 2FA 密钥，标题是 B；复制按钮会把 A 的凭证贴到 B 的上下文里。
- **建议**：`const seq = ++detailSeq.current`，回包 `if (seq !== detailSeq.current) return`；或 `AbortController` 取消上一个请求。

### F-07 [P1] 列表请求既无竞态保护也无 `catch`：旧响应覆盖新筛选，失败完全静默

- **位置**：`Accounts.tsx:321-347`、`Proxies.tsx:102-132`、`RunningTasks.tsx:106-124`、`Dashboard.tsx:92-101`、`TaskHistory.tsx:55-67`、`Settings.tsx:595-606`
- **证据**：全部是 `setLoading(true); try { … } finally { setLoading(false) }` —— 没有 `catch`，`useEffect` 里也没有 `.catch`。接口 500/断网 → unhandled rejection，`loading` 归 false，表格保留旧数据、没有任何提示。全仓只有 `components/TaskLogPanel.tsx:139` 用了 `AbortController`（SSE），列表页 0 处。
- **影响**：按 `banned` 筛却看到 registered 行（慢响应后到）；后端故障时界面「看起来正常，只是空/旧」。
- **建议**：抽 `useListQuery(fetcher, deps)`：内部 `AbortController` + 序号 + `catch → message.error` + `error` state；`MailServicePage.tsx:161-166` 是全仓唯一把「加载失败」渲染出来的页面，照它改。

### F-08 [P1] 重复请求：同一份数据在启动/切页时被拉多次（规模最大的仍是 `/config`）

- **位置**：`App.tsx:136` + `App.tsx:225`（`/auth/status` ×2）、`RegisterTaskPage.tsx:134` + `:262`（`/config` ×2，且切平台再拉一次整份 81 键）、`App.tsx:237` + `PanelManagement.tsx:157`（`/integrations/panels` ×2）、`Settings.tsx:595` + `PanelConfigPanel.tsx:268`（`/config` ×2）、`Accounts.tsx:529` + `:532`（每次点「注册」重拉 `/config` + `/platforms`）
- **证据**：以上均为不同组件/不同 effect 各拉一次（与 StrictMode 无关）；`/platforms` 有 promise 缓存（`lib/platforms.ts` 的 `capabilitiesPromise` 范式），`/config` 没有任何缓存。
- **建议**：给 `/config`、`/auth/status`、`/integrations/panels` 加一个 TTL 模块级缓存（30 s 即可），并在写配置后主动 invalidate；`Proxies.load()` 拆成 `loadProxies/loadBalance/loadCfg`，按动作只刷需要的那一份。

### F-09 [P1] 无谓全量拉取：`/icloud/aliases` 712 KB / 1941 条，筛选全在客户端

- **位置**：`frontend/src/api/icloud.ts:252-261`（`listICloudAliases(accountId)` 只有 `?account_id=`，无分页/无关键词）、`ICloud.tsx:115-131`（每次挂载/切主号都全量重拉）、`ICloud.tsx:169-182`（`filterAliases` 客户端过滤）
- **证据**：`GET /api/icloud/aliases` 实测 **712 390 B / 1941 条**；页面上任何一次筛选、翻页、搜索都是纯前端，数据已经全在内存里。
- **影响**：进入 iCloud 页首屏固定多 700 KB（未压缩，见 F-43）；主号多、别名多时 `filterAliases` 每次输入都全量遍历。
- **建议**：接口加 `page/page_size/keyword/pool_status`，前端改成服务端分页（`TaskHistory` 已是范式）；退一步至少按 `account_id` + `pool_status` 让后端过滤，前端只做展示。

### F-16 [P1] 切平台不清 `page` / `selectedRowKeys`：越界空页 + 跨平台选择残留

- **位置**：`Accounts.tsx:288-290`（只 `setCurrentPlatform(platform)`）、`Accounts.tsx:1275-1278`（`rowSelection` 受控且从不主动清空）
- **证据**：在 `/accounts/chatgpt` 第 20 页点侧栏切到 `/accounts/grok`（390 条 / 8 页）→ `page` 仍是 20 → `GET …&page=20` 返回空 `items`，界面空白、分页器停在 20。勾选几行再切平台，`selectedRowKeys` 保留 A 平台 UUID，此时「删除选中」会把 A 的 id + `platform=B` 发下去。
- **建议**：`platform` 变化时 `setPage(1); setSelectedRowKeys([])`；或在 `load` 里 `items.length === 0 && page > 1` 自动回落到最后一页。

---

## 4. 契约与错误处理（上轮结论复核，仍在）

### F-13 [P1] `PUT /config` 返回的 `ignored` 没有任何前端读取 → 拼错键仍提示「保存成功」

- **位置**：`Settings.tsx:611-660`（`save`，最后 `message.success('保存成功')`）、`Proxies.tsx:292-309`（`saveBalanceCfg`）、`components/mail/MailServicePage.tsx:206`；全仓 `grep -rn 'ignored' src/` → **0 命中**
- **证据**：后端 `api/config.py` 明确把被白名单丢弃的未知键收进 `ignored` 并随响应返回，注释写着「前端可据此提示『这些字段没生效』」。三处调用方都把响应体丢掉。
- **影响**：字段名写错、后端白名单漏加时，用户得到「保存成功」但配置没生效 —— 这正是后端注释里描述过的事故形态。
- **建议**：`const res = await apiFetch(...)`；`res.ignored?.length` 非空就 `message.warning(\`这些字段未生效：${res.ignored.join(', ')}\`)`。

### F-14 [P1] `apiFetch` 把 FastAPI 422 的 `detail` 数组当字符串抛 → 用户看到 `[object Object]`

- **位置**：`frontend/src/lib/utils.ts:35-43`
- **证据**：`throw new Error(json.detail || text)`；实测 `GET /api/accounts?page=abc` →
  ```json
  {"detail":[{"type":"int_parsing","loc":["query","page"],"msg":"Input should be a valid integer…"}]}
  ```
  `String(array)` = `"[object Object]"`。可达路径：注册数量用 `<Input type="number" min max>`（`Accounts.tsx:1297-1300`，HTML 的 `min/max` 不参与 antd 校验），填 `20000` 直接 422，而 `handleRegister`（`:525-581`）又没有 `catch` → 连 `[object Object]` 都看不到。
- **建议**：`apiFetch` 里判断 `Array.isArray(json.detail)`，拼成 `loc.join('.') + ': ' + msg`；`count/concurrency` 补 `rules={[{ type: 'number', min: 1, max: MAX_REGISTER_COUNT }]}`。

### F-15 [P1] 批量删除账号忽略 `{deleted, not_found}` → 删 0 条也报「批量删除成功」

- **位置**：`Accounts.tsx:461-480`
- **证据**：确认框的 `onOk` 里 `await apiFetch('/accounts/batch-delete', …)` 后直接 `message.success('批量删除成功')`，不读响应。后端 `api/accounts.py` 返回 `{deleted, not_found, total_requested}`；同仓 `Proxies.tsx:169-200` 与 `TaskHistory.tsx:81-89` 都读了结果并弹明细。
- **影响**：配合 F-16（跨平台残留选择）会出现「提示成功、实际一条没删」。
- **建议**：照抄 `Proxies.batchDel`：`已删除 N 条 / 未找到 M 条`，`M > 0` 时列出 id。

### F-22 [P1] 危险动作缺 `catch`：失败时用户得不到任何反馈

- **位置**：`Accounts.tsx:452-459`（单条删除）、`482-492`（新增）、`525-581`（注册）、`583-592`（详情保存）、`TaskHistory.tsx:78-92`（批量删除历史）、`Settings.tsx:611-660`（保存配置）、`RegisterTaskPage.tsx:173-225`（提交注册）、`Proxies.tsx:202-205`（启停代理）
- **证据**：全部是 `try { … } finally { setLoading(false) }`，没有 `catch`。以 `handleRegister` 为例：422/400 → `finally` 复位 loading → 异常冒泡成 unhandled rejection → 弹窗原地不动、**零提示**。
- **影响**：最常见的后端校验失败（数量越界、缺邮箱渠道、代理不可用）表现为「点了没反应」，用户会重复点。
- **建议**：统一 `catch (e) { message.error(e instanceof Error ? e.message : '操作失败') }`；或抽 `runAction(fn, { success, error })` 高阶函数，顺带统一 loading。

### F-23 [P1] 错误态与空态同形：接口挂了显示「暂无数据」

- **位置**：`RunningTasks.tsx:398-400`（`Empty description="暂无任务记录"`）、`ICloud.tsx:671`（`Empty description="还没有 iCloud 主号…"`）、`Accounts.tsx:1269-1284`（Table 默认空态）、`TaskHistory.tsx:218-239`（同）、`Dashboard.tsx`（`stats` 为 `null` 时数值全 0）
- **证据**：`load` 没有 `error` state（F-07），失败后 `data` 保持 `[]`/旧值 → 渲染分支与「真的没有数据」完全一致。
- **影响**：后端 500 时用户以为「账号被清空了」，是本类审计里最容易被误判为数据丢失的形态。
- **建议**：`error ? <Alert type="error" action={<Button onClick={load}>重试</Button>}/> : data.length ? … : <Empty/>`。

---

## 5. 交互缺陷（上轮结论复核，仍在）

### F-10 [P1] `Proxies`：检测失败后按钮永久转圈；启停失败完全静默；「检测全部」用 3 s 猜测完成

- **位置**：`Proxies.tsx:207-214`（`check`）、`:202-205`（`toggle`）；后端 `api/proxies.py` 的 check 是 `background_tasks.add_task(...)`
- **证据**：
  ```tsx
  const check = async () => {
    setChecking(true)
    await apiFetch('/proxies/check', { method: 'POST' })   // 抛错 → 下面两行永不执行
    setTimeout(() => { load(); setChecking(false) }, 3000)
  }
  ```
- **影响**：一次网络抖动 → 「检测全部」永久 `loading`（只能刷新页面）；后台检测通常远超 3 s，刷回来的仍是旧计数。
- **建议**：`try/catch/finally` + `message.error`；检测改成轮询 `/proxies` 的 `last_checked` 直到变化（或后端返回 job id）。

### F-25 [P1] 收银台链接在 `setInterval` 回调里 `window.open`，会被浏览器拦截

- **位置**：`RegisterTaskPage.tsx:227-239`（`:235` 的 `t.cashier_urls.forEach(url => window.open(url, '_blank'))`）
- **证据**：不是用户手势，Chrome/Safari 默认拦截弹出的多个窗口；同一函数里 `setInterval` 的 id 既没存 ref 也没有卸载清理，回调没有 `try/catch` —— 一次请求失败就再也不 `clearInterval`，离开页面后仍在每 2 s 打接口并 `setState`。
- **建议**：`useEffect` 里起轮询并 `return () => clearInterval(id)`；收银台改成页面内的链接列表 + 「全部打开」按钮（用户手势）。

### F-29 [P2] Solver 状态卡：5 s 轮询不看 `document.hidden`；失败显示「未运行」；重启无确认/无 catch

- **位置**：`Settings.tsx:253-281`
- **证据**：`checkSolver` 的 `catch { setRunning(false) }`（`:260-262`）把「请求失败」显示成「未运行」；`restartSolver`（`:265-269`）只有 `await apiFetch('/solver/restart')`，无 loading、无确认；`setInterval(..., 5000)`（`:273`）在后台标签页照跑。
- **建议**：`catch → setRunning(null)` + 文案「状态未知」；轮询加 `visibilitychange` 暂停；重启加确认。

### F-30 [P2] 登录页硬编码暗色主题

- **位置**：`pages/Login.tsx:210`（`<ConfigProvider theme={darkTheme}>`）
- **证据**：`App.tsx:177-179` 从 `localStorage.theme` 取主题、`:219-222` 写回，登录页完全绕过这个单一真相源；亮色用户被登出后看到一张暗色卡片。
- **建议**：`theme={localStorage.getItem('theme') === 'light' ? lightTheme : darkTheme}`，或把主题读取抽成 `useThemeMode()` 两处共用。

---

## 6. P2 清单（数据/渲染/打包，逐条带位置与证据）

| 编号 | 位置 | 证据 | 建议 |
|---|---|---|---|
| F-17 | `lib/taskKinds.ts:13-20`、`RunningTasks.tsx:237-245` | `KINDS` 只有 `manual/api/schedule/backfill_rt/bind_2fa/refresh_token/auto_refresh/chatgpt2api_sync`；实测 `/api/tasks` 837 条里有 **13 条** `source:"action:*"`（`action:backfill_refresh_token`×3、`action:refresh_token`×2、`action:probe`×2、`action:check_plus_trial`×2、`action:probe_local_status`×2、`action:sync_grok2api_status`×1）＋`probe_status`×1 → 卡片直接显示英文 id | `source.startsWith('action:')` 时查动作表（可复用 `lib/panelComparison.ts` 的 `PanelActionKey`），未知值再回落 |
| F-18 | `RunningTasks.tsx:421`（`kind={(logTask?.source \|\| 'register') as TaskKind}`）、`TaskLogPanel.tsx:25-35,74` | `KIND_TEXT[kind] \|\| KIND_TEXT.register` —— 未知 kind 静默落到「注册」，补 RT/探测任务的日志页显示「注册成功：N / 已注册：N」 | `kind` 用 `taskKindMeta` 归一后的 key，并补一套 `batch_action` 通用文案 |
| F-20 | `components/settings/DataBackupPanel.tsx:86`、`:140` | 两处用裸 `fetch`（响应是 ZIP / 请求体是原始字节），只带 `Authorization`，没有 `X-Panel-Auth-Required` 的 401 处理（`lib/utils.ts:28-34` 有） | 抽 `handleAuthFailure(res)` 复用；401+该头时 `clearToken()` 跳登录 |
| F-24 | `Proxies.tsx:549`、`ICloud.tsx:668`、`DataBackupPanel.tsx:224`、`MailImportPanel.tsx:801` | 四处 `pagination={false}` 全量渲染；`Proxies.tsx:311` 的 `columns` 在组件体内定义（未 `useMemo`），每次 `setState` 都整表重算 | 加 `pagination={{ pageSize: 50, showSizeChanger: true }}` 或 `virtual`；`columns` 用 `useMemo` |
| F-26 | `components/TaskLogPanel.tsx:247` | `setLines((previous) => [...previous, payload.line!])` —— SSE 每行一次浅拷贝，无 `MAX_LINES` 截断，长任务可到上千行 | 环形缓冲：`prev.length > 2000 ? prev.slice(-1500) : prev` 再 concat |
| F-31 | `App.tsx:279-324` | 为给 `.ant-table-content/.ant-table-body` 补 `tabindex/role/aria-label`，对 `document.body` 做 `MutationObserver(childList,subtree)` + 每帧 `querySelectorAll`；`RunningTasks` 2.5 s 一次重渲染、`TaskLogPanel` 每行一次 `setState` 都会触发 | 收进 `<ScrollableTable>` 包装组件，只在表格挂载/数据变化时跑一次；别依赖 antd 内部 class 名 |
| F-33 | `lib/accountFormat.ts:50,60,274-275`、`lib/icloud.ts:83,96,101`、`TaskHistory.tsx:127`、`RunningTasks.tsx:62-90` | 六套时间格式化并存；`TaskHistory.tsx:127` 还是内联 `new Date(text).toLocaleString('zh-CN')`；只有 `lib/time.ts:42-84` 正确处理「无时区串 = UTC」 | 全部收敛到 `lib/time.ts`，`formatDuration` 也搬进去并加单测 |
| F-35 | `vite.config.ts:29-48`（`{ name: 'pages', test: /src[\\/]pages[\\/]/ }`）、`App.tsx:24-46`、`App.tsx:200` | `static/assets` 只有 5 个 JS：`index 10 739`、`pages 202 688`、`vendor-react 229 841`、`vendor-antd 1 140 405`、`runtime 716`（gzip 合计 483 595）。所有页面被合进**一个** `pages` chunk → 11 个 `lazy()` 只省执行不省下载；`App.tsx:200` 的注释写的是 `advancedChunks`，配置里其实是 `codeSplitting` | 拆成 `pages-core`（Dashboard/Login/Accounts）与 `pages-rest` 两组；注释与配置对齐（保留 59→5 的收敛目标） |
| F-36 | `Settings.tsx:343`、`Settings.tsx:367` | 直接 `localStorage.setItem/removeItem('auth_token', …)`，绕过 `lib/utils.ts:8-14` 的 `setToken/clearToken` | 统一走 `setToken/clearToken`，键名只留一个常量 |

---

## 7. 可访问性（上轮结论复核，仍在）

### F-37 [P1] 纯图标按钮没有可访问名

- **位置**：`Proxies.tsx:397-402`（启停，只有 `SwapLeft/RightOutlined`）、`Proxies.tsx:410`（删除，`Popconfirm` 内的 `DeleteOutlined`）、`Accounts.tsx:194-199`（行内「更多」，只有 `MoreOutlined`）
- **证据**：三处 `<Button icon={…}/>` 没有 `aria-label`、没有 `title`、没有可见文字 → 读屏只念「button」。对比 `TaskHistory.tsx:212`、`PageHeader.tsx:50` 都正确加了 `aria-label`。
- **建议**：`aria-label={record.is_active ? '禁用该代理' : '启用该代理'}`、`aria-label="删除代理"`、`aria-label="更多操作"`。

### F-38 [P1] 代理标签只能鼠标点击（`<Tag onClick>` 不可聚焦）

- **位置**：`Proxies.tsx:334-341`；同类：`Proxies.tsx:504-513`（`Tag.CheckableTag`）
- **证据**：antd `Tag` / `CheckableTag` 都渲染 `<span onClick>`，无 `role`、无 `tabIndex`、无 `onKeyDown`（`node_modules/antd/es/tag/CheckableTag.js:41-44`）→ 键盘用户无法给代理打「注册/绑定」标签，而这是「均衡绑定」的前置条件。
- **建议**：改用 `Checkbox.Group` / `Tag` 上补 `role="button" tabIndex={0} onKeyDown`（Enter/Space）。

### F-39/F-40 [P2] 表单控件缺可访问名；分页缺 `showTotal`

- **位置**：`Proxies.tsx:452-456`（`Switch`）、`:461-466`（`InputNumber`）、`Accounts.tsx:1233/1246/1256/1263`（4 个 `Select`/`DatePicker` 只有 `placeholder`）、`Proxies.tsx:496-501`（地区输入）、`Accounts.tsx:1279`（分页无 `showTotal`）
- **证据**：`placeholder` 不进 `aria-label`；`Switch` 渲染成无名 `role="switch"`，`InputNumber` 是无名 `spinbutton`。全仓只有 20 处 `aria-label`（搜索框 `Accounts.tsx:1225`、刷新按钮 `:1158`、`TaskHistory.tsx:212` 等是对的）。
- **建议**：逐个补 `aria-label`（「状态筛选」「Plus 试用筛选」「开始时间」「结束时间」「按负载均衡」「单代理上限」「地区标签」）；分页补 `showTotal`。

> 正面：`App.tsx:487-488` 有 skip-link，`index.css` 有 `:focus-visible` 描边，`App.tsx:279-295` 给可横向滚动的表格补了 `tabindex/role=region`，`PageHeader.tsx:50` 汉堡按钮有 `aria-label`。这几处不用改。

---

## 8. 工具链真实输出

### 8.1 `npx tsc --noEmit`（空跑，见 F-44）

```
$ cd frontend && npx tsc --noEmit ; echo $?
0            # 零输出 —— 但这是「没检查任何文件」的 0
```

### 8.2 `npx tsc -b`（真门禁）

```
$ cd frontend && npx tsc -b ; echo $?
0            # 当前 HEAD e6f8670 通过
```
对照：同一个命令在 `a3470d3` 上是 **exit 2**：
```
src/pages/Accounts.tsx(424,20): error TS2339: Property 'has_token' does not exist on type
  '{ token?: string; extra_json?: string }'.
```
（`e6f8670` 给 `getAccessToken` 的入参补了 `has_token?: boolean` 才好。）

### 8.3 `npm run lint`

```
$ npm run lint
✖ 39 problems (34 errors, 5 warnings)      exit code = 1
```
- 规则分布：`@typescript-eslint/no-explicit-any` **30**、`react-hooks/exhaustive-deps` **5**、`react-hooks/set-state-in-effect` **2**、`react-refresh/only-export-components` **1**、`no-empty` **1**
- 文件分布：`Accounts.tsx` 15、`Settings.tsx` 10、`App.tsx` 3、`Login.tsx` 2、`MailImportPanel.tsx` 2，其余 7 个文件各 1
- 30 个 `any` 里最值得先补的三个类型：`Account`（`Accounts.tsx:224,95,391…`）、`ProxyRow`（已在用）、`TaskSnapshot`（`RunningTasks.tsx:250`）；补完可一次消掉 20+

### 8.4 构建产物（`static/assets`，18:46 那次构建）

```
index-CR2x9rLV.js         10 739 B   gzip   3 837
pages-C-FOa0PM.js        202 688 B   gzip  60 218
vendor-react-Br-GQWxU.js 229 841 B   gzip  72 590
vendor-antd-Bply7nht.js 1 140 405 B  gzip 346 491
rolldown-runtime-hePW80VL.js 716 B   gzip     459
index-BuWxJHFt.css        14 481 B   gzip   3 813
JS 合计 1 584 389 B / gzip 483 595 B，**5 个 JS 文件**
```
与「59 → 5 个 chunk」一致；`/api/tasks` 的 `etag: "381ace95…"` + `cache-control: no-cache` 仍生效，空闲预取仍在（`App.tsx:202-217`）。

### 8.5 `npm run check`（源码级守卫，见 F-45）

```
$ npm run check
exit code = 1        # 无输出
$ node scripts/run_panel_upload_entry_checks.mjs
{"checked":7,"failures":["没有调用既有的 /actions/{platform}/{action_id}/batch 端点"]}
```

---

## 9. 建议修复顺序

1. **今天**：F-01（一行 `map(Number)` → 直接 `split(',')`）＋把 F-45 的守卫改成断言「不出现 `map(Number)`」——否则这条 P0 会一直「已修但没修」。
2. **今天**：F-42（`check_dir=False` + 请求期判存在）——发版窗口整站 404，改动 3 行。
3. **本周**：F-07/F-22（统一 `catch`）→ F-23（错误态别等于空态）→ F-06/F-16（竞态与状态归位）——这四条一起做，抽 `useListQuery` + `runAction` 两个小工具就能覆盖 6 个页面。
4. **本周**：F-41（ErrorBoundary）、F-44（`tsc -b` 进门禁）、F-15/F-13（读响应体）。
5. **下周**：F-43（API gzip，一行）、F-09（`/icloud/aliases` 服务端分页）、F-08（`/config` 缓存）、F-37/F-38（a11y）。
6. **随手**：F-17/F-18（`action:*` 标签）、F-24/F-26（长列表与日志上限）、F-33（时间格式化收敛）、F-35（chunk 分组 + 注释订正）、30 个 `any`（先补 3 个类型）。

---

## 附：证据与口径

- 静态阅读：`frontend/src` 全部 48 个 `.ts/.tsx`（本轮重点重读 `Accounts.tsx`、`AccountExportModal.tsx`、`TaskHistory.tsx`、`Settings.tsx`、`Proxies.tsx`、`RunningTasks.tsx`、`RegisterTaskPage.tsx`、`TaskLogPanel.tsx`、`App.tsx`、`main.tsx`、`lib/utils.ts`、`lib/taskKinds.ts`、`api/icloud.ts`）。
- 只读实测（`127.0.0.1:8000`，**仅 GET**）：
  - `GET /api/accounts?platform=chatgpt&page=1&page_size=1` → 1462 B，单行 14 键，**无 `token`/`password`**，有 `has_token:true`/`has_password`
  - `GET /api/accounts?platform=chatgpt&page=1&page_size=50` → 160 233 B（gzip 13 515 B）
  - `GET /api/tasks` → 837 条 / 374 349 B / `etag`，13 条 `source:"action:*"`
  - `GET /api/tasks/logs?page=1&page_size=20` → `total: 1661`
  - `GET /api/icloud/aliases` → 712 390 B / 1941 条
  - `GET /api/proxies` → 5 条 856 B（`url` 已脱敏为 `socks5://***:***@…`）；`GET /api/proxies/balance` → 同一形态的脱敏 url（`balanceMap` 键仍是同一字符串，前端映射未坏）
  - `GET /api/accounts?page=abc` → 422，`detail` 为数组（F-14 证据）
  - `GET /api/accounts/stats` → `{total:1057, by_platform:{grok:390, chatgpt:667}}`
  - `GET /`、`/accounts/chatgpt`、`/assets/*.js` → 18:49 全 404、18:51 全 200（F-42 的时间线）
- 构建/类型检查：在 `/root/.hermes/cache/scratch/head{2}/frontend`（`git archive HEAD` + 软链 `node_modules`）里跑，**没有**覆盖仓库 `static/`、没有影响线上服务。
- 未执行任何写操作（POST/PUT/PATCH/DELETE 一律未调用）；未修改源码；唯一写入文件为本报告。
