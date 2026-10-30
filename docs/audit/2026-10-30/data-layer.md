# data-layer 审计

[MODE: SANDBOX]

审计对象：`core/db/`（`models_account.py` / `models_infra.py` / `models_platform.py` / `repository.py` / `merge.py` / `migrations.py` / 四个领域仓储）、`core/storage/`（SQLite/JSON 双后端）、`migrations/versions/`、`core/credential_crypto.py`、`core/models/`、`scripts/dump_accounts.py`、`scripts/merge_platform_dbs.py`、`scripts/encrypt_existing_credentials.py`。

方法：静态审阅 + 只读真库（`data/account_manager.db`，`mode=ro`）+ **真库副本 / 空库上的可写实验**（`/root/.hermes/cache/scratch/dlaudit/`）+ 单文件测试（`tests/test_repository_backends.py`，11 passed / 22 subtests）。真库未被写入；所有 `alembic upgrade/downgrade`、并发、加密实验都在副本或临时库上执行。

基线说明：任务书写的是 `4680c8d`，审计期间工作树前进了两次——**`c2c502b`**（`fix(db): 模型与迁移链的索引漂移`，新增 `e5b2c8f71a34` 与两条复合索引声明）与 **`a3470d3`**（列表类响应/任务日志不再下发凭据正文）。数据层代码在 `a3470d3` 里没有被改动；只有 `api/accounts.py` 行号偏移（本报告已按最新工作树校正 D-04 的引用）与 `services/task_runners.py:270,579` 的代理串打码（已在 D-20 标注）。其余条目以 `c2c502b` 的代码取证为准。

结论先行：**SQLite 与 JSON 两个后端并不等价**——不是「个别字段没对齐」，而是账号写路径、Outlook 池导入、判重、任务日志形状四条主路径在 JSON 分支上行为不同，其中三条会明文落凭据或直接 500。迁移链在「空库」上能跑通，但**跑不进本应用自己建的库**，且 `c3f8a1d64b27` 的 downgrade 在真实数据形态下会把 `accounts` 表搞成空表。凭据加密覆盖了 `accounts.password` / `extra_json` 六键 / 配置口令 / TOTP 日志，但**没覆盖 `accounts.token`（1057/1057 明文）、`extra.cookies`（607 行明文）、Outlook 池的 password/refresh_token、代理 URL 内嵌口令**。UUID 化后仍有一处 `int(uuid)` 的必崩调用。

---

## 0. 结论速览

> **第二轮复核（2026-10-30，同一审计目录）**：新增 1 条 **P0**（列表响应明文下发 grok SSO）、
> 6 条 P1、6 条 P2，见 **§6**。其中 R-01 与 HEAD `a3470d3`「列表类响应不再下发凭据正文」
> 直接冲突 —— 该提交堵住了 `token` 列，但没堵 `extra.sso`。

| 审计项 | 结论 | 最严重发现 |
|---|---|---|
| (1) SQLite / JSON 等价性 | ❌ 不等价 | D-01/D-02 凭据明文落盘；D-03 Outlook 池写错表；D-04 JSON 改账号 500；D-05 混合后端不建表；D-06 并发丢写 |
| (2) 迁移链可重复 / downgrade / baseline | ❌ 仅空库可用 | D-12 应用自建库无法 `upgrade`；D-13 downgrade 失败后 `accounts` 变空表；D-14 baseline 类型漂移 |
| (3) 凭据加密覆盖面 | ⚠️ 主账号密码/extra 已加密，其余有洞 | D-17 `token` 1057 行明文；D-18 cookies 607 行明文；D-19 78 个配置键在口令白名单外 |
| (4) 唯一索引 / 去重边界 | ⚠️ 主键约束在，边界多处不一致 | D-15 老库大小写重复会让**启动崩溃**；D-26 合并会用 `{}` 覆盖凭证；D-25 JSON 代理无唯一约束 |
| (5) UUID 后遗留 int 假设 | ❌ 有 1 处必崩 + 多处误导注释 | D-29 `api/actions.py:263` `int(uuid)` → ValueError（接口 500） |
| (6) 事务 / 会话泄漏 | ⚠️ 无未关闭会话，但有回滚/长事务语义问题 | D-32 `IntegrityError` 重试会 `rollback()` 调用方整个事务；D-34 多处 O(N) 全表读 |

**已验证成立、不重复报的项**（本轮只做回归确认）：

- `accounts.password` 列：真库 1057 行，明文 0 行；`extra_json` 里 AT/RT/IDT/ST/SSO/totp 六键**出现时全部是 `v1:` 密文**（按 key 统计：明文 0 个）。
- `configs` 里 `chatgpt2api_api_key` / `cpa_api_key` / `grok2api_password` 均为密文。
- `data/secrets/totp_journal/*.json` 已是 `totp_secret_cipher`。
- `accounts.id` 已是 UUID（1057/1057 长度 36），`legacy_id` 列与 `ix_accounts_legacy_id` 唯一索引存在。
- 真库唯一索引是表达式版 `uq_accounts_platform_email ON accounts(platform, lower(email))`，不是裸列版。
- `JsonStore._read()` 对损坏 JSON 抛错而不是当空库（不会静默覆盖）。

---

## 1. 现场基线（只读真库）

```
accounts          1057 行   platform: chatgpt 667 / grok 390
                   password 非空 1057，明文 0
                   token    非空 1057，明文 1057        ← 见 D-17
                   extra_json 含 v1: 1057 行
                   extra.cookies 明文 607 行             ← 见 D-18
                   extra.password 明文 1 行              ← 见 D-18
                   legacy_id IS NULL 1 行（迁移后新建的号）
                   (platform, lower(email)) 重复 0 组
proxies           5 行，5/5 的 url 含 `user:pass@`      ← 见 D-20
configs           143 键，78 个不在 CONFIG_SCHEMA 内     ← 见 D-19
                  gptmail_api_key / maliapi_api_key 明文
task_runs         837 行，59 行 logs_json 含 `scheme://user:pass@`（与 proxy.md P1-10 同一批）
alembic_version   e5b2c8f71a34（= head；D-16 记录的是 d4e7f1a25b90 —— 见 §6 复核说明）
data/platforms/outlook.db   3 行，password/refresh_token 均为空串
data/platforms/icloud.db    3 主号（source=remote，credentials_cipher 为空）
                            1941 别名，share_token 全明文（22 字符）
文件权限          data/secrets/credential_key 0644、account_manager.db 0644  ← 见 D-22
data/accounts.json  存在但 `accounts: {}`（空骨架，见 D-31 与 §5）
```

---

## 2. 逐项详查

### (1) SQLite 与 JSON 后端是否真的等价

**结论：不等价。** 读路径有 11 处绕过仓储直连 SQLite，写路径有 3 处只在一边实现，另有 1 处把「Outlook 模型」交给了「账号仓储」。

#### D-01 [P0] JSON `upsert` 把 password 与 extra 凭证**明文**写进 `accounts.json`

- 位置：`core/db/repository.py:529-550`（`_json_upsert`），关键在 `:547` 与 `:549`。
- 证据：`merge_account_fields()` 对非 `AccountModel` 输入走 `extra = getattr(incoming, "extra")` / `json.dumps(extra)`（`core/db/repository.py:160-162`），`password` 走 `_keep()` 取 `incoming.password`（明文）。这两个值随后被 `store.update()/store.insert()` **原样**写入文件，不经过 `AccountModel.__init__` / `set_extra` 的加密钩子。
- 复现（临时 DATA_DIR，JSON 模式）：

```
upsert(AccountModel(password="pw1", extra_json={"access_token":"AT1"}))     # 构造器加密
upsert(AccountRecord(password="pw2", extra={"access_token":"AT2",
                                            "refresh_token":"RT2",
                                            "cookies":"c2"}))               # 插件产出
→ accounts.json: password 非 v1:（明文 "pw2"）
                 access_token PLAINTEXT len 3
                 refresh_token PLAINTEXT len 3
                 cookies PLAINTEXT len 2
```

- 影响：JSON 是**全新部署的默认后端**（`core/storage/__init__.py:44`）。插件注册产出的 `AccountRecord` 走 `save_account()` → `upsert()`，即每个新注册账号的 AT/RT/SSO/cookies 全部明文落盘。这条直接把「凭据全量加密」在默认部署上作废。
- 建议：`_json_upsert` 不要直接写 `fields`。构造 `AccountModel(**fields)`（或对 `fields["extra_json"]` 调 `encrypt_extra`、对 `password` 调 `encrypt_text`）后再 `_json_save`，让两条后端共用同一套加密钩子；并在 `tests/test_repository_backends.py` 增加「JSON upsert 后文件里 password/AT 必须是 `v1:`」的断言。

#### D-02 [P0] SQLite `upsert` 更新路径把 `extra_json` **明文**写回

- 位置：`core/db/repository.py:388-395`。
- 证据：

```python
if existing is not None:
    for name, value in fields.items():
        setattr(existing, name, value)   # extra_json 是普通列，setattr 不加密
```

`AccountModel.extra_json` 是普通字段（`core/db/models_account.py:92`），只有 `__init__`（`:96-126`）与 `set_extra()`（`:165-175`）会加密。`password` 因为改成了 property 所以 `setattr` 仍会加密；`extra_json` 不会。

- 复现（临时 SQLite 库）：

```
pw_is_cipher True
access_token  PLAINTEXT len 3
refresh_token PLAINTEXT len 3
cookies       PLAINTEXT len 2
```

- 影响：**更新已有账号**（重注册、补 RT、面板回填）是最常见的写路径，每次都会把该账号的 AT/RT/SSO 从密文降级成明文。真库目前 1057 行都还有 `v1:`，说明线上还没大面积踩到（或踩到后被后续 `set_extra` 覆盖回来了），但代码路径是通的。
- 建议：更新分支改为 `existing.set_extra(json.loads(fields["extra_json"]))`（输入是 `AccountModel` 时直接保留其 `extra_json` 密文）；或把 `extra_json` 也做成 property，setter 内 `encrypt_extra`。加一条测试：`upsert(AccountRecord(...))` 两次后，库里 `extra_json` 的 AT 必须是 `v1:`。

#### D-03 [P0] Outlook 池导入走错仓储：JSON 下写脏 `accounts.json` + 明文密码 + 池表为空

- 位置：`services/mail_imports/providers.py:364-381`。
- 证据：

```python
account = OutlookAccountModel(
    email=record.email,
    password=record.password,            # 明文
    client_id=record.client_id,
    refresh_token=record.refresh_token,  # 明文
    ...
)
account_repository.save(account, session=session)   # ← 账号仓储，不是 outlook 仓储
```

`AccountRepository.save()` 在 JSON 模式下走 `_json_save()`（`core/db/repository.py:481-526`），只保留账号列：`platform/email/password/user_id/region/token/status/extra_json/cashier_url`。`OutlookAccountModel` 没有 `platform`，于是：

```
accounts.json rows: 1
platform='' email='u@outlook.com'
keys=['cashier_url','created_at','email','extra_json','id','password','platform',
      'region','status','token','updated_at','user_id']
password prefix: PLAINTEXT-
```

- 影响：
  1. 池表 `outlook_accounts` 一行都没写（JSON 分支把数据写到了别的文件；SQLite 分支只是「碰巧」因为 `session.add()` 对任意模型都生效才没坏）。
  2. `client_id` / `refresh_token` / `account_type` / `mailapi_url` / `enabled` / `used_platforms` 全部丢失。
  3. 密码以明文进入 `accounts.json`，且这条假账号 `platform=""` 会污染统计与判重。
  4. 调用方随后 `session.refresh(account)`（`:382`）在 JSON 下必然抛 `InvalidRequestError`，被 `except` 吞成「行 N: 创建失败」——用户看到的是导入失败，但 `accounts.json` 已经被写脏。
- 建议：改为 `outlook_repository.add(**payload)`；`OutlookAccountModel` 的 `password` / `refresh_token` 加透明加解密 property（与 `AccountModel.password` 同型），或在 `OutlookRepository.add/set_fields` 内统一 `encrypt_text`。补一条 JSON 模式的 Outlook 导入端到端测试（断言 `outlook_accounts.json` 有行、`accounts.json` 不变）。

#### D-04 [P0] JSON 后端下「编辑已有账号」必 500：`sess.refresh()` 对 detached 模型

- 位置：`api/accounts.py:391-394`（`sess.refresh(existing)` 在 `:394`）。
- 证据：更新分支没有像新建分支那样判断 `sess.object_session(saved)`：

```python
with account_repository.session_for(body.platform) as sess:
    account_repository.save(existing, session=sess)
    sess.commit()
    sess.refresh(existing)      # ← JSON 下 existing 来自 json_record_to_model，detached
```

复现：

```
REFRESH RAISED: InvalidRequestError Instance '<AccountModel ...>' is not persistent within this Session
```

- 影响：JSON 部署下「新增（邮箱已存在）→ 走更新分支」100% 500。新建分支已经修过同型问题（`:409-413` 的注释与 `if sess.object_session(saved) is not None` 守卫），更新分支漏了。
- 建议：与新建分支同判据 `if sess.object_session(saved) is not None: sess.refresh(saved)`；或让 `AccountRepository.save()` 返回「已刷新」的模型并禁止调用方再 refresh。

#### D-05 [P0] 混合后端（`STORAGE_BACKEND=json` + `STORAGE_BACKEND_ACCOUNTS=sqlite`）启动后**一张表都没有**

- 位置：`core/storage/__init__.py:110-120`（`all_tables_use_json`）与 `:158-184`（`accounts_backend_enabled`）。
- 证据：`required` 里包含 `"accounts"`，`covered = set(JSON_TABLES) | {"accounts"}` 是**常量**，完全不看 `STORAGE_BACKEND_ACCOUNTS`。于是：

```
all_tables_use_json: True
accounts_backend_enabled: False      # 账号明确要求留在 SQLite
init_db 后目录: ['platforms']         # 没有 account_manager.db，也没跑迁移
upsert → OperationalError: no such table: accounts
```

- 影响：`core/storage/__init__.py:174-175` 明确把「配置用 JSON、账号留 SQLite」写成受支持的混合部署，但 `init_db()`（`core/db/__init__.py:150`）会因此跳过 `create_all` 与 `run_migrations`，账号侧完全不可用。这是文档承诺与实现的直接矛盾。
- 建议：`all_tables_use_json()` 增加 `accounts_backend_enabled("json")` 条件；或把 `init_db` 改成「按表判断」：只要有一张表走 SQLite 就建库并跑迁移。补一条混合模式的启动测试。

#### D-06 [P0] JSON 存储的「线程安全」不成立：锁是**每实例**的，临时文件名是固定的

- 位置：`core/storage/json_backend.py:35-37`（`self._lock = threading.RLock()` 在 `__init__` 里）、`:63-72`（固定 `path.tmp`）；`core/storage/account_store_json.py:83-84`（每次 `AccountStoreJson(path)` 新建 `JsonStore`）；`core/db/repository.py:216-236`（`_json_store()` **每次调用都新建**）。
- 证据：

```
two stores share lock: False

# 20 个线程各自 upsert 一个账号（_read 里加 5ms 放大窗口），多轮结果：
rows: 3        （另一轮 7）
errors: 11     ['FileNotFoundError']   # accounts.json.tmp -> accounts.json
```

- 影响：注册任务线程 + 请求线程并发写 JSON 时，后写者用自己读到的旧快照覆盖前写者 → **账号静默丢失**；固定 `.tmp` 还会让两个写者互相搬走对方的临时文件，直接抛异常。20 个并发写只有 3~7 个能落下，其余不是丢就是抛。`JsonStore` 的注释声称「线程安全（进程内）」，实际只在「同一个实例」内成立，而调用方每次都在造新实例。
- 建议：把锁做成**按解析后路径的模块级注册表**（`dict[Path, RLock]` + 一把全局锁保护注册表），临时文件用 `f"{path}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp"`；或直接引入 `filelock` 做跨进程。补一条并发测试（20 线程 × upsert，断言 20 行且无异常）。

#### D-07 [P1] 11 处 `select(AccountModel)` 绕过仓储：JSON 下 `no such table`，分库下漏号

- 位置（代表性 3 处，均为热路径）：
  - `core/mailboxes/channels/outlook/mailbox.py:159-169`（取 Outlook 邮箱前算「本平台已注册」集合，只查默认库）
  - `services/panel_sync.py:298-303`（面板拉回写本地，`session.get(AccountModel, id)`）
  - `services/chatgpt_two_factor.py:373-376`（2FA 写库前按 email 找账号）
- 全量清单（`grep -rn "select(AccountModel"` 共 15 行命中，扣掉 2 处注释与 2 处 `core/db/migrations.py` 内部使用，实际绕过仓储的调用点 11 处）：`api/actions.py:142,154`、`services/chatgpt_account_selection.py:92,101`、`services/mail_imports/providers.py:252`、`services/chatgpt_two_factor.py:375,428`、`services/task_runners.py:519`、`services/panel_push.py:494`、`core/mailboxes/channels/outlook/mailbox.py:160`、`platforms/grok/migrate_sso_only.py:91`。
- 证据：JSON 模式下 `init_db` 不建 `.db`（D-05 的正常路径），这些 `Session(current_engine())` 第一次查询即 `OperationalError: no such table: accounts`；即便表存在，它们只查**默认库**，分库平台的账号查不到。`tests/test_json_deployment_end_to_end.py` 只覆盖了 config/accounts/proxies 三条写路径，没有覆盖这些读。
- 建议：给 `AccountRepository` 增加 `iter_all()` / `find_by_email_any_platform()`，把上述 11 处逐个改走仓储；短期至少在 JSON 模式下 `raise` 明确错误而不是 `no such table`。用 `grep -c "select(AccountModel"` 作为回归门禁（目标 0）。

#### D-08 [P1] `registered_emails()` 没有 JSON 分支：JSON 下直接抛 `no such table`

- 位置：`core/db/repository.py:311-333`。
- 证据：方法内没有任何 `_json_store()` 判断，直接 `AccountRepository._session_for()`。JSON 模式复现：

```
registered_emails RAISED OperationalError (sqlite3.OperationalError) no such table: accounts
# 副作用：在 DATA_DIR 下生成了一个空的 account_manager.db
```

- 影响：目前没有生产调用方（`grep registered_emails` 只命中定义），属于「公开 API 但一边没实现」。一旦有人用它做批量判重，JSON 部署即 500；同时它会**创建空的 `.db` 文件**，正好破坏 `all_tables_use_json` 想避免的「JSON 部署不该有 .db」。
- 建议：补 JSON 分支（`store.platforms_by_email` 或 `store.list_all` 过滤），或直接删掉这个方法避免误用。

#### D-09 [P1] `platforms_by_email()` JSON 分支：`emails=None` 返回 `{}`，且忽略 `platform` 过滤

- 位置：`core/db/repository.py:943-949`。
- 证据：

```python
wanted = [normalize_email(e) for e in (emails or [])]
if not wanted:
    return {}          # SQLite 分支此时返回「全部地址」
```

- 影响：SQLite 语义是「不传 emails = 返回全部」，JSON 返回空；`platform` 参数在 JSON 分支完全没用到，指定平台也会返回所有平台。当前两个调用方（`services/mail_imports/providers.py:179`、`services/icloud/accounts.py:185`）都传了地址列表，所以是**潜伏差异**，但函数契约已经两边不一致。
- 建议：`emails is None` 时遍历 `store.list_all()` 构建全量映射；`platform` 非空时按平台过滤后再聚合。

#### D-10 [P1] 任务日志同一接口两种字段名：`detail_json`（SQLite）vs `detail`（JSON）

- 位置：`core/db/task_log_repository.py:24`（`_FIELDS` 用 `detail`）、`:34`（SQLite 分支输出 `detail_json`）、`:40`（JSON 分支输出 `detail`）；`services/task_service.py:127-149` 同样两套形状。
- 证据：`_from_sqlite()` 返回 `{"detail_json": ...}`，`_from_json()` 返回 `{"detail": ...}`。`GET /api/tasks/logs` 直接透传（`api/tasks.py:642-655`），前端拿到哪个键取决于后端。
- 影响：当前前端不读 `detail_json`（`grep frontend/static` 为 0），所以是契约级问题而非线上故障；但任何按 SQLite 形状写的新调用方在 JSON 部署下会 `KeyError`。这与「换后端不换行为」的设计前提冲突。
- 建议：统一成一个键（推荐 `detail_json`，与模型列名一致），在 `_from_json` 里把 `detail` 序列化成字符串；或在仓储层做形状归一。

#### D-11 [P2] 其余 JSON/SQLite 语义差异（逐条登记）

| # | 位置 | SQLite | JSON |
|---|---|---|---|
| a | `core/db/repository.py:650-652` | `delete(id, platform)` 按平台过滤 | 忽略 `platform`，直接按 id 删 |
| b | `core/db/repository.py:577` vs `core/storage/account_store_json.py:171-176` | `platform == platform` 精确匹配 | `lower()` 后匹配 |
| c | `core/db/repository.py:1197` | `created_at IS NULL` 排最前（`reverse=True` 把 `True` 排前） | `None` 用 `datetime.min` 兜底，排最后 |
| d | `core/db/repository.py:554-585` | `list_accounts` 无 ORDER BY（rowid 序） | 按 `created_at` 升序 |
| e | `core/db/outlook_repository.py:99-118` | `email == lower(input)` 精确匹配 | `lower(row) == lower(input)` 大小写无关 |
| f | `core/db/icloud_alias_repository.py:193` vs `:170` | `address` 统一 `strip().lower()` 后落库 | `str(address)` 原样落库、原样匹配 |
| g | `core/db/icloud_account_repository.py:120-130` | `find_remote` 带 `source='remote'` | 不带 `source` 过滤 |
| h | `core/db/proxy_repository.py:164-181` | 返回 `ProxyModel`（`tags` 是 str） | 返回 `ProxyRecord`（`tags` 是 list） |

- 影响：a/b/c/d 目前被调用方绕过或数据形态掩盖；e/f 在「历史行带大写」时会让 SQLite 查不到、JSON 查得到；g 在本地行恰好有 base_url 时误命中；h 是类型不一致，`api/proxies.py` 只用 `id` 所以没炸，但任何遍历 `row.tags` 的新代码会在 SQLite 下逐字符迭代。
- 建议：把「邮箱/地址归一」下沉到模型层（`@validates` 或统一 property），从根上消掉 e/f；`find_remote` 的 JSON 分支补 `source` 条件；`ProxyRepository.get_many` 的 SQLite 分支统一返回 `ProxyRecord`。

---

### (2) 迁移链：可重复执行、downgrade、baseline 与模型一致性

**结论：空库可跑通；本应用自己建的库跑不进去；downgrade 在真实数据形态下会把 `accounts` 搞成空表。**

#### D-12 [P0] `alembic upgrade head` 无法应用到 `create_all` 建的库；`stamp 77cc` 后又在 `d4e7` 撞列

- 位置：`migrations/versions/77cc4baded71_baseline_existing_schema.py:21-35`（baseline 直接 `create_table`，无 `has_table` 守卫）；`migrations/versions/d4e7f1a25b90_add_proxy_tags.py:32-35`（`add_column` 无列存在判断）；`core/db/__init__.py:150-165`（应用启动只跑 `create_all` + 手写 runner，**从不 stamp alembic**）。
- 证据（临时库）：

```
# A. 先 create_all（= 应用真实建库方式），再 upgrade
$ alembic upgrade head
sqlalchemy.exc.OperationalError: table accounts already exists

# B. 按 docs/refactor/MIGRATION-LOG.md 的「老库」流程：先 stamp 再 upgrade
$ alembic stamp 77cc4baded71 && alembic upgrade head
sqlalchemy.exc.OperationalError: duplicate column name: tags
# 且此时 c3f8 已经执行完，legacy_id 被写成了 UUID 文本：
# ('95d5de90-...', '914c2bf9-...', 'text')
```

- 影响：
  1. 任何「用本应用跑起来过」的库都没有 `alembic_version`，直接 `upgrade` 必失败；按文档 `stamp 77cc` 也只是把失败点从 baseline 推迟到 `d4e7`。
  2. B 的失败发生在 `c3f8` 之后，`legacy_id` 已被写成 UUID 字符串（`INTEGER` 亲和列存 text），「回滚用 legacy_id 还原整数主键」的契约被静默破坏。
  3. `init_db` 与 alembic 是两套并行的 schema 真相源，谁都不知道对方改了什么。
- 建议：
  - `77cc` 的 `upgrade()` 用 `sa.inspect(conn).has_table()` 逐表守卫（已存在则跳过），`d4e7` 用 `PRAGMA table_info('proxies')` 判断 `tags` 是否存在；
  - `init_db()` 在 `create_all` 之后自动 `alembic stamp head`（或至少写一个 `schema_version` 行），让两条轨合并；
  - 补测试：`create_all` → `alembic upgrade head` 必须成功；`stamp 77cc` → `upgrade head` 必须成功。

#### D-13 [P0] `c3f8a1d64b27.downgrade()` 在真实数据形态下失败，并把 `accounts` 留成空表

- 位置：`migrations/versions/c3f8a1d64b27_accounts_id_to_uuid.py:101-123`，关键在 `:122`：

```sql
INSERT INTO accounts (id, ...) SELECT COALESCE(legacy_id, rowid), ... FROM accounts_uuid
```

- 证据（临时库：一行 `legacy_id=2` + 一行迁移后新建的 `legacy_id=NULL`）：

```
$ alembic downgrade b7c1e4a92d30
sqlalchemy.exc.IntegrityError: (sqlite3.IntegrityError) UNIQUE constraint failed: accounts.id
[SQL: INSERT INTO accounts (id, ...) SELECT COALESCE(legacy_id, rowid), ... FROM accounts_uuid]
```

失败后的库状态：

```
tables: [... 'accounts_uuid', 'accounts']
accounts count: 0            ← 新表建好了、数据没进去
accounts_uuid count: 2       ← 真实数据被改名留在这里
alembic_version: c3f8a1d64b27  ← 版本号没回退（SQLite DDL 非事务）
```

- 影响：`docs/adr/0008-uuid-primary-key.md:87` 把 `alembic downgrade b7c1e4a92d30` 写成正式回滚手段。真实库已有 1 行 `legacy_id IS NULL`（迁移后新建），只要再出现一次「legacy_id 与 rowid 撞号」就必然失败；失败后应用读到的是**空的 accounts 表**（1057 个号「消失」），且版本号停在 c3f8，重跑 downgrade 也不会自愈。
- 建议：
  1. 改成「先算目标 id 集合、检测冲突、再写」：`SELECT legacy_id, rowid` 两列先取出来在 Python 里做冲突消解（冲突时给新 id = `max(legacy_id, rowid)+n`），或直接把 `legacy_id` 为 NULL 的行改用 `max(legacy_id)+rowid` 偏移；
  2. 整个 downgrade 包在显式事务里（SQLite 的 DDL 虽非事务，但可以把「建新表 → 灌数据 → 校验行数 → 改名」做成可重入的多步，失败时保留 `accounts_uuid` 并在日志里打印恢复命令）；
  3. 补一条带数据的 downgrade 测试（至少覆盖 legacy_id 空洞 + 新建行两种形态）。

#### D-14 [P1] baseline 与模型类型漂移：所有时间列是 `VARCHAR`，模型是 `DATETIME`

- 位置：`migrations/versions/77cc4baded71_baseline_existing_schema.py:32-33,62-64,83-85,99,110,135-136,159-160`（全部 `sa.String()`）；模型侧 `core/db/models_account.py:93-94`、`core/db/models_infra.py:30,51-52,63`、`core/db/models_platform.py:38-40,89-91,136-137` 都是 `datetime`。
- 证据（空库 `alembic upgrade head` vs `create_all` 逐列对比）：

```
DIFF accounts         created_at alembic=VARCHAR  create_all=DATETIME
                      updated_at alembic=VARCHAR  create_all=DATETIME
DIFF icloud_accounts  created_at / updated_at / last_sync_at  VARCHAR vs DATETIME
DIFF icloud_aliases   created_at / updated_at                 VARCHAR vs DATETIME
DIFF outlook_accounts created_at / updated_at / last_used     VARCHAR vs DATETIME
DIFF proxies          last_checked                            VARCHAR vs DATETIME
DIFF task_logs        created_at                              VARCHAR vs DATETIME
DIFF task_runs        created_at / updated_at                 VARCHAR vs DATETIME
```

- 影响：SQLite 靠亲和性容忍，读写暂时都能跑；但两条建库路径产出的 schema 不同（TEXT 亲和 vs NUMERIC 亲和），`alembic check` 又因为 SQLite 反射限制不报类型差异（见 D-16），等于**永远不会被发现**。将来换 Postgres 或做 `CAST`/时间比较时会集中爆发。
- 建议：baseline 不改历史（已发布的 revision 应保持原样），新增一个 revision 用 `batch_alter_table` 把时间列改成 `sa.DateTime()`；或在 `env.py` 的 `compare_type=True` 下让 `alembic check` 至少能报出来（SQLite 下仍有限制，需要测试兜底）。

#### D-15 [P1] 手写 runner 的「唯一索引」与模型定义不同名同义，且老库大小写重复会让**启动崩溃**

- 位置：`core/db/migrations.py:358-362`（先 `UPDATE ... lower(trim(email))`）、`:364-412`（再去重）、`:417-419`（建**裸列**唯一索引）；对照模型 `core/db/models_account.py:36-42`（表达式索引 `lower(email)`）与 alembic `b7c1e4a92d30:57-64`。
- 证据（构造「老库形态」：裸列唯一索引 + `User@X.ai` / `user@x.ai` 两行）：

```
$ _ensure_account_email_unique(engine)
RAISED IntegrityError (sqlite3.IntegrityError)
  UNIQUE constraint failed: accounts.platform, accounts.email
[SQL: UPDATE accounts SET email = lower(trim(email)) WHERE email <> lower(trim(email))]
```

- 影响：
  1. 这正是 `b7c1e4a92d30` 文档里说「实测 `USER@X.AI` 能绕过裸索引」的那批数据。runner 的步骤顺序反了（先归一、后去重），归一那一步就会撞上已有的裸索引；异常没有被捕获，`run_migrations()` → `init_db()`（`main.py:147`）→ **服务起不来**。
  2. 索引名相同、定义不同：在「裸索引已存在、表达式索引不存在」的库上，`:417` 的 `IF NOT EXISTS` 会保留弱约束，`alembic check` 又反射不到表达式索引（D-16），于是弱约束可以长期存在。
- 建议：把 `_ensure_account_email_unique` 改成「先按 `lower(email)` 分组去重（不先 UPDATE）→ 再 UPDATE → 再建表达式索引」，整段包 `try/except` 只记日志不阻塞启动；索引定义改成 `lower(email)` 与模型一致；长期看应删掉这段手写 DDL，统一交给 alembic（见 D-12）。

#### D-16 [P2] `alembic check` 对表达式唯一索引是盲的；真库版本还落后 head 一个 revision

- 位置：`migrations/env.py:112` 反射告警；`migrations/versions/b7c1e4a92d30:59-64`。
- 证据：

```
SAWarning: Skipped unsupported reflection of expression-based index uq_accounts_platform_email
UserWarning: autogenerate skipping metadata-specified expression-based index ... can't reflect
No new upgrade operations detected.
```

真库 `alembic_version = d4e7f1a25b90`，head 是 `e5b2c8f71a34`（`alembic upgrade head` 在副本上只跑 e5b2 且成功，因为索引已由 runner 建好）。

- 影响：最关键的约束「同一平台邮箱唯一」**无法被 autogenerate 校验**——索引被删/被换成弱定义都不会报警。真库版本号落后但 schema 实际已对齐，属于「版本号说谎」，会让「`alembic current` == head」这类运维判断失真。
- 建议：在 `tests/test_migration_index_parity.py` 里显式查 `sqlite_master.sql` 断言索引定义包含 `lower(email)`（不能只比索引名）；部署脚本在 `init_db` 后统一 `alembic stamp head`。

---

### (3) 凭据加密覆盖面

**结论：主账号的 password/extra 六键、配置口令、TOTP 日志已覆盖；`token` 列、`extra.cookies`、Outlook 池、代理 URL、schema 外配置键仍是明文。**

#### D-17 [P0] `accounts.token` 列 1057/1057 明文，且迁移会主动把解密后的 SSO 写进去

- 位置：`core/db/models_account.py:86`（`token: str = ""`，普通列）；`core/db/migrations.py:751-757`（`UPDATE accounts SET token = ?` 写入 `decrypt_extra()` 之后的 `sso`）。
- 证据（只读真库）：

```
token 非空 1057 / 明文（非 v1:）1057
```

`_normalize_grok_token_column` 的注释明确说「token 列 = 平台主凭证的镜像（grok → sso）」，并在 `:746` 先 `decrypt_extra`，`:756` 把明文 SSO 写回 `token`。

- 影响：`token` 是 AT/SSO 的**完整副本**。即使 `extra_json` 全加密，拿到 DB 文件的人直接读 `token` 就能冒用全部 1057 个账号；`credential_crypto` 的「六键加密」在这条镜像列上被完全绕过。
- 建议：二选一——(a) `token` 也走 `encrypt_text`（读侧 `AccountModel.token` 改 property 透明解密，改动面与 password 同型）；(b) 删除镜像列，读侧统一 `get_credential(extra, ...) or token_column_credential(...)` 的兜底链已在（`core/credential_fields.py:178-192`），可以逐步停写。迁移脚本 `scripts/encrypt_existing_credentials.py` 必须同步覆盖 `token`。

#### D-18 [P1] `extra.cookies`（607 行）与 `extra.password`（1 行）不在加密白名单

- 位置：`core/credential_crypto.py:41-50`（`SENSITIVE_EXTRA_KEYS` 只有 6 个键）；`core/models/account.py:199-209`（`cookies` 直接写 `extra["cookies"]`）。
- 证据（只读真库，按 key 统计明密文）：

```
cookies   plain=607  cipher=0
password  plain=1    cipher=0
```

- 影响：ChatGPT 的 `cookies` 是完整的 Web session 凭据（含 `__Secure-next-auth.session-token`），泄露即可免密登录；607/1057 行明文，且这是注册链路的常规产物。`extra.password` 虽然只有 1 行，但说明插件确实会往 extra 里塞密码。
- 建议：`SENSITIVE_EXTRA_KEYS` 增加 `cookies` / `cookie_header` / `password` / `api_key` / `client_secret`；`encrypt_existing_credentials.py` 的 `_verify` 用同一集合（它已经 import 了 `SENSITIVE_EXTRA_KEYS`，加键即自动覆盖）。注意 `cookies` 可能被 `LIKE` 查询（面板比对），加密前先确认没有 SQL 依赖。

#### D-19 [P1] 78 个配置键不在 `CONFIG_SCHEMA` 内，口令白名单是闭集 → 明文入库

- 位置：`core/config_schema.py:135-137`（`SECRET_CONFIG_KEYS` 只从 schema 的 `secret=True` 派生）；`core/config_store.py:20-27`（`_seal` 只加密白名单键）。
- 证据（只读真库）：

```
configs 143 键，其中 78 个不在 CONFIG_SCHEMA
gptmail_api_key    PLAINTEXT len=15
maliapi_api_key    PLAINTEXT len=27
（另有 freemail_password / duckmail_api_key / duckmail_bearer / laoudo_auth
  等键名，当前值为空，一旦填写即明文）
```

- 影响：`config_store.set()` 接受任意 key，而 `_seal` 只认 10 个硬编码键。邮件渠道（freemail/duckmail/laoudo/gptmail/maliapi）的密码与 API key 全部落在白名单外，`GET /api/config` 还会回明文（`api/config.py` 只对 `SECRET_CONFIG_KEYS` 打码）。
- 建议：`_seal` 改成「白名单 ∪ 键名正则」双判据（`(?:^|_)(key|token|secret|password|passwd|pwd|bearer|auth)$`），或把 78 个键补进 `CONFIG_SCHEMA`；补一条测试：对 schema 外但形似口令的键 `set()` 后，库里必须是 `v1:`。

#### D-20 [P1] `proxies.url` 内嵌 `user:pass`，5/5 明文；历史任务日志里还有 59 条副本

- 位置：`core/db/models_infra.py:59`（`url: str = Field(unique=True)`，无加密）；`services/task_runners.py` 的「使用代理: …」日志（与 `docs/audit/2026-10-30/proxy.md` P1-10 同一批）。
- 证据（只读真库）：

```
proxies 5 行，url like '%@%' = 5
task_runs logs_json like '%://%@%' = 59
```

- 补充：`a3470d3` 已在写日志前调用 `_redact_proxy(...)`（`services/task_runners.py:270,579`），**新**日志不再含明文；但库里已有的 59 行历史记录不会自动消失，且 `proxies.url` 本身仍是明文。

- 影响：代理出口是注册链路的网络身份，`user:pass` 泄露等于代理池被白嫖/被封。日志里的 59 条是同一份凭据的第二个副本（且 `GET /api/tasks/{id}` 会回给前端）。
- 建议：代理 URL 拆成 `scheme/host/port/username/password_cipher` 四段存储（`ProxyRecord` 已集中，改动面可控），或至少对 `url` 整体 `encrypt_text` 并在 API 层脱敏；任务日志写入前用统一 `redact_proxy_url()` 打码。

#### D-21 [P1] 存量加密脚本只扫默认库，且不覆盖 token / Outlook 池 / 代理

- 位置：`scripts/encrypt_existing_credentials.py:44-46`（`current_engine()` 单库）、`:156-159`（`_verify` 同样单库）。
- 证据：`_accounts()` 只 `SELECT ... FROM accounts`（默认 engine），没有遍历 `platform_database_registry.configured_platforms()`；`_configs()` 只处理 `SECRET_CONFIG_KEYS`；完全没有 `outlook_accounts` / `proxies` / `token` 分支。
- 影响：分库部署（`DATABASE_URL_GROK=...`）下平台库里的明文账号不会被加密，`--verify` 还会报「0 明文」给出虚假的安全感。真库当前 `data/platforms/chatgpt.db` / `grok.db` 是 0 字节，所以还没暴露；一旦启用分库就是缺口。
- 建议：复用 `core/db/migrations.py:_all_engines()` 遍历所有库；增加 `token`、`outlook_accounts.password/refresh_token`、`proxies.url` 三段；`--verify` 同样遍历并汇总。

#### D-22 [P1] 密钥文件与数据库文件是 0644

- 位置：`core/secret_box.py:85`（新建时 `0o600`），但没有对**已存在**的文件做 `chmod`；`core/paths.py` 的 `ensure_data_dirs()` 也没收紧目录权限。
- 证据：

```
644 data/account_manager.db
644 data/secrets/credential_key
755 data/secrets
```

- 影响：同机其他用户可以直接读走密钥 + 密文库，AES-GCM 的「静态加密」在这台机器上等于 0。容器里如果以 root 跑、挂载卷给其他容器共享，同样成立。
- 建议：`_load_or_create_key()` 读到已有文件时 `os.chmod(path, 0o600)`；启动时对 `data/`、`data/secrets/` 做 `chmod 700`；文档里把「挂载卷权限」写成部署检查项。

#### D-23 [P2] `icloud_aliases.share_token` 是明文 bearer 凭据

- 位置：`core/db/models_platform.py:134-135`。
- 证据：真库 1941 行，`share_token` 全为 22 字符明文。
- 影响：设计上「链接即权限」（`docs` 注释也这么写），但 DB 泄露即可枚举所有别名的最新邮件入口。属于设计取舍，不是 bug。
- 建议：若要收紧，存 `sha256(token)`，查询时对输入做同样哈希（`get_by_share_token` 是唯一读点，改动很小）；否则在文档里显式登记为「已知明文凭据」。

#### D-24 [P2] `dump_accounts.py` 的打码清单是手写子集

- 位置：`scripts/dump_accounts.py:36-54`。
- 证据：`_SECRET_KEYS` 覆盖 AT/RT/ST/IDT/SSO/cookies/password，但漏 `api_key` / `client_secret` / `bearer` / `cookie`（单数）/ `auth`；`user_id`、`cashier_url` 原样输出；大小写变体只列了 4 个 camelCase。
- 影响：导出快照用于 diff 时，插件新加的凭据键会以明文进 `/tmp/before.json`。
- 建议：直接复用 `core/credential_crypto.SENSITIVE_EXTRA_KEYS | {"cookies","cookie_header","password","api_key","client_secret"}`，并对 key 做 `lower()` 归一后匹配。

---

### (4) 唯一索引 / 去重逻辑的边界

**结论：真库的表达式唯一索引是好的；但「大小写」「空邮箱」「并发 upsert」「合并去重」四条边界上，两条后端/两套迁移的行为不一致，其中一条会导致启动崩溃（D-15）。**

#### D-25 [P1] JSON 后端的 `proxies` 没有 url 唯一约束

- 位置：`core/db/proxy_repository.py:82-88`（JSON `add` 直接 `insert`）；对照 `core/db/models_infra.py:52`（`url: str = Field(unique=True)`）。
- 证据：`bulk_add` 先 `find_by_url` 再 `add`（`api/proxies.py:59-63`），但 `add` 本身在 JSON 分支不做存在性检查；SQLite 分支靠 `UNIQUE(url)` 抛 `IntegrityError`。并发/直接调用 `add` 时 JSON 会插重复行，之后 `find_by_url` 只返回第一条，健康计数分叉。
- 建议：JSON `add` 内先 `store.first(url=...)`，存在则返回已有行（或抛与 SQLite 同型的错误）；把「url 唯一」写进 `ProxyRepository` 的契约测试（两后端同一断言）。

#### D-26 [P1] `merge.py`：`extra_json="{}"` 会覆盖对方的完整凭证；id 冲突换号后原 id 丢失

- 位置：`core/db/merge.py:151-164`（空值不覆盖规则只判 `value.strip()==""`）、`:228` / `:260-269`（`_ensure_unique_id`）。
- 证据：

```python
if isinstance(value, str) and not value.strip() and str(existing.get(key) or "").strip():
    continue          # 只挡住空串
merged[key] = value   # "{}" 不是空串 → 覆盖
```

`_ensure_unique_id()` 在 id 冲突时生成新 UUID，注释说「旧值记到 legacy_id 之外不动」——实际既没写 `legacy_id`，也没在任何地方保留旧 id。

- 影响：目标库有 AT/RT（`extra_json` 非空），源库同账号是 `{}`（导入时的空壳），合并后凭证被清空；`legacy_id` 列本来正是为「保留旧 id」设计的，却没用上，任务日志/面板记录按旧 id 对账会断。
- 建议：把 `("", "{}", "[]", "null")` 统一视为空值；`_ensure_unique_id` 冲突时把旧 id 写进 `legacy_id`（目标列存在时），并保留一张 `id 映射` 写进报告。

#### D-27 [P2] 空邮箱在 DB 层是合法值，唯一索引会把它锁成「每平台一行」

- 位置：`core/db/models_account.py:72`（`email: str`，NOT NULL 但允许 `""`）；`core/db/repository.py:353-355`（`upsert` 会拒绝空邮箱）。
- 证据：`upsert` 有守卫，但 `AccountModel(...)` 直接构造 + `save()`、或裸 SQL 导入可以写入 `email=""`；表达式索引 `(platform, lower(email))` 对空串生效，于是第二个空邮箱行会 `IntegrityError`。
- 影响：目前所有入口（`api/accounts.py:369`、`:612`、`upsert`）都做了守卫，属于防御纵深问题。
- 建议：模型层加 `@field_validator("email")` 拒绝空串，或在 DB 层加 `CHECK(length(trim(email)) > 0)`（新迁移）。

#### D-28 [P2] 手写去重会丢掉 `legacy_id`

- 位置：`core/db/migrations.py:370-372`（SELECT 列表没有 `legacy_id`）、`:400-408`（UPDATE 也不写它）。
- 证据：重复行合并只保留 `password/user_id/region/token/status/cashier_url/extra_json`，`legacy_id` 取 keep 行的值；如果 keep 行是 NULL 而重复行有值，旧 id 永久丢失。
- 影响：去重后再 downgrade，`COALESCE(legacy_id, rowid)` 会给这一行分配一个与历史无关的整数 id（叠加 D-13 的撞号风险）。
- 建议：SELECT/UPDATE 都带上 `legacy_id`，按「非空优先」合并。

---

### (5) UUID 化后是否还有遗留 int 假设

**结论：数据层基本干净（`accounts.id` 是 `str`、`get_many` 用 `str()`），但 API 层有一处 `int(uuid)` 必崩，另有 4 处注释仍在教人「id 是每库自增」。

#### D-29 [P0] `api/actions.py:263` 对 UUID 做 `int()`：批量 CLIProxyAPI 同步必 500

- 位置：`api/actions.py:263`。
- 证据：

```python
sync_result = sync_results.get(int(acc_model.id or 0), {})
```

`sync_chatgpt_cliproxyapi_status_batch()` 的返回字典以 `account.id` 为键（`services/cliproxyapi_sync.py:682-689`），而 `account.id` 现在是 UUID 字符串。复现：

```
int(uuid) -> ValueError invalid literal for int() with base 10: '7ef16b1a-...'
```

- 影响：`_execute_batch_cliproxy_sync()` 的调用链（批量动作 → 面板状态同步）在 UUID 迁移后 100% 抛 `ValueError`，接口 500。这是本轮唯一一处**确定性崩溃**的 int 遗留。
- 建议：改成 `sync_results.get(acc_model.id, {})`（键本来就是同一个对象），并加一条 `tests/test_accounts_uuid_pk.py` 的断言：批量同步在 UUID 主键下不抛异常。

#### D-30 [P2] 误导性注释仍在描述「每库自增 id / 撞号」

- 位置：`core/db/repository.py:594-596`、`:646-647`、`:727`、`:802`；`api/accounts.py:60`、`:728`、`:764`。
- 证据：例如 `repository.py:594`「账号 id 是**每库各自自增**的，不同库里的同一个数字指向不同账号」——UUID 后不再成立。代码里的 `func.lower(platform)` 兜底过滤本身无害（UUID 全局唯一），但注释会让后来者以为还需要防撞号，继续写多余的过滤。
- 影响：认知负担 + 可能诱导新代码继续用 int 假设。
- 建议：把注释改成「UUID 全局唯一；platform 过滤仅用于限定库/防御历史脏数据」，或直接删掉多余过滤（保留 platform 校验以防御跨库误传）。

#### D-31 [P2] `AccountStoreJson` 里还留着 int 自增游标（死代码）

- 位置：`core/storage/account_store_json.py:88-109`（`_cursor` / `_next_id` / `_max_existing_id`）。
- 证据：`insert()` 已改成 `row["id"] = str(row.get("id") or uuid.uuid4())`（`:117`），`_next_id` 无调用方；真库 `data/accounts.json` 里还留着 `"_meta": {"account_id": {"value": 1}}` 的旧游标。
- 影响：死代码 + 残留元数据，容易被误认为「JSON 账号还是自增 id」。
- 建议：删除三个方法；迁移脚本清理 `_meta.account_id`（可选）。

---

### (6) 事务 / 会话泄漏

**结论：没有发现「Session 开了不关」的泄漏（仓储都有 `finally: close()`，`with` 用得规范）；真正的问题是「回滚调用方事务」「长事务里做全表读」「每库重复扫」这三类语义/性能问题。**

#### D-32 [P1] `upsert()` 的 `IntegrityError` 重试会 `rollback()` 调用方整个事务

- 位置：`core/db/repository.py:399-408`。
- 证据：

```python
except IntegrityError:
    sess.rollback()          # sess 可能是 FastAPI 注入的共享 session
    existing = sess.exec(...)
```

- 影响：调用方在同一 session 里已经写过的其它行（例如批量导入的前 N 条）会被一起回滚，且调用方完全不知情；随后 `sess.commit()` 只提交重试后的这一行。批量导入接口（`api/accounts.py` 的 import 循环）正好是「一个 session 多次 upsert」的形态。
- 建议：用 `sess.begin_nested()`（SAVEPOINT）包住单行插入，冲突时只回滚 savepoint；或让 `upsert` 在传入 `session` 时要求调用方自己处理 `IntegrityError`，仓储只负责「查已存在」。

#### D-33 [P1] `OutlookRepository.set_fields()` / `add()` 不做加密，`requeue_account` 会把明文写回池

- 位置：`core/db/outlook_repository.py:200-242`（`add` / `set_fields` 原样写 `payload`）；`core/mailboxes/channels/outlook/mailbox.py:330-355`（`requeue_account` 用 `set_fields(password=..., refresh_token=...)` 与 `session.add(OutlookAccountModel(...))`）。
- 证据：`grep encrypt_text/decrypt_text` 在整个 Outlook 链路上为 0；`get_email()` 从池里取出的 `password` 直接用于 IMAP 登录（`mailbox.py:286-305`），说明当前设计就是明文往返。
- 影响：今天真库 3 行 password/RT 都是空串（mailapi_url 模式），所以没暴露；一旦导入密码型 Outlook 账号（`services/mail_imports/providers.py` 正是这么写的），就是明文落库 + 明文回写。
- 建议：与 `AccountModel.password` 同型，把 `OutlookAccountModel.password` / `refresh_token` 改成 `*_cipher` + property；`OutlookRepository.add/set_fields` 内统一 `encrypt_text`。这是 D-03 的根因，修 D-03 时一并做。

#### D-34 [P2] 长事务 / 全表读：日志分页先 `len(all())`，跨库计数每次全扫

- 位置：`api/tasks.py:655`（`total = len(s.exec(q).all())`）、`core/db/repository.py:1071-1090`（`_distinct_keys` 每次 count 都跨库拉全部 `(platform,email)`）、`core/db/repository.py:1169-1201`（`list_all_accounts` 每库取 `offset+limit` 行再合并）。
- 证据：`task_logs` 已 1661 行、`accounts` 1057 行；`get_logs` 为了算 total 把整表读进内存再丢弃，`count_all_accounts` 每次都重建全量 key 集合。
- 影响：数据量再涨一个量级后，日志页/账号页会明显变慢；长事务还会持有 SQLite 读锁，阻塞写入（SQLite 单写者）。
- 建议：`get_logs` 用 `select(func.count()).select_from(TaskLog)`；`count_all_accounts` 用 SQL `COUNT(DISTINCT platform || '\x00' || lower(email))` 或缓存 key 集合（按 `updated_at` 失效）。

#### D-35 [P2] `PlatformDatabaseRegistry.configure()` 清空 engine 缓存但不 `dispose()`

- 位置：`core/db/registry.py:52-62`（`self._engines.clear()`）、`:144-152`（`dispose_all()` 才会 dispose）。
- 证据：`init_db()` 每次启动都 `reload_from_env()` → `configure()`；测试里反复 `configure()` 会累积未关闭的连接池。
- 影响：连接泄漏（进程内），长时间运行/多次热更新后 SQLite 文件句柄增多。
- 建议：`configure()` 里先对旧 engine 逐个 `dispose()` 再清空；或复用 `dispose_all()`。

#### D-36 [P2] 启动迁移每次对 1057 个地址逐条 UPDATE

- 位置：`core/db/migrations.py:227-243`（`for addr, token in owner.items(): UPDATE ...`）。
- 证据：`owner` 来自全部库的 `accounts`（真库 1057 行），每个地址一条 UPDATE；`_run_one` 对默认库 + 每个平台库都跑一遍。
- 影响：启动时间随账号数线性增长，且每次启动都在写库（即使 `used_platforms` 已填好，`WHERE ... = ''` 会让它变成空更新，但语句仍要执行 1057 次）。
- 建议：改成一条 `UPDATE ... WHERE lower(email) IN (...)` 分批（500/批），或先 `SELECT` 出 `used_platforms=''` 的行再只更新这些。

---

## 3. 修复优先级建议

| 优先级 | 条目 | 一句话 |
|---|---|---|
| 立刻 | D-01 / D-02 / D-03（根因 D-33） | 三条写路径会把 AT/RT/SSO/cookies/password 明文落盘；先堵住「新数据不再明文」 |
| 立刻 | D-04 / D-29 | 两个确定性 500（JSON 改账号、批量面板同步） |
| 立刻 | D-12 / D-13 | 迁移链进不了应用库；downgrade 会把 `accounts` 变空表 |
| 立刻 | D-15 | 老库大小写重复 → 启动崩溃 |
| 本周 | D-05 / D-06 / D-08 | 混合后端不可用；JSON 并发丢号；JSON 判重 API 抛错 |
| 本周 | D-17 / D-18 / D-19 / D-20 | 存量明文凭据（token/cookies/配置/代理） |
| 下周 | D-07 / D-09 / D-10 / D-11 | 读路径收敛到仓储、统一返回形状 |
| 下周 | D-14 / D-16 / D-21 / D-22 | schema 类型对齐、校验盲区、脚本覆盖、文件权限 |
| 排期 | D-23、D-24、D-27、D-28、D-30、D-31、D-34、D-35、D-36 | 设计取舍、注释、死代码、性能 |

**回归测试建议（每条对应一个已复现的 bug）：**

1. `test_json_upsert_encrypts_credentials`：JSON 模式 upsert `AccountRecord` 后，`accounts.json` 的 `password`/`access_token`/`cookies` 必须以 `v1:` 开头。
2. `test_sqlite_upsert_encrypts_extra_on_update`：SQLite 模式二次 upsert 后，`extra_json` 的 AT 必须是 `v1:`。
3. `test_outlook_import_json_backend`：JSON 模式导入 Outlook，`outlook_accounts.json` 有行、`accounts.json` 不变、password 是密文。
4. `test_create_account_update_json_backend`：JSON 模式对已存在邮箱 `POST /api/accounts` 返回 200。
5. `test_mixed_backend_creates_accounts_table`：`STORAGE_BACKEND=json` + `STORAGE_BACKEND_ACCOUNTS=sqlite` 启动后 `accounts` 表存在。
6. `test_json_store_concurrent_upsert`：20 线程 upsert，20 行且无异常。
7. `test_alembic_upgrade_on_create_all_db`：`create_all` → `upgrade head` 成功。
8. `test_uuid_downgrade_with_new_rows`：含 `legacy_id=NULL` 行的库 downgrade 成功且行数守恒。
9. `test_ensure_unique_index_with_case_duplicates`：大小写重复老库启动不抛异常。
10. `test_batch_cliproxy_sync_uuid_ids`：UUID 主键下批量同步不抛 `ValueError`。

---

## 4. 复现命令（全部在临时目录，不碰真库）

```bash
D=/root/.hermes/cache/scratch/dlaudit
mkdir -p "$D"

# 真库副本（只读复制，之后的写实验都在副本上）
cp data/account_manager.db "$D/real_copy.db"
ALEMBIC_DATABASE_URL="sqlite:///$D/real_copy.db" .venv/bin/python -m alembic check
ALEMBIC_DATABASE_URL="sqlite:///$D/real_copy.db" .venv/bin/python -m alembic upgrade head

# 空库：整链 upgrade → downgrade base → 再 upgrade
rm -f "$D/fresh.db"
ALEMBIC_DATABASE_URL="sqlite:///$D/fresh.db" .venv/bin/python -m alembic upgrade head
ALEMBIC_DATABASE_URL="sqlite:///$D/fresh.db" .venv/bin/python -m alembic downgrade base
ALEMBIC_DATABASE_URL="sqlite:///$D/fresh.db" .venv/bin/python -m alembic upgrade head

# 应用自建库（create_all）→ upgrade（预期失败：table accounts already exists）
DATABASE_URL="sqlite:///$D/createall.db" .venv/bin/python - <<'PY'
import os
os.environ["DATABASE_URL"] = "sqlite:////root/.hermes/cache/scratch/dlaudit/createall.db"
from sqlmodel import SQLModel
from core.db.base import current_engine
import core.config_store, core.db.models_account, core.db.models_infra, core.db.models_platform
SQLModel.metadata.create_all(current_engine())
PY
ALEMBIC_DATABASE_URL="sqlite:///$D/createall.db" .venv/bin/python -m alembic upgrade head

# JSON 后端明文落盘（临时 DATA_DIR / 临时密钥）
env -i PATH="$PATH" HOME=/root \
  DATA_DIR="$D/probe" CREDENTIAL_ENCRYPTION_KEY_FILE="$D/probe/key" \
  STORAGE_BACKEND=json \
  .venv/bin/python -c '...'   # 见 D-01 片段：upsert(AccountRecord(...)) 后读 accounts.json

# 只读真库取证
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("file:data/account_manager.db?mode=ro", uri=True)
print(c.execute("select count(*) from accounts where token<>'' and token not like 'v1:%'").fetchone())
print(c.execute("select count(*) from proxies where url like '%@%'").fetchone())
PY
```

---

## 5. 附：本轮未展开、建议下一轮补审的点

- `services/task_store_io.py` 的 `TaskRun` 快照归一与 JSON 分支的 `None` 字段（`put_json` 会把 `None` 原样写入，SQLite 是 NOT NULL + 默认值；目前快照都有默认值，未触发）。
- `core/db/icloud_alias_repository.py` 的 `update_where(match={...None...})`：SQLite `= NULL` 永不命中，JSON `str(None)=="None"` 会命中（`core/storage/query.py:57-64` 的 `eq` 已处理 None，但 `update_where` 走的是自己的字符串比较，`:231-240`）。当前调用方（`services/icloud/aliases.py:399`）只传字符串，未触发。
- `AccountStoreJson.platforms_by_email()` 是 O(N) 全表扫且被逐地址调用（`core/db/repository.py:949`），邮箱池候选多时是 O(N×M)。
- `data/accounts.json` 目前是空骨架但真实数据在 SQLite：一旦有人把 `STORAGE_BACKEND` 切成 `json`，界面会显示 0 个账号，且仓库里**没有** sqlite→json 的迁移脚本（`scripts/` 只有 merge/dump/encrypt）。建议在文档与启动日志里显式警告，或提供 `scripts/export_accounts_json.py`。

---

## 6. 第二轮补充（2026-10-30 复核）

复核方式：只读静态审阅 + 对真库 `file:...?mode=ro` 跑 `PRAGMA` / `EXPLAIN QUERY PLAN` / 行数与
长度统计 + 在**库副本**上调用 `api/accounts.py::_slim_account_row()` 复现响应形状 +
单文件测试 `tests/test_accounts_list_payload.py`（10 passed，未改动任何源码）。
真库 `quick_check = ok`；`alembic_version = e5b2c8f71a34`（= head）—— 与 D-16 记录的
`d4e7f1a25b90` 不一致，可能审计后跑过 `upgrade head`（或 D-16 读的是副本）。
**D-16 的「版本号落后 head」结论按当前真库不成立，但「表达式索引反射盲区」仍成立**
（`migrations/env.py:112` 的 `Skipped unsupported reflection of expression-based index`
告警与索引定义无关，属 alembic/SQLite 反射限制）。以下条目与 §1–§5 不重复。

> **行号口径**：§6 的行号以本轮复核时的**工作树**为准。复核期间 `core/db/repository.py`
> 有未提交改动（新增 `AccountRepository.update_extra()`，+53 行），因此该文件中
> **436 行之后**的引用比 HEAD 大 53（例：`stats()` 在 891 而非 838）。每条都同时给了
> 函数名/代码片段，改版后按名字定位即可。其余文件（`api/accounts.py`、`core/db/migrations.py`、
> `core/proxy_pool.py`、`services/task_store_io.py`）行号与 HEAD 一致。

### R-01 [P0] 列表接口把 grok 的 SSO 正文（明文）下发给客户端 —— `a3470d3` 漏掉的那把钥匙

- 位置：`api/accounts.py:165`、`:167`、`:225`、`:241-253`、`:283`
- 证据：
  - `_LIST_HEAVY_EXTRA_KEYS = ("session_token", "id_token", "cookies", "cpa_record")`（:165）与
    `_LIST_TOKEN_EXTRA_KEYS = ("access_token", "refresh_token")`（:167）**都是黑名单**，
    两边都没有 `sso` / `sso_token`。
  - `slim = decrypt_extra(data)`（:225）先把整份 `extra_json` 解密，再按黑名单 pop。
  - `core/credential_crypto.py:41-50` 的 `SENSITIVE_EXTRA_KEYS` 明确包含 `sso`
    —— 代码自己把 SSO 定性为「可直接冒用身份的 bearer 凭证」，落盘加密、读侧解密，
    然后原样进了列表响应。
  - 库副本实测：对 `platform='grok'` 的真实行调用 `_slim_account_row()`，返回的
    `extra_json` 含 `"sso": "eyJ0eX...NiJ9.eyJ..."`（明文 JWT）。同一行的 `token` 列确实被
    :283 的 `exclude={"password","token"}` 排掉了 —— **列被堵住，extra 里的同一把钥匙没堵**。
  - `tests/test_accounts_list_payload.py` 全文 0 处提及 `sso`（`grep -c sso` = 0），10 条用例全绿，
    所以回归测试挡不住。
- 影响：`GET /api/accounts?page_size=1000` 一次最多返回 1000 行，可把整个 grok 号池（390 行）
  的 SSO 正文批量交给调用方。面板未设密码时中间件只放行本机回环（`api/auth.py:337`），
  但任何已登录会话 / 本机进程 / 浏览器扩展都能一次拖走全部登录态。这与 D-17（`token` 列
  静态明文）是**同一批凭据的两个出口**：D-17 修了静态，这条还是传输侧。
- 建议：
  1. 黑名单改**白名单**：列表只放行确实要渲染的键（`register_proxy`、`chatgpt_local`、
     `probe_status`、`sync_statuses`…），其余一律 pop —— 以后新增凭证字段不会再漏；
  2. 过渡期至少把 `sso`、`sso_token` 并入 `_LIST_TOKEN_EXTRA_KEYS` 并补 `has_sso` 布尔
     （与 `has_access_token` 同款，:241-253 的循环已经是这个形状，加一项即可）；
  3. `decrypt_extra` 改成「只解密要放行的键」，不要先全量解密再丢弃（顺带省掉 P0 之外的 CPU）；
  4. 测试用 `SENSITIVE_EXTRA_KEYS` **注册表驱动**断言：列表响应里不得出现其中任何一个键。

### R-02 [P1] 引擎没配 WAL / busy_timeout：写并发靠 pysqlite 默认值，仓内已有 `database is locked` 实测记录

- 位置：`core/db/base.py:57`（`engine = create_engine(DATABASE_URL)`）、
  `core/db/registry.py:109`（平台库同款裸 `create_engine`）
- 证据：全仓无 `connect_args`、无 `event.listens_for(engine, "connect")`、应用侧无
  `PRAGMA journal_mode/busy_timeout/foreign_keys`（grep 只命中 `migrations.py` / `merge.py` 的
  `PRAGMA table_info`）。真库：`journal_mode=delete`、`busy_timeout=5000`（= pysqlite 默认，
  非显式配置）、`synchronous=2`。
- 影响：`journal_mode=delete` 下写事务阻塞读者、每个事务一次 fsync；并发写超时直接
  `database is locked`。这不是假设：`services/chatgpt_two_factor.py:242`、`:313`、`:407`
  三处注释都写着「另开连接写库就是 `database is locked`（实测复现）」，并为此专门加了文件
  写前日志兜底。当前形态是 FastAPI 线程池 + 后台任务线程 + 启动迁移同时写一个文件。
- 建议：在引擎创建处统一挂 connect 事件：`journal_mode=WAL`（持久化）、
  `busy_timeout=15000`、`synchronous=NORMAL`、`foreign_keys=ON`（见 R-06）。
  `services/data_bundle.py` 的导出已用 `VACUUM INTO`，WAL 下仍然正确。

### R-03 [P1] 每追加一行日志就整行 UPDATE 一次任务快照（写放大 50–100 倍）

- 位置：`services/task_service.py:95`（`log()` 无条件 `persist_task_snapshot`）→
  `services/task_store_io.py:159-178`（UPDATE 全字段）
- 证据：更新分支把 `meta_json` / `logs_json` / `errors_json` / `cashier_urls_json` /
  `control_json` 全部重新 `json.dumps` 写回（:169-173），不区分「只多了一行日志」。
  真库单行最大 `logs_json = 158_093` B（`task_1793122095317`）；
  `task_runs` 累计 `sum(length(logs_json)) = 7_365_755`、`sum(length(errors_json)) = 4_719_115`。
- 影响：一个 100 账号任务按每号 5 行日志 = 500 次写，每次都重写整份 logs/errors
  （末段上百 KB），写放大 50–100 倍；在 `journal_mode=delete` 下每次都是一次完整事务 + fsync，
  写锁持有时间被拉长，直接放大 R-02。
- 建议：`log()` 做节流（按时间/条数合并，如 500 ms 或 20 条落一次盘），任务收尾强制 flush；
  或把日志拆到 `task_log_lines(task_id, seq, text)` 子表、快照只存计数 —— 后者同时解掉
  「列表页不敢带 logs」的老问题（D-34）。

### R-04 [P1] 判重热路径 `find_by_email` 用不到唯一索引，按平台全扫

- 位置：`core/db/repository.py:276-280`（同型：`registered_emails` :325-329、
  `platforms_by_email` :1021）
- 证据（真库 `EXPLAIN QUERY PLAN`）：
  - `WHERE platform = ? AND email = ?` → `SEARCH accounts USING INDEX ix_accounts_platform_created_at (platform=?)`
  - `WHERE platform = ? AND lower(email) = ?` → `SEARCH accounts USING INDEX uq_accounts_platform_email (platform=? AND <expr>=?)`
- 影响：唯一索引只承担「防重复」的约束作用，查询完全没吃到；每次判重要把该平台全部行读出来
  再比 email（chatgpt 现 667 行/次）。`is_registered()` 是注册链路**每个邮箱动手前**的必经调用，
  `upsert()` 开头还要再来一次（:364-368）。
- 建议：email 条件改成 `func.lower(AccountModel.email) == key`（值已是 `normalize_email()`
  的小写形态），三处一起改；改完 `ix_accounts_email` 可删（见 R-09）。

### R-05 [P1] `platform` 写入不归一、查询却一律按小写比较 —— 大小写不同的行会「消失」

- 位置：写入 `core/db/repository.py:352`（`platform = str(getattr(account, "platform", "") or "")`，
  无 `strip()/lower()`）、建行 `:397`；读取 `:958`、`:1015`、`:857`、`:692`、`:726`
  （一律 `== xxx.strip().lower()` 或 `func.lower(platform)`）
- 证据：`normalize_email()` 存在且到处调用（`core/db/base.py:86`），platform 没有对应归一；
  真库当前 `platform <> lower(platform)` 行数为 0 —— 只是**现有数据恰好**都小写。
  与 D-11(b) 的区别：那是 JSON/SQLite 两个后端对同一行的匹配差异，这里是**写入口缺归一**。
- 影响：任何插件/导入脚本产出一行 `platform="Grok"`，该行会出现在「不带 platform 的全量列表」里，
  但在所有按平台过滤的列表/计数/导出里静默消失（`WHERE platform='grok'` 匹配不到）；
  更糟的是 `uq_accounts_platform_email` 对 platform **大小写敏感**，同一邮箱可以同时存在
  `Grok` 与 `grok` 两行，而业务判重（走 lower 比较）认为只有一行。
- 建议：加 `normalize_platform()`（`strip().lower()`），在 `upsert` / `save` / `AccountModel.__init__`
  三处入口归一；补一条一次性 `UPDATE accounts SET platform = lower(trim(platform))`；
  之后可以去掉 `func.lower(platform)`（它同时挡住主键查找的索引使用，:639/:673）。

### R-06 [P1] 声明了外键但从不启用（`foreign_keys=0`）

- 位置：`core/db/models_platform.py:100`（`account_id: int = Field(index=True,
  foreign_key="icloud_accounts.id")`）；引擎处 `core/db/base.py:57` / `core/db/registry.py:109`
- 证据：`data/platforms/icloud.db` 的 DDL 确有
  `FOREIGN KEY(account_id) REFERENCES icloud_accounts (id)`（`PRAGMA foreign_key_list` 可见，
  `on_delete=NO ACTION`），但 `PRAGMA foreign_keys = 0`，应用侧没有 connect 事件打开它。
  当前 1941 条别名里孤儿 0 条 —— 只是还没人删过主号。
- 影响：删除 iCloud 主号会留下 `account_id` 指向不存在行的别名，它们仍出现在别名列表/取号池，
  到取号时才发现主号没了。约束写了等于没写。
- 建议：与 R-02 一起 `PRAGMA foreign_keys=ON`；删除主号改成显式级联
  （或 `ondelete="CASCADE"` + 重建表）；加一条一次性 SQL 找出历史孤儿。

### R-07 [P1] `page_size` 无上限，叠加「全表 stats」——一条请求即可拉满内存与 CPU

- 位置：`api/accounts.py:324`（`page_size = max(1, int(page_size or 20))`，无上界）；
  `core/db/repository.py::stats()`（工作树 `:891`，整表读在 `:910` / `:935`）
- 证据：`/api/accounts/stats` 每次把 1057 行**整行**读进内存（`extra_json` 平均 7 935 B、
  最大 17 105 B，全表约 8.4 MB）只为算两个 `group by`；列表接口的 `page_size` 只夹下界，
  `page_size=100000` 会把整库拉进内存并逐行 AES 解密（`_slim_extra_for_list` :225）。
- 影响：两个最热接口的开销都只随账号数线性增长；`page_size` 无上界与 R-01 组合 =
  「一条请求导空号池」的完整链路。
- 建议：`page_size` 夹到 `min(200, ...)`；`stats()` 改
  `select(platform, status, func.count()).group_by(platform, status)`（`ix_accounts_status` 可覆盖），
  或加一张按 `updated_at` 失效的汇总缓存。

### R-08 [P2] UUID 之后 `MIN(id)` / `ORDER BY id` 不再等于「最早那条」

- 位置：`core/db/migrations.py:365-368`（`MIN(id) keep_id`）、`:373`（`ORDER BY id`）；
  `migrations/versions/b7c1e4a92d30...py:48-50`（`DELETE ... NOT IN (SELECT MIN(id) ...)`）
- 证据：docstring 写「保留 id 最小的一条」（:342），这句话在自增整数主键下等于「最早注册的」；
  `core/db/models_account.py:66-68` 记录 id 已改 UUID、旧整数存 `legacy_id`。UUID 字典序与创建
  时间无关，现在保留的是**随机一条**。
- 影响：去重时可能丢掉「最早、字段最全」的行（与 D-28 的 `legacy_id` 丢失叠加）；
  该函数每次启动都跑，等于每次启动都可能做一次随机的取舍。
- 建议：排序键改成 `COALESCE(legacy_id, 9223372036854775807), created_at, id`；
  并把这套逻辑下沉进一次性 revision（见 D-12/D-15），不要每次启动重跑。

### R-09 [P2] 索引配置与真实查询不匹配：既有冗余也有缺口

- 位置/证据（真库 8 个 accounts 索引 + `EXPLAIN`）：
  - `ix_accounts_email`（单列）：没有任何查询只按 email 过滤（`find_by_email` 带 platform，
    `email_contains` 是 `LIKE '%x%'` 用不上索引）→ 纯维护成本。
  - `ix_accounts_platform`：被 `ix_accounts_platform_created_at` 前缀完全覆盖 → 冗余。
  - `ix_accounts_updated_at`：全仓无 `ORDER BY/filter updated_at` 的查询 → 纯维护成本。
  - 缺口：真正热的 `WHERE platform=? AND email=?` 没有可用索引（R-04）。
- 影响：1057 行的量级下只是写放大；账号数上到万级后每次 upsert 要多维护 3 个索引页。
- 建议：R-04 改完后删 `ix_accounts_email` / `ix_accounts_platform` / `ix_accounts_updated_at`，
  用一条 alembic revision 收敛（注意模型侧 `index=True` 也要同步删，否则 `create_all`
  又会建回来 —— 这正是 D-14/D-16 那类漂移的来源）。

### R-10 [P2] 代理计数每次成功/失败都开新会话提交；`_find_by_url` 会退化成全表扫

- 位置：`core/proxy_pool.py:77-104`（`report_success` / `report_fail`：各开 `Session` + `commit`）；
  `:39-42`（URL 未规范化时 `select(ProxyModel)` 全表扫）；`:44-76`（`get_next` 全量加载 + Python 排序）
- 证据：注册链路每个账号至少触发两次计数写；`_find_by_url` 先按 `url` 精确查，规范化后不同则
  逐行比对。当前只有 5 条代理，成本可忽略，但这是**每账号必经**的写放大点（与 R-02 叠加）。
- 建议：计数走内存累加 + 定时 flush（或 `UPDATE ... SET success_count = success_count + 1`
  单语句，避免读-改-写）；`_find_by_url` 先 `normalize_proxy_url()` 再精确匹配唯一列。

### R-11 [P2] 导入前的「安全网」是裸文件拷贝（导出却是 `VACUUM INTO`）

- 位置：`services/data_bundle.py:491`（`shutil.copy2(source, destination)`）；
  对照 `:139-154`（导出用 `VACUUM INTO`，注释里明确写了「直接 copy 会撕裂」）
- 证据：同一份代码两种口径：导出安全、导入前备份不安全。导入前虽有「运行中任务守卫」，
  但后台维护任务（`services/chatgpt_maintenance` 等）仍可能在写。
- 影响：导入失败需要回滚时，回滚源本身可能是一个撕裂/半写的库 —— 安全网在关键时刻失效。
- 建议：`_backup_current_data` 也走 `VACUUM INTO`（只读连接 + 目标路径不能已存在），
  失败再退回 copy 并打 warning。

### R-12 [P2] `config_store.get()` 每次调用都开一个 Session，无进程内缓存

- 位置：`core/config_store.py:294-303`（SQLite 分支：`Session(current_engine())` + `s.get`），
  `_backend_name()` 每次重算
- 证据：全仓 67 处配置读取；configs 只有 143 行、全表 < 100 KB。任务链路里按账号/按动作反复读。
- 影响：每账号 N 次额外 round-trip + 连接获取，且都是对同一个热点表的重复读。
- 建议：启动时 `get_all()` 进内存 + `set/delete` 时失效（`set_many` 已是批写，失效点集中）。

### R-13 [P2] 20% 的库文件是空闲页，永不回收

- 位置：运维面（无代码位置）；相关：`services/log_cleanup.py` 只删行不回收
- 证据：`PRAGMA page_count=8229`、`freelist_count=1660`（≈ 6.8 MB / 33.7 MB，20%）、
  `auto_vacuum=0`；全仓无 `VACUUM` / `incremental_vacuum` 调用。
- 影响：删日志/删账号后文件不缩小，备份包与 `VACUUM INTO` 导出都带着这份空洞（导出会紧凑化，
  但日常备份/拷贝不会）。
- 建议：`auto_vacuum=INCREMENTAL` 需要一次全量 `VACUUM` 生效；在那之前提供一个显式的
  「压缩数据库」运维入口（`VACUUM INTO` 新文件 + 原子替换），并在日志清理后提示。

### §6 修复顺序

1. **R-01**（一个提交、改动小、影响最大）：黑名单改白名单 + 补 `sso` + 注册表驱动测试。
2. **R-02 / R-03 / R-06**：引擎 pragma（WAL、busy_timeout、foreign_keys）+ 日志写入节流。
   三条互相放大，一起改收益最大。
3. **R-04 / R-05 / R-09**：判重改 `lower(email)`、platform 归一（含一次数据修正）、索引收敛。
4. **R-07**：`page_size` 上界 + `stats()` 下推到 SQL。
5. **R-08 / R-10 / R-11 / R-12 / R-13**：随 D-12/D-15 的迁移收敛一并处理，其余按运维节奏。

