# 重构度量

四个结构性指标 + 一个行为等价性检查。**行数不是指标** —— 死代码只有 92 行，
减行数没有意义；减路径数才有。

## 基线（阶段 0 实测，2026-11）

```
生产代码      52,454 行 / 207 文件
测试          55,807 行 / 211 文件 / 2,969 test
HTTP 端点     102
事故注释      293
兼容 shim     47
真死代码      92 行（0.18%）
```

> **基线口径**：以下数字于 2026-11 在 base commit `5ba4bac` 的独立 worktree 上用
> 本节命令实测。此前的 12 / 480 是另一套路径集（少了 `modules/`、只匹配
> `from api import`），已作废。M2 拆成两个口径：**声明数**（阶段 2 的目标）与
> **散落数**（真实耦合面）。

| # | 指标 | 基线 | 阶段 1 后 | 阶段 2 后 | 阶段 3 后 |
|---|---|---:|---:|---:|---:|
| M1a | `from api import` 违规（服务层反向依赖 API） | 14 | **0** ✅ | 0 | 0 |
| M1b | `from api.<mod> import <私有 helper>`（真分层倒置） | 5 | **0** | 0 | 0 |
| M2a | 动作声明数（plugin `"id": "`） | 17 | 17 | **7** | 7 |
| M2b | 动作 id 字符串散落 | 97 | 97 | ≤ 40 | ≤ 20 |
| M2c | 已归类动作 / 能力数 | — | **17 / 6** | 17 / 6 | 17 / 6 |
| M3 | 鸭子类型 shim（账号/代理空壳类） | 6 | **2** | 2 | 0 |
| M4 | 账号表示模型 | 2 | **1** | 1 | 1 |
| M5 | `except Exception` | 471 | ≤ 450 | ≤ 420 | ≤ 380 |
| M6 | 其中静默 `pass` | 80 | ≤ 65 | ≤ 45 | ≤ 25 |

M1–M4 是**结构性**的，必须达标；M5/M6 是渐进的，不达标不阻塞阶段推进。

## 命令

```bash
# M1 分层倒置
grep -rn "from api import\|import api" --include=*.py services/ platforms/ core/ | wc -l

# M2a 动作声明数（阶段 2 的目标：17 → 7 个能力）
grep -rn '"id": "' platforms/chatgpt/plugin.py platforms/grok/plugin.py | wc -l

# M2b 动作 id 字符串散落（真实耦合面）
grep -rn '"probe_local_status"\|"refresh_token"\|"upload_cpa"\|"sync_cliproxyapi_status"' \
  --include=*.py api platforms services | wc -l

# M3 鸭子类型 shim
grep -rn "class _[A-Z][A-Za-z]*:" --include=*.py api core services platforms | wc -l

# M4 账号表示（`class Account` + `class AccountModel`）
grep -rn "^class Account\b\|^class AccountModel\b" --include=*.py core | wc -l

# M5/M6 宽泛异常
grep -rn "except Exception" --include=*.py api core services platforms | wc -l
grep -rn -A1 "except Exception" --include=*.py api core services platforms | grep -c "pass"
```

## 行为等价性（比测试更强）

```bash
# 阶段 0 存基线
python scripts/dump_accounts.py --platform chatgpt > /tmp/accounts-before.json

# 每阶段末对比
python scripts/dump_accounts.py --platform chatgpt > /tmp/accounts-after.json
diff <(jq -S 'map(del(.updated_at))' /tmp/accounts-before.json) \
     <(jq -S 'map(del(.updated_at))' /tmp/accounts-after.json)
```

**空 diff = 数据层行为等价。** 阶段 1 结束时这个 diff **必须为空**（阶段 1 不改平台逻辑）。

阶段 2 起允许出现已知差异，但必须逐条解释并记录在 `docs/refactor/DIFF-LOG.md`：

| 允许的差异 | 原因 |
|---|---|
| `extra` 键从 `chatgpt_registration_mode: "refresh_token"` 变为 `"refresh_token_login"` | TokenPlan 归一 |
| `client_id` 从缺省变为实际签发者 | 修复 663/663 为空的历史 bug |
| 新增 `observations.*` 键 | 观测/意图分离 |

不允许的差异：`status` 值变化、`credentials` 任一字段丢失、`register_proxy` 变化。

## 每个阶段的"完成"判定

```
M1–M4 达标
AND 契约测试全绿（tests_A.txt 里全部文件）
AND dump diff 为空 或 差异已登记
```

三条缺一不可。**只有测试全绿不算完成** —— 测试只覆盖你想到的路径。

## 口径修正记录（阶段 1 实测）

**M1 拆成两个数**：原口径 `grep "from api import\|import api"` 数出来的 14 行**全部**是
`from api import tasks as _api` —— 那是刻意的延迟绑定门面（测试 patch `api.tasks` 必须被
服务层看见），不是分层倒置。真正的倒置是 `from api.actions import _apply_action_result`
这类**私有 helper 跨层引用**，共 5 处，原口径一条都没数到（因为写的是 `from api.actions`
而不是 `from api import`）。

T3.2a 把那 5 处搬到了 `services/action_execution.py`，M1b = 0。
M1a 要等 T3.1 把任务注册表从 `api/tasks.py` 挪进 `TaskService` 才能动 —— 那是阶段 3 的活。

**M3 的原口径不可复现**：`grep "class _[A-Z][A-Za-z]*:"` 会匹配到 docstring 里的示例
（我自己写的注释就污染过一次），而且把 `_Job` / `_Entry` / `_Connection` 这类正常私有类
也算成 shim。改为只数「账号/代理鸭子类型空壳」：基线 6（`_A`×2、`_SyncAccount`、
`_UploadAccount`、`_JsonProxy`、`_LegacyProxyParts`），现在 2（剩两个代理相关的）。

## T2.1 记录：能力层已建立，dispatch 尚未切换

`core/capabilities.py` 定义了 6 个能力（probe / refresh / backfill / bind_2fa /
upload / sync_status），17 个动作 id 全部登记，`tests/test_capability_registry.py`
用**平台真实声明的动作列表**反向校验 —— 新加动作不登记会当场红。

已经切过去的：

* `GET /actions/{platform}` 每条动作多返回一个 `capability` 字段（前端可按能力分组，
  不用再硬编码 17 个 id）；
* `_apply_action_result` 里 `action_id in ("backfill_refresh_token", "bind_2fa")`
  → `capability_of(action_id) in (BACKFILL, BIND_2FA)`（1:1 映射，行为等价）。

**还没切的**（T2.2）：`plugin.execute_action` 里 20 处 `action_id == "..."` 与
`_apply_action_result` 里 3 处。不能机械替换 —— 同一个能力下的动作**输入不同**：
`probe_local_status` 读 `data["probe"]`，`check_plus_trial` 不产出 probe；
`sync_cliproxyapi_status` 读 `data["sync"]`，另两个同步动作的 data 里没有 `sync`。
按能力合并会让后者的 `apply_*_policy` 收到 `None`，是否等价要逐个验证，不能靠推断。

## T1.6 记录：配置键从三处定义收敛到一处

`core/config_schema.py` 现在是 69 个配置键的唯一身份来源（键名 + 分组 + 是否口令）。
`api/config.py` 的 `CONFIG_KEYS` / `SECRET_CONFIG_KEYS` 由它派生，文件从 263 行降到 165 行。

派生关系不是靠人守的，`tests/test_config_schema.py` 钉了四件事：

1. `api.CONFIG_KEYS` 与 schema **顺序**完全一致（顺序即界面顺序）；
2. `api.SECRET_CONFIG_KEYS` 与 schema 的 secret 集合相等（10 个）；
3. 代码里所有 `config_store.get("字面量")` 的键都必须登记过 —— 拼错键名会静默返回
   默认值，这类 bug 以前永远不报错；
4. `GET /api/config/schema` 的响应只含 `keys` / `secrets` / `groups`，**不含任何值**。

新增 `GET /api/config/schema` 给前端渲染表单用，第三份键名列表可以退休了。

## T2.2 记录：状态策略按能力分派

`_apply_action_result` 的 ChatGPT 分支从「逐个比 4 个动作 id」变成「按 3 个能力分派」，
`check_plus_trial` / `sync_sub2api_status` / `sync_chatgpt2api_status` 不再需要枚举 ——
它们的结果里没有 `probe` / `sync` 键，策略函数收到 `None` 返回空串，行为与逐 id 完全一致。

这条等价不是推理出来的：`tests/test_capability_dispatch.py` 先钉住现状（6 条，含
「禁用不被无信号的同步洗掉」），再改分派，改完 6 条仍绿。

同时把「动作 id → 面板 key」的映射从三处（api 的 dict、ChatGPT 插件三元式、Grok 插件
写死字符串）收敛到 `services/panel_status_sync.ACTION_PANEL_KEYS` 一处。

## T3.1 记录：任务运行时下沉，M1a 归零

`api/tasks.py` 902 → 814 行。搬进 `services/task_service.py`（201 行）的是：

| 符号 | 原位置 | 服务层调用次数 |
|---|---|---:|
| `_log` → `log` | `api/tasks.py:297` | 55 |
| `_task_store` → `task_store` | `api/tasks.py:25` | 24 |
| `_save_task_log` → `save_task_log` | `api/tasks.py:314` | 14 |
| `_persist_task_snapshot` → `persist_task_snapshot` | 本来就在 `services/task_store_io.py` | 13 |
| `normalize_register_retry_times` | `api/tasks.py:42` | 2 |
| `_auto_upload_integrations` → `auto_upload_integrations` | `api/tasks.py:375` | 2 |
| `_account_already_registered` → `account_already_registered` | `api/tasks.py:359` | 2 |
| `MAX_DEAD_END_ROUNDS` | `api/tasks.py:39` | 2 |

**循环 import 的解法**：`task_service` 在 `log()` 里延迟 import
`task_store_io.persist_task_snapshot`；`task_store_io` 反过来在 `_store()` 里取
`task_service.task_store`。两个方向都是函数内 import，模块加载期不互相等待。

**patch 目标随之移动**：`test_register_task_controls.py` 与
`test_environment_preflight.py` 里 9 处 `patch("api.tasks._save_task_log")` 改为
`patch("services.task_service.save_task_log")` —— 函数搬了，patch 就得打在它现在
所在的地方，否则 patch 的是 `api/tasks.py` 里那个不再被调用的别名（假绿）。
`test_task_module_layout.py` 保持原样：它断言的是「`api.tasks` 上还能取到这个名字」，
属于结构性契约，仍然成立。

**还剩一处真违规**（不在这条指标口径里）：`services/chatgpt_maintenance.py:791`
`from api.integrations import push_panel_endpoint`。那个函数有 ~100 行推送逻辑住在
API 层，归 T3.3（`PanelAdapter`）一起搬。

## T3.3 记录：推送编排下沉，最后一处分层倒置清掉

`api/integrations.py` **481 → 237 行**，`services/panel_push.py` 333 → 630 行。

搬下去的是整套推送编排：`_PANEL_PUSH_KIND`（覆盖式 vs 新建式）、
`_push_account_uploader`（按「面板 × 平台」挑上传器）、`_panel_remote_deleter`、
`_persist_push_results`（推送结果写回 `sync_statuses.<面板>` 并自动登记）、
`_lookup_account_row` / `_password_for_row`，以及 `push_panel_endpoint` 本体
（改名 `push_local_rows`）。

**HTTP 语义留在 API 层**：未知面板在服务层抛 `ValueError`，端点翻译成 404。
服务层不再 import `HTTPException` —— 之前为了这一行反向依赖 FastAPI。

`services/chatgpt_maintenance.py` 的自动维护现在直接
`from services.panel_push import push_local_rows`，不再绕 api。

### 剩下的 `from api.*`

| 位置 | 性质 |
|---|---|
| `services/task_runners.py:32` | `if TYPE_CHECKING:` 里的请求模型标注，**运行期不求值**（`from __future__ import annotations`） |
| 两处注释 | 记录历史，不是代码 |

即：**运行期的跨层 import 为 0**。

## T1.4 记录：凭据字段级加密（默认开）

`core/credential_crypto.py`（144 行）+ 两个卡点接线。加密的是 `extra_json` 里六把
**可直接冒用身份**的 bearer 凭证：

    access_token / refresh_token / id_token / session_token / sso / totp_secret

其余键（`client_id`、`cookies`、同步状态、面板登记）保持明文 —— 它们要么不是秘密，
要么需要被 SQL 查询（加密后 `WHERE extra_json LIKE '%x%'` 就失效）。

### 三个卡点（缺一个就漏）

| 位置 | 作用 |
|---|---|
| `AccountModel.set_extra()` | 写路径正门：落盘前加密 |
| `AccountModel.__init__()` | **旁路**：导入/测试/面板回填直接 `AccountModel(extra_json=...)`，绕过正门。不补这道，明文照旧落盘 |
| `AccountModel.get_extra()` | 读路径正门：返回前解密（老明文原样通过） |

另外三处直接解析 `extra_json` 的地方也都改成走解密视图：
`repository.merge_account_fields`（比较用 `get_extra()`，写回仍保留原始密文串）、
`migrations` 的 grok token 修补（`except CredentialKeyError`，不吞其他异常）、
`api/accounts._slim_extra_for_list`（不解密就解不出 AT 的 iat/exp，列表「AT 有效期」整列空掉）。

### 一条测试被**改写**而不是修绿

`test_chatgpt_two_factor_persistence::test_secret_is_written_to_the_database`
原先断言 `json.loads(row.extra_json)["totp_secret"] == SECRET` —— **它把泄漏当成了契约**。
现在断言三件事：落盘不是明文、有 `v1:` 信封、`get_extra()` 读出来是明文。

### T1.4b：`password` 列也加密了（列名不变，免迁移）

做法：字段改名 `password_cipher`，但 `sa_column=Column("password", …)` ——
**数据库列名保持 `password`**，所以不需要 Alembic revision、不需要回填。
`password` 变成一个透明加解密的 property，67 处 `account.password` 一行没改。

三个必须同时处理的旁路：

| 旁路 | 症状 | 处理 |
|---|---|---|
| `AccountModel(password=…)` 关键字 | `password` 不是 pydantic 字段，构造直接报错 | `__init__` 里先 `pop("password")`，构造完经 setter 写入 |
| `json_record_to_model` 的字段过滤 | 按 `model_fields` 过滤会把 `password` 丢掉 → JSON 后端读出来密码全空 | 过滤白名单补上 `{"password"}` |
| `_json_save` 写 payload | 用 `getattr(model, "password")` 会写**明文**进 JSON 文件 | 改读 `password_cipher` |
| `_slim_account_row` 的 `has_password` | `jsonable_encoder` 现在吐 `password_cipher`，标记消失 | 两个键名都收 |

实测确认：`upsert` 后库里 `select password` 是 `v1:…`，`acc.password` 读回明文，
明文串不出现在列值里。

### 开关

`CREDENTIAL_ENCRYPTION=0` 关掉（默认开）。关掉后两个函数退化成恒等映射，
用于排障：「解不开」时先确认是密钥文件丢了还是数据真坏了。
密钥文件位置见 `core/secret_box.py`（`CREDENTIAL_ENCRYPTION_KEY` /
`CREDENTIAL_ENCRYPTION_KEY_FILE`）。**密钥丢了 = 所有 RT 不可恢复**，
备份策略必须把密钥文件与数据库一起备（`data_bundle` 已经这么做了）。

## M6 清零 + T1.2c 记录

### M6：静默 `pass` 71 → 3

70 处 `except Exception: pass` 改成 `logger.debug("忽略异常（保留原行为）", exc_info=True)`。
**只加日志，不动控制流** —— 这是唯一能批量做且零行为变化的改法。剩下 3 处在
`platforms/chatgpt/protocol/auth_flow.py`，按 C6 阶段 1–3 不碰该文件，留到阶段 4。

没有追 M5（`except Exception` 447 → ≤380）：把宽捕获收窄成具体类型，**没有证据就是
在盲改行为** —— 原来被吞掉的异常会开始往上冒。M5 只能逐条读上下文、确认「这里只可能
抛哪几种」再改，属于长尾人工活，且 PLAN 自己写了「M5/M6 不达标不阻塞阶段推进」。

### T1.2c：代理只剩一种表示

`_JsonProxy`（4 个字段）删除，两条后端统一返回 `ProxyRecord`（7 个字段，与 `proxies`
表一一对应）。修的是**静默功能缺失**：JSON 部署下 `success_count` / `fail_count` /
`last_checked` 被丢掉，代理池的「成功率」永远显示 0，且没有任何报错。

`api/proxies.py` 的 `p.is_active = not p.is_active; save(p)` 语义不变 —— `save()` 在
SQLite 分支按 id 取 ORM 行再改（不再假设传进来的是 ORM 实例）。

至此 M3（账号/代理鸭子类型空壳）= **0**。

## T1.5 记录：2FA 写前日志的密钥加密

`services/chatgpt_two_factor.py` 的写前日志（`SECRETS_DIR/totp_journal/<digest>.json`）
原先把 `totp_secret` **明文**落盘，只有目录 0700 / 文件 0600 兜着。备份、同步盘、
`data_bundle` 导出都会把它带走。

现在写 `totp_secret_cipher`（`v1:` 信封）。**键名换掉而不是复用**：老版本的读侧会把
`totp_secret` 直接当明文写进库，复用键名等于让旧进程把密文当密钥写进去（2FA 直接废掉）。

读侧 `_read_journal_secret()` 两种格式都认：新格式解密，老格式（明文）原样返回 ——
升级前写下的条目还在盘上。

**解密失败不吞**：异常冒到逐条的 `except`，记一行「恢复失败」并**保留文件**。
这条路径的文件是密钥的**唯一副本**（服务端不下发第二次），删掉等于把号永久锁死。
`tests/test_totp_journal_crypto.py` 三条测试分别钉住「文件里没有明文」「老明文能恢复」
「解不开保留文件」。
