# 计划：全量修正 + 全部线路可行性验证

## 背景

用户报「注册流程也出问题了，前面一大堆注册失败的账号」。上一轮修了两个
必然失败点（`login_password_verify` 401 raise、`icloud_remote` 属性名），
但**没有验证其它线路**，也没有把「线路可跑」变成可重复的检查。

目标不是「让测试变绿」——基线 2978 全绿时注册仍在失败。目标是把**每条
线路真的跑一遍**，把跑不通的修好，并把验证固化成可重复的命令。

## 线路矩阵（必须全部覆盖）

### ChatGPT 注册方式（2）
- `refresh_token`（默认）：注册 → 拿 RT
- `access_token_only`：注册 → 只拿 AT

### ChatGPT 注册身份（3）
- `email`：邮箱注册
- `phone`：纯手机号（接码平台）
- `phone_with_email`：手机号 + 邮箱

### 邮箱渠道（5）
- `outlook` / `microsoft`（微软邮箱池）
- `mail_import`（导入的邮箱）
- `icloud_local`（本地 iCloud 主号）
- `icloud_remote`（远程 iCloud 仓库）

### Grok（2）
- `register`（注册）
- `refresh_token` / `refresh_oauth`（刷新两条）

### 数据面（2 后端 × 7 表）
- 配置 / 账号 / 代理 / 任务 / 任务日志 / iCloud 主号 / iCloud 别名 / 邮箱池

## 全局约束

- **不许把「测试通过」当作「线路可用」的证据**：线路验证必须走真实调用链
  （打真实端点，或被显式 mock 的成功/失败响应驱动全流程），并断言**最终
  产物**（账号落库、凭证完整、状态正确）。
- 每条线路验证失败时，**先修根因**，不许用 `try/except` 吞掉后报成功。
- 新增/修改的行为都要有回归测试；变异验证必须能变红。
- 全量 pytest 必须绿；eslint 基线 51；前端检查脚本全绿。
- `core` 不许 import `services`（`tests/test_dependency_direction.py`）。
- 不许新增未绑定的 `except Exception:`（`tests/test_exception_handlers_not_too_wide.py`）。
- 真实账号/凭证不得写进测试或提交历史。

## 任务

### Task 1: 线路验证的公共夹具

建 `tests/route_harness.py`：一套可复用的「跑通一条线路」夹具。

- `fake_http(responses)`：按顺序返回预设响应的 session 替身，**记录请求**
  供断言（URL / body / headers）。
- `assert_account_persisted(email, *, expect_rt=..., expect_at=...)`：
  从仓储读回账号，断言凭证完整性与状态。
- `assert_no_secret_leak(text)`：断言日志/响应里没有明文凭证。

判据：夹具自身有 3+ 条测试证明它**能抓住**错误（凭证缺失、状态错、泄漏）。

### Task 2: ChatGPT 邮箱注册线路（email × 5 种邮箱渠道）

对 5 种邮箱渠道 × `email` 注册身份，验证端到端：

- 渠道取号 → 提交注册 → 拿到 AT/RT → 落库 → 状态正确
- 已存在账号（`login_password`）→ 密码 401 → **降级 OTP**（上一轮修的，
  要在这条线路里被真实覆盖）

判据：5 条子测试全绿；`login_password` 分支的 OTP 降级有断言。

### Task 3: ChatGPT 手机号线路（phone / phone_with_email）

- 接码平台取号 → 注册 → 落库
- `phone_with_email` 要同时绑定邮箱
- 接码失败/超时的降级与报错要清楚

判据：两条子测试全绿；失败路径给出可诊断的报错（不是裸 `KeyError`）。

### Task 4: ChatGPT 两种注册方式（refresh_token / access_token_only）

- `refresh_token`：必须拿到 RT，`chatgpt_has_refresh_token_solution=True`
- `access_token_only`：只有 AT，**不许**谎报有 RT

判据：两条子测试全绿；`access_token_only` 的账号在界面上不被算作「有 RT」。

### Task 5: Grok 线路（注册 + 两条刷新）

- `register`：注册 → 拿到 SSO
- `refresh_token`（grok2api 刷新）
- `refresh_oauth`
- 已删除的 CPA/Sub2API 动作**不许**再出现

判据：3 条子测试全绿；动作清单里没有已删除的动作。

### Task 6: 任务化线路（每条动作都能跑成任务）

- `batch-action` 覆盖 chatgpt 与 grok 的每个动作
- 任务落库、日志、状态（success/failed/skipped）正确
- 停止/跳过能生效

判据：动作清单里的每个 id 都能建任务并跑完；至少一条断言停止生效。

### Task 7: 数据面线路（两种后端 × 全部表）

- `STORAGE_BACKEND=json` 与 `sqlite` 下，7 张表的读写都正确
- 账号 + 别名 + 邮箱池的**跨表关联**正确（`list_aliases` 的主号邮箱）
- JSON 部署不产生 `.db` 数据

判据：两种后端各跑一遍同一套断言；JSON 部署的 `.db` 检查要断言「无数据」。

### Task 8: 全量验证与文档

- 全量 pytest / eslint / 前端检查脚本
- 跑一次真实的注册冒烟（1 个账号，真实网络，失败也要记录原因）
- 把「线路矩阵 + 如何复验」写进 `docs/MAINTENANCE.md`

判据：三套检查全绿；文档里能照着复验每条线路。
