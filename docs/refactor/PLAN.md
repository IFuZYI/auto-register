# 完全性重构计划

4 阶段、8–11 周单人（UUID 主键迁移 +1 周）。目标是**路径收敛**，不是行数减少。

## 完成定义

三件套同时满足才算完成：

1. **四个结构性指标达标**（见 [METRICS.md](./METRICS.md)）
2. **契约测试全绿**：`tests/test_*contract*.py` + `tests/test_*semantics*.py` + `tests/test_*flow*.py`
3. **字段级 dump diff 为空**：同一批账号重构前后导出的 JSON 逐字段相等（排除 `updated_at`）

## 非目标

| 不做 | 理由 |
|---|---|
| 重写 | 293 条事故注释是唯一规格；无参考实现可对照（见 ADR-0003） |
| 删 Grok / 4 面板 / JSON 后端 | 全部保留（用户 2026-11 决策） |
| 删手机**绑定** | 保留：Codex RT 撞 add-phone 时唯一的出路（`mixins/add_phone.py` + `services/sms/*`） |
| 引入 async / 队列 / 微服务 | 瓶颈是上游风控，不是并发模型 |
| 改 `auth_flow.py` 的控制流 | 阶段 1–3 只碰它的边界，不碰内部分支；分支收敛留到阶段 4 之后单独立项 |
| `accounts` 以外换 UUID 主键 | `ORDER BY id` 在别名池/邮箱池/日志里是承重语义（ADR-0008） |

**唯一删掉的产品能力是「手机号注册」**（用手机号作为账号身份），见 T0.6 与 ADR-0007。

## 阶段 0 — 知识固化与保命（1 周 + 2 天）

动结构之前先让改动可回滚、知识可留存。

| 任务 | 产出 | 验收 |
|---|---|---|
| T0.1 注释抽取 | `docs/field-notes/` 10–12 篇（见 [FIELD-NOTES.md](./FIELD-NOTES.md)） | 每篇能独立读懂，含 file:line 出处 |
| T0.2 测试三分类 | A 契约 / B 实现锁定 / C 事故回归，三份清单 | B 类清单进 issue，标注"阶段 3 前置" |
| T0.3 Alembic 基线 | `alembic.ini` + `migrations/env.py` + baseline revision（`render_item` 把 SQLModel 类型渲染成 `sa.String`） | 空库 `upgrade head` 通（9 表）；现有库 `stamp head` 后 `alembic check` 无变更 |
| T0.3b（推迟） | `migrations.py` 改 no-op | 需先改写 `tests/test_icloud_remote_columns.py` 的 7 个测试（11 次调 `run_migrations(engine)`，直接断言旧补列逻辑）；与阶段 1 首个真实 revision 一并做 |
| T0.4 数据快照 | `data/` 全量 tar + 账号表 JSON 导出 | 快照可在 5 分钟内还原 |
| T0.5 去重 + 唯一索引 | 清重复行 + `uq_accounts_platform_email` | 并发 20 线程注册同邮箱只产生 1 行 |
| T0.6 **删除手机号注册** | 见下方清单，约 −2,100 行 | 全量测试绿；`grep run_phone_register` = 0；前端注册流程只剩「邮箱」 |

**门禁 G0**：T0.1 的 10 篇文档存在；`alembic upgrade head` 两条路径都通；唯一索引存在且并发测试通过；T0.6 删除后 dump diff 中**存量账号一行不少**。

**回滚 R0**：文档 + 索引 + 一次删除提交；回滚 = revert 该提交 + 删索引 + 还原快照。

### 顺序约束（硬）

```
去重 → 唯一索引 → UUID 主键 → 分库合并 → 加密
```

**不能反**：

- 加密后无法用 `lower(email)` 去重、无法建唯一索引、无法跨库比对 —— 加密必须最后。
- UUID 必须在分库合并**之前**：合并时按 `uid` 去重（同 uid = 同一账号）比按 `(platform,email)` 更可靠。

### T0.6 删除清单

**删（约 2,100 行）**

| 对象 | 行数 | 说明 |
|---|---:|---|
| `platforms/chatgpt/protocol/phone_flow.py` | 964 | 整文件：`PhoneRegisterMixin` / `run_phone_register` / `PhoneAccountCreatedError` / `_PhoneUnusable` / `_PhoneFlowBroken` |
| `tests/test_chatgpt_phone_register.py` | 1,104 | 整文件（纯手机注册用例） |
| `auth_flow.py:30, 99` | 2 | import 与基类 |
| `registration_engine.py:43-44, 128-130, 179-214` | ~40 | 常量、`run()` 分支、`_run_phone()` |
| `chatgpt_registration_mode_adapter.py:25-26, 38-39, 223-230` | ~15 | `register_flow` 归一 |
| `plugin.py:65-75` | 11 | 「注册流程」三选项 → 单值 |
| `frontend/src/lib/chatgptRegisterFlow.ts` | ~20 | phone 选项 |
| `tests/test_rt_route_reaches_protocol_flow.py` | 编辑 | 只改引用 `phone_with_email` 的断言 |
| `tests/test_exception_handlers_not_too_wide.py` | 编辑 | 扫描列表里去掉 `phone_flow.py` |

**留（一个都不能动）**

| 对象 | 理由 |
|---|---|
| `platforms/chatgpt/protocol/mixins/add_phone.py`（429 行） | Codex RT 撞 add-phone 时的绑号路径 |
| `services/sms/*`（providers 785 + controller + parsing） | 上面那条路径的接码实现 |
| `api/sms.py`（4 端点） | 接码配置与测试 |
| `registration_engine._build_sms_callback()` | 构造 `sms_callback` 注入 `AuthFlow` |
| `tests/test_chatgpt_add_phone_sms_loop.py`（216 行） | 绑号循环的回归测试 |
| `chatgpt_register_flow` 这个 extra 键 | 存量账号的**观测**值（它们是怎么建出来的），不清洗 |

**观测 / 意图的区分**（见 [GLOSSARY.md](./GLOSSARY.md)）：新注册只提供 `email` 一个选项（意图），但存量账号 `extra.chatgpt_register_flow = "phone"` 原样保留（观测）—— 它是「这个号的出生方式」，删了就再也查不出来。

## 阶段 1 — 数据层（3–4 周）

UUID 签名会穿透到 `platforms/`（不再保证零平台改动），但**行为必须完全一致** —— 由 dump diff 保证。

| 任务 | 改动 | 验收 |
|---|---|---|
| T1.0 `accounts.id` → UUID | ✅ **已完成**（`c3f8a1d64b27`）。实际影响面比原估小得多：**12 处签名 + 5 个路由 + 前端 2 个类型**，不是 91 处 —— 那 91 里 79 处是 `icloud_accounts.id`（另一张表，仍是 int）。无任何外键指向 `accounts` | 全链路跑通；`legacy_id` 可回滚；`alembic check` 无变更 |
| T1.1 `AccountRecord` + `Credentials` | ✅ **已完成**：`core/models/account.py`（268 行），`Account` 是**别名**不是子类；`AccountModel.to_record()` 是唯一 ORM→领域转换点 | `tests/test_account_record.py` 9 条往返等价全绿；M4 = 1 |
| T1.2 删鸭子类型 shim | ✅ **部分完成**：删掉 `_A`×2 / `_SyncAccount` / `_UploadAccount`（账号侧 4 个）。剩 `_JsonProxy`（代理 dict→属性）与 `_LegacyProxyParts`（frozen dataclass，其实不是 shim） | 账号侧 shim = 0；`_JsonProxy` 待代理域有 `ProxyRecord` 后再删 |
| T1.3 分库塌缩 | `PlatformDatabaseRegistry` 保留接口、恒返回默认 engine；按 `uid` 去重合并 | 合并脚本把 N 个 db 文件并成 1 个；行数守恒 |
| T1.4 全量加密 | password / AT / RT / ST / IDT / SSO / `totp_secret` / 配置口令 | 库文件 grep 不到任何明文 token；`v1:` 前缀可轮换 |
| T1.5 2FA journal 加密 | `_write_secret_journal` 改用 `secret_box` | journal 文件 grep 不到明文 secret；恢复后删除 |
| T1.6 `config_schema.py` | 69 个键一处定义，`CONFIG_KEYS` / `SECRET_CONFIG_KEYS` 派生 | 新增键只需改一处；`test_secret_config_contract` 绿 |

**门禁 G1**：
- `test_repository_backends`、`test_storage_backend`、`test_storage_coverage_gate`、`test_account_export*` 全绿
- 新增 `test_account_record_roundtrip`、`test_credentials_sealed`、`test_uuid_roundtrip`
- dump diff 为空（`id` 类型变化除外，其余字段必须逐字相等）
- 库文件二进制 grep 不到 `eyJ`（JWT 头）与 `sk-`

**回滚 R1**：`legacy_id` 列保留一版；单库文件 + 密钥文件成对回滚；`CREDENTIAL_ENCRYPTION_ENABLED=0` 可读旧明文（一个版本内）。

## 阶段 2 — 能力层（2–3 周）

17 个动作 id → 7 个能力；3 处状态判定 → 1 张表；5 个 Token 方案函数 → 1 个枚举。

| 任务 | 改动 | 验收 |
|---|---|---|
| T2.1 `Capability` / `CapabilitySpec` / `Outcome` | 新建 `core/platform_contract.py` | 两平台跑同一套参数化断言 |
| T2.2 `execute_action` 降级为适配器 | 查表 → 调新方法，签名不变 | 前端零改动；17 个旧 id 全部可调 |
| T2.3 `_apply_action_result` 9 分支 → 1 行 | 由 `Outcome.status_patch` 驱动 | `api/actions.py` 该函数 ≤ 15 行 |
| T2.4 `PROBE_STATE_TO_STATUS` 表 | 合并 `classify_local_probe_state` + `apply_chatgpt_status_policy` | 12 个探测态 × 期望状态表驱动测试 |
| T2.5 `ErrorKind` + `Policy` | 统一 chatgpt `refresh_kind` 与 grok 同名概念 | 新增错误类型只改一张表 |
| T2.6 `TokenPlan` 枚举 | 5 个函数 → 1 枚举 + 2 派生属性 | 旧值（`refresh_token` / `rt` / `1`）仍可解析 |
| T2.7 `AccountProxyBinder` | 3 处绑定实现 → 1 处 | 三处调用点行为一致（表驱动测试） |

**门禁 G2**：
- `test_panel_*` 15 个文件全绿
- `grep -c '"probe_local_status"\|"refresh_token"'` ≤ 8
- 新增 `test_provider_contract`、`test_status_transition_table`、`test_token_plan_legacy`

**回滚 R2**：`execute_action` 适配器保留旧实现为 fallback，可一键切回。

## 阶段 3 — 编排层（1–2 周）

| 任务 | 改动 | 验收 |
|---|---|---|
| T3.1 `TaskService` | 任务状态机从 `api/tasks.py` 下沉 | `api/tasks.py` 每个端点 ≤ 5 行 |
| T3.2 去 `api` 依赖 | 12 处 `from api import tasks` → 注入 `TaskContext` | `grep -r "from api import" services/` = 0 |
| T3.3 `PanelAdapter` | 4 处注册 → 1 处；`FETCHERS` 并入适配器 | 新增面板只改 1 个文件 |
| T3.4 测试改写 | 12 个 B 类测试从 patch 模块状态改为构造注入 | B 类清单清空 |

**门禁 G3**：新增 `test_task_service_without_web`（不 import fastapi 能跑通一次任务）；4 个指标全部达标。

**回滚 R3**：`TaskService` 与旧 `api/tasks.py` 并存一个版本，路由层开关切换。

## 风险表

| 风险 | 触发条件 | 缓解 |
|---|---|---|
| UUID 破坏「id 排序 = 创建顺序」 | 10+ 处 `ORDER BY id`（别名池取号 `icloud_alias_repository.py:709`、邮箱池 `outlook_repository.py:75,163`、日志 `api/tasks.py:742`） | 只换 `accounts` 主键；其余表保留 int（ADR-0008）。`accounts` 列表改按 `created_at` 排序（已有索引） |
| JSON 后端排序变化 | `json_backend.py:131` 数字主键按数值排，UUID 字符串按字典序 | 列表显式按 `created_at` 排序；测试覆盖两种后端顺序一致 |
| 存量手机号账号刷新退化 | 它们的 `email` 是手机号，登录路线必然失败 | 不引入「硬隔离 Codex」；严格模式做成开关且**默认关**（H1′） |
| 加密后列表页变慢 | `extra_json` 无法索引 | 高频筛选字段提成独立列（`platform`/`status`/`email` 已在列上）；其余内存过滤 |
| 分库合并丢数据 | 两库有同 `(platform,email)` 行 | 按 `uid` 去重优先；dry-run 出冲突清单 |
| Alembic 与手写迁移冲突 | `migrations.py` 仍在启动时跑 | 阶段 0 改为 no-op（Alembic 接管），保留函数签名 |
| 事故回归 | 阶段 2 改状态映射时语义漂移 | 12 态表驱动测试 + dump diff |
| 阶段 1 变更面变大 | UUID 穿透到 `platforms/` | 行为等价由 dump diff 保证；`legacy_id` 保留一版可回滚 |

## 已定决策

| # | 决策 | 记录 |
|---|---|---|
| H1′ | **不做硬隔离**：Codex 兜底保持「session 与 OAuth 都失败后才跑」。严格模式做成配置项，**默认关** | 风险表 + ADR-0007 |
| H2 | 3–4 个高频字段提成独立列，其余内存过滤 | 本文件 |
| H3 | 分库合并冲突保留 `updated_at` 最新 | 本文件 |
| H4 | `accounts.id` 换 UUID 主键；其余表保留 int | ADR-0008 |
| Q2 | 只做 A 类减法 + 删手机注册；Grok/面板/JSON 保留 | ADR-0007 |
| Q3 | 全量凭据加密（含 `totp_secret` 与配置口令） | ADR-0004 |
| Q4 | 允许破坏兼容：一次性迁移 + 快照 + 旧键只读一版 | 本文件 |

## 软性默认（我已定，可改）

- 分支名 `refactor-v2`（原定 `refactor/v2` 建不了：`refs/heads/refactor` 已存在，git 不允许 `refactor` 与 `refactor/v2` 共存）
- 每任务一提交，提交信息 `refactor(phaseN): T<id> <一句话>`
- ADR 从 0003 起编号
- field-notes 文件名用英文 slug，正文中文
- 阶段 1 完成后打 tag `v0-refactor-data-layer`，作为可回滚锚点
