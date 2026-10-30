# performance 审计

[MODE: SANDBOX]

- **仓库**：`/data/workspace/Register`，分支 `main`
- **审计基线**：`a3470d3`（本报告全部测量与 `path:line` 的行号口径）。审计期间 HEAD 又被推进到 `519c821`，漂移与复核见"审计前提"第 2 条
- **解释器**：`.venv/bin/python`（3.12.3）
- **机器**：2 vCPU / 1967 MB RAM / 4095 MB swap（审计时已用 2315 MB）/ load average 4.19 / 内核 6.8.0
- **数据**：真实库 `data/account_manager.db`（33,705,984 B）的**只读副本**（`sqlite3.backup()` 复制到 scratch，源库全程 `mode=ro`，未写入一行）。规模：`accounts` 1057（chatgpt 667 / grok 390）、`task_runs` 837、`task_logs` 1661、`configs` 143、`proxies` 5、`icloud_aliases` 1941（`platforms/icloud.db`）；`sum(length(extra_json))=8,387,956 B`、`logs_json=7,365,755 B`、`errors_json=4,719,115 B`，任务日志条目合计 **88,856** 条、errors 条目 **66,830** 条
- **方法**：`git archive a3470d3` 冻结快照 + 数据副本，`127.0.0.1:8000` 起**单 worker** uvicorn；每端点 warmup 1 次 + 7 次 `curl -w` 取样；用 `/proc/<pid>/stat` 的 `utime+stime` 取"进程 CPU 时间/请求"（不受排队影响）；cProfile 定位热点；SQLAlchemy `load` 事件统计每请求物化的 ORM 行数；`EXPLAIN QUERY PLAN`；纯函数微基准

### 审计前提（必须先说的四件事）

1. **审计开始时 8000 端口没有服务**：`curl http://127.0.0.1:8000/api/health` 返回 exit code 7（connection refused），`ss -ltn` 无监听。本报告所有耗时都是我自己按上面的方式起服务测的，不是"线上正在跑的服务"的数字。
2. **HEAD 在审计期间前进了 5 个提交**：`4680c8d → c2c502b → 9630980 → c93b71f → a3470d3 → e6f8670 → 519c821`。我在 18:46 用 `git archive a3470d3` 冻结了快照（`e6f8670`/`519c821` 尚未出现），之后 `api/accounts.py` 仍在被其他 agent 编辑（工作树 SHA 与 HEAD 不一致）。**所有数字都来自 `a3470d3` 的冻结快照**。为了让 `path:line` 现在还能点开，我把每条找行的锚点在 `519c821` 上逐条复核过：只有 `core/db/repository.py` 整体 **+53 行**（`count_all_accounts 1000→1053`、`list_all_accounts 1098→1151`、`per_db 1170→1223`、切片 `1197→1253`），`core/proxy_pool.py`、`services/task_runners.py` 的锚点未动，其余文件（`api/accounts.py`、`api/tasks.py`、`services/task_store_io.py`、`core/config_store.py`、`main.py`、`core/db/base.py`、`api/icloud.py`、`services/icloud/aliases.py`）在两次提交之间**未变**。`519c821` 只改了代理写回路径（`rebind`/`rebalance` 从整行 `upsert` 改成 `update_extra`），不改本报告描述的任何读热路径。
3. **机器本身在跑别的任务**：load 4.19 / 2 核、swap 已用 2.3 GB、可用内存 254 MB。同一端点的 wall 时间被放大且抖动大（例如 `/api/accounts` 的 p50 73 ms、某次取样 254 ms）。因此下表**以"进程 CPU 时间/请求"为主**（进程自身 utime+stime，不受排队影响），wall 只作参考。
4. **a3470d3 刚做过安全收敛**：列表接口不再下发 `token`/`password` 正文（`api/accounts.py:283` 的 `exclude={"password","token"}`）。这直接把 `/api/accounts?page_size=1000` 的响应从旧版本的 3.53 MB 降到 **2.37 MB**，但**没有减少任何 CPU**——每行仍要解密 `extra_json`、解析 JWT、重建 dict。本报告的数字是收敛后的。

---

## 0. 结论摘要

| # | 维度 | 判定 | 关键实测数字 | 主要发现 |
|---|---|---|---|---|
| 1 | 单请求 CPU 成本 | **1 个端点 0.6 秒 CPU、3 个 >0.3 秒** | `ps1000` **578 ms CPU**/2.37 MB；`include_logs` **624 ms CPU**/13.72 MB；`plus_status` **354 ms CPU**/31 B；`icloud/aliases` **410 ms CPU**/712 KB | F-01…F-04 |
| 2 | 全表扫描后再切片 | **9 个端点单次物化 ≥837 行，其中 7 个本可避免** | `page=21` 取 1050 行回 50；`plus_status` **2114** 行回 31 B；`stats` 1057 行回 124 B；`balance` 1062 行回 929 B；`aliases` 3885 行 | F-05…F-09 |
| 3 | 轮询与写放大 | **ETag 只省带宽不省 CPU；每行日志全量重写整份 JSON** | 200 与 304 的 CPU **都是 90 ms**；`_upsert_task_run` **5.72 ms/次**（158 KB 日志）→ 单任务写放大 ≈ **180 MB** | F-10 / F-11 |
| 4 | N+1 与逐行提交 | **鉴权每请求查一次库；批量检测 50 个号 ≈ 1.2 s 纯 DB** | `config_store.get` **0.85 ms/请求**（且重读 `.env`）；单次 `upsert` **22.9 ms** ×50 | F-12 / F-13 |
| 5 | SQLite 配置 | **未开 WAL、`synchronous=FULL`、2 MB page cache、15 条连接** | `journal_mode=delete`、`synchronous=2`、`cache_size=-2000`；单次 `upsert` **22.9 ms** | F-14 |
| 6 | 传输压缩 | **API 完全没有 gzip** | 160 KB→13.8 KB（11.6×）、374 KB→24 KB（15.3×）、2.37 MB→233 KB（10.2×） | F-15 |
| 7 | 启动 | **warm 启动到首个 200 约 7 s** | `import main` 4.9–6.2 s；`init_db` 0.67–0.76 s；import 期串行预压 **240 ms** | F-16 |
| 8 | 并发 | **CPU 饱和后线性退化，重请求拖慢轻请求** | 4 个并发 `ps1000`：单个 **2.4 s**（独占 0.56 s）；轻端点 14 ms → **50 ms** | F-17 / F-18 |

一句话：**SQL 层很便宜（所有端点 SQL 合计 < 10 ms），贵的是 Python——把 1000+ 行读成 ORM 对象、逐行解密/解析/重组 dict、把 13.7 MB JSON 拼出来再算一遍 MD5。1057 行这个量级不该出现 0.6 秒 CPU 的 GET。**

---

## 1. 接口实测（CPU 为主，wall 为辅）

单 worker、回环、无 gzip、每端点 warmup 1 + 7 次取样；CPU 为 5 次请求的进程 CPU 时间均值（含框架与序列化）：

| 端点 | wall p50 (min) | **CPU/请求** | 响应体 | 备注 |
|---|---:|---:|---:|---|
| `GET /api/accounts?page_size=1000` | 818 ms (558) | **578 ms** | 2,366,343 B | 无 page_size 上限 |
| `GET /api/accounts?plus_status=plus` | 820 ms (437) | **354 ms** | **31 B** | 两次全表扫描 |
| `GET /api/tasks?include_logs=1` | 609 ms (481) | **624 ms** | 13,724,404 B | 837 行全列 |
| `GET /api/accounts?page=21&page_size=50` | 368 ms (196) | **178 ms** | 151,293 B | 物化 1050 行 |
| `GET /api/icloud/aliases` | 300 ms (265) | **410 ms** | 712,390 B | 同查询跑两遍 |
| `GET /api/tasks`（前端 2.5 s 轮询） | 276 ms (127) | **90–98 ms** | 374,349 B | ETag 命中仍 90 ms |
| `GET /api/accounts/stats` | 216 ms (121) | **62 ms** | **124 B** | 1057 行整行加载 |
| `GET /api/proxies/balance` | 178 ms (150) | **204 ms** | **929 B** | 扫 1057 号 + 解密 |
| `GET /api/accounts/export` | 147 ms (75) | 82 ms | 104,199 B | 非真流式 |
| `GET /api/accounts?page_size=50`（前端默认） | 93 ms (60) | 50 ms | 160,234 B | 前端默认值 |
| `GET /api/accounts`（默认 20） | 73 ms (45) | 32 ms | 58,612 B | |
| `GET /api/icloud/accounts` | 68 ms (51) | 90 ms | 1,519 B | 1941 行只为计数 |
| `GET /api/tasks/logs?page=1&page_size=50` | 46 ms (33) | 72 ms | 13,568 B | total 全表装载 |
| `GET /api/integrations/panels` | 13 ms (7.6) | — | 1,838 B | |
| `GET /api/proxies` | 10.6 ms (6.7) | 8 ms | 856 B | |
| `GET /api/config` | 8.7 ms (7.8) | 12 ms | 2,338 B | |
| `GET /api/auth/status` | 7.3 ms (5.4) | 6 ms | 39 B | 框架基线 |
| `GET /api/platforms` | 5.8 ms (4.3) | 8 ms | 2,302 B | |
| `GET /`（index.html） | 5.8 ms (4.4) | — | 1,063 B | 未压缩 |

> 小端点的 6–8 ms CPU 基本是 FastAPI/Starlette 的固定开销（含一次 `config_store.get`，见 F-13）；把这条基线减掉，`stats`（62 ms）、`balance`（204 ms）这类"只返回几百字节"的端点几乎全是无用功。

---

## 2. 逐条发现

### F-01（高）`/api/accounts?page_size=1000`：无上限 + 每行 0.58 ms CPU

- **位置**：`api/accounts.py:324`（`page_size = max(1, int(page_size or 20))`——只有下界，**没有上界**）→ `:368`（`[_slim_account_row(row) for row in items]`）→ `:260`（`_slim_account_row`）→ `:283`（`jsonable_encoder`）→ `:225`（`decrypt_extra`）→ `:201`（`_slim_extra_for_list`）→ `:170`（`_at_claims_for_list`）
- **证据**：
  - 实测 `page_size=1000` → **818 ms wall / 578 ms CPU / 2,366,343 B**；`page_size=5000/100000` 都返回全部 1057 行（2,540,284 B）——**上限不存在**。
  - cProfile（1000 行）：`_slim_account_row` **cum 773 ms**（0.77 ms/行）、`_slim_extra_for_list` **cum 465 ms**、`decrypt_extra` **146 ms**、`_at_claims_for_list` **69 ms**（1000 次 base64+JWT 解析）、`jsonable_encoder` 每行新建 dict。
  - 微基准（200 行，`extra_json` 平均 **10,308 B**）：`json.loads` 0.018 ms、`decrypt_extra` 0.025 ms、`_slim_extra_for_list` 0.058 ms、`_slim_account_row` **0.308 ms/行**（p95 0.714）。
  - 前端最大只提供 100（`frontend/src/pages/Accounts.tsx:1279` 的 `pageSizeOptions: ['20','50','100']`），1000 这个值只有脚本/攻击面在用。
- **放大效应**：任意能访问 `/api/*` 的客户端一个 GET 就能让 2 核机器烧掉 **0.58 核·秒**；`page_size=100000` 一次 2.5 MB。这是当前最省力的 CPU 放大入口。
- **建议**：
  1. `page_size` 硬上限 200（与前端最大 100 留余量），在 `api/accounts.py:324` 加 `min(200, ...)`；
  2. 列表**不要解密**：`has_access_token` / `at_exp` 在写入时算好存 `extra_json`（`at_exp` 是常量），读取侧直接读，省掉每行 0.025 ms 的 AES 与 0.07 ms 的 JWT 解析；
  3. 需要全量时走 `/api/accounts/export`（82 ms CPU / 104 KB CSV）。

### F-02（高）`/api/tasks?include_logs=1`：13.72 MB / 624 ms CPU，且前端从不用它

- **位置**：`api/tasks.py:811`（`body = _list_persisted_tasks(include_logs=include_logs)`）→ `services/task_store_io.py:298-299`（`if include_logs: rows = s.exec(select(TaskRunModel)).all()`）→ `services/task_store_io.py:100`（`_task_run_to_snapshot` 逐行 `_json_loads`）
- **证据**：
  - 837 个 `TaskRunModel` 全列物化，响应 **13,724,404 B**（gzip 后仍 812,844 B）。
  - cProfile：`_task_run_to_snapshot` **cum 421 ms**、`_json_loads` **4185 次 / 323 ms**、`_normalize_snapshot` 837 次 / 42 ms；`list_tasks` cum 1,056 ms（含响应序列化）。
  - 库里 `logs_json` 7.37 MB + `errors_json` 4.72 MB，合计 88,856 条日志 + 66,830 条 error。
  - `rg include_logs frontend/src` **零命中**：前端只调 `/api/tasks`（`services/task_store_io.py:240` 的轻量分支）与单任务详情。
- **建议**：这个参数没有调用方，直接删除，或改成 `GET /api/tasks/{id}/logs?page=` 的单任务语义；若必须保留，加 `task_id` 必填 + 分页。删掉后 F-02 的 624 ms CPU 与 13.7 MB 峰值一起消失。

### F-03（高）`plus_status`：一次请求扫两遍全表，820 ms 换 31 字节

- **位置**：`api/accounts.py:332-339`（`total = _count_accounts(...)`）→ `:137-147`（`plus_status` 时 `_count_accounts` 内部调 `_filtered_accounts` 全量）→ `:341-351`（再调一次 `_filtered_accounts` 后 Python 切片）
- **证据**：
  - `GET /api/accounts?plus_status=plus` → ORM 物化 **2114 行 = 1057 × 2**，响应 **31 B**（空数组）。
  - cProfile：`filter_accounts_by_plus_status` **被调 2 次**（`n=2`，cum 568 ms）、`account_plus_status` 2114 次 / 550 ms、`decrypt_extra` 2114 次 / 302 ms、`decrypt` 6186 次 / 240 ms。
  - 实测 **820 ms wall / 354 ms CPU**。
- **建议**：
  1. 短期：`total` 复用 `items` 那次扫描的结果（现在扫两遍），可立刻减半；
  2. 根治：`plus_status` 落成 SQLite 生成列（`json_extract(extra_json,'$.chatgpt_local.plan')`）+ 索引，把过滤与分页下推到 SQL；
  3. 列表不解密（同 F-01）可再省 302 ms 中的大部分。

### F-04（中）`/api/icloud/aliases`：同一条查询跑两遍

- **位置**：`api/icloud.py:339-343`（`"items": list_aliases()` + `"pool": pool_summary()`）→ `services/icloud/aliases.py:88` 与 `:428`（`pool_summary` 内部**又**调 `icloud_alias_repository.list_aliases()`）
- **证据**：ORM `load` 事件 **3885 行**（1941 别名 ×2 + 3 主号），响应 712,390 B 中 `pool` 只有 4 个整数；cProfile：`list_aliases` cum 221 ms + `pool_summary` cum 194 ms，`_from_model` 3882 次 / 69.6 ms。实测 **300 ms wall / 410 ms CPU**。
- **建议**：`pool_summary` 接受已取到的 rows（`pool_summary(rows=rows)`）或改 SQL `GROUP BY pool_status`；只读计数在 `platforms/icloud.db` 上实测 **0.24 ms**（走 `ix_icloud_aliases_account_id` 覆盖索引），比现在的 52 ms `count_by_account` 快 200 倍。

### F-05（中）`/api/accounts/stats`：整行加载 1057 行，只为返回 124 B

- **位置**：`core/db/repository.py:909-910`（`_scan` 里 `rows = list(sess.exec(select(AccountModel)).all())`）与 `:935`
- **证据**：ORM 物化 **1057 行**，响应 **124 B**，实测 **216 ms wall / 62 ms CPU**。同库等价 SQL `select platform,status,count(*) from accounts group by 1,2` 实测 **16 ms**（5 行结果）。
- **建议**：改成 `GROUP BY platform, status` 的聚合查询（每个库一条），预期 62 ms → <5 ms。账号页每次打开都会调它。

### F-06（中）`/api/proxies/balance`：扫 1057 个账号 + 逐个解密，返回 929 B

- **位置**：`api/proxies.py:148-165`（`rows = proxy_repository.list_all(...)` + `counts = proxy_pool.bound_counts()`）→ `core/proxy_pool.py:108-128`（`:120` `for account in account_repository.list_all_accounts()`）→ `AccountModel.get_extra()`（逐行 `json.loads` + 解密）
- **证据**：ORM 物化 **1062 行**（1057 账号 + 5 代理），响应 **929 B**，实测 **178 ms wall / 204 ms CPU**；cProfile：`bound_counts` cum 369 ms（其中 `decrypt_extra` 97 ms、`decrypt` 74 ms、`normalize_proxy_url` 11.7 ms）。`list_all_accounts()` 单次 **51 ms**。
- **注意**：直接用 SQLite JSON1 写 `json_extract(extra_json,'$.register_proxy') GROUP BY` 实测仍要 **128 ms**——因为 `extra_json` 合计 **8.39 MB**，JSON 解析成本躲不掉。**只换 SQL 只能减半，真正的解法是建绑定表**：`account_proxy_binding(account_id, platform, proxy_url)`，绑定/换绑时同步写，`balance` 变成一条带索引的 `GROUP BY`（预期 <5 ms）。
- **建议**：短期先复用 `list_all_accounts()` 的结果做一次性缓存（5 s TTL）或只在代理页可见时刷新；长期建绑定表。

### F-07（中）`/api/tasks/logs`：为了 `total` 把 1661 条日志全装进 ORM

- **位置**：`api/tasks.py:655`（`total = len(s.exec(q).all())`，随后 `:656` 再查一次带 `offset/limit` 的同一条件）
- **证据**：ORM 物化 **1711 个 `TaskLog`**，响应 **13,568 B**，实测 **46 ms wall / 72 ms CPU**。同库 `select count(*) from task_logs` 实测 **0.03 ms**。
- **建议**：`total = s.exec(select(func.count()).select_from(TaskLog).where(...)).one()`，一次 count + 一次分页查询；预期 72 ms → <5 ms。

### F-08（中）深层分页：`per_db = offset + limit`，第 21 页读 1050 行

- **位置**：`core/db/repository.py:1223`（`per_db = None if limit is None else max(0, int(offset)) + int(limit)`）；`:1253`（合并后再 `rows[start:start+limit]`）
- **证据**：`GET /api/accounts?page=21&page_size=50` → ORM 物化 **1050 行**，只返回 50 行/151,293 B，实测 **178 ms CPU**（第 1 页 50 行只要 50 ms CPU）。成本随页码线性增长：第 100 页 = 5000 行。
- **建议**：跨库合并场景用**键集分页**（`WHERE (created_at, id) < (:last_created, :last_id) ORDER BY created_at DESC LIMIT :n`）。本部署目前 `account_platform_keys()==[]`（未配平台库），实际只查默认库，可先在单库路径用 `LIMIT/OFFSET` 下推（SQLite 也要扫过 offset 行，但不在 Python 里物化 ORM 对象），跨库上线后再做键集。

### F-09（中）`/api/icloud/accounts`：`count_by_account` 把 1941 条别名全读出来数

- **位置**：`core/db/icloud_alias_repository.py:511-516`（`for row in s.exec(select(ICloudAliasModel)).all(): counts[key] += 1`）
- **证据**：ORM 物化 **1944 行**，响应 **1,519 B**（3 个主号），实测 **68 ms wall / 90 ms CPU**；等价 `GROUP BY account_id` 实测 **0.24 ms**。
- **建议**：`select(ICloudAliasModel.account_id, func.count()).group_by(...)`，预期 90 ms → <2 ms。

### F-10（高）`_upsert_task_run`：每追加一行日志就全量重写整份 JSON → O(n²)

- **位置**：`services/task_store_io.py:170-171`（`row.logs_json = _json_dumps(normalized["logs"])`、`errors_json` 同理），由 `:181` `persist_task_snapshot()` 触发；而 **`services/task_service.py:95` 的 `log()` 每写一行日志就调一次 `persist_task_snapshot()`**
- **证据（实测）**：取库里日志最多的任务 `task_1793122095317`（**2266 条日志 / 158,093 B**）：
  ```
  _upsert_task_run(该任务)  p50 = 5.72 ms   min 5.45   max 10.08
  ```
  按 2266 行算：写放大 = Σ(70 B × k) ≈ **180 MB**；时间 ≈ 2266 × 平均 2.9 ms ≈ **6.6 s**（写一次的成本随日志增长）。叠加 F-14 的 `synchronous=FULL`，每次都是真实 fsync。
- **建议**：日志单独走已有的 `task_logs` 表（`INSERT`），`persist_task_snapshot` 只写计数器/状态；或至少把快照持久化节流到 1 s 一次（当前每行一次）。这是长任务把磁盘写满/拖慢整机的主要来源。

### F-11（高）`/api/tasks` 的 ETag：先构造 374 KB body 再算 MD5，304 与 200 同价

- **位置**：`api/tasks.py:811`（构造 body）→ `:812`（`hashlib.md5(json.dumps(body, sort_keys=True))`）→ `:816`（命中才 304）
- **证据**：
  - 200：**90 ms CPU** / 374,349 B；304：**90 ms CPU** / 0 B（5 次取样，进程 CPU 完全相同）。
  - 前端每 **2.5 s** 轮询一次（`frontend/src/lib/pollBackoff.ts:13`，有活跃任务时），即单标签页 **3.6% 的 1 个核**常驻；任务页开着一天 ≈ **7.8M CPU·ms**。
  - 其中 `_list_persisted_tasks` 139 ms（cProfile，含 profiler 开销）、`_normalize_snapshot` 837 次 / 27 ms，其余是 374 KB 的 JSON 序列化 + MD5。
- **建议**：ETag 改成 O(1) 指纹（`SELECT max(updated_at), count(*) FROM task_runs`，一条 SQL），命中就直接 304，**不构造 body**；body 只在未命中时构造。预期轮询 90 ms → <5 ms CPU。

### F-12（中）鉴权中间件：每个 `/api/*` 请求都重读 `.env` + 查一次 SQLite

- **位置**：`main.py:216`（`if not _cs.get("auth_password_hash", "")`）→ `core/config_store.py:287-300`（每次 `get()` 都 `with Session(...)` + `s.get(ConfigItem, key)`）→ `:101-111` `_runtime_env_values()` → `:74-83` `_load_env_file()`（**无缓存，每次请求读一遍 `.env` 文件**）
- **证据**：微基准 `config_store.get('auth_password_hash')` **p50 0.852 ms / p95 1.967 ms**（200 次）；`config_store.get('chatgpt_auto_refresh_enabled')` p50 1.241 ms。`/api/config` 总 12 ms CPU，其中 ~0.85 ms 是"查自己有没有设密码"。
- **建议**：`auth_password_hash` 在进程内缓存（`config_store.set()` 时失效），或中间件只在启动时读一次 + `PUT /api/config` 后刷新；`_runtime_env_values()` 用 `mtime` 或 `lru_cache` 缓存。每请求省 ~0.85 ms 与一次连接获取。

### F-13（中）`check_accounts_valid`：全表加载 + 2 条 SQL/账号 + 每号一次 commit

- **位置**：`core/scheduler.py:168-201`：`:171` `rows = account_repository.list_all_accounts(...)`（**无 limit**）、`:174` `[:limit]`、`:184` `account_repository.get(...)`、`:200` `account_repository.upsert(fresh)`
- **证据**：
  - `list_all_accounts()` 实测 **51 ms / 1057 行**，而调用方只要 50 个（`api/accounts.py:725` 的 `/check-all` 传 `limit=50`）——为了 50 个号先物化 1057 个。
  - 单账号 `get()` **0.93 ms**；单次 `upsert()` **22.86 ms**（p50；min 12.97）——每次 upsert 都是独立事务 + `synchronous=FULL` 的 fsync。
  - 50 个账号 ≈ 51 ms（全表）+ 50 ×（0.93 + 22.9）≈ **1.2 s 的纯 DB 时间**，还没算插件的网络探测。
- **建议**：`list_all_accounts(limit=limit)` 下推（仓储已支持 limit）；循环外开一个 Session 复用 identity map；状态变更累积后**一次 commit**（失败再逐条重试）。

### F-14（高）SQLite 用默认配置：无 WAL、`synchronous=FULL`、2 MB page cache、15 条连接

- **位置**：`core/db/base.py:57` `engine = create_engine(DATABASE_URL)`——没有任何 `connect_args`/`PRAGMA`/`pool_size`
- **实测（运行期读 pragma）**：
  ```
  journal_mode = delete          ← 读写互斥：写事务阻塞所有读
  synchronous  = 2 (FULL)        ← 每次 commit 都 fsync
  cache_size   = -2000           ← 2 MB page cache（库 33.7 MB）
  busy_timeout = 5000            ← 冲突时最多阻塞 5 秒
  pool         = QueuePool(size=5, max_overflow=10)  ← 最多 15 条连接
  ```
- **证据**：单次 `account_repository.upsert()` **22.86 ms**（同机同库）；任务日志每条一次 commit（F-10）× 88,856 条历史日志。
- **影响**：读请求（列表/统计）与写（任务快照）在 `delete` 模式下互斥，`busy_timeout=5000` 意味着冲突时**阻塞 5 秒**才报错——这是"偶发 2 秒响应"的另一个来源。
- **建议**：`connect` 事件里统一设 `PRAGMA journal_mode=WAL; synchronous=NORMAL; cache_size=-65536; busy_timeout=5000;`（WAL 下读写不互斥，`NORMAL` 在 WAL 下仍安全），并把 `pool_size` 提到 10–20；写路径合并事务（见 F-10/F-13）。

### F-15（中）API 响应完全没有 gzip

- **位置**：`main.py:258` 只有 `CORSMiddleware`；`main.py:377` 附近解释了"为什么静态资源不用 `GZipMiddleware`"，但**结论被误用到了 API 上**
- **证据（带 `Accept-Encoding: gzip` 请求，响应无 `content-encoding`，传输字节与 identity 完全相同）**：

| 端点 | identity | gzip -6 | 压缩比 |
|---|---:|---:|---:|
| `/api/accounts?page_size=50` | 160,234 | **13,793** | **11.6×** |
| `/api/tasks` | 374,349 | **24,489** | **15.3×** |
| `/api/accounts?page_size=1000` | 2,366,343 | **232,999** | **10.2×** |
| `/api/icloud/aliases` | 712,390 | 125,910 | 5.7× |
| `/api/tasks?include_logs=1` | 13,724,404 | 812,844 | 16.9× |

- **影响**：走 SSH 隧道/公网时，账号页首屏多传 ~146 KB，任务页每次轮询 374 KB。
- **建议**：给 `application/json` 加 `GZipMiddleware(minimum_size=1024)`（对已压缩的静态资源不会重复压），静态资源那条 `_GzipStatic` 路径保持不变。

### F-16（低）启动：warm 7 秒到首个 200，import 税占大头

- **位置**：`main.py:1-30`（15 个 router 全在模块顶层 import）、`main.py:401`（`self._precompress()` 在 `__init__` 里同步执行）
- **证据**：
  ```
  进程启动 → 首个 200：7 s（warm；本机 load 4.19）
  import main        : 6209 ms / 4945 ms（两次）
  init_db()          : 670 ms / 764 ms
  load_all()         : 146 ms / 306 ms
  _GzipStatic 预压   : 240.4 ms（6 个文件，1.60 MB，gzip -9 串行）
  ```
  `-X importtime` self time top：`fastapi.openapi.models` **732 ms**（cum 1,078 ms）、`fastapi.exceptions` 136 ms、`sqlalchemy.sql` 74 ms；本仓库模块 `api.accounts` cum 121 ms、`api.icloud` cum 115 ms、`api.tasks` cum 55 ms。
- **建议**：
  1. 路由懒加载（FastAPI 支持在 lifespan 里 `include_router`），把 `api.accounts`/`api.icloud`/`api.tasks` 移出关键路径；
  2. 预压改 `gzip -6` 或放后台线程（1.14 MB 的 antd 单文件占大头），首屏只压 1 个文件不值得阻塞启动；
  3. `init_db()` 的 0.7 s 大部分是 alembic 版本检查 + `PRAGMA table_info` 循环，可加"schema 版本一致就跳过"的快速路径。

### F-17（低）并发：CPU 饱和后线性退化，重请求把轻请求拖慢 3.5 倍

- **位置**：同步 `def` 端点跑在 Starlette 的 anyio 线程池（默认 40 线程），CPU 只有 2 核；`/api/accounts?page_size=1000` 这类 0.58 s CPU 的请求会占满一个核
- **证据**（同一时刻对运行中的服务压测）：
  ```
  轻端点 /api/auth/status 独占        : p50 14.0 ms
  2 个 ps1000 在飞时同一端点          : p50 49.7 ms（min 33.1，max 298.7）
  4 个 ps1000 并发：wall 2460 ms，单个 2373–2458 ms（独占 554–804 ms），
                    4 个串行 sum 9616 ms → 并发几乎无加速（CPU 绑定）
  ```
- **建议**：先按 F-01/F-03/F-04 把单请求 CPU 降下来；若要多用户并发，改用多 worker（每个 worker 独立 SQLite 连接，WAL 下可并发读），并给重端点加并发闸门（信号量 1–2）。

### F-18（低）批量任务的 `concurrency` 没有上界

- **位置**：`api/tasks.py:118`（`concurrency: int = 1`，无 `le=`；refresh/bind2fa/probe 等多处同款）→ `services/task_runners.py:640`（`max_workers = max(1, min(int(concurrency or 1), max(total, 1)))`）→ `ThreadPoolExecutor(max_workers=...)`
- **对比**：register 任务有上限（`api/tasks.py:74` `le=MAX_REGISTER_CONCURRENCY`，值为 200）
- **证据**：代码路径明确；`concurrency=500` 会直接创建 500 个线程（每个都在打网络），在 2 GB 机器上是内存/句柄风险。
- **建议**：给所有批量请求模型的 `concurrency` 加 `le=`（与 register 一致，或按平台设 20–50），并在 runner 侧再 clamp 一次。

### F-19（低）`index.html` 未压缩

- **位置**：`main.py:564-600` 的 `spa_fallback` 用 `FileResponse(index_path)` 直接返回，没走 `_GzipStatic`
- **证据**：`_GzipStatic` 已经为 `index.html` 预压出 592 B（`_gzip_cache` 里有），但 `GET /` 返回 `content-length: 1063`、无 `content-encoding`。
- **建议**：spa_fallback 复用 `_GzipStatic` 的缓存结果。1 KB 影响不大，顺手修即可。

---

## 3. 内存与 swap

| 指标 | 数值 |
|---|---:|
| 就绪时 `VmRSS` / `VmSwap` | **96,696 KB** / 0 |
| 压测后 `VmHWM`（峰值） | **199,776 KB** |
| 压测后 `VmRSS` / `VmSwap` | 158,996 KB / 5,988 KB |
| 进程 major fault 累计 | 12,648 |
| 系统内存 / swap | 1967 MB 总、1713 MB 已用 / 2315 MB 已用 |

**F-20（中）大响应把峰值内存推到 ~200 MB**：`include_logs` 一次要同时持有 837 个 ORM 对象、13.7 MB 的 body dict、13.7 MB 的 JSON 字符串、再一份 MD5 输入；`ps1000` 要持有 1000 个解密后的 dict。峰值过后 RSS 不回落（159 MB），在 2 GB 机器上直接把服务推进 swap，于是 wall 时间的尾巴被放大（`page=21` 的 p50 368 ms vs max 1662 ms）。建议：删掉 `include_logs`（F-02）、ETag 用 O(1) 指纹（F-11）、`JSONResponse` 改 `StreamingResponse` + `iterencode`，并给容器加 `mem_limit` 监控 `working_set` 而非只看 RSS。

---

## 4. 修复清单（按 收益/成本 排序）

| 优先级 | 改动 | 位置 | 预期收益（按实测推算） |
|---|---|---|---|
| P0 | `page_size` 上限 200 | `api/accounts.py:324` | 单请求 578 ms CPU → ~120 ms；堵住 2.5 MB 放大 |
| P0 | ETag 改 O(1) 指纹，命中不建 body | `api/tasks.py:811-816` | 轮询 90 ms → <5 ms CPU；每标签页省 ~3.4% 核 |
| P0 | 去掉/收敛 `include_logs` | `api/tasks.py:811`、`services/task_store_io.py:298` | 624 ms CPU / 13.7 MB → 不再存在 |
| P0 | 日志追加走 `task_logs` 表，快照只写计数器 | `services/task_store_io.py:170-171`、`services/task_service.py:95` | 每行 5.72 ms → <0.5 ms；写放大 O(n²)→O(n) |
| P1 | SQLite 开 WAL + `synchronous=NORMAL` + 64 MB cache | `core/db/base.py:57` | 读写不再互斥；单次 upsert 22.9 ms 降一个数量级 |
| P1 | `stats` / `count_by_account` 改 SQL 聚合 | `core/db/repository.py:910`、`core/db/icloud_alias_repository.py:513` | 62 ms → <5 ms；90 ms → <2 ms |
| P1 | `plus_status` 双扫改单扫 + 生成列 | `api/accounts.py:332-351` | 354 ms → ~120 ms（单扫）→ <20 ms（生成列） |
| P1 | `/api/tasks/logs` 用 `COUNT(*)` | `api/tasks.py:655` | 72 ms → <5 ms |
| P1 | API 加 gzip | `main.py:258` | 160 KB→13.8 KB、374 KB→24 KB |
| P1 | `pool_summary` 复用 rows / `GROUP BY` | `services/icloud/aliases.py:428` | 410 ms → ~200 ms |
| P2 | 深分页改键集分页 | `core/db/repository.py:1223,1253` | 第 21 页 1050 行 → 50 行；178 ms → ~30 ms |
| P2 | 列表不解密（`has_*`/`at_exp` 写入时算好） | `api/accounts.py:225,232` | 0.31 ms/行 → ~0.1 ms/行 |
| P2 | `config_store.get` 进程内缓存 + `.env` 缓存 | `core/config_store.py:287,101` | 每请求 -0.85 ms、-1 条 SQL |
| P2 | `check_accounts_valid` 下推 limit + 单 Session + 批 commit | `core/scheduler.py:171,184,200` | 50 个号 1.2 s → ~0.1 s |
| P2 | 代理建 `account_proxy_binding` 表 | `core/proxy_pool.py:120` | `balance` 204 ms → <5 ms（SQL1 只能到 128 ms） |
| P3 | 启动路由懒加载 + 预压降级/后台 | `main.py:1-30,401` | 启动 -0.3~0.8 s、-240 ms |
| P3 | 批量任务 `concurrency` 加上界 | `api/tasks.py:118` 等 | 防 500 线程 |
| P3 | `index.html` 走预压缓存 | `main.py:564-600` | 1 KB |

---

## 附录 A：复现方法

```bash
# 0) 真实库只读副本（不碰生产库）
/data/workspace/Register/.venv/bin/python - <<'PY'
import sqlite3
src, dst = "data/account_manager.db", "/tmp/perf/account_manager.db"
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True); d = sqlite3.connect(dst)
s.backup(d); d.close(); s.close()
PY

# 1) 冻结基线并起服务（工作树在审计期间被并发编辑，必须冻结）
git archive a3470d3 | tar -x -C /tmp/perf/src
cp -a /data/workspace/Register/static /tmp/perf/src/     # static/ 是未跟踪的构建产物
cd /tmp/perf/src && DATA_DIR=/tmp/perf/data \
  /data/workspace/Register/.venv/bin/python -m uvicorn main:app --port 8000 --workers 1

# 2) 逐端点计时（warmup 1 + 取样 7）
curl -s -o /dev/null -w '%{http_code} %{time_total} %{size_download}\n' \
  'http://127.0.0.1:8000/api/accounts?page_size=1000'

# 3) 进程 CPU 时间/请求（不受 load 影响）
awk '{print $14+$15}' /proc/<pid>/stat     # utime+stime，单位 tick（100/s）

# 4) 每请求物化了多少行（ORM load 事件）见 scratch/measure_rows.py
# 5) 304 路径：取 ETag 后带 If-None-Match 重放，比较 CPU 与 body
# 6) 启动：`python startup_breakdown.py` + `python -X importtime -c "import main"`
```

## 附录 B：原始数据文件（scratch，未入库）

| 文件 | 内容 |
|---|---|
| `timings2.json` | 19 个端点的 wall p50/min/max 与字节数 |
| `rows.json` | 每端点的 ORM 物化行数 / SQL 条数 / 响应字节 |
| `profile_own.json` | 6 个热端点的仓库内函数 CPU/cum 归因 |
| `microbench.py` 输出 | `config_store.get`、`_upsert_task_run`、`_slim_account_row`、SQLite pragma |
| `concurrency.py` 输出 | 独占 vs 并发、轻请求被拖慢的实测 |
| `repo_bench.py` 输出 | `list_all_accounts` / `bound_counts` / `count_by_account` 与等价 SQL 的对比 |
