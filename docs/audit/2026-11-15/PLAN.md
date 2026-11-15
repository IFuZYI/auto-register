# 全量检测计划（2026-11-15）

## Spec（绑定的权威）

用户要求：**完成全量检测，彻底规避 BUG**。

拆成两条可判定的性质：

1. **覆盖**：全仓生产代码（约 6.9 万行 / 前端 1.8 万行）按职责分 lane 扫过，
   每条 lane 有明确的责任面，不留"没人看"的区域。
2. **可判定**：每条发现必须带**可执行证据**（复现命令或失败测试），
   不接受"读代码觉得可疑"。无法复现的只能记为 `hypothesis`，不得进入修复波。

判定"彻底"的标准不是"零发现"，而是：**每一条 Critical/Important 都有
复现证据、修复或明确裁决**（parked 必须带 ruling）。

## 基线

- HEAD：`fca2eec`（工作树有 62 改动 + 30 未跟踪，均为本会话前序工作）
- 全量测试现状：`3895 passed, 1 failed`（失败为 `test_maintenance_doc.py`
  的 eslint 基线漂移，已确认早于本会话改动存在）
- 既有审计先例：`docs/audit/2026-10-30/`（10 lane、31 批修复）

## Global Constraints（全lane适用）

- **只读审计 lane 不得修改生产代码**。报告写 `docs/audit/2026-11-15/<lane>.md`。
- **每条发现必须编号**（`<LANE>-<序号>`）并带：严重度（Critical/Important/Minor）、
  文件:行、复现步骤或命令、影响面、建议修法。
- **禁止把"可疑"写成"缺陷"**：没有复现路径的一律标 `hypothesis`。
- **不得读取或引用**：`.env`、`data/secrets/`、任何真实凭据、代理口令、账号邮箱。
  引用凭据一律用占位符。
- **不得写入**生产数据库、不得触发真实上游请求（审计 lane 只读代码与本地测试）。
- 报告不得粘贴大段源码（`>10` 行为限），给 `文件:行` 引用即可。
- 修复波中每批改动必须带**能先失败的测试**（TDD），并在冻结工作树上跑全量。

## Task 1 — 数据层：双后端等价、迁移、事务、并发写

范围：`core/db/`、`core/db/*_repository.py`、`migrations/`、`scripts/merge_platform_dbs.py`。

必查：

- SQLite 与 JSON 两种后端对同一仓储 API 是否**语义等价**（排序、唯一约束、
  分页、`NULL` 处理、并发计数）。
- 迁移链是否只在空库可用（先例 `data-layer.md` 曾指出这点）；`init_db()` 是否
  覆盖全部表。
- "先算再写"的竞态：读快照 → 改 → 整行写回，是否会覆盖并发写入或复活已删行。
- 加密字段的读写路径是否有一处漏加密/漏解密。

产出：`docs/audit/2026-11-15/data-layer.md`。

## Task 2 — 注册链路：四平台主链与回退

范围：`platforms/chatgpt/`、`platforms/grok/`、`platforms/kiro/`、`platforms/aws/`、
`services/task_runners.py`。

必查：

- 主链失败后的回退路径是否都收敛（Codex OAuth / 密码登录 / 补 RT / 绑定 2FA）。
- **部分成功**是否被当成成功（账号建了但凭据空 —— 先例已有 `partial/missing` 语义，
  检查是否所有平台都落了这个标记）。
- 重试轮数、dead-end 判定、域名窗口 failover 的交互是否会互相抵消。
- 资源释放（来源认领、别名租约、浏览器、代理）在**每一个**异常分支是否都发生。

产出：`docs/audit/2026-11-15/registration.md`。

## Task 3 — iCloud 收件链路（本会话重灾区）

范围：`platforms/icloud/`、`services/icloud/`、`modules/mail/icloud_*.py`、
`services/chatgpt_otp_mailbox.py`。

必查：

- **静默降级**：上游 `method != imap` 的所有调用点，是否都拿到了可信度信号。
  本会话实测：降级路径读到 0 封而 IMAP 读到 2 封。
- **读参数**：`limit` 有下界（太小被上游候选截断而丢信）与上界（顶到 IMAP
  预算悬崖而降级）；`days` 窗口对等码是否过大。
- 熔断粒度：主号级 vs 服务级；冷却期内是否还会发请求。
- 轮询容错的边界：连续错误预算、分段退避、熔断是否**先于**容错判定。
- 别名租约：续约、回收、`in_use` 泄漏。

产出：`docs/audit/2026-11-15/icloud-mail.md`。

## Task 4 — 代理池与账号绑定

范围：`core/proxy_pool.py`、`core/proxy_utils.py`、`api/proxies.py`、
`services/task_runners.py` 的绑定段。

必查：

- 跨平台写错行（先例：ChatGPT 任务改掉 Grok 号绑定）。
- `report_fail` 的误杀成本：一次失败就停用是否合理；`reactivate` 是否可达。
- 绑定切换在**注册后**发生，是否可能把注册代理的统计记到绑定代理上。
- 打码：任何回吐代理 URL 的接口/日志是否都过 `redact_proxy_url`。

产出：`docs/audit/2026-11-15/proxy.md`。

## Task 5 — API 契约与安全

范围：`api/`、`main.py`、`core/secret_box.py`、`core/credential_*`。

必查：

- 错误码 → HTTP 状态映射是否会让**业务失败**被前端误判为登录过期（`401`）。
- 列表/详情接口是否下发任何凭据（含明文密码、cookies、api key、代理口令）。
- 备份/导出是否包含解密密钥。
- 鉴权：面板 token、`X-Panel-Auth-Required`、iCloud 上游业务失败的区分。

产出：`docs/audit/2026-11-15/api-security.md`。

## Task 6 — 任务运行时与生命周期

范围：`services/task_service.py`、`services/task_runners.py`、`core/task_runtime.py`、
`core/task_store*`。

必查：

- 任务**永久 running** 的所有可能路径（收尾段抛异常、`finish()` 被跳过）。
- 停止/跳过：是否真能打断长睡与在飞请求；`stop_requested` 的传播。
- 并发任务共享可变状态（进程内单例、模块级 dict）是否会导致跨任务污染。
- 任务计数（`registered` / `success` / `skipped`）与实际是否一致。

产出：`docs/audit/2026-11-15/task-runtime.md`。

## Task 7 — 前端

范围：`frontend/src/`（页面、组件、lib）、`frontend/scripts/`。

必查：

- 「所选 vs 全部筛选」语义：每个批量动作的 `selected/all_filtered` 分支是否
  与后端收到的 id 集合一致（先例：UUID 被 `Number()` 吞掉）。
- 状态映射是否与后端四值语义一致（正常/过期/失效/禁用）。
- 危险操作（删除、停用、覆盖）是否有确认；失败是否有可见反馈（不许静默）。
- 轮询：是否有永久 loading、是否有用固定秒数猜后台完成。

产出：`docs/audit/2026-11-15/frontend.md`。

## Task 8 — 测试质量与门禁

范围：`tests/`、`tests/*_double.py`、`scripts/check_*.py`。

必查：

- **假绿**：测试是否断言 mock 而非真实行为；测试替身是否从被测谓词推导行为。
- **未收集**：`__main__` 守卫内/之后的测试定义（已有 `check_dead_tests.py`，
  验证它是否真的在跑并覆盖新文件）。
- **同名测试文件**：`tests/` 与 `.worktrees/*/tests` 是否有互相 import 冲突。
- 变异缺口：本会话新增的几组护栏，是否有对应的"回退即红"证明。
- 关键路径无测试的模块（例如 `rebind` 曾经零测试）。

产出：`docs/audit/2026-11-15/tests-quality.md`。

## Task 9 — 死代码、重复实现、宽捕获

范围：全仓生产代码。

必查：

- 无引用符号（区分生产/测试语料）、不可达语句、`__main__` 内的死测试。
- 同一逻辑的多份实现（去重候选）。
- 宽捕获 `except Exception` 中**吞掉**而非转译的站点，重点看是否吞掉了
  可恢复/不可恢复的区分。

产出：`docs/audit/2026-11-15/deadcode.md`。

## Task 10 — 性能与资源

范围：全仓（重点 `api/`、`services/`、`core/proxy_pool.py`）。

必查：

- 单请求耗时/体积异常（先例：单请求 988ms/3.5MB）。
- N+1：逐行提交、每请求重读配置、每次调用重开会话。
- 无界增长：进程内 dict/缓存无上限、任务日志无裁剪。
- 阻塞调用出现在 async 路径上。

产出：`docs/audit/2026-11-15/performance.md`。

## Task 11 — 汇总与修复波

汇总 10 条 lane 的 Critical/Important，按"用户可感知"排序，逐条：
复现 → 修复（带先失败的测试）→ 冻结全量 → 记录。

产出：`docs/audit/2026-11-15/SUMMARY.md`。

## 执行裁决（Ruling）

- **Ruling**：10 条 lane 作为 Task 1–10 并行只读审计（互不写同一文件、不改生产代码，
  不构成"实现冲突"），Task 11 汇总后串行修复。理由：串行 10 条只读审计纯串行要
  10 轮往返而收益为零。代价：若两条 lane 报同一处，需在汇总去重。
- **Ruling**：修复波的任务在上游 lane 出结论后**动态生成**，不在本计划里预先编号。
  理由：修复对象依赖审计发现，预先编号会写成猜测。代价：修复波的任务数不可预估。