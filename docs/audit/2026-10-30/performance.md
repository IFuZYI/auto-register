# performance 审计

[MODE: SANDBOX]

- **仓库**：`/data/workspace/Register`，分支 `main`，审计目标 HEAD `4680c8d`（审计期间仓库被其他 agent 推进到 `c93b71f`，见"审计前提"第 3 条）
- **解释器**：`.venv/bin/python`（3.12.3）
- **机器**：2 vCPU / 1967 MB RAM / 4095 MB swap（审计时已用 2048 MB）/ 内核 6.8.0
- **数据**：真实库 `data/account_manager.db`（33.7 MB）的 **只读副本**（`sqlite3.Connection.backup()` 复制到 scratch，全程 `mode=ro` 打开源库，未写入一行）。规模：`accounts` 1057（chatgpt 667 / grok 390）、`task_runs` 837、`task_logs` 1661、`configs` 143、`proxies` 5、`icloud_aliases` 1941（平台库）
- **方法**：`git archive 4680c8d` 冻结快照 + 数据副本，在 `127.0.0.1:8000` 起单 worker uvicorn，用 `curl -w` 逐条打点（每端点 warmup 1 次 + 8~10 次取样）、`cProfile` 定位热点、SQLAlchemy `before/after_cursor_execute` + ORM `load` 事件统计每请求的 SQL 条数与**实际物化的行数**、`-X importtime` 拆启动

### 审计前提（两件必须先说的事）

1. **审计开始时 8000 端口没有服务在跑**：`curl http://127.0.0.1:8000/` 返回 exit code 7（connection refused），`ss -ltn` 无监听。本报告的所有耗时都是我自己按上面的方式起服务测的，不是"线上正在跑的服务"的数字。
2. **工作树在审计期间被并发编辑**：`git status` 有 3 个未提交改动（`core/db/models_account.py`、`core/db/models_infra.py`、`migrations/env.py`，mtime 18:10:33 / 18:11:00）。18:09 我第一次尝试启动时直接崩在 import：

```
File "/data/workspace/Register/core/db/models_infra.py", line 21, in TaskLog
    __table_args__ = (Index("ix_task_logs_platform_id", "platform", "id"),)
NameError: name 'Index' is not defined
```

   这是"改到一半"的瞬时状态（`from sqlalchemy import Index` 还没落盘），一分钟后再 import 就正常了。为了让数字可复现、且不受别人继续改文件影响，**全部测量基于 `git archive 4680c8d` 的冻结快照**。

3. **审计期间仓库被并发推进**：其他 agent 在 18:10–18:37 之间提交了 `c2c502b`（模型/迁移索引）、`9630980`（actions UUID）、`c93b71f`（auth 加固，**把回环判定搬到 `core/net_utils.py`，`main.py` 因此整体上移 15 行**）。本报告的性能结论全部来自 `4680c8d` 的冻结快照；为了让 `path:line` 现在还能点得开，**下文 `main.py` 的行号已按当前 HEAD `c93b71f` 重新核对**（`231→216`、`411→401`、`330→320`、`269→258`、`588→573`），其余文件（`api/accounts.py`、`api/tasks.py`、`core/db/repository.py`、`core/proxy_pool.py`、`services/*`）在两次 commit 之间未变，行号与 `4680c8d` 一致。三个 commit 都不改本报告描述的任何热路径逻辑（只改了索引声明、UUID 转换、鉴权分支与 CORS 头）。

---

## 0. 结论摘要

| # | 维度 | 判定 | 关键实测数字 | 主要发现 |
|---|---|---|---|---|
| 1 | 接口耗时 | **1 个端点接近 1 秒，4 个 >200 ms** | `/api/accounts?page_size=1000` **988 ms / 3.53 MB**；`/api/tasks?include_logs=1` **810 ms / 13.72 MB**；`/api/icloud/aliases` 428 ms；`/api/proxies/balance` 213 ms | F-01…F-06 |
| 2 | 全表扫描 | **8 个端点把 1000+ 行读进内存再切片** | `page=21` 取 1050 行只回 50；`plus_status` 取 **2114** 行回 31 B；`/tasks/logs` 取 1711 行回 50；`/proxies/balance` 取 1057 行回 999 B；`/icloud/aliases` 取 3882 行 | F-07…F-12 |
| 3 | N+1 | **4 处真 N+1 + 1 处 O(n²)** | 鉴权中间件每请求 1 条 SQL；`check_accounts_valid` 2 条/账号；任务探测同账号查 2 次；**日志每写一行全量重写 146 KB（9.6 ms）** | F-13…F-18 |
| 4 | 启动 | **冷启 11.7 s / 热启 4.8–6.4 s** | `import main` 2388 ms（第三方 import 图 ~1.2 s，`fastapi.openapi.models` 单个 236 ms）；`init_db` 304 ms；静态资源预压 136 ms | F-19 |
| 5 | 内存 / swap | **峰值 244 MB，89 MB 被换出** | 就绪 96.6 MB → 重请求后 HWM **244 MB**，`VmSwap` **89 MB**；5 次重请求触发 **1054 次 major fault（211 次/请求）** | F-20 |
| 6 | SQLite 配置 | **未开 WAL、`synchronous=FULL`、2 MB page cache** | `journal_mode=delete`、`synchronous=2`、`cache_size=-2000`、连接池 5+10 | F-21 |
| 7 | 定时任务 | **当前全关，开销≈0；打开后一轮 0.4–1.0 s** | tick 9.2 ms / 2 SQL；`run_auto_refresh_pass` 扫 667 账号 **402 ms**（冷轮 1002 ms / 43 SQL） | F-22 |
| 8 | 前端首屏 | **7 请求 / 477 KB(gzip)，antd 占 71%** | `vendor-antd` 1.14 MB → **346 KB gzip**；API **完全没有 gzip**：242 KB 可压到 41 KB（5.9×） | F-23…F-25 |

一句话：**单请求的瓶颈不在 SQL（全部端点 SQL 合计 < 10 ms），而在 Python 侧——把整表读进 ORM、逐行解密/序列化、以及把 13 MB JSON 在内存里拼出来再算 MD5。** 1057 行这个量级本不该出现秒级响应。

---

## 1. 接口实测耗时（curl 逐条）

### 1.1 全量打点表

单 worker、回环、无 gzip、每端点 warmup 1 次后取样 8 次（`%{time_total}` / `%{size_download}`）：

| 端点 | p50 | p95 | min | max | 响应体 |
|---|---:|---:|---:|---:|---:|
| `GET /api/accounts?page_size=1000` | **988.4 ms** | 1396.7 | 890.0 | 1396.7 | 3,526,673 B |
| `GET /api/tasks?include_logs=1` | **809.5 ms** | 891.6 | 632.2 | 891.6 | 13,724,404 B |
| `GET /api/icloud/aliases` | **427.5 ms** | 450.1 | 300.6 | 450.1 | 712,390 B |
| `GET /api/proxies/balance` | **212.7 ms** | 302.6 | 170.7 | 302.6 | **999 B** |
| `GET /api/accounts/export`（CSV） | 171.9 ms | 315.2 | 119.0 | 315.2 | 104,199 B |
| `GET /api/tasks` | 171.6 ms | 250.6 | 91.8 | 250.6 | 374,349 B |
| `GET /api/accounts?page=21&page_size=50` | 132.9 ms | **2068.4** | 103.4 | 2068.4 | 243,819 B |
| `GET /api/accounts?page_size=50`（**前端默认**） | 107.6 ms | 232.4 | 54.7 | 232.4 | 242,654 B |
| `GET /api/accounts/stats` | 105.0 ms | 210.9 | 61.8 | 210.9 | **124 B** |
| `GET /api/icloud/accounts` | 89.9 ms | 377.4 | 75.6 | 377.4 | 1,519 B |
| `GET /api/accounts`（默认 20） | 89.4 ms | 125.9 | 44.3 | 125.9 | 96,213 B |
| `GET /api/tasks/logs?page=1&page_size=50` | 51.9 ms | 172.9 | 39.1 | 172.9 | 13,568 B |
| `GET /api/accounts?platform=chatgpt` | 34.6 ms | 68.6 | 20.4 | 68.6 | 96,212 B |
| `GET /api/integrations/panels` | 10.9 ms | 123.0 | 8.9 | 123.0 | 1,838 B |
| `GET /api/proxies` | 9.9 ms | 13.1 | 5.4 | 13.1 | 926 B |
| `GET /api/config` | 9.7 ms | 13.6 | 8.1 | 13.6 | 2,338 B |
| `GET /api/auth/status` | 9.0 ms | 12.0 | 4.7 | 12.0 | 39 B |
| `GET /api/platforms` | 6.2 ms | 9.8 | 5.1 | 9.8 | 2,302 B |
| `GET /`（index.html） | 5.4 ms | 8.3 | 3.9 | 8.3 | 1,063 B |
| `GET /assets/vendor-antd-*.js`（identity） | 16.6 ms | 30.2 | 12.8 | 30.2 | 1,140,405 B |

> **p95 尾巴的来源不是代码**：这台机器 1967 MB 内存已用 1696 MB、swap 已用 2048/4095 MB，服务进程 89 MB 常驻被换出。见 F-20：5 次重请求产生 1054 次 major fault。`page=21` 的 p50 132 ms 与 p95 2068 ms 差 15 倍就是这个原因，不是抖动噪声可以解释的。

### 1.2 最慢的 6 个端点与代码热点

#### F-01（高）`/api/accounts?page_size=1000` — 988 ms / 3.53 MB，逐行解密+序列化是唯一热点

- **位置**：`api/accounts.py:344-352`（取数）→ `api/accounts.py:359`（`_slim_account_row`）→ `api/accounts.py:279` `jsonable_encoder` → `api/accounts.py:282` `_slim_extra_for_list` → `api/accounts.py:225` `decrypt_extra` → `api/accounts.py:232` `_at_claims_for_list`
- **证据（cProfile，1000 行）**：

```
1540.7ms  list_accounts
1363.5ms  _slim_account_row        ×1000   → 1.36 ms/行
 809.3ms  _slim_extra_for_list     ×1000
 331.9ms  (anyio threadpool 调度)
 141.1ms  _filtered_accounts
 116.3ms  _at_claims_for_list      ×1000   (base64+JWT 解析)
```

- **微基准（200 行样本，`extra_json` 平均 7.7 KB）**：

```
json.loads            0.045 ms/行
decrypt_extra         0.088 ms/行
_slim_extra_for_list  0.202 ms/行
jsonable_encoder      0.055 ms/行
```

- **放大因子**：`page_size` 无上限（`api/accounts.py:311-312` 只有 `max(1, int(...))`，没有上限）。任意客户端一个 GET 就能让 2 核机器烧掉 1 秒 CPU。这是**未认证面**（回环放行）上最省力的 DoS 入口。
- **建议**：
  1. `page_size` 硬上限 200（前端最大选项就是 100，见 `frontend/src/pages/Accounts.tsx:1256`）；
  2. 列表不要解密：`has_access_token` / `at_exp` 在**写入时**算好存进 `extra_json`（`at_exp` 是常量，不随读取变化），读取侧直接读，省掉 0.088 ms/行的 AES；
  3. 真需要全量时走 `/api/accounts/export`（171 ms，CSV 流式），不要用列表接口。

#### F-02（高）`/api/tasks?include_logs=1` — 810 ms / 13.72 MB

- **位置**：`api/tasks.py:811` → `services/task_store_io.py:298-299`（`include_logs=True` 走 `select(TaskRunModel)` 全列）→ `services/task_store_io.py:100` `_task_run_to_snapshot`（逐行 `_json_loads`）
- **证据**：837 个任务全量物化；`_json_loads` 调用 **4185 次**（350.1 ms）、`_task_run_to_snapshot` 837 次（466.3 ms）、`_normalize_snapshot` 837 次（51.3 ms）。库里 `logs_json + errors_json` 合计 **11.53 MB**，最大单条 `errors_json` 36.3 KB。
- **关键点**：**前端从不传 `include_logs`**（`rg include_logs frontend/src` 零命中）。这 13.7 MB 的路径只有脚本/测试在用，却是一个公开 GET。
- **建议**：把 `include_logs` 改成 `?logs=1&task_id=...` 的单任务语义，或直接删除该参数（明细已有 `GET /api/tasks/{id}`）；至少加 `page/page_size`。

#### F-03（高）`/api/icloud/aliases` — 428 ms / 712 KB，同一条查询跑了两遍

- **位置**：`api/icloud.py:339-345`（`list_aliases()` + `pool_summary()`）→ `services/icloud/aliases.py:92` 与 `services/icloud/aliases.py:428` **各自**调 `icloud_alias_repository.list_aliases()`
- **证据**：ORM `load` 事件统计 **3882 行 = 1941 × 2**（别名表实际 1941 行、3 个主号）；响应 712,390 B，其中 `pool` 字段只有 4 个整数。
- **建议**：`pool_summary` 接受已取到的 rows（`pool_summary(rows=rows)`）或在 SQL 层 `GROUP BY pool_status`（1 行 4 列），省掉一次 1941 行全表 + 一次 JSON 组装。

#### F-04（中）`/api/proxies/balance` — 213 ms 只为返回 999 B

- **位置**：`api/proxies.py:128-142` → `core/proxy_pool.py:108-128` `bound_counts()` → `core/proxy_pool.py:120` `account_repository.list_all_accounts()`（**无 limit**）
- **证据**：ORM 物化 **1057 个 AccountModel + 5 个 ProxyModel**，逐行 `account.get_extra()`（json.loads 7.7 KB）→ 只为统计"每个代理挂了几个号"。cProfile：`bound_counts` 623 ms（含 profiler 开销）/ curl 213 ms。
- **建议**：`SELECT json_extract(extra_json,'$.register_proxy') AS url, platform, count(*) FROM accounts GROUP BY 1,2`（SQLite JSON1，本库可用），或建一张 `account_proxy_binding(account_id, proxy_url)` 索引表。这个端点被代理页每次打开都调。

#### F-05（中）`/api/tasks` — 172 ms / 374 KB，且 304 仍要 ~98 ms

- **位置**：`api/tasks.py:811-818`
- **证据**：837 个任务 → `_list_persisted_tasks` 246.8 ms；`_json_loads` **2511 次**（61.4 ms）；`_normalize_snapshot` 837 次（44.3 ms）；最后 `hashlib.md5(json.dumps(body, sort_keys=True))` 对 374 KB 再序列化一遍。
- **304 实测**（前端每 2.5 s 轮询一次，`frontend/src/lib/pollBackoff.ts:13`）：

```
304 0.112s / 0.194s / 0.099s / 0.079s / 0.082s / 0.099s / 0.081s / 0.097s   body=0 B
```

  即 **ETag 只省了带宽，没省 CPU**：每次轮询仍要建 837 个快照 dict、拼 374 KB 字符串、算 MD5。
- **建议**：ETag 用「库里 `max(updated_at)` + `count(*)`」这种 O(1) 指纹（1 条 SQL），命中就直接 304，不再构造 body；body 只在未命中时构造。按 2.5 s 轮询算，单标签页每年省掉的 CPU 约等于 1 个核的 3.5%。

#### F-06（中）`/api/accounts` 默认列表 — 89–108 ms，其中 40 ms 是"为了 total 全表扫"

- **位置**：`api/accounts.py:323` `total = _count_accounts(...)` → `core/db/repository.py:999-1039` `count_all_accounts` → 不指定 platform 时走 `core/db/repository.py:1071-1095` `_distinct_keys()`
- **证据**：`EXPLAIN QUERY PLAN SELECT platform, email FROM accounts` → **`SCAN accounts`**（1057 行全表）；cProfile `_count_accounts` 40.6 ms（`_distinct_keys` 40.0 ms）。它每次列表请求都跑，即使用户只看第 1 页。
- **建议**：`total` 允许"近似"或缓存 5 s；更彻底的做法是给 `(platform, lower(email))` 已有唯一索引（`uq_accounts_platform_email`）加一个覆盖索引 `(platform, email)`，让 `_distinct_keys` 走 covering index（当前 `platform,email` 两列没有联合索引，只能全表扫）。

---

## 2. 全表扫描：把 1000+ 行读进内存再切片

用 ORM `load` 事件统计"每个请求实际物化了多少行 ORM 对象"（比 `cursor.rowcount` 可靠，SQLite 对 SELECT 返回 -1）：

| 端点 | 物化行数 | 实际返回 | 代码位置 | 说明 |
|---|---:|---|---|---|
| `GET /api/accounts?page=21&page_size=50` | **1050** | 50 行 / 243,819 B | `core/db/repository.py:1170` `per_db = offset + limit`；`:1197-1200` 合并后再切 | 翻到第 21 页就要读 1050 行；第 100 页读 5000 行 |
| `GET /api/accounts?page_size=1000` | **1000** | 1000 行 / 3.53 MB | `api/accounts.py:344` | 客户端可控 |
| `GET /api/accounts?plus_status=plus` | **2114** | **31 B**（空结果） | `api/accounts.py:332-342` + `:137-147` + `:100-109` | `total` 与 `items` **各扫一遍全表**（`_count_accounts` 一次、`_filtered_accounts` 一次） |
| `GET /api/accounts/stats` | **1057** | 124 B | `core/db/repository.py:837-889`（`_scan` 里 `list(sess.exec(select(AccountModel)).all())`，`:857`） | 纯计数却加载整行；应为 `GROUP BY platform, status` |
| `GET /api/accounts/export` | 1057 | 1057 行 / 104 KB | `api/accounts.py:424-436` | **合理**（导出就是要全量），但可流式 |
| `GET /api/tasks/logs?page=1&page_size=50` | **1711** | 50 行 / 13,568 B | `api/tasks.py:655` `total = len(s.exec(q).all())` | 为了 total 把 1661 条日志全装进 ORM，再 `offset/limit` 查第二遍 |
| `GET /api/proxies/balance` | **1057** | 999 B | `core/proxy_pool.py:120` | 见 F-04 |
| `GET /api/icloud/aliases` | **3882** | 712 KB | `api/icloud.py:339-345` + `services/icloud/aliases.py:92,428` | 1941×2，见 F-03 |
| `GET /api/icloud/accounts` | **1944** | 1,519 B | `core/db/icloud_alias_repository.py:511-516` | `count_by_account()` 为算 3 个主号的别名数，把 1941 条别名全读出来在 Python 里数 |
| `GET /api/tasks?include_logs=1` | 837 | 13.72 MB | `services/task_store_io.py:298-299` | 见 F-02 |

### F-07（高）深分页：`offset+limit` 下推，但每库都取 `offset+limit` 行

- **位置**：`core/db/repository.py:1169-1181`，注释自己写明了："每库最多需要 offset+limit 行"；`:1197-1200` 再 `rows.sort()` + 切片。
- **证据**：`page=21&page_size=50` → `per_db = 1000 + 50 = 1050` → ORM 物化 1050 行，只为返回 50 行（p50 132 ms，p95 2068 ms）。`page=1` 只物化 50 行（107 ms）。
- **代价**：第 N 页的成本是 O(N×page_size)，翻到第 100 页 = 5000 行 + 排序。1057 行的库现在还不致命，但这是**线性放大**，账号到 1 万时第 100 页就是 10 万行。
- **建议**：跨库合并场景用**键集分页**（`WHERE (created_at, id) < (:last_created, :last_id) ORDER BY created_at DESC LIMIT :n`），或先只在默认库分页（当前 1057 行账号全在默认库）。

> 顺带一个事实：本部署 `platform_database_registry.configured_platforms() == []`（没有配任何 `DATABASE_URL_<平台>`），所以 `account_platform_keys()` 返回空列表，列表请求实际**只查默认库**（实测 `/api/accounts` 全请求 3 条 SQL：鉴权配置 1 + 去重键 1 + 取数 1）。`data/platforms/*.db` 里只有 `icloud.db` 有数据（1941 条别名）。**但这条跨库路径一旦按文档配上 `DATABASE_URL_GROK` 等就会立刻生效**：每多一个平台库，列表/计数/均衡各多一次连接 + 一次查询（`core/db/repository.py:1173-1175`、`:1087-1089`、`:873-876`），届时深分页的 `offset+limit` 会按库数再乘一遍。现在不改，等分库上线就是 N 倍。

### F-08（高）`plus_status` 过滤：一次请求扫两遍全表，且分页在 Python 里做

- **位置**：`api/accounts.py:332-342`（注释："内存过滤路径：取全量再切片"）、`api/accounts.py:100-109`、`api/accounts.py:137-147`
- **证据**：`GET /api/accounts?plus_status=plus` → 物化 **2114 行**（= 1057 × 2），p50 **697 ms**，返回 **31 B**（空数组）。cProfile：`_filtered_accounts` ×2 = 1655 ms（含 profiler），`_add` 2114 次。
- **建议**：`plus_status` 落到 `extra_json` 的生成列（SQLite `ALTER TABLE accounts ADD COLUMN plus_status TEXT GENERATED ALWAYS AS (json_extract(extra_json,'$.chatgpt_local.plan')) VIRTUAL`）+ 索引，即可下推 SQL；在迁移完成前，至少让 `total` 复用 `items` 那次扫描的结果（现在扫两遍）。

### F-09（中）`/api/tasks/logs`：为了 total 全表装载

- **位置**：`api/tasks.py:650-657`
- **证据**：1661 条日志 → 物化 **1711** 个 `TaskLog` 对象（cProfile：`get_logs` 313.8 ms，`TaskLog.__new__` 1711 次 35.5 ms），然后**再查一次**带 `offset/limit` 的同一条件。响应只有 13,568 B。
- **建议**：`total = s.exec(select(func.count()).select_from(TaskLog).where(...)).one()`；一次 `count` + 一次分页查询。

### F-10（中）`/api/accounts/stats`：整行加载只为计数

- **位置**：`core/db/repository.py:856-857`（`rows = list(sess.exec(select(AccountModel)).all())`）
- **证据**：1057 行 ORM 物化 → 返回 124 B，p50 105 ms。每次打开账号页都会调。
- **建议**：`select(AccountModel.platform, AccountModel.status, func.count()).group_by(...)`，每个库一条（当前只有默认库），合计 < 5 ms。

### F-11（中）`/api/icloud/accounts`：`count_by_account` 全表

- **位置**：`core/db/icloud_alias_repository.py:494-516`（`for row in s.exec(select(ICloudAliasModel)).all(): counts[key] += 1`）
- **证据**：1941 条别名 → 物化 1944 行，返回 1,519 B（3 个主号）。
- **建议**：`select(ICloudAliasModel.account_id, func.count()).group_by(ICloudAliasModel.account_id)`。

### F-12（低）`/api/accounts/export`：可接受，但仍是全量 ORM

- **位置**：`api/accounts.py:424-436`
- **证据**：1057 行 → 104 KB CSV，p50 172 ms；`StreamingResponse` 里其实只有**一个** chunk（`iter([output.getvalue()])`），没有真正流式。
- **建议**：`yield` 按行生成（`writer.writerows` 分块），并把 `list_all_accounts` 换成 `yield_per`/游标，峰值内存可从 ~10 MB 降到常数。

---

## 3. N+1：循环里开 Session / 查库

### F-13（高）鉴权中间件：每个 `/api/*` 请求多 1 条 SQL

- **位置**：`main.py:215-216` → `core/config_store.py:287-300`（每次 `get()` 都 `with Session(...)` + `s.get(ConfigItem, key)`，**无缓存**）
- **证据**：微基准 `config_store.get('auth_password_hash')` **0.85 ms/次**（p50，200 次）；cProfile 里每个端点的 `auth_middleware` 都占 3–7 ms（含 `_runtime_env_values()` 0.16 ms 的 `.env` 重读）。
- **影响**：`/api/config` 总耗时 9.7 ms，其中 0.85 ms 是"查自己有没有设密码"；轮询类端点（2.5 s 一次）每年多跑 1260 万条无用 SQL。
- **建议**：`auth_password_hash` 在进程内缓存（`config_store.set()` 时失效），或中间件只在启动时读一次 + `PUT /api/config` 后刷新。

### F-14（中）`check_accounts_valid`：2 条 SQL/账号

- **位置**：`core/scheduler.py:177-200`（`for acc in accounts:` → `:184` `account_repository.get(...)` → `:200` `account_repository.upsert(fresh)`）
- **证据**：`POST /api/accounts/check-all` 以 `limit=50` 调它（`api/accounts.py:716-722`）→ **100 条 SQL / 50 个账号**，且每个 `upsert` 都是独立事务（`synchronous=FULL` 下每次 fsync）。
- **建议**：循环外开一个 Session，用 `s.get()` 复用 identity map；状态变更累积后一次 commit。

### F-15（中）任务探测：同一个账号查两次

- **位置**：`services/task_runners.py:778-782` 与 `:787-789`（`_run_probe_status._handle` 里两个 `platform_session("chatgpt")` + `s.get(AccountModel, account_id)` 同一主键）
- **证据**：代码路径明确——第一次取 `plat_account` 打网络，第二次再取一遍写回结果。批量探测 100 个号 = 200 条 SELECT + 200 次连接获取。`services/task_runners.py:950-960`、`:1029-1030` 同款。
- **建议**：第一次 `s.get()` 后把对象 detached 保存，写回时用 `s.merge()`；或直接复用同一个 Session（网络调用期间不持连接可以用 `s.expunge()`）。

### F-16（低）批量删除日志：N 次 DELETE

- **位置**：`api/tasks.py:676-683`（`for log in logs: s.delete(log)`）
- **证据**：上限 1000 条（`api/tasks.py:666`）→ 最坏 1000 条独立 `DELETE` + 1 次 commit。
- **建议**：`s.exec(delete(TaskLog).where(TaskLog.id.in_(ids)))`，一条 SQL。

### F-17（低）面板回填：每个平台开一次 Session

- **位置**：`api/integrations.py:205-206`（`for platform_key, platform_rows in by_platform.items(): with platform_database_registry.session_for(platform_key) as s:`）
- **证据**：平台数 = 2（chatgpt/grok），当前代价可忽略；但这是"平台数 × 账号数"的双层循环，且内层每个账号一次 `backfill_...commit=True`（`:214`）——100 个号 = 100 次 commit。
- **建议**：把 `commit=True` 改成整批一次 commit（失败再逐条重试）。

### F-18（高）`_upsert_task_run`：每写一行日志全量重写整个日志数组 → O(n²)

- **位置**：`services/task_store_io.py:159-178`（`:170-171` `row.logs_json = _json_dumps(normalized["logs"])`、`errors_json` 同理），由 `services/task_store_io.py:181-188` `persist_task_snapshot()` 触发；而 **`services/task_service.py:95` 的 `log()` 每追加一行日志就调一次 `persist_task_snapshot()`**（`task_store.append_log` 之后紧跟）
- **证据（实测）**：取库里日志最多的任务 `task_1793122095317`（2266 条日志 / 146 KB）：

```
_upsert_task_run(该任务) p50 = 9.6 ms   min 4.7   max 14.7
→ 每追加一行日志就全量重写 146 KB 日志数组
```

  按 2266 行算，累计 ≈ **11–22 s**（写一次的成本随日志增长，9.6 ms 是最终大小下的值），且写入量是 O(n²)：1+2+…+2266 ≈ 257 万行 × ~65 B ≈ **167 MB 写放大**。叠加 F-21（`synchronous=FULL` + `journal_mode=delete`），每次都是真实 fsync。
- **建议**：日志单独一张 `task_logs` 表（已有这张表！`api/tasks.py:636` 在用），`persist_task_snapshot` 只写计数器/状态，`logs` 追加走 `INSERT`；或至少把快照持久化节流到 1 s 一次（当前是每行一次）。

---

## 4. 启动时间与内存占用

### 4.1 启动耗时

| 场景 | 到首个 200 的耗时 |
|---|---:|
| 首次启动（无 `__pycache__`、冷页缓存） | **11.678 s** |
| 第二次（warm） | 4.774 s |
| 第三次（warm） | **6.429 s** |

分步拆解（warm，进程内计时）：

| 阶段 | 耗时 |
|---|---:|
| `import main`（15 个 router + 平台插件） | **2388.1 ms** |
| `init_db()`（alembic + legacy 迁移） | 304.4 ms |
| `load_all()`（平台插件） | 127.0 ms |
| `migrate_stored_accounts()`（grok SSO 迁移） | 148.6 ms |
| `recover_pending_totp_secrets()` | 10.3 ms |
| `scheduler.start()` | 2.7 ms |
| `solver start_async()` | 0.6 ms |
| **合计** | **2982 ms** |

`-X importtime` 自身耗时 top（self time）：

```
235.6 ms  fastapi.openapi.models      ← FastAPI import 图自带，openapi_url=None 也照拉
107.0 ms  main
 82.0 ms  sqlalchemy.sql
 68.6 ms  core.db.models_platform
 54.1 ms  api.icloud
 43.2 ms  core.db.models_infra
```

累计：`fastapi` 730 ms、`core.db` 554 ms、`sqlmodel` 457 ms。

#### F-19（中）启动 3 秒是"import 税"，其中约 1.2 秒花在第三方库的 import 图上

- **位置**：`main.py:1-30`（15 个 router 全部在模块顶层 import）、`main.py:191-202`、`main.py:401`
- **证据**：上表。`fastapi.openapi.models` 的 **235.6 ms self time** 是 FastAPI 自己的 import 图带进来的（`fastapi.applications` → `fastapi.routing` → `openapi.models`），**与 `openapi_url=None` 无关，配置关不掉**；列在这里是为了说明 2.4 s 里最大的一块不在本仓库代码里，靠"少 import"而不是"改配置"才能省。
- **另有一处启动开销**：`main.py:401` `_GzipStatic.__init__` → `_precompress()` 在 **import 期串行 gzip -9** 全部静态资源。实测 **136.1 ms**（其中 `vendor-antd` 单个 74.2 ms）。
- **建议**：
  1. 路由改懒加载：`api.icloud`（54.1 ms）、`api.tasks`（48.0 ms）、`api.accounts`（101.8 ms cumulative）这些只在对应前缀命中时才 import —— FastAPI 支持 `app.include_router` 在 lifespan 里做，可把这部分挪出关键路径；
  2. 预压改成 `gzip -6`（1.14 MB 的 antd 从 74 ms 降到约 45 ms）或放到后台线程；首屏只压 1 个文件，不值得阻塞启动；
  3. `init_db()` 304 ms 里大部分是 alembic 版本检查 + `PRAGMA table_info` 循环（`core/db/migrations.py` 多处），可加"schema 版本一致就跳过"的快速路径。

### 4.2 内存与 swap

| 指标 | 数值 |
|---|---:|
| 就绪时 `VmRSS` | **96.65 MB** |
| 就绪时 `VmSize` / `Threads` | 342.7 MB / 4 |
| 重请求后 `VmHWM`（峰值） | **244.0 MB** |
| 重请求后 `VmRSS` / `VmSwap` | 146.2 MB / **89.0 MB** |
| 系统内存 | 1967 MB 总 / 1696 MB 已用 / 93 MB free |
| 系统 swap | **2048 MB / 4095 MB 已用** |

#### F-20（高）大响应把峰值内存推到 244 MB，其中 61% 被换出，直接制造 p95 尾巴

- **位置**：`services/task_store_io.py:298-299`（837 行全列）→ `api/tasks.py:812-814`（`json.dumps(body)` 再复制一份 374 KB→13.7 MB 字符串 + md5）；`api/accounts.py:279` 每行 `jsonable_encoder` 产生新 dict
- **证据**：
  - 单次 `include_logs=1` 让 RSS 117 MB → **159 MB**（+42 MB），三次后稳定在 163 MB，HWM **244 MB**；
  - `/proc/PID/stat` 的 `majflt`：5 次重请求从 37,979 → **39,033**，即 **211 次 major fault / 请求**（每次都是磁盘 I/O，因为页面已被换出）；
  - `VmSwap` 最终 **89 MB**，占 RSS 的 61%。
- **为什么重要**：这不是"内存泄漏"，是**瞬时峰值 + 分配器不归还**。Python 把 13.7 MB 的 JSON 字符串、837 个 dict、md5 的副本同时持有；峰值过后 RSS 不回落到 96 MB（实测停在 163 MB）。在 2 GB 的机器上，这直接把服务推进 swap，于是同一端点 p50 172 ms / p95 250 ms 的差距被放大到 `page=21` 的 132 ms / 2068 ms。
- **建议**：
  1. F-02 去掉 `include_logs` 后，这条 13.7 MB 峰值自然消失；
  2. `JSONResponse` 改成 `StreamingResponse` + `iterencode`，避免"body dict + 字符串"双份；
  3. ETag 用 O(1) 指纹（F-05），不构造 body 就没有峰值；
  4. 部署侧给容器加 `mem_limit` 并监控 `container_memory_working_set_bytes`，不要只看 RSS。

#### F-21（高）SQLite 用默认配置：无 WAL、`synchronous=FULL`、2 MB page cache、15 连接

- **位置**：`core/db/base.py:57` `engine = create_engine(DATABASE_URL)` —— 没有任何 `connect_args` / `PRAGMA` / `pool_size`
- **实测（运行期读 pragma）**：

```
journal_mode = delete          ← 不是 WAL：读写互斥，写事务阻塞所有读
synchronous  = 2 (FULL)        ← 每次 commit 都 fsync
cache_size   = -2000           ← 2 MB page cache（库 33.7 MB）
busy_timeout = 5000
pool         = QueuePool(size=5, max_overflow=10)   ← 最多 15 条连接
```

- **影响**：
  - 任务运行时每条日志一次 commit（F-18）+ `synchronous=FULL` → 每次写都等磁盘；
  - 读请求（列表/统计）与写（任务快照）在 `delete` 模式下互斥，`busy_timeout=5000` 意味着冲突时**阻塞 5 秒**才报错——这正是"偶发 2 秒响应"的另一个来源；
  - 2 MB page cache 对 33.7 MB 的库（`accounts` 表 `extra_json+password+token` 合计 **9.3 MB**）意味着全表扫基本都走页缓存外的读。
- **建议**：`connect` 事件里统一设 `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA cache_size=-65536; PRAGMA busy_timeout=5000;`（WAL 下读不阻塞写，`synchronous=NORMAL` 在 WAL 下仍然安全），并把 `pool_size` 提到 10–20。

---

## 5. 定时任务（scheduler）

- **机制**：`core/scheduler.py:135-142`，固定 **60 s tick** 的守护线程；每个 tick 对每个 job 调 `job.due(now)` → `interval_seconds()`。
- **当前注册的 job**（`services/chatgpt_maintenance.py:805-814`）：`chatgpt_auto_refresh`（5 min，`AUTO_REFRESH_INTERVAL_SECONDS = 5*60`）、`chatgpt2api_auto_sync`（10 min）。
- **真实库里的开关**：`chatgpt_auto_refresh_enabled = 'false'`、`chatgpt2api_auto_sync_enabled = 'false'` → **两个都关着，当前 tick 只读配置**。

#### F-22（低）当前开销可忽略；一旦打开，单轮 0.4–1.0 s 且写放大明显

- **位置**：`core/scheduler.py:144-166`（tick）、`services/chatgpt_maintenance.py:396-470`（一轮扫描）
- **证据（实测）**：

```
tick（两个 job 都关）           : 9.2 ms,  2 条 SQL      ← 每 60 s，即 0.015% CPU
run_auto_refresh_pass(扫描 667) : 402 ms, 1 条 SQL      ← 纯 Python：667 次 JWT 解析
  同一轮"冷"跑（有 21 个账号要写计划）: 1002 ms, 43 条 SQL（21 条 UPDATE extra_json）
```

  `_write_extra`（`services/chatgpt_maintenance.py:617`）在循环里逐账号 UPDATE（`:449`、`:463`、`:470`），每个都是独立事务。
- **换算**：开启自动刷新 = 每 5 min 一次 0.4–1.0 s 的 CPU 尖峰 + 最多 5 个账号 × 3 s 间隔（`_SCAN_BATCH_SIZE=5`、`_PER_ACCOUNT_DELAY_SECONDS=3.0`）的串行执行，即每轮至少 15 s 占用一个线程。
- **建议**：
  1. 扫描侧把 `plan_auto_refresh` 的 JWT 解析结果缓存进 `extra_json`（`at_exp` 已在那里），避免每轮 667 次 base64+json；
  2. 计划写回合并成一次 `executemany`/`UPDATE ... WHERE id IN (...)`；
  3. tick 的 `interval_seconds()` 读配置加缓存（见 F-13），把每 tick 2 条 SQL 降到 0。

---

## 6. 前端首屏关键路径

### 6.1 静态资源

`static/index.html` 引 1 个 entry + 4 个 `modulepreload` + 1 个 stylesheet，**共 7 个请求**：

| 资源 | 原始 | gzip（服务端实测传输） | 占比(gzip) |
|---|---:|---:|---:|
| `index.html` | 1,063 | **1,063（未压）** | 0.2% |
| `vendor-antd-Bply7nht.js` | 1,140,405 | **346,756** | **71.0%** |
| `vendor-react-Br-GQWxU.js` | 229,841 | 72,592 | 14.9% |
| `pages-VU1Zj5SO.js` | 201,839 | 59,864 | 12.3% |
| `index-BuWxJHFt.css` | 14,481 | 3,794 | 0.8% |
| `index-BwCgHzLo.js` | 10,739 | 3,816 | 0.8% |
| `rolldown-runtime-hePW80VL.js` | 716 | 459 | 0.1% |
| **合计** | **1,606,520 B（1.53 MB）** | **488,601 B（477 KB）** | 100% |

#### F-23（中）首屏 477 KB，antd 一家占 346 KB

- **位置**：`frontend/vite.config.ts:19-50`（`codeSplitting.groups` 把 `antd|@ant-design|rc-*` 全部塞进 `vendor-antd`）
- **证据**：上表。1.14 MB 原始 / 346 KB gzip 说明**整包 antd 都在首屏 chunk 里**（不是按需）。首屏 4 个 `modulepreload` + 1 个 entry script（`static/index.html:10-14`），CSS 是 render-blocking。
- **建议**：
  1. 确认 `antd` 是否按需引入（当前 `import { Table, Modal, ... } from 'antd'` 依赖 tree-shaking；1.14 MB 说明 shaking 没生效或引了 `antd/dist/reset.css` 之外的整包）；
  2. 把 `vendor-antd` 拆成"首屏必需"（Table/Button/Modal/Form）与"设置页才用"（DatePicker/Upload/ColorPicker 等，这些是 antd 里最重的）；
  3. 用 brotli 替代 gzip：同体积通常再小 15–20%（本机未装 `brotli` 模块，未实测，只作方向建议）；`main.py:404-407` 的 `_SKIP_EXT` 已经跳过 `.br`，加一层 `.br` 静态文件即可。

#### F-24（中）`index.html` 没被压缩（1,063 B 走了未压缩路径）

- **位置**：`main.py:573-602` `spa_fallback` 用 `FileResponse(index_path)` 直接返回，没走 `_GzipStatic`
- **证据**：`curl -H 'Accept-Encoding: gzip' /` → `dl=1063`（= 原始大小），而 `_GzipStatic` 的 `MIN_SIZE=1024` 本来会压它。
- **建议**：1 KB 影响不大，但顺手在 `spa_fallback` 里复用 `_GzipStatic` 的压缩结果，或直接把 index.html 内联进响应。

### 6.2 首屏 API 调用

`App.tsx` 挂载即发 3 个请求（`/api/auth/status`、`/api/platforms`、`/api/integrations/panels`），账号页再发 3 个（`/api/accounts?page_size=50`、`/api/accounts/export-formats`、`/api/actions/{platform}`）——**首屏共 6 个 API + 7 个静态 = 13 个请求**。

#### F-25（高）API 响应完全没有 gzip：242 KB 的列表可压到 41 KB

- **位置**：`main.py:377` 注释解释了"为什么不用 `GZipMiddleware`"（为了给静态资源按变体算 ETag），但**结论被误用到了 API 上**——`app.add_middleware` 只加了 CORS（`main.py:258-263`），没有任何响应压缩。
- **证据（同一 URL，带/不带 `Accept-Encoding: gzip` 传输字节完全相同）**：

| 端点 | identity | 带 gzip 请求头 | gzip 压缩后 | 压缩比 |
|---|---:|---:|---:|---:|
| `/api/accounts?page_size=50` | 242,654 | **242,654** | 40,988 | **5.9×** |
| `/api/tasks` | 374,349 | **374,349** | 23,494 | **15.9×** |
| `/api/accounts?page_size=1000` | 3,526,673 | **3,526,673** | 615,749 | 5.7× |
| `/api/tasks?include_logs=1` | 13,724,404 | **13,724,404** | 814,231 | 16.9× |
| `/api/icloud/aliases` | 712,390 | **712,390** | 127,238 | 5.6× |
| `/api/tasks/logs?page=1` | 13,568 | 13,568 | 1,902 | 7.1× |

  响应头里只有 `vary: Origin`（CORS），没有 `content-encoding`。
- **影响**：走 SSH 隧道/公网时，账号页首屏要多传 200 KB，任务页轮询每次 374 KB（虽然 304 会省掉 body，但首次和状态变化时是实打实的）。
- **建议**：加 `GZipMiddleware(minimum_size=1024)`（它对 `/api/*` 生效、对已压缩的静态资源不会重复压），或写一个只作用于 `application/json` 的压缩中间件；静态资源那条 `_GzipStatic` 路径保持不变。

### 6.3 轮询

- `frontend/src/lib/pollBackoff.ts:13-19`：有活跃任务 **2.5 s**、空闲 **15 s**。
- 每次轮询 `/api/tasks`：服务端 172 ms（构造 body），命中 ETag 时 **~98 ms**（8 次取样 79–194 ms，仍构造 body + MD5）。**单标签页 ≈ 1 个核的 3.5–4.4%**，且每次都会把 244 MB 峰值内存的那条路径走一遍（如果同时有人打开 `include_logs`）。

---

## 7. 修复清单（按 收益/成本 排序）

| 优先级 | 改动 | 位置 | 预期收益（实测推算） |
|---|---|---|---|
| P0 | `page_size` 上限 200 | `api/accounts.py:311-312` | 堵住 988 ms/3.5 MB 的单请求放大 |
| P0 | ETag 改 O(1) 指纹（`max(updated_at)`+`count`），命中即 304 不建 body | `api/tasks.py:811-818` | 轮询 172 ms → <5 ms；消除 244 MB 峰值的一半 |
| P0 | 去掉/收敛 `include_logs`（改单任务语义） | `api/tasks.py:789-818`、`services/task_store_io.py:298` | 810 ms / 13.7 MB → 不再存在 |
| P0 | `_upsert_task_run` 只写计数器，日志走 `task_logs` 表 | `services/task_store_io.py:159-178` | 每行日志 9.6 ms → <1 ms，写放大 O(n²)→O(n) |
| P1 | SQLite 开 WAL + `synchronous=NORMAL` + 64 MB cache | `core/db/base.py:57` | 读写不再互斥；写延迟降一个数量级 |
| P1 | `stats` / `count_by_account` / `bound_counts` 改 SQL 聚合 | `core/db/repository.py:837`、`core/db/icloud_alias_repository.py:494`、`core/proxy_pool.py:108` | 1057 行 → 1 行；105 ms → <5 ms；213 ms → <10 ms |
| P1 | `/api/tasks/logs` 用 `COUNT(*)` 算 total | `api/tasks.py:655` | 1711 行 → 1 行；52 ms → <10 ms |
| P1 | `pool_summary` 复用已取到的 rows / `GROUP BY` | `services/icloud/aliases.py:428` | 3882 行 → 1941 行；428 ms → ~220 ms |
| P1 | API 加 gzip | `main.py:258` 附近 | 列表 242 KB → 41 KB（5.9×），任务 374 KB → 23 KB（15.9×） |
| P2 | 深分页改键集分页 | `core/db/repository.py:1170,1197` | 第 21 页 1050 行 → 50 行 |
| P2 | `plus_status` 落生成列 + 索引 | `api/accounts.py:332-342` | 2114 行 → 50 行；697 ms → ~50 ms |
| P2 | 列表不解密（`has_*`/`at_exp` 写入时算好） | `api/accounts.py:225,232` | 1.36 ms/行 → ~0.3 ms/行；ps1000 988 ms → ~250 ms |
| P2 | `config_store.get` 进程内缓存 | `core/config_store.py:287` | 每请求 -0.85 ms；每 tick -2 SQL |
| P2 | 鉴权/`check_accounts_valid`/任务探测去 N+1 | `main.py:216`、`core/scheduler.py:184,200`、`services/task_runners.py:778,787` | 每批 50 账号 -100 SQL |
| P3 | 启动：路由懒加载 + 预压降级/后台 | `main.py:1-30`、`main.py:401` | 启动 -0.2~0.5 s / -30 ms |
| P3 | antd 拆包 + brotli | `frontend/vite.config.ts:19-50` | 首屏 477 KB → ~350 KB |

---

## 附录 A：复现方法

```bash
# 0) 真实库只读副本（不碰生产库）
python - <<'PY'
import sqlite3, pathlib
src, dst = "data/account_manager.db", "/tmp/perf/account_manager.db"
s = sqlite3.connect(f"file:{src}?mode=ro", uri=True); d = sqlite3.connect(dst)
s.backup(d); d.close(); s.close()
PY

# 1) 冻结 HEAD 快照并起服务（工作树在审计期间被别人改过，必须冻结）
git archive 4680c8d | tar -x -C /tmp/perf/src
cd /tmp/perf/src && DATA_DIR=/tmp/perf/data /data/workspace/Register/.venv/bin/python -m uvicorn main:app --port 8000

# 2) 逐端点计时（warmup 1 + 取样 8）
curl -s -o /dev/null -w '%{http_code} %{time_total} %{size_download}\n' \
  'http://127.0.0.1:8000/api/accounts?page_size=1000'

# 3) 每请求物化了多少行（ORM load 事件）
#    见 /root/.hermes/cache/scratch/regperf/measure_rows.py

# 4) 304 轮询路径
ETAG=$(curl -s -D - -o /dev/null http://127.0.0.1:8000/api/tasks | grep -i '^etag:' | cut -d' ' -f2 | tr -d '\r')
curl -s -o /dev/null -H "If-None-Match: $ETAG" -w '%{http_code} %{time_total} %{size_download}\n' \
  http://127.0.0.1:8000/api/tasks

# 5) 启动拆解
DATA_DIR=/tmp/perf/data /data/workspace/Register/.venv/bin/python -X importtime -c "import main" 2>&1 | sort -t'|' -k1 -rn | head
```

## 附录 B：原始数据文件（scratch，未入库）

| 文件 | 内容 |
|---|---|
| `curl_timings.json` / `final_timings.json` | 30 个端点的 p50/p95/min/max/bytes |
| `profile.json` | 18 个端点的 cProfile top 函数 + SQL 直方图 |
| `rows.json` | 每端点的 ORM 物化行数 / SQL 条数 / 响应字节 |
| `marks.json` | 启动分步耗时 |
| `server_8000.log` | 启动日志（含 `[Scheduler] 已启动`、`[OK] 周期任务: [...]`） |
