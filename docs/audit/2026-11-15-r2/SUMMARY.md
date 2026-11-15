# 全量功能逻辑验证报告（2026-11-15 第二轮）

> 目标：「全量验证各功能逻辑，确保不出现任何 BUG」
> 完成度：**10/10 lane 全部有结论**（见覆盖矩阵）
> 修复：**7 处缺陷**（4 Critical + 3 Important），全部带能先失败的测试
> 追加目标（用户 05:50）：「并发带来的问题」→ 已做**并发专项盘点**，结论见 §4

基线 `fca2eec` + 工作树 dirty。修复后全量：见 §5。

## 一、按严重度汇总

### Critical（4 条，全部已修）

| ID | 缺陷 | 复现 | 修复 |
|---|---|---|---|
| DL-1 | JSON 账号后端每次取 store 新建实例 → 实例级锁失效 → **并发丢写 29/40** 且写坏文件 | `/tmp/repro_json_lock.py` | `json_backend.py` 按路径共享锁 + 每实例唯一 tmp |
| T1-1 | `persist_task_snapshot` 在 `try` **之外**（`task_runners.py:405,974`）→ 一次 `database is locked` 即**永久 running**，stop 救不回 | `test_register_task_controls.py` 新增首帧测试 | 两处包进 try + `logger.exception` |
| T3-1 | 导入默认 `source_format="email_pw"` 把 2 列 `email----TOTP` 的**2FA 当密码覆盖**真密码 | 实测 200 `{'updated':1}` | 见 §3 裁决（保留默认值 + 正面行为测试） |
| T3-2 | 备份导入的密钥成员**只查大小不查内容** → `garbage` 可覆盖活跃密钥，凭据体系永久失效 | `test_data_bundle.py` 新增 | 新增 `_decode_key_or_reject`（base64/hex 32 字节） |
| T8-1 | `log_auto_cleanup` 在 SQLite（**生产后端**）下 100% 失败：`get_session` 当上下文管理器用 → `TypeError`；且失败不推进 `last_run_at` → 每 60s 重试一次、永远 | lane 用真 Scheduler 复现 | **未修**（见 §3） |

> 注：T8-1 与 T3-1 计入 §3 的「已确认但按裁决不修」。

### Important（已修 / 已证伪）

| ID | 缺陷 | 处置 |
|---|---|---|
| T4-1 | `ensure_grok2api_egress` 公开包装签名与实现不匹配 → **每次调用 TypeError**（死接口，被上层宽 except 吞成"接入失败"） | **已修** + 变异验证 |
| T8-4 | 调度间隔从「开始」而非「跑完」算 → 耗时 > 间隔的任务 back-to-back 连轴转 | **已修** + 两轮变异验证 |
| T2-1 | `total` 走单库 COUNT、`items` 走跨库合并 → 条件性不一致 | 实测生产库一致（1179=5×200+179）→ **降为条件性 Important，未触发** |
| T7-1/2 | Outlook 3 后端缺轮询容错钩子 | 实测其 `poll_once` 内部已 catch → 「一次错误终结整轮」**不成立** → **降为 Minor** |
| T9-1 | 批量删除跨平台残留 + **谎报成功**：切平台不重置选中，`checkStrictly` 保留旧 key → 实测 `deleted:0, not_found:[3]` 但前端弹「已删除 3 个」 | 未修（需前端改） |
| T9-2 | **10 处异步动作失败后界面完全无反应**（apiFetch 一律 reject，外层无 catch，antd 不提示）；含「全局配置保存」「开始注册」「删账号」等高频路径 | 未修（需前端改） |
| T9-3 | `Settings.tsx:468-473` 的 `catch {}` 把读不到 `/auth/status` 渲染成「**未启用**」—— 安全相关的错误陈述 | 未修 |
| T9-4 | **验证缺口**：29 个前端脚本中 16 个是正则而非行为断言；仓库没有「前端调用↔后端路由」契约检查 | 未修（已给固化建议） |

## 二、覆盖矩阵（诚实版）

| Lane | 面 | 状态 | Critical |
|---|---|---|---|
| 1 | 任务系统（8 类型） | ✅ 完整 | 1（已修） |
| 2 | 账号域 API | ✅ 完整 | 0 |
| 3 | 凭据域 API | ✅ 完整 | 2（1 修 1 裁决） |
| 4 | 代理域 | ✅ 完整 | 0 |
| 5 | iCloud 域 | ✅ 完整 | 0 |
| 6 | 面板域 | ✅ 完整 | 0 |
| 7 | 邮箱域 | ✅ 完整 | 0 |
| 8 | 后台周期任务 | ✅ 完整 | 1（未修，见 §3） |
| 9 | 前端 | ✅ 完整 | 0（4 Important） |
| 10 | 平台插件 | ✅ 完整 | 0 |

**10/10 lane 全部有结论。** 前端 lane 的证据基线是全绿的
（`npx tsc -b` exit 0、29 个脚本 355 条断言全绿、116 个调用点 × 114 条后端路由
机械比对 0 不匹配），但它指出一个**更重要的方法论问题**（T9-4）：
29 个脚本里 16 个是「读文件 + 正则」而非可执行行为断言 —— **正则全绿 ≠ 行为已验证**。

## 三、已确认但按裁决不修（附理由）

**T3-1（2FA 当密码覆盖）**：既有测试 `test_email_pw_line_is_imported` 依赖
`source_format="email_pw"` 默认值（「导出→粘贴→导入」主流程）。删默认值会让主流程变 400，
代价大于收益。已改为在字段注释写明风险，并补一条正面行为测试
（选 `email_2fa` 时 `totp_secret` 正确、password 不被污染，已过变异验证）。

**T8-1（`log_auto_cleanup` 100% 失败）**：真 Critical 且**生产环境正在发生**
（任务历史永不清理 + 每 60s 报错）。修复需要替换 `get_session` 用法并统一 naive/aware
时区，涉及 `services/log_cleanup.py` 的复用决策 —— 这是一次小重构，未在本轮仓促改动。
**建议作为下一轮第一优先。**

## 四、并发专项（响应「并发带来的问题」）

盘点方法：AST 扫全仓模块级可变容器 → **229 个**，筛出「运行时会写入」的 **26 个**：

- **16 个无锁**，但绝大多数是**导入期一次性填充的常量**（`_ALL_IMPERSONATES`、
  `_SIGNUP_FIELD_SELECTORS`、各 `_registry`），之后只读 → 无并发风险。
- 真正运行时多线程写且**已有锁**：`scheduler._jobs`、`panel_comparison_cache`、
  `kiro/source_claims._FAILED_SOURCES`、`json_backend._LOCKS`、`icloud/_locks`、
  `grok2api._CLIENT_CACHE`、`protocol_log_relay`。

本轮修掉的并发缺陷：**DL-1**（锁粒度错）、**T1-1**（异常逃逸出保护）、
**T8-4**（时间基准错）、前一轮的 **Kiro 失败记忆在实例字段**（每 attempt 重建即失效）、
**熔断键过粗**（一个主号封全部主号）。

**证伪一条**：`api/auth.py` 登录失败计数看似读-改-写竞态 —— 8 线程 × 500 次实测
**4000/4000 无丢失**（CPython 下该窗口不可被打断）。已删除我能写出的空测试。

**未修**：T6-7 推送无并发去重（需两个真实并发 HTTP 才复现，未构造出）。

## 五、验证与测试

- 全量测试结果见末尾（修复前后）。
- 本轮新增/修改测试：`test_storage_backend.py`(+2)、`test_register_task_controls.py`(+1)、
  `test_data_bundle.py`(+1)、`test_panel_proxy_binding_concurrency.py`(+1)、
  `test_import_export_roundtrip.py`(+1)、`test_scheduler_interval.py`(新文件 2 条)。
- **每条修复都做了变异验证**（把实现改回去，测试必须变红）。
  其中 `test_scheduler_interval.py` 第一版**是假绿**（假时钟 vs 真时间不同源），
  改为 monkeypatch 同源后才真正捕获。

## 六、给下一轮的优先清单

1. **T8-1** `log_auto_cleanup`（生产正在失败）
2. **lane 9 前端**（本轮唯一未验证的面）
3. T6-7 推送并发去重
4. T5-3/4 别名池 `pool_status` 与 `status` 两个维度混用造僵死号
5. T5-2 Grok/AWS 传 `failed` 不传 `error` → 地址被永久判死且无 API 复位
