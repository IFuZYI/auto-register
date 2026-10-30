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
| M1a | `from api import tasks` 延迟门面（T3.1 才能拆） | 14 | 14 | 14 | **0** |
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
