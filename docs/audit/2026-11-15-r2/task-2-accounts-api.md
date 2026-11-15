# Task 2 — 账号域 API：列表/详情/批量动作的语义
状态: 完成

范围：`api/accounts.py`、`api/actions.py`。所有结论都由**驱动端点**得到（临时 SQLite 库 +
`fastapi.testclient.TestClient`，复刻 `tests/test_icloud_remote_api.py` 的 fixture 风格），
脚本在 `/tmp/t2_probe/`，未新增/修改任何仓库文件（本报告除外）。

驱动方式（每个 probe 脚本开头都跑这段，下文用「驱动」代指）：

```python
# /tmp/t2_probe/harness.py 摘要
default_engine = create_engine(f"sqlite:////tmp/t2db/default.db")
SQLModel.metadata.create_all(default_engine,
    tables=ACCOUNT_TABLES + INFRA_TABLES + [ConfigItem.__table__])
core.db.engine = default_engine
platform_database_registry.configure({"chatgpt": "", "grok": ""})  # 指向临时库
app.include_router(accounts_router, prefix="/api"); app.include_router(actions_router, prefix="/api")
client = TestClient(app, raise_server_exceptions=False)
```

> 踩坑记录（对后续审计有用）：只建 `ACCOUNT_TABLES` 时，`config_store.get_all()`
> 打 `configs` 表会抛 `no such table: configs`，**每个** action 端点都变成裸 500 ——
> 那是夹具缺表，不是产品缺陷。补上 `ConfigItem` 表后真实行为才显现。
> 本报告里所有 action 结论都取自补表之后的运行。

## 结论

列表/详情的**凭据面**是干净的（密文集里 21 个键没有一个以正文出现在列表响应里，
`password`/`token` 正文也不在），详情端点还带了 `Cache-Control: no-store`。
批量动作的 `ids` 解析与 action 白名单都做对了 —— 未知 action 是 **400 带可用清单**，
非法 id 被丢弃而不是 500。分页参数 `page=0`/`page_size=-1` 都**钳位**而不是 500。

问题集中在**「total 与 items 不同源」**这条缝上：`total` 走 SQL `COUNT(*)`（只数一个库），
`items` 走跨库合并（平台库 + 默认库），于是只要默认库里还留着同平台的历史行，
分页就会多出一页、`total` 比实际可翻出的行少。这是分页语义的核心不变量，
不是展示瑕疵：前端分页器按 `total` 算页数，最后一页的行永远翻不到。

另外发现两处**真实凭据外泄面**（列表与详情都会下发 `register_proxy` 里的
`user:pass@`；`extra` 里的 `api_key`/`apiKey` 不在密文集里，列表原样下发）。

## 发现

### T2-1 · Important · 分页 `total` 与 `items` 不同源，跨库历史行被漏计（可复现）

- **file:line**：`api/accounts.py:362-391`（`total = _count_accounts(...)` 与
  `items = _filtered_accounts(...)` 分别调用）；根因在
  `core/db/repository.py:1149-1153`（`count_all_accounts` 指定 platform 时只开
  `keys[0]` 一个库做 `COUNT(*)`）对比
  `core/db/repository.py:1270-1293`（`list_all_accounts` 扫平台库 **+ 默认库**）。
- **复现**（`/tmp/t2_probe/p5_crossdb.py`、`p7_final.py`）：
  1. 温度库按 `platform_database_registry.configure({"chatgpt": <tmp>/chatgpt.db})` 分库；
  2. 往 chatgpt 平台库插 30 行；
  3. 往**默认库**插一行 `AccountModel(platform="chatgpt", email="legacy-only-default@x.com")`；
  4. `GET /api/accounts?platform=chatgpt&page_size=200`。
- **实测**：
  ```
  baseline         total= 30 items= 30
  after legacy     total= 30 items= 31     ← total 少 1，items 多 1
  paged walk       rows=31 reported_total=30 pages=4
  emails past total boundary: ['cg00@x.com']
  status=registered -> total=25 items=26
  ```
  即：`total` 说 3 页（30/10），实际能翻出 4 页；`total` 之后那一页的行只能靠
  「多翻一页」碰到，前端分页器（`frontend/src/pages/Accounts.tsx` 用 `total` 驱动
  `filteredTotal` 与页数）不会去翻它。
- **blast radius**：任何曾经分过库、默认库还留着历史同平台行的部署（`core/db/repository.py`
  的注释自己写明这是「分库迁移前的历史遗留行」的预期状态）。表现是账号列表少显示、
  导出/批量选号少算 —— 用户会以为账号丢了。不指定 platform 时另一个反向问题：
  `_distinct_keys` 按 `(platform,email)` 去重计数，而 `list_all_accounts` 也去重，
  这条**是一致的**（实测 `total=55 items=55`），所以只有「指定 platform」这条路径坏。
- **fix**：让 `count_all_accounts(platform=...)` 与 list 同口径 —— 平台库 `COUNT(*)`
  **加上**默认库里同条件的行（按 `(platform,email)` 去重，复用 `_distinct_keys`
  的收集逻辑；成本可接受，默认库里同平台行通常是 0）。或者退一步：`list_all_accounts`
  在指定 platform 时不并入默认库（但会丢历史行，不推荐）。

### T2-2 · Important · 列表/详情把 `register_proxy` 里的代理口令原样下发

- **file:line**：`api/accounts.py:228-284`（`_slim_extra_for_list` 只处理
  `SENSITIVE_EXTRA_KEYS` 与 4 个重型键，`register_proxy` 原样保留）
  与 `api/accounts.py:927-929`（详情端点 `acc.get_extra()` 全量返回，含 `register_proxy`）。
  密文集 `core/credential_crypto.py:41-77` 里没有 `register_proxy`。
- **复现**（`/tmp/t2_probe/p6_misc.py`）：
  ```python
  acct.set_extra({"register_proxy": "socks5h://user-***:pass-***@10.0.0.9:1080"})
  GET /api/accounts?email=proxyprobe   → extra_json 里 register_proxy 仍是
                                        "socks5h://user-***:pass-***@10.0.0.9:1080"
  GET /api/accounts/{id}?platform=chatgpt → 同样明文
  ```
- **blast radius**：`socks5h://user:pass@host:port` 的 userinfo 是**代理账号凭据**，
  拿到即可白用该出口（计费/封号归属），也能拿它去撞同凭据的其他服务。列表端点一次
  `page_size=200` 就能拖走整个账号池的代理口令 —— 与「列表不下发凭据」这条已确立的
  契约（同文件 306-310 行、`tests/test_accounts_list_payload.py:test_list_keeps_fields_the_page_uses`
  反而**断言 `extra.get("register_proxy")` 必须存在**）直接冲突：这条测试把泄漏面
  钉成了契约。
- **fix**：把 `register_proxy` 拆成「有代理」布尔 + 脱敏后的 `scheme://host:port`
  （`core/proxy_utils` 应已有归一化函数，加一个 `redact_userinfo`），
  正文只在详情端点给（或详情也脱敏）。同步改那条断言。

### T2-3 · Important · `extra` 里的 `api_key` / `apiKey` 不在密文集，列表原样下发

- **file:line**：`core/credential_crypto.py:41-77`（`SENSITIVE_EXTRA_KEYS` 缺
  `api_key` / `apiKey` / `proxy_password`）；下游 `api/accounts.py:190-194`
  从该集合派生白名单，因此这些键既**不加密落盘**也**不脱敏下发**。
- **复现**（`/tmp/t2_probe/p2_leak.py`）：
  ```python
  acct.set_extra({"api_key": "sk-***", "apiKey": "sk-camel-***", "proxy_password": "pp-***"})
  GET /api/accounts?email=leakprobe
  → extra_json = {"api_key": "sk-***", "apiKey": "sk-camel-***", "proxy_password": "pp-***", ...}
  ```
  同一次响应里 `has_access_token` 是布尔（说明瘦身确实跑了），只有这几个键漏过 ——
  所以这不是「路径没走到」，是「清单没有这个键」。
- **blast radius**：`core/credential_fields.py` 的 JSON 导入是 catch-all（注释
  155-158 行：「未知键照收」），任何用户从别处导出的 `api_key` 都会原样进 extra 并
  落明文；列表接口再把它们发给浏览器。密文集注释自己记录过同一类事故
  （2026-11-13：`client_secret`/`kiro_aws_*` 漏在清单外被明文下发），这次是同一
  模式的第三次。
- **fix**：把 `api_key`/`apiKey`/`proxy_password`（以及同类 `secret`/`token` 后缀的
  未知键）纳入 `SENSITIVE_EXTRA_KEYS`；或改成**默认脱敏**（只放行白名单键），
  这样「新增一个凭据键」不再是每次都要记得改清单的事故点。

### T2-4 · Minor · `page_size` 负数被钳成 1，`page_size=0` 变成 20 —— 静默且不自洽

- **file:line**：`api/accounts.py:354` `page_size = min(max(1, int(page_size or 20)), 200)`。
- **复现**（`/tmp/t2_probe/p1_paging.py`）：
  ```
  page_size=-1   → n=1     page_size=0  → n=20
  page_size=-50  → n=1     page_size=201 → n=57(全部)
  page=0 → page=1（钳位，正常）     page=-100 → page=1
  ```
- **说明**：**不 500**，这是好消息；但 `0 → 20` 与 `-1 → 1` 是两套语义（`0` 走了
  `or 20` 的假值分支，`-1` 走了 `max(1, ...)`）。调用方写错时得到的是「看起来正常
  的不同页面大小」，排障时无从发现。返回体里也**没有**回显实际生效的 `page_size`
  （只有 `page`），客户端算不出自己拿到的是哪一档。
- **fix**：`page_size` 用于 `Query(20, ge=1, le=200)` 让 FastAPI 直接 422；或在响应里
  回显生效的 `page_size`。前者更好（契约显式）。

### T2-5 · Minor · 批量端点的非法 id 被**静默丢弃**，`total` 不反映请求规模

- **file:line**：`services/chatgpt_account_selection.py:31-54`（`normalize_account_ids`
  丢掉任何不匹配 UUID/正整数的值）+ `api/actions.py:139-144`
  （归一后为空 → 400「账号 ID 列表不能为空」）。
- **复现**（`/tmp/t2_probe/p6_misc.py`）：
  ```
  ids=[<有效 uuid>, "not-an-id"] → 200 total=1（坏 id 无声消失，无 missing 记录）
  ids=["not-an-id"]              → 400 "账号 ID 列表不能为空"（措辞指向"空列表"，不是"id 非法"）
  ids=[1, 2]（旧整数 id）        → 200 total=2 failed=2 "账号不存在"
  ```
- **说明**：**不是 500**，且空列表语义正确。但「有效 id + 垃圾 id」这一档里，垃圾 id
  既不算失败也不进 `missing`，`total` 比请求少 —— 前端按 `total` 渲染进度会得出
  「发了 2 个只处理了 1 个」而没有任何行说明原因。与 `_resolve_batch_accounts`
  对「存在但查不到」的 id 会明确回 `{"message": "账号不存在"}` 的口径不一致。
- **fix**：归一阶段保留被丢弃的原始值，作为 `invalid` 列表回填进响应的 `items`
  （`ok=false, message="ID 格式非法"`），或直接 400 报出第一个非法值。

### T2-6 · Minor · 单账号动作端点对未知 action 返回 400，但**批量**端点的同一语义在
未实现动作上返回 200 + `failed=1`

- **file:line**：`api/actions.py:483-484`（单账号：`except NotImplementedError → 400`）
  对比 `api/actions.py:419-446`（批量：逐条 `except Exception` 吞成 item 失败，整体 200）。
- **复现**（`/tmp/t2_probe/p3_ids.py`）：
  ```
  单账号 POST /actions/chatgpt/{id}/no_such_action  → 400 {"detail":"未知操作: no_such_action"}
  批量   POST /actions/chatgpt/no_such_action/batch  → 400（白名单挡住了）✔
  批量   POST /actions/chatgpt/upload_cpa/batch      → 200 failed=1 "CPA API URL 未配置"
  ```
- **说明**：**白名单这一层是对的**（未知 id 根本进不去执行循环，这正是
  `tests/test_batch_action_validation.py` 钉住的行为，实测复现）。剩下的缝只在
  「平台**声明了**但当前配置下跑不通」的动作：单账号给 400，批量给 200。两个端点对
  同一件事的 HTTP 语义不一致，客户端没法统一处理。严重度低（批量把逐条结果放进
  `items` 是有意的设计），但值得统一成「顶层 200 + 明确 `error_kind`」或都 400。
- **fix**：在 `items[]` 里加 `error_kind`（`unsupported` / `config_missing` / `upstream`），
  前端就不用靠 message 字符串猜。

### T2-7 · Verification gap（Important）· 批量 `all_filtered` 与列表筛选口径只有部分测试覆盖

- **file:line**：`api/actions.py:159-174`（`all_filtered` 的 WHERE 子句自己拼了一份）
  对比 `api/accounts.py:362-391`（列表拼的是仓储里那份 `_list_filters`）。
- **实测一致的部分**（`/tmp/t2_probe/p6_misc.py`, `p7_final.py`）：
  `email="CG0"` → 批量 total=10 == 列表 total=10（大小写归一一致）；
  `status=banned` → 批量 5 == 列表 5；`plus_status=plus` → 批量 0 == 列表 0。
- **不一致的部分**：`all_filtered=True` 的批量选号**只查平台库**（`session` 来自
  `platform_session_from_path`），而列表会把默认库的历史行并进来。上面 T2-1 那个
  场景下，列表 `items=31` 而批量 `total=30` —— 同一次「处理当前筛选的 N 个」，
  两个端点给出不同的 N，**没有任何测试**覆盖这条跨库差异。
- **为什么算 gap 而不是缺陷**：需要「默认库留同平台历史行」这个前置（T2-1 同一根因），
  在本仓的干净部署里不出现；但它正是 T2-1 会放大的地方，修 T2-1 时必须一起看。

## hypothesis

- **H1**：`_count_accounts` 的 `plus_status` 分支（`api/accounts.py:152-162`）对
  **每一页**都全量拉一次再 `len()`，`plus` 筛选项下的列表是 O(全表) 每请求。
  实测只到 25 行量级，未能证明在 1000+ 行时会成为性能问题 → 不作为发现。
- **H2**：`_LIST_TOKEN_EXTRA_KEYS` 是为 `access_token` 等**正文**建的，但它同时会为
  `has_password` 再写一次 `has_password`（`api/accounts.py:268-280` 的循环里
  `password` 在 `_LIST_TOKEN_EXTRA_KEYS` 里，而 327-330 也写 `has_password`）。
  实测两处写的是同一个值，**未观察到矛盾**，仅记录为冗余。
- **H3**：`get_account` 的 `response: Response = None` 依赖注入在部分 FastAPI 版本上
  可能拿不到对象，导致 `no-store` 头静默丢失。实测该头**存在**，未复现。
- **H4**：`normalize_account_ids` 接受纯数字 id 是为了兼容旧客户端，但
  `AccountModel.legacy_id` 有 `unique=True` 且可为 `None`；若某部署复用整数 id 段，
  可能命中错误账号。未构造出复现，仅记录。

## 覆盖清单（分页筛选 / ids 解析 / action 白名单 / 凭据泄露）

### 分页 / 筛选 / 排序

| 用例 | 结果 | 判定 |
|---|---|---|
| `page=1,2,3`（chatgpt, 30 行, size 20） | 20 / 10 / 0 | 边界正确 |
| 逐页走到空，合并去重 | `total` == 实际行数（干净库） | 一致 |
| `page=0` / `-1` / `-100` | 200，钳成 `page=1` | 不 500 ✔ |
| `page_size=0` | 200，变成 20 | 静默（T2-4） |
| `page_size=-1` / `-50` | 200，变成 1 | 静默（T2-4） |
| `page_size=9999` | 200，钳到 200 | 上界生效 ✔ |
| `page=abc` / `page_size=abc` | 422 | 正确 ✔ |
| `page=1000000` | 200，空 items | 正确 ✔ |
| `status=banned` | total=5 items=5 | 一致 |
| `email=cg0`（小写/嵌套匹配） | total=10 items=10 | 一致 |
| `platform=icloud` / `outlook`（池键） | 200 total=0 | 不 500 ✔（有 `tests/test_db_modular.py:test_accounts_api_returns_empty_for_pool_platforms`） |
| `platform=nope` | 200 total=0 | 合理 |
| `created_at_start/end`（区间中点切） | 16 / 15，互补 | 一致 |
| `start > end` | 200 total=0 | 合理 |
| `created_at_start=not-a-date` | 422 | 正确 ✔ |
| `created_at_start=2026-01-01`（无时区） | 200 | `UtcDatetime` 归一 ✔ |
| `plus_status=plus/unchecked` | 1 / 24，合计 == 25（无筛选总数） | 一致 |
| `plus_status=zzz` | 200 total=0 | 静默（可接受） |
| **`platform=chatgpt` + 默认库历史行** | **total=30 items=31** | **T2-1 缺陷** |
| 排序 | `created_at` 倒序，跨库合并后重新排序 | 正确（`repository.py:1295`） |

### `ids` 解析（`POST /actions/{platform}/{action_id}/batch`）

| 输入 | 结果 | 判定 |
|---|---|---|
| `account_ids=[]` | 400「账号 ID 列表不能为空」 | ✔ |
| `[""]` / `["0"]` / `[-5]` / `["abc"]` | 400（归一后为空） | ✔ 不 500 |
| 大写 UUID | 200，命中 | ✔ 大小写归一 |
| `"  <uuid>  "`（带空白） | 200，命中 | ✔ strip |
| `null` / `[{"id": ...}]` | 422 | ✔ 类型校验 |
| 不存在的 UUID | 200 `failed=1, "账号不存在"` | ✔ 明确 |
| 有效 + 不存在 | 200 `total=2`, 1 成功 1 失败 | ✔ |
| 跨平台 id | 200 `failed=1, "账号不存在"` | ✔ 不误伤 |
| 重复 id ×2 | 200 `total=1` | ✔ 去重 |
| 1001 个 id | 400「单次最多处理 1000 个」 | ✔ |
| 有效 + 垃圾 id 混合 | 200，垃圾 id **无声消失** | T2-5 |
| `[1, 2]`（旧整数 id） | 200 `failed=2` | 兼容路径 ✔ |

### `action` 白名单

| 用例 | 结果 | 判定 |
|---|---|---|
| 未知 action（批量） | **400**，detail 列出可用动作 | ✔（`tests/test_batch_action_validation.py` 三条断言，实测复现） |
| 未知 action（单账号） | 400「未知操作」 | ✔ |
| 未注册平台 | 404 | ✔（同上测试 `test_unregistered_platform_still_404`） |
| 平台间交叉动作（chatgpt 用 grok 的 `probe`） | 400 | ✔ |
| 声明了但配置缺失（`upload_cpa`） | 批量 200 `failed=1`；单账号 200 `ok=false` | 语义不一致 → T2-6 |
| 声明了但未实现（`refresh_oauth` 在 grok 上转发） | 200 `failed=1`，message 说明原因 | 可接受 |

### 凭据泄露

| 检查 | 结果 |
|---|---|
| 列表行字段 | `password`/`password_cipher`/`token`/`token_cipher` **均不在**；有 `has_password`/`has_token` |
| 列表 `extra_json` | 密文集 21 个键全部变成 `has_*` 布尔；`access_token`/`refresh_token`/`session_token`/`id_token`/`cookies`/`cpa_record`/`totp_secret` 正文都不在 |
| 详情字段 | 返回明文 `password`/`token` + 全量 extra（契约如此，`tests/test_accounts_list_payload.py:test_detail_endpoint_still_returns_everything` 钉住） |
| 详情缓存头 | `Cache-Control: no-store` ✔ |
| 动作响应 | `_redact_action_payload` 递归剔除 `_ACTION_SECRET_KEYS`（`api/actions.py:67-80`） |
| **`register_proxy`** | **列表 + 详情都带明文 userinfo** → T2-2 |
| **`api_key` / `apiKey` / `proxy_password`** | **列表原样下发**（不在密文集）→ T2-3 |
| `remote_password_cipher` | 列表原样下发，但值本身是密文（`v1:` 前缀），非明文泄漏 |