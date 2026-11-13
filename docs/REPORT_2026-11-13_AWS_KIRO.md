# AWS / Kiro 平台建设报告

> 日期：2026-11-13 · 分支：`develop` · HEAD：`fd7fdc6`
> 范围：`main..HEAD` 23 个提交、84 个文件、+12201 / −442

## 摘要

在账号面板中新增 **AWS Builder ID** 与 **Kiro** 两个平台，打通「AWS 注册 → SSO →
Kiro 授权 → 凭据落库 → 测活/刷新 → 导出」的完整链路；真机注册 **23 个 Kiro 号**
（其中本轮批量 20 个）；并完成 4 轮审计/评审与全量设计优化。
当前全量测试 **3758 passed / 1 skipped**（`frozen_verify` 包裹）。

## 1. 背景与目标

- 原面板只有 ChatGPT / Grok；用户要求新增 AWS 与 Kiro，并参考
  `reference/register/kirox`、`reference/register/any-auto-register` 的协议实现。
- Kiro 的凭据由 **AWS Builder ID** 换出：AWS 是「授权根」，Kiro 是「派生」。
- 要求：真机可注册、凭据可刷新、导入导出与账号管理器 `kiro.json` 一致、
  数据只以密文落库、前端可用。

## 2. 交付内容

### 2.1 平台

| 平台 | 实现 | 关键点 |
| --- | --- | --- |
| AWS Builder ID | 纯协议（`curl_cffi`） | OIDC 注册 → 设备授权 → portal → 16 步注册 → SSO → device token |
| Kiro | 纯协议 | AWS 来源（复用/指定/新建）+ Kiro IDE OAuth（PKCE） |

- 指纹：macOS Chrome 131 + Apple M4 + 真实 canvas 直方图 + `lsUbid`，
  与 `chrome131` TLS 指纹一致（旧版 Windows UA 被 AWS 判 `BLOCKED`）。
- SSO 登录：`get-identity-user` → `get-password`（JWE）→
  `get-email-otp-login-credential`（邮箱验证码）→ 授权码。
- 半成品恢复：密码已设/号已建但 SSO 失败时落库为「待补凭据」，不白烧号。

### 2.2 授权账号页

- 侧栏 **授权账号 → AWS**（`/authorized-accounts/aws`），AWS 从「平台管理」移出。
- 台账式列表：已注册平台、授权链（行内展开派生 Kiro）、脚注说明。
- 动作：注册 AWS、导入/导出、测活、刷新凭据、删除、未派生号「补 Kiro」。

### 2.3 Kiro 注册来源

| 模式 | 行为 |
| --- | --- |
| `reuse`（默认） | 优先复用没被 Kiro 用过的 AWS 号；没有则按开关自动新建 |
| `manual` | 指定 `kiro_aws_email` 复用 |
| `auto` | 直接新建 AWS + Kiro |

注册页提供三选一 + 「没有可用 AWS 时自动注册」开关；点名邮箱时强制新建。

### 2.4 自动维护

- AWS/Kiro 各 10 个维护键（`CONFIG_KEYS` 110→130，secrets 12），
  周期任务 `aws/kiro_auto_probe` + `auto_refresh` 四条。
- `probe_checked_at` 记录最近检测时间，抽检 `oldest` 策略按它轮转。
- 设置页新增 AWS/Kiro 分区；无面板，故不渲染「同步策略」。

### 2.5 数据导入导出

- CSV：追加 `client_id` / `client_secret` / `sso`（旧 11 列顺序不变）。
- 通用 JSON：导出含 `kiro_aws_*` 快照与 `name`；导入接受 camelCase 别名归一。
- `kiro.json` v2（保留格式，API-only）：与账号管理器样例逐层同构
  （键集/类型/version=2），`source=plugin`；导入时 idc 账号映射回 `source=aws`。
- `kiro_auth_token`（Agent2API，专用入口）：单账号 → 扁平
  `kiro-auth-token.json`；多账号 → `{"version":2,"accounts":[…]}`。每条有
  唯一 `id`（email:uid 哈希）、`email`、小写 `loginProvider`、无 `uid`；
  idc 用 AWS SSO 凭据（`kiro_aws_*`）、`profileArn=""`、`expires=1` 强制刷新，
  social 用 Kiro OAuth 凭据。缺 AWS 快照的 idc 直接 400，不导无效文件。
- 入口：平台管理 → Kiro → 更多 → 导出凭据（确认 + loading + 延迟 revoke）。
- 契约守卫：`scripts/check_kiro_agent2api_export.py` +
  `tests/test_kiro_agent2api_contract.py`（id/email/uid/loginProvider/idc 分支）。

### 2.6 设计优化

- 仪表盘：四张统计卡 → 「库存带」（总数 + 三档状态 + 状态条）+ 两列分布清单。
- 代理页：「均衡配置」整卡 → 一条配置带；注册页窄控件并排。
- 全局：表格行距 12、卡片头 48/14、搜索按钮降级、焦点环/减少动效/细滚动条。

## 3. 真机结果

| 批次 | 结果 |
| --- | --- |
| 首轮 | 3 个 Kiro 号（iCloud 远程 + socks5h 代理），RT 刷新实测通过 |
| 批量 | 20 个任务：18 成功、2 个 iCloud 远程瞬时超时；补号 2/2 成功 |
| 合计 | **Kiro 23 个**；AWS 24 个（23 个已派生 + 1 个待补） |

所有号：`Kiro FREE`、`source=aws`、有 AT/RT、Kiro 记录带 AWS 密码、
敏感字段 `v1:` 密文；抽查 `refresh_token` 可换新 AT。

## 4. 质量工程

| 轮次 | 方式 | 结果 |
| --- | --- | --- |
| 审计 1 | 4 个只读子代理（协议/数据/前端/测试） | 8 Critical / 32 Important，修复 25 项 |
| 审计 2 | 3 个只读子代理（后端/前端/测试） | 1 Critical / 25 Important，修复 26 项 |
| 评审 3 | 整条分支 review | 1 Critical / 7 Important，修复 8 项 |
| 平台同步 | SDD：3 任务 + 每任务 review + 终审 | 0 Critical / 1 Important，修复并复审 |
| 评审 4 | 最近两个提交 | 0 Critical / 2 Important，修复 |

累计修复 **6 个独立 Critical**（列表明文凭据、extra 密码、JSON 后端明文、
筛选丢 AWS、已存在行 upsert 明文、探测成功丢 RT），全部有回归测试。

质量设施：
- `scripts/check_no_secrets.py`：提交前扫描密钥字面量；
- `scripts/frozen_verify.py`：运行期改树判无效；
- `scripts/check_dead_tests.py` + 测试：禁止 `__main__` 内的死测试；
- 金标测试：指纹 JSON 键集/关键值、`kiro.json` 键集逐层对比。

## 5. 关键提交

| 提交 | 内容 |
| --- | --- |
| `e3edf13` | 新增 AWS / Kiro 平台 |
| `6a3e500` | 指纹 + SSO 修复，真机 3 个号 |
| `07aec56` | AWS 移到「授权账号」页 |
| `b292b69` | Kiro 来源三选一 + 已注册平台列 |
| `1da7670` | 半成品落库 + 补 Kiro + 指纹金标 |
| `059d44e` | 审计 1 修复（凭据泄漏等） |
| `056007b` / `3139673` | 授权页台账化 / 二级菜单 |
| `d7712b0` | 导出凭据（kiro.json v2） |
| `434ff29` | 全量设计优化 |
| `1ee0a2d` | 审计 2 修复（upsert 明文等） |
| `f0f5f29` | 评审 3 修复（探测写回等） |
| `3ee1a73`…`bcad5f0` | 平台同步：自动维护 / 导入导出 / 任务页 / 终审 |
| `d1f10e7` / `fd7fdc6` | 死测试守卫 + 评审修复 |
| `58ac73b`…`5e09470` | 导出凭据改为 Agent2API 格式（唯一 id / idc AWS 凭据） |
| `17627e4` / `e41dffb` | Agent2API 导入契约守卫 + 终审修复 |

## 6. 已知限制与 backlog

- 同一出口连续注册会被 AWS 限流（`400 Please try signing in again`）；
- 复用登录（邮箱 OTP）真机端到端仍受出口限流影响，未完整复验；
- 启动迁移 `_encrypt_plaintext_extras` 每次全表解析，大号池有启动成本；
- `GET /accounts/export` 仍是旧 8 列 CSV，与导出注册表未统一；
- 授权链只取前 200 条 Kiro（副标题已注明）；
- AWS 测试真实 `sleep(1–3s)`，套件偏慢；
- `_sync_strategy("aws"/"kiro")` 读未登记键（今天回退 `local_only`）。

## 7. 自我改进

- **已应用** `cand-073d480ba68b-b9ea1c`：死测试 AST 守卫
  （`scripts/check_dead_tests.py`，eval 3/3，commit `d1f10e7`）。
- **候选** `cand-894d3caf3b7e-709f5d`：契约测试必须能在父提交/变异上变红。

## 8. 附录

- 平台文档：[PLATFORM_AWS_KIRO.md](PLATFORM_AWS_KIRO.md)
- 维护/配置：[MAINTENANCE.md](MAINTENANCE.md)
- 平台清单：[../README.md](../README.md)
- 复现命令：`.venv/bin/python -m pytest tests/ -q`（约 9 分钟）；
  `cd frontend && npx tsc -b && npm run build`。
