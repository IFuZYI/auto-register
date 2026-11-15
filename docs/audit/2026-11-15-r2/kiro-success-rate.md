# Kiro 成功率低：根因与修复（2026-11-15）

## 结论

**Kiro 功能本身可用**（历史成功 52 个），低成功率由**三个叠加缺陷**造成，
每个都独立致命。修复后实测 **3/3 成功（100%）**，修复前 0/27、4/100。

## 三个根因（按影响排序）

### 1. 【决定性】`_pick_unused_aws` 把 `needs_sso` 半成品当成可复用来源

库里 AWS 号分两类（实测对比同一个成功案例与失败案例）：

| 字段 | 成功过的号 | 那 3 个失败的 |
|---|---|---|
| `refresh_token` / `access_token` / `sso` / `device_code` | ✅ 有 | ❌ **全无** |
| `needs_sso` | — | ✅ `True` |

`needs_sso=True` 是 `platforms/aws/plugin.py:231` 的 `_save_partial_account` 打的标记，
注释原文：「**密码已设、凭据没拿到**」。这类号的 SSO 授权从未完成 —— 拿它去复用登录，
**收码可以成功，但提交 OTP 会被 `HTTP 400 Please try signing in again` 拒掉**。

现场证据（task_1794717139351_574351）：

```
04:33:20 [iCloud 远程] 收到验证码: 276625     ← 等码已修好，确实收到了
04:33:22 [FAIL] post: HTTP 400 ... Please try signing in again
```

**为什么这直接打成 0%**：生产 55 个 AWS 里 52 个完整号已被 Kiro 用掉，
**剩下 3 个全是半成品**，于是每一轮都选中必然失败的号。

- 修复：新增 `_is_complete()`，排除 `needs_sso`。
- 降级路径：`kiro_auto_register` 默认开 → 没来源时自动新建 AWS + Kiro（已实测走通）。

### 2. 等码时长被全局配置压到 60 秒（Kiro/AWS 需要 300）

生产配置 `mailbox_otp_timeout_seconds = 60`。Kiro 与 AWS 都调
`get_mailbox_otp_timeout(default=300)`，但**默认值只在配置缺失时生效** —— 配置 60
就直接拿 60。而 `core/base_platform.py:125` 的注释自己写着「iCloud 转发有时要一两分钟
才到，120 会误判成收不到」；`platforms/aws/plugin.py` 的注释更明确记录
「AWS 验证码邮件约 **3 分钟**后才到，180s 会卡在边界上」。

现场：`04:32:20 开始 → 04:33:20 收到码（正好 60 秒）→ 04:33:22 提交失败`。

- 修复：Kiro 与 AWS 各加 `OTP_TIMEOUT_FLOOR = 300`，取 `max(配置, 300)`。
  配置调大仍尊重（有测试钉住），调得过小时兜住。

### 3. `_pick_unused_aws` 连续挑选会**泄漏认领**

每次挑号把新认领写进 `self._source_claim`，**直接覆盖旧值而不释放** →
旧号永久留在 `_CLAIMED_EMAILS` 里，此后再也选不到：

```
第 1 次挑 → a@x.ai（认领）
第 2 次挑 → b@x.ai（认领，覆盖 _source_claim）
release() → 只释放 b@x.ai；_CLAIMED_EMAILS 仍留 a@x.ai  ← 泄漏
```

候选池本来就浅（55 个只剩 3 个），一条泄漏足够把「还能用」变成「永远选不到」。

- 修复：接手新认领前先 `release_registration_resources()`。

## 修复前后对比（真实任务）

| | 修复前 | 修复后 |
|---|---|---|
| 等码 | `等待验证码超时 (60s)`，从未收到 | 15–60 秒内收到 |
| 来源选择 | 选中 `needs_sso` 半成品 → HTTP 400 | 跳过半成品 → 自动新建 |
| **成功率** | **0/27、4/100（0–8%）** | **3/3（100%）** |

## 测试与变异验证

| 测试 | 钉住的行为 | 变异验证 |
|---|---|---|
| `PickUnusedAwsSkipsHalfBakedSourcesTests` | 半成品不被选中 | 去掉 `_is_complete` → 变红 ✓ |
| `KiroOtpTimeoutFloorTests`（3 条） | 等码 ≥300、配置更大时尊重、AWS 同 | 去掉 floor → 变红 ✓ |
| `RepeatedPickMustNotLeakClaimsTests` | 连续挑选不泄漏认领 | 去掉 release → 变红 ✓ |

## 仍存在的限制（诚实说明）

1. **`needs_sso` 半成品不会被自动修复** —— 它们仍在库里，只是不再被当来源。
   要救它们需要走「补 SSO」流程（`needs_sso=True` 的语义就是待补）。
2. AWS 新号注册依赖 AWS 侧风控与接码，长期成功率仍受外部制约；
   本轮只证明了**本地链路不再自伤**。
3. `HTTP 400 Please try signing in again` 有多种成因（cookie 重复、csrf 不同步、
   会话过期），代码注释已记录三种。本次修的是「源本身没有凭据」这一种；
   其余仍按原样处理。
