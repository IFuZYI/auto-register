# 重构总提示词（START HERE）

> 用途：新开 agent 会话，把 `---` 之间整段贴进去。这是**全量 4 阶段**的总提示词，
> 取代 `KICKOFF.md`（那份只覆盖阶段 0，可删）。
> 详细步骤仍在 `PROMPT-agent.md`，本文件负责「目标 + 边界 + 任务索引 + 技能挂载」。

---

## GOAL

```yaml
goal:
  name: 账号管理台结构重构（路径收敛）
  objective: >
    在不改变外部行为的前提下，把 52,454 行生产代码里「同一语义的 N 条实现路径」
    收敛为 1 条：每个能力只有一条路径，每个语义只有一个定义处。
    行数不是目标 —— 真死代码只有 92 行，减行数没有意义。

  success_criteria:
    M1: "from api import 违规 12 → 0"
    M2: "字符串动作 id 散落 17 → ≤ 8"
    M3: "鸭子类型 shim 13 → 3（只留三个真适配器）"
    M4: "账号表示模型 2 → 1"
    tests: "契约测试（tests_A 清单）全绿"
    equivalence: "字段级 dump diff 为空；阶段 2 起允许差异但必须逐条登记"

  non_goals:
    - 不重写（293 条事故注释是唯一规格）
    - 不删 Grok / 4 面板 / JSON 后端
    - 不引入 async / 队列 / 微服务
    - 不重构 auth_flow.py 的内部署辑
    - accounts 以外的表不换 UUID 主键

  budget: "8–11 周单人"
  rollback: "每阶段一个 tag + data/ 快照；每任务一提交"
```

---

你是这个仓库的重构执行者。仓库 `/data/workspace/Register/.worktrees/refactor`，
从当前分支切 `refactor/v2` 开工。

## 一、开工前必读（按顺序，读完再动手）

| # | 文件 | 它回答什么 |
|---|---|---|
| 1 | `docs/refactor/PLAN.md` | 4 阶段、任务表、门禁、回滚点、已定决策 |
| 2 | `docs/refactor/PROMPT-agent.md` | 每个任务的详细步骤与验收命令（主作业单） |
| 3 | `docs/refactor/FIELD-NOTES.md` | 293 条事故注释的 12 个主题，改哪块先读哪篇 |
| 4 | `docs/refactor/METRICS.md` | 6 个指标与基线，每阶段末对比 |
| 5 | `docs/refactor/GLOSSARY.md` | 能力/路径/观测/意图/派生/事故保险的精确含义 |
| 6 | `docs/adr/0003`–`0008` | 6 个已定决策，不要重新讨论 |

## 二、八条硬约束

| # | 约束 | 检查方式 |
|---|---|---|
| C1 | **不重写**。不新建平行实现，只改造现有实现 | 新增文件必须能在 PLAN 任务表找到出处 |
| C2 | **不删事故保险**。带 `实测`/`踩过` 注释的分支保留语义，只许改写法 | `git diff` 不得删除这类注释的 hunk |
| C3 | **先测试后代码**（RED → GREEN → REFACTOR） | 每任务 ≥ 1 个新 test |
| C4 | **每任务一提交**：`refactor(phaseN): T<id> <一句话>` | `git log` 可追溯 |
| C5 | **门禁不过不进下一阶段** | 门禁输出贴进报告 |
| C6 | 阶段 1–3 不碰 `auth_flow.py` 内部署辑 | 该文件只允许签名/常量级 diff |
| C7 | **顺序不可反**：`去重 → 唯一索引 → UUID → 分库合并 → 加密` | 加密后无法 SQL 去重/建唯一索引 |
| C8 | 不确定就停下写 `docs/refactor/BLOCKERS.md` | 不许猜 |

## 三、环境事实（已核实，不要重查）

```
生产代码      52,454 行 / 207 文件        测试  55,807 行 / 211 文件 / 2,969 test
真死代码      92 行（0.18%）              事故注释 293 条
兼容 shim     47 处                       鸭子类型 shim 13 个
HTTP 端点     102

基线指标：M1 12 / M2 17 / M3 13 / M4 2 / except 480（pass 80）
```

- **本 worktree 无 `data/`**：T0.4 快照跳过，报告标注「需在生产侧执行」，不要伪造数据。
- **无 `reference/`**，注释引用的 `/home/chatgpt2api` 不在本机。判断只依据现有代码 + 注释实测数据。

## 四、已定决策（不要重新讨论）

| # | 决策 |
|---|---|
| Q2 | 只做 A 类减法（合并等价路径）+ 删手机号注册；Grok / 4 面板 / JSON 后端保留 |
| Q3 | 全量凭据加密：password / AT / RT / ST / IDT / SSO / `totp_secret` / 配置口令 / 2FA journal |
| Q4 | 允许破坏兼容：一次性迁移 + 快照 + 旧键只读一版 |
| H1′ | 不做硬隔离：Codex 兜底保持「session 与 OAuth 都失败后才跑」；严格模式做成开关且默认关 |
| H2 | 3–4 个高频字段提成独立列，其余内存过滤 |
| H3 | 分库合并冲突保留 `updated_at` 最新 |
| H4 | `accounts.id` → UUID；其余表保留 int（`ORDER BY id` 是承重语义） |

## 五、技能挂载表（按任务加载，不要全量加载）

| 阶段 / 任务 | 挂载 skill | 为什么 |
|---|---|---|
| **全程** | `test-driven-development` | C3 要求 RED→GREEN→REFACTOR |
| **全程** | `requesting-code-review` | 每阶段末自审：安全扫描 + 质量门 |
| **全程** | `git-repo-maintenance` | worktree / 分支 / 提交纪律 |
| T0.1 注释抽取 | `repo-docs-maintenance` | 文档与代码同步，避免再次漂移 |
| T0.2 测试三分类 | `codebase-audit` | verify-before-fix：先分清哪些测试在守护现状 |
| T0.5 去重 + 唯一索引 | `race-condition-fixes` | 并发 upsert 的原子性 |
| T0.6 删手机注册 | `feature-removal` | 删功能/配置面/死代码的标准流程 |
| T1.0 UUID 主键 | `large-file-surgical-edits` + `zero-change-refactor` | 91 处签名跨文件改造，行为必须零变化 |
| T1.1 `AccountRecord` | `duplicated-table-consolidation` | 凭证字段表在多处重复，收敛成一处 |
| T1.2 删 13 个 shim | `zero-change-refactor` | 纯结构替换 |
| T1.3 分库塌缩 | `zero-change-refactor` + `race-condition-fixes` | 合并脚本要幂等 |
| T1.4 全量加密 | `one-shot-credential-persistence` | TOTP 只下发一次，journal 必须先落地 |
| T1.6 `config_schema` | `duplicated-table-consolidation` | 69 个键三处定义 → 一处 |
| T2.x 能力层 | `zero-change-refactor` | 17 个 action id 降级为适配器 |
| T2.4 状态表 | `systematic-debugging` | 语义漂移时先复现再改 |
| T2.6 `TokenPlan` | `modular-feature-switches` | 旧值兼容 + 严格模式开关 |
| T3.1 `TaskService` | `zero-change-refactor` | 状态机搬迁不改行为 |
| T3.2 去 api 依赖 | `large-file-surgical-edits` | 12 处 `from api import` 逐一替换 |
| T3.3 `PanelAdapter` | `duplicated-table-consolidation` | 4 处注册 → 1 处 |
| 任务重试相关 | `task-failure-tolerance` | `register_retry_times` / `MAX_DEAD_END_ROUNDS` |
| OTP 相关改动 | `otp-code-pipeline` | `issued_after` / `exclude_codes` 三重过滤 |
| 定时刷新窗口 | `timezone-safe-scheduling` | `plan_auto_refresh` 的 24h/1h 窗口 |
| 前端删手机选项 | `user-facing-copy` | 文案与校验提示 |

## 六、全量任务清单

### 阶段 0 — 知识固化与保命（1 周 + 2 天）

| 任务 | 一句话 | 验收 |
|---|---|---|
| T0.1 | 注释抽取 → `docs/field-notes/` 12 篇 | `_index.json` ≥ 280 条；`git diff` 无 `.py` |
| T0.2 | 测试三分类 A/B/C | 三份清单存在；B 类进 BLOCKERS |
| T0.3 | Alembic 基线；`migrations.py` 改 no-op | 空库 + 现有库 `upgrade head` 都通 |
| T0.4 | `data/` 快照 + 账号导出 | 5 分钟内可还原（**本机无 data/，标注生产侧执行**） |
| T0.5 | 去重 + `uq_accounts_platform_email` | 并发 20 线程同邮箱只 1 行 |
| T0.6 | 删手机号注册（−2,100 行） | 全量测试绿；`grep run_phone_register` = 0；存量账号一行不少 |

**门禁 G0** → tag `v0-refactor-baseline`

### 阶段 1 — 数据层（3–4 周）

| 任务 | 一句话 | 验收 |
|---|---|---|
| T1.0 | `accounts.id` → UUID（91 处签名 + 13 路由 + 前端 5 处） | dump diff 为空；`legacy_id` 可回滚 |
| T1.1 | `AccountRecord` + `Credentials`；旧模型降级为适配器 | 三向往返等价测试 |
| T1.2 | 删 13 个鸭子类型 shim | `grep -c "class _[A-Z]"` = 3 |
| T1.3 | 分库塌缩（按 `uid` 去重合并） | 行数守恒；`platform_session()` 签名保留 |
| T1.4 | 全量加密（含 `totp_secret`、配置口令） | 库文件 grep 不到 `eyJ` / `sk-` |
| T1.5 | 2FA journal 加密 | journal grep 不到明文 secret |
| T1.6 | `config_schema.py`（69 键一处定义） | 新增键只改一处 |

**门禁 G1** → tag `v0-refactor-data-layer`

### 阶段 2 — 能力层（2–3 周）

| 任务 | 一句话 | 验收 |
|---|---|---|
| T2.1 | `Capability` / `CapabilitySpec` / `Outcome` | 两平台同一套参数化断言 |
| T2.2 | `execute_action` 降级为适配器 | 17 个旧 id 全部可调 |
| T2.3 | `_apply_action_result` 9 分支 → 1 行 | 该函数 ≤ 15 行 |
| T2.4 | `PROBE_STATE_TO_STATUS` 表 | 12 探测态 × 期望状态表驱动 |
| T2.5 | `ErrorKind` + `Policy` | 新增错误类型只改一张表 |
| T2.6 | `TokenPlan` 枚举（5 函数 → 1） | 旧值全部仍可解析 |
| T2.7 | `AccountProxyBinder`（3 处 → 1 处） | 三处行为一致；注册路径不判「可绑定」 |

**门禁 G2** → tag `v0-refactor-capability`

### 阶段 3 — 编排层（1–2 周）

| 任务 | 一句话 | 验收 |
|---|---|---|
| T3.1 | `TaskService` 下沉 | `api/tasks.py` 每端点 ≤ 5 行 |
| T3.2 | 去 `api` 依赖（12 处 → 注入 `TaskContext`） | `grep -r "from api import" services/` = 0 |
| T3.3 | `PanelAdapter`（4 处注册 → 1 处） | 新增面板只改 1 个文件 |
| T3.4 | 改写 12 个 B 类测试 | B 类清单清空 |

**门禁 G3** → tag `v1.0-refactored`；4 个指标全部达标

## 七、工作循环

```
读对应 field-note
  → 写失败测试（优先表驱动：输入→期望 写成一张表）
  → 改实现直到通过
  → 跑该任务相关的**全部**测试（不只新测试）
  → 跑门禁命令
  → 提交
  → 按 §八 报告
```

每阶段末额外跑：

```bash
pytest -q tests/ -x --timeout=300
python -m compileall -q core services platforms api
echo "M1 $(grep -rn 'from api import' --include=*.py services/ | wc -l)"
echo "M2 $(grep -rn '"probe_local_status"\|"refresh_token"\|"upload_cpa"' --include=*.py api platforms services | wc -l)"
echo "M3 $(grep -rn 'class _[A-Z]' --include=*.py api core services platforms | wc -l)"
python scripts/dump_accounts.py --platform chatgpt > /tmp/after.json
diff <(jq -S 'map(del(.updated_at))' /tmp/accounts-before.json) \
     <(jq -S 'map(del(.updated_at))' /tmp/after.json)
```

**diff 非空即停止**，先解释差异再继续。

## 八、报告格式（每任务一次）

```
T<id> 完成
- 改动：<文件列表>（+N / −N）
- 验收：<命令> → <输出摘要>
- 指标：M1 12→12 / M2 17→17 / M3 13→13 / M4 2→2 / M5 480→478 / M6 80→80
- 下一步：T<next>
- 阻塞：<无 / 具体问题>
```

## 九、停止条件（其余自己拍板）

- 代码事实与 PLAN / ADR 冲突 → 写 BLOCKERS，停
- 需要改 PLAN 里标为「留」「非目标」的东西 → 停
- 测试失败 30 分钟内无法定位 → 停
- 需要动 `data/`、生产库、外部服务 → 停
- 需要删 `services/sms/*` / `mixins/add_phone.py` / `test_chatgpt_add_phone_sms_loop.py` → 停

## 十、禁止清单

- 不重构 `auth_flow.py` 内部分支
- 不给 `accounts` 以外的表换 UUID
- 不做「顺手优化」的无关改动；不改注释措辞
- 不用 `git rebase` / `force push` / 改写已推送历史
- 不为了让测试通过而放宽断言（改断言 = 改规格，必须停下来问）

## 十一、中断恢复

1. 读 `docs/refactor/BLOCKERS.md`
2. `git log --oneline -20` 找最后一个 `refactor(phaseN): T<id>` 提交
3. 读该任务在 PLAN 的验收条件
4. 跑验收命令 —— 通过进下一个，失败重做该任务
5. **不要**根据「代码看起来做了一半」推测进度；只认验收命令

---

现在从 T0.1 开始。先读 6 份文档，然后直接动手，不要先问「要不要开始」。
每完成一个任务按 §八 报告一次；阶段门禁通过后停下来等我确认再进下一阶段。
