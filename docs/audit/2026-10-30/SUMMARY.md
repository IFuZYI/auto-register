# 全量检测报告（2026-10-30）

> 范围：`/data/workspace/Register` 全仓（生产代码 5.2 万行 / 207 文件，测试 5.6 万行 / 211 文件）
> 方法：10 条审计 lane 并行（每条独立子代理、只读、报告落盘）+ 控制器侧门禁
> 基线：`4680c8d`（代理标签/均衡上线后的版本）

## 一、结论

**系统可用。** 10 条 lane 的结论里没有「整体不可用」——每一条都给出了能跑通的
主路径证据；问题集中在**边界分支**、**跨平台**、**凭据暴露面**与**前端「所选」语义**。

本轮**已修 11 批**（每批带回归测试），其中 4 条是「用户一点就报错/静默失效」的
功能性缺陷：

| 症状 | 根因 | 提交 |
|---|---|---|
| 勾选账号后「补RT / 刷新Token / 上传（所选）」必提示「请先选择」；「导出」退化成导出全部（含明文密码） | 前端把 UUID 主键 `Number()` 成 NaN 后过滤光 | `e6f8670` |
| 「批量同步 CLI 代理状态」必 500 | 后端 `int(uuid)` | `9630980` |
| 任务历史 `total=1661`，第 51 条以后翻不到 | 固定拉 50 条 + 客户端分页 | `e6f8670` |
| 老库升级后启动即 `no such column: proxies.tags` | Alembic 迁移不在 `init_db()` 链路里 | `519c821` |
| ChatGPT 任务的代理异常会改掉 **Grok 号**的绑定 | `rebind` 只按 email、跨平台取最新行 | `519c821` |
| 均衡/换绑把并发写入的 token/密码回滚，还会复活已删账号 | 整行快照 `upsert` | `519c821` |

## 二、审计覆盖与结论矩阵

| Lane | 报告 | 结论 | 最严重发现 |
|---|---|---|---|
| 数据层与迁移 | `data-layer.md` | ⚠️ 双后端不等价、迁移链仅空库可用、加密有洞 | `token` 1057 行明文（已修）、cookies 607 行明文、`c3f8a1d64b27` downgrade 会清空 accounts |
| 注册链路 | `registration.md` | ⚠️ 主链可用，回退与并发有缺口 | 见报告 P0 段 |
| 动作与任务 | `actions-tasks.md` | ⚠️ 17 动作映射完整，生命周期有洞 | 收尾段无 `try/finally` → 任务永久 running；`int(uuid)` 500（已修） |
| 代理池 | `proxy.md` | ❌ 4 条 P0（全部已修） | 跨平台写错号、整行快照覆盖、复活删号、老库缺列 |
| 面板对接 | `panels.md` | ⚠️ 方向判定正确，外围有活锁 | 拉回被「登记」闸门锁死；`delete_old` 删掉唯一记录 |
| 前端 | `frontend.md` | ❌ 5 条 P0（全部已修） | UUID 被 `Number()` 吞掉、历史翻页、危险操作无确认 |
| API 契约与安全 | `api-security.md` | ❌ 2 条严重（已修）+ 23 条中低 | setup 抢注接管、备份含解密密钥、列表下发凭据 |
| 性能与资源 | `performance.md` | ⚠️ 4 条 P0 | 单请求 988ms/3.5MB（已修）、tasks 轮询 172ms/373KB |
| 死代码/坏味道 | `deadcode-quality.md` | ℹ️ 无 P0 | 2 个整模块死、14 组重复实现、453 处宽捕获 |
| 测试质量 | `tests-quality.md` | ⚠️ 覆盖不均 | 关键路径缺兜底（`rebind` 之前零测试） |

## 三、本轮已修（11 批，全部带测试）

### 1. `c2c502b` 模型 ↔ 迁移 ↔ 生产库的索引漂移
- `ix_accounts_platform_created_at` / `ix_task_logs_platform_id` 是生产库的**承重索引**，
  模型里没声明 → `alembic check` 想删掉它们；空库 `upgrade head` 又建不出来。
- 修：模型补声明、新增迁移 `e5b2c8f71a34`（幂等）、`env.py` 加 `include_object`
  把 icloud/outlook 三张「住在 `data/platforms/*.db` 的表」排除在主库对比之外。
- 验证：真库与空库 `alembic check` 均为 `No new upgrade operations detected`。

### 2. `9630980` 批量同步 CLI 代理状态对 UUID 主键必 500
`sync_results.get(int(acc_model.id))` → `str(...)`；同文件的兄弟函数本来就用 `str`。

### 3. `c93b71f` 鉴权面 5 条
- `/api/auth/setup` 在中间件之外且「没设密码就直接设」→ 任何来源可抢注接管（**严重**）。
  改为首次只允许本机回环（`APP_ALLOW_REMOTE_SETUP=1` 可放开容器部署）。
- 改密不要旧密码 → 现在必须 `old_password`。
- `logout` 空实现、改密不换密钥 → 引入 `_rotate_jwt_secret()`，logout/改密/关鉴权都换；
  环境变量管密钥时如实返回 `revoked: false`。
- `X-Forwarded-For` 可伪造绕开登录退避 → 只有 `APP_TRUST_PROXY=1` 才采信。
- CORS 全开 → 只放行本机来源。

### 4. `a3470d3` 列表类响应不再下发凭据正文
账号 `token`（列表一次可拖走全池 AT/SSO）、代理 `url`（含账号密码）、任务日志里的
代理串 —— 全部打码/裁掉，详情端点保持全量；前端改用 `has_token`。

### 5. `e6f8670` 前端 5 条 P0
UUID 被 `Number()` 吞掉（5 处）、任务历史服务端分页、批量删除与「关密码保护/关 2FA」
加二次确认。

### 6. `519c821` 代理池 4 条 P0
- `rebind` 改 `(account_email, *, account_id, platform)`：优先 id+platform；只给 email
  且跨平台命中多个时**拒绝并告警**（不猜）。
- `_persist_register_proxy` 不再硬编码 chatgpt，`_load_account_fields` 带出 id/platform。
- 新增 `account_repository.update_extra()`：只 `UPDATE extra_json`，行不存在返回 False
  （不插入 → 不复活删号），`rebalance` 失败计入 skipped。
- 新增幂等 `_ensure_proxy_tags_column()`，排在 `run_migrations` 最前。

### 7. `bb81e93` `accounts.token`（AT/SSO）加密落盘
列名保持 `token`、属性改 `token_cipher` + `@computed_field token`（与 `password` 同法）。
存量 1057 行已就地加密（备份 `/root/Register-data-backup-token-20261030-185531.tgz`），
`--verify` 全 0，解密读回仍是 1988 字符 JWT。

### 8. `e711c7c` 列表分页上界
`accounts` 1..200（原 988ms/3.5MB 可被单请求放大）、`tasks/logs` 1..500。

### 9. `c539dda` JSON 后端 token 回归 + JSON 落盘封口
`json_record_to_model` 用 `model_fields` 过滤 kwargs，`token` 变成 property 后被过滤掉
→ JSON 后端读出来是空串（与当年 `password` 同一个坑）。顺带把 `_json_upsert` 的明文
password/token 改成落盘密文（D-01/D-02）。

### 10. `bb01a71` 复核轮：子代理报告返回后补的四条
- **grok SSO 仍在列表里**（data-layer 本轮新增 P0）：`_slim_extra_for_list` 先解密再按
  黑名单 pop，`sso` 两个名单都没进 → 一页 200 条能拉走 390 个 grok 号的 SSO。
  并入 token 名单 → 裁正文、留 `has_sso`。
- **「导出所选」仍 422**（frontend F-01，我上一轮只修了父组件）：`AccountExportModal`
  仍 `.split(',').map(Number)` → `[NaN]` → `[null]`。改字符串；守卫测试补「不许
  `.map(Number)`」——只看类型字面量的那条全绿但功能是坏的。
- **未认证 logout 能踢全员**（api-security N1）：`/api/auth/logout` 在中间件之外且无
  `require_auth`，任何人一个 POST 就轮换全局 JWT 密钥。加认证依赖。
- **构建窗口整站 404**（frontend F-42）：`vite build` 会 `emptyOutDir`，而挂载只在启动
  时判断目录存在 → 18:46 构建、18:42 启动的进程在 18:49 整站 404。改成先 `makedirs`
  再无条件挂载。

### 11. `c2c502b` 之外的迁移防护
`test_migration_index_parity.py` 5 条 + 老库补 `tags` 1 条：空库/真库/老库三条路径
都钉住（模型、迁移、生产形态三方一致）。

## 四、仍待处理（按优先级）

### P0（功能/数据风险，建议下一轮先做）
1. **`extra.cookies` 607 行明文**（data-layer D-18）——cookies 是会话凭据，
   与 AT 同级；加进 `SENSITIVE_EXTRA_KEYS` + 存量脚本即可，但会影响
   `merge_account_fields` 的比较语义，需要一轮回归。
2. **`c3f8a1d64b27` downgrade 会把 `accounts` 变空表**（D-13）——至少加护栏：
   downgrade 前要求显式 `--force`，或在文档里标红「不要 downgrade」。
3. **JSON 后端四条主路径不等价**（D-01..D-06）：Outlook 池写错表、改账号 500、
   凭据明文落盘。要么补齐，要么在启动时明确拒绝「账号走 JSON 后端」。
4. **任务收尾无 `try/finally`（actions F-03）→ 任务永久 running**：
   进程内异常路径会让任务卡在 running，`test_tasks_list_perf` 之外没有兜底。
5. **面板「拉回」被登记闸门锁死**（panels F-06）：生产库 CPA 0/667 登记，
   「更新本地」永远不会执行。

### P1
- 性能：`/api/tasks` ETag 仍是 O(n)（304 分支 172ms）；`include_logs` 建议收敛为
  单任务语义；`task_logs` upsert O(n²)（每行日志 9.6ms）；SQLite 开 WAL；
  `stats`/`bound_counts` 改 SQL 聚合。
- 安全：`GET /api/accounts/export` 直出明文密码 CSV 无二次确认；
  备份导出含 `credential_key`（设计如此，但应加确认 + `no-store`）；
  响应缺 `Cache-Control: no-store`；`page_size` 之外还有 5 个批量接口无上限。
- 前端：16 条 P1（竞态、无 catch 的静默失败、错误态=空态、图标按钮无名字）。
- 死代码/重复：2 个整模块、12 个符号、14 组重复实现（JWT 解码 7 份、
  `_get_config_value` 5 份且已语义漂移）。
- 测试：`rebind`/`rebalance` 之前零测试（已被本轮 7 条补上），
  面板推送与任务控制仍缺兜底。

### P2
各报告 P2 段，量最大的是 `deadcode-quality.md`（453 处宽捕获、5 类魔法数字）
与 `performance.md`（N+1、全表扫描）。

## 五、明确「不做」的取舍

| 项 | 理由 |
|---|---|
| ETag 改 O(1) 指纹 | 需要 task_store 全局版本计数器，否则会漏「内存里刚变、库里没落」的任务 → 出现「一直显示运行中」。172ms 换这个风险不划算。 |
| 删除 `ix_task_logs_platform` | 已随本轮迁移删除（复合索引前缀已覆盖），不是「不做」而是做完了。 |
| 强制把账号迁回 SQLite 后端 | JSON 后端是用户明确保留的能力（Q2）。修不修由下一轮决定，本轮只记录差异。 |
| 关掉 `page_size` 之外的批量上限 | 各端点已有 1000 条上限，性能报告建议更低，但会改变现有前端批量操作语义。 |

## 六、门禁结果

| 门禁 | 结果 |
|---|---|
| 全量测试 | **3131 passed / 0 failed / 1 skipped**（326s，含本轮全部 11 批改动与新增 45+ 条回归） |
| `alembic check`（真库 / 空库） | 均 `No new upgrade operations detected` |
| `compileall` | 通过 |
| `pyflakes`（undefined name） | 0 |
| 前端 `npm run build`（`tsc -b && vite build`） | 通过，5.2s |
| 前端 eslint | 39 problems（与基线一致，无新增） |
| 明文凭据扫描 | 账号密码 / AT·SSO / extra 六键 / 配置口令 / 2FA journal 全 0 |
| 黑盒冒烟 `scripts/e2e_smoke.py --sweep` | **46 步 0 失败**（含 `app.openapi()` 全路由 sweep） |
| 运行中服务 | `http://127.0.0.1:8000`，版本 `bb01a71`，`stale: false`，SPA `/` 200 |

> 注意：`npx tsc --noEmit` 在本仓库**不可靠**（项目引用下不检查页面文件），
> 前端门禁一律用 `npm run build`。
