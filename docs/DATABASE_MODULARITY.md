# 数据库模块化：分库、邮箱唯一键、判重

本文说明账号数据层的设计，以及「不同平台用不同数据库」「邮箱作唯一账号 key」
「已注册不重复注册」三项能力的用法。

## 1. 分层结构

```
core/db/
├── __init__.py         # 包入口：向后兼容导出 + init_db/save_account
├── base.py             # 工具（_utcnow、normalize_email）、默认库 engine、会话依赖
├── models_infra.py     # 跨平台基础设施表：任务、日志、代理
├── models_account.py   # 账号池表（所有平台共用，随平台分库）
├── models_platform.py  # 平台专属表：Outlook 邮箱池、iCloud 主号/别名
├── registry.py         # 平台分库注册表（URL → Engine，惰性建表）
├── repository.py       # 账号仓储（邮箱唯一键、判重、透明分库）
└── migrations.py       # 轻量 schema 迁移（每个库都跑）
```

**关键约束**：`core/db/__init__.py` 继续导出 `AccountModel`、`engine`、
`save_account`、`get_session` 等旧名字，所以历史代码无需改动即可运行。

## 2. 分库

### 2.1 两种配置方式

```bash
# 方式一：按平台名（推荐，直观）
DATABASE_URL_GROK=sqlite:///data/grok.db
DATABASE_URL_CHATGPT=sqlite:///data/chatgpt.db
DATABASE_URL_ICLOUD=sqlite:///data/icloud.db

# 方式二：JSON 总表（适合从配置中心下发）
PLATFORM_DATABASE_URLS={"grok": "sqlite:///data/grok.db", "chatgpt": "sqlite:///data/chatgpt.db"}
```

两种方式可以混用，JSON 总表优先。

### 2.2 未配置即回落（纯增量能力）

没配分库的平台透明使用默认库（`DATABASE_URL`），行为与重构前完全一致。
所以**可以只给一个平台分库**，其余不受影响。

### 2.3 哪些表进哪个库

| 表 | 归属 | 说明 |
|----|------|------|
| `accounts` | **平台库** | 每个平台库各建一份，按 `platform` 字段区分 |
| `task_runs` / `task_logs` / `proxies` | 默认库 | 跨平台基础设施 |
| `configs` | 默认库 | 全局配置 |
| `outlook_accounts` | `outlook` 库 | 邮箱池（多个注册平台共用，故独立键名） |
| `icloud_accounts` / `icloud_aliases` | `icloud` 库 | iCloud 主号与隐私邮箱 |

### 2.4 代码里怎么用

```python
# ① 平台库会话（自动选库）
from core.db import platform_session

with platform_session("grok") as session:
    ...

# ② FastAPI 依赖
from core.db import platform_session_dep

@router.get("/x")
def endpoint(session: Session = Depends(platform_session_dep("grok"))):
    ...

# ③ 只要 engine
from core.db import platform_engine
engine = platform_engine("grok")
```

> `platform_session` 返回的是普通 `Session`，可以直接 `with` 使用。

## 3. 邮箱唯一键

### 3.1 规范形式

邮箱作为唯一业务键，写入前统一经 `normalize_email()` 处理（去空白 + 转小写）：

```python
from core.db import normalize_email

normalize_email("  User@X.AI ")   # -> "user@x.ai"
```

因此 `User@X.ai` 与 `user@x.ai` 被视为**同一账号**，不会各存一行。

### 3.2 数据库级约束

迁移会给 `accounts` 建唯一索引：

```sql
CREATE UNIQUE INDEX uq_accounts_platform_email ON accounts (platform, email);
```

注意是 `(platform, email)` 复合唯一 —— 同一个邮箱可以分别在 Grok 和 ChatGPT
注册，各占一行；但同一平台内不允许重复。

老库若有重复行，迁移会自动合并（保留 id 最小的一条，其余字段按「非空优先」
补齐），再建索引。

## 4. 已注册不重复注册

### 4.1 判重 API

```python
from core.db import account_repository

account_repository.is_registered("grok", "a@x.ai")                  # True/False
account_repository.is_registered("grok", "a@x.ai", include_invalid=False)
account_repository.registered_emails("grok", ["a@x.ai", "b@x.ai"])  # 批量
```

`include_invalid=True`（默认）把失效账号也算已注册 —— 邮箱已被占用，
重复注册没有意义。需要「失效账号重试」时传 `False`。

### 4.2 三处自动生效点

| 位置 | 行为 |
|------|------|
| `api/tasks.py` 任务循环 | 指定邮箱时先判重，命中即 `skipped`（不打验证码、不建会话） |
| `platforms/grok/plugin.py` | 注册入口判重，命中直接抛错（可用 `grok_allow_duplicate=1` 关闭） |
| `api/accounts.py` 新增/导入 | 同平台同邮箱走更新而非重复插入 |

### 4.3 关闭判重

```python
extra = {"grok_allow_duplicate": "1"}   # Grok 插件允许重复注册
```

任务层的判重只在**指定邮箱**（`req.email`）时触发。批量自动建邮的场景不判重，
因为每次都是全新邮箱。

## 5. 跨库查询（分库后最易踩的坑）

分库之后，「账号在哪张表」取决于平台配置。**散落的裸查询会在分库后静默漏读**
——比如 Grok 账号进了 Grok 库，但导出还在默认库查，页面看起来「账号消失了」。

所有账号读写都应该走仓储：

```python
from core.db import account_repository

# 跨库列出（不指定平台时扫默认库 + 所有平台库并合并）
account_repository.list_all_accounts(
    platform="grok",          # 可选：指定后只查该平台库
    status="registered",      # 可选
    email_contains="a@x",     # 可选，大小写不敏感
    created_at_start=...,     # 可选，时间下界（下推到 SQL）
    created_at_end=...,       # 可选，时间上界（下推到 SQL）
    limit=20, offset=0,       # 可选：分页下推到 SQL
)

# 分页总数（不下拉整行；未指定平台时按唯一键去重计数，与列表一致）
account_repository.count_all_accounts(platform="grok", status="registered")

# 按 id 取（API 层只有 id，必须跨库找）
account_repository.get(123)
account_repository.delete(123)

# 批量（避免 N+1：每库一次 IN 查询，不是每个 id 开一次会话）
account_repository.get_many([1, 2, 3])           # 保持传入顺序
account_repository.delete_many([1, 2, 3])        # → (deleted, not_found)

# 统计（跨库汇总）
account_repository.stats()
```

### 5.0 ⚠️ ID 撞号：同一数字在不同库指向不同账号

**账号 id 是每库各自自增的**。配置了分库之后，Grok 库里的 `id=1` 和默认库里
某个 ChatGPT 账号的 `id=1` 是两个不同的账号。

所以**指定 `platform` 时，默认库那一步必须再按 platform 过滤**，否则：

| 操作 | 后果 |
|------|------|
| `get(1, platform="chatgpt")` | 返回 Grok 的账号（读到别人的数据） |
| `delete(1, platform="chatgpt")` | **删掉 Grok 的账号**（数据丢失，不可恢复） |

`get` / `delete` / `get_many` / `delete_many` 都已修正为「指定 platform 时，
默认库查询附加同平台约束」。回归测试见
`tests/test_account_repository_batch.py::TestIdCollisionAcrossDatabases`。

API 层的 `GET/PATCH/DELETE /accounts/{id}` 与 `POST /accounts/batch-delete`
都接受可选 `platform` 参数；前端账号页本来就按平台分区，会带上它。

**分页下推**：`limit`/`offset` 会传给每个库的 SQL（每库取 `offset+limit`
行再合并截断），避免「整表读进内存只为了切一页」。跨库合并后 offset 需要
统一施加，所以每库先多取 `offset + limit` 行。

`plus_status` 这类存在 `extra_json` 里的条件**无法下推**，带该条件时接口会
退回「全量取回 → 内存过滤 → 切片」，`total` 也走内存计数。

**已在仓储层改造的调用方**（分库后不会漏读）：

| 位置 | 说明 |
|------|------|
| `api/accounts.py` | 列表/导出/导入/单账号增删改查/测活 |
| `api/actions.py` | 批量操作按 `{platform}` 路径参数选库 |
| `api/integrations.py` | 回填按平台分组，各组用自己库的会话 |
| `api/tasks.py` | 回填 RT / 绑定 2FA 的目标选择走 `chatgpt` 库 |
| `core/scheduler.py` | trial 到期检查、批量测活跨库 |
| `services/chatgpt_sync.py` | CPA / Sub2API 同步结果写回 `chatgpt` 库 |
| `services/icloud_service.py` | iCloud 主号/别名走 `icloud` 库 |

跨库同邮箱去重：同一 `(platform, email)` 可能同时存在于平台库与默认库
（分库迁移前的历史行）。`list_all_accounts` 以平台库为权威源去重，不会重复返回。

## 5.1 索引

列表接口的查询模式都有对应索引，避免数据量上来后退化成全表扫描：

| 表 | 索引 | 服务的查询 |
|----|------|-----------|
| `accounts` | `uq_accounts_platform_email`（唯一） | 判重、upsert |
| `accounts` | `ix_accounts_platform_created_at` | 列表：按平台 + 时间倒序 |
| `accounts` | `ix_accounts_status` | 状态筛选 |
| `task_logs` | `ix_task_logs_platform_id` | 日志列表：按平台 + id 倒序 |
| `task_logs` | `ix_task_logs_created_at` / `ix_task_logs_status` | 时间/状态筛选 |

**老库升级**：`create_all` 不会改动已存在的表，所以新增索引由迁移
`_ensure_hot_query_indexes()` 按表存在与否幂等补建（`init_db()` 自动跑）。

## 5.2 状态取值

账号状态统一用 `core.base_platform.AccountStatus`：

```python
AccountStatus.REGISTERED   # "registered"
AccountStatus.TRIAL        # "trial"
AccountStatus.SUBSCRIBED   # "subscribed"
AccountStatus.EXPIRED      # "expired"
AccountStatus.INVALID      # "invalid"
```

两个判定语义**不同**，不要混用：

| 方法 | 判定方式 | 用途 |
|------|---------|------|
| `is_active(v)` | 白名单：只认 registered/trial/subscribed | 测活——宁可漏测也不重测失效账号 |
| `counts_as_registered(v)` | 黑名单：只有 invalid/expired 不算 | 判重——未知状态按已注册处理，避免重复占用邮箱 |
| `coerce(v)` | 转枚举，未知值回落 REGISTERED | 读取历史行（库里可能有枚举外取值），不抛异常 |

`coerce` 是为了避免 `AccountStatus(raw)` 在遇到枚举外历史值（如 chatgpt 侧的
`active`/`banned`）时抛 `ValueError` 打断整个操作。

## 6. 迁移

```python
from core.db import init_db

init_db()   # 建表 + 迁移，启动时调用（main.py 已接）
```

`init_db()` 会：
1. 在默认库建全部表；
2. 在**每个平台库**建账号表；
3. 对每个库跑迁移：补列、建唯一索引、合并重复行、回填 iCloud share_token、
   同步 Outlook 邮箱池的 used 状态。

迁移是幂等的，可以重复执行。

## 7. 新增一个平台的步骤

以新增 `foo` 平台为例：

```bash
# ① 想要独立库就配（不配也行）
DATABASE_URL_FOO=sqlite:///data/foo.db
```

```python
# ② 平台插件照常写（platforms/foo/plugin.py）
@register
class FooPlatform(BasePlatform):
    name = "foo"
    ...
```

```python
# ③ 落库走仓储（自动进 foo 库）
from core.db import account_repository
account_repository.upsert(account)   # 自动 normalize_email + 判重 + 分库
```

```python
# ④ 平台专属表（可选）
# core/db/models_platform.py 里加模型，并把表对象加进对应 *_TABLES
```

```python
# ⑤ 建表
from core.db import init_db
init_db()   # 平台库里的账号表会自动建好
```

## 8. 测试

```bash
python -m pytest tests/test_db_modular.py -q    # 分库/唯一键/判重/跨库 专项
python -m pytest tests/ -q                      # 全量
```

`tests/test_db_modular.py` 覆盖：
- 邮箱键大小写/空白归一
- 分库配置解析（JSON 与按平台环境变量两种）
- 未配置回落默认库
- 账号落到各自库 + 跨平台隔离
- 同平台同邮箱唯一（覆盖而非新增）
- 不同平台同邮箱互不干扰
- 判重（含 `include_invalid` 语义、批量判重）
- 统计跨库汇总
- 迁移建唯一索引 + 合并重复行 + 幂等
- 跨库列出/过滤/按 id 取/删除/去重
- upsert 接受 AccountModel 且保留 extra_json
