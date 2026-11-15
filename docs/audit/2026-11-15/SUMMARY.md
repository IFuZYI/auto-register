# 全量检测报告（2026-11-15）

> 目标：全仓检测、彻底规避 BUG。
> **实际完成度：10 条 lane 中 2 条有结论，8 条未完成。**
> 本报告如实标注覆盖面 —— 未审计的区域不得视为干净。

基线：`fca2eec` + 工作树 dirty（62 改 / 30 未跟踪）
修复后全量：**3897 passed / 1 failed**（失败为 `test_maintenance_doc.py` 的 eslint
基线漂移，已确认早于本轮改动存在）。

## 一、已修（1 条 Critical + 1 条 Important）

### DL-1 [Critical] JSON 账号后端并发写丢数据、并能写坏整个库文件

- 根因：`AccountRepository._json_store()`（`core/db/repository.py:243`）**每次调用都新建**
  `AccountStoreJson` → 新建 `JsonStore` → 锁是实例级的 → **等于没有锁**。
- 实测（40 线程各新建实例、并发 update 同一记录的不同字段）：
  **只有 11 个字段生效，丢 29 个**；并发还抛
  `FileNotFoundError: accounts.json.tmp -> accounts.json` 与
  `JsonStoreError: 不是合法 JSON（Extra data …）`。
- 可达性：当前生产是 sqlite（日志 `[storage] 存储后端: sqlite`），**尚未触发**；
  但 `DEFAULT_BACKEND = json`，全新部署默认开启。
- 修复：按**文件路径**共享锁（`_lock_for()`）+ 临时文件名**每实例唯一**
  （`.{pid}-{uuid}.tmp`）。
- 回归：`tests/test_storage_backend.py` 新增 2 条；**双向变异验证**（退回实例锁 +
  固定 tmp 名 → 两条立即变红）。

### DL-2 [Important] 原子写的临时文件名固定 → 跨实例/跨进程互撞

与 DL-1 同一处修复覆盖；独立列出因为它还防**多进程**（误起第二个 `main.py`）。

## 二、已记录未修（1 条 Minor）

### TASK-1 [Minor] `finish()` 保护不一致

`services/task_runners.py:1194`（批量动作成功路径）用 `try/finally` 保护 `finish()`，
而 `:864` / `:900`（注册分支）裸调。

**只判 Minor 的理由**：`TaskStore.finish`（`core/task_runtime.py:364`）只做内存字段赋值，
无 I/O、无序列化，我**构造不出**它抛异常的现实路径。按"可复现才判 Critical/Important"
的标准，不给更高严重度。

## 三、覆盖矩阵（诚实版）

| Lane | 状态 | 结论 |
|---|---|---|
| 1 数据层 | ✅ 有结论 | 1 Critical + 1 Important，已修 |
| 2 注册链路 | ❌ **未审计** | 子代理 17 分钟未落盘即中断，零产出 |
| 3 iCloud 收件 | ❌ **未审计** | 同上；但本会话早前已单独修过 5 处（见下） |
| 4 代理池 | ❌ **未审计** | 同上 |
| 5 API 安全 | ❌ **未审计** | 同上（控制器自查了 401 映射，结论正确） |
| 6 任务运行时 | ⚠️ 部分 | 1 Minor；停止打断、跨任务污染、计数漂移未查 |
| 7 前端 | ❌ **未审计** | 未派发 |
| 8 测试质量 | ❌ **未审计** | 未派发（控制器自查：3897 用例全被收集、dead-test 门禁通过） |
| 9 死代码/宽捕获 | ❌ **未审计** | 未派发（控制器粗测：600 处宽捕获、132 处疑似静默吞） |
| 10 性能资源 | ❌ **未审计** | 未派发 |

### 为什么 8 条没跑完（不是借口，是事实）

子代理（单模型 `deepseek-v4.1-flash-1017`，并发上限 5）在"全量面 + 每条发现必须复现"
的任务下，17 分钟仍零落盘。控制器中断后改为自己取证，只能覆盖少量面。

**因此这次不能声称"完成全量检测"。** 真实达成的是：在可覆盖的面里找到并修掉了
1 条可复现的 Critical（JSON 后端丢数据/写坏文件）。

## 四、本会话早前已修的相关缺陷（非本轮审计发现，供对照）

| 缺陷 | 位置 | 验证 |
|---|---|---|
| 静默降级：0 封被当作"没有邮件" | `services/icloud/remote.py` | 真实上游：imap 2 封 vs web_api 0 封 |
| 读参数下界（丢信）/ 上界（预算悬崖） | `modules/mail/icloud_remote.py` | limit 5/10→0 封，≥15→2 封 |
| 熔断键粒度过粗 | `services/icloud/upstream_gate.py` | 主号级 vs 服务级 |
| 熔断判定顺序（死代码） | `core/mailboxes/base.py` | 变异验证 |
| 只读秘钥下"检测"恒 403 | `platforms/icloud/remote_client.py` | 真实上游 4 主号全 200 |
| Kiro 复用同号 55 次 | `platforms/kiro/source_claims.py` | 真实库：候选推进 |

## 五、下一步（要真正"彻底"还需要的）

1. 重跑 lane 2/4/5/7/8/9/10，**每 lane 限定范围 + 强制先落盘**（本轮教训）。
2. lane 3 补齐：`method/warning` 的全部消费点只有 1 处（已确认），但要查
   `_baseline_degraded` 在**所有**路径的置位/清除。
3. lane 8：给本轮新增的 6 组护栏补"回退即红"的变异证明（部分已做）。
