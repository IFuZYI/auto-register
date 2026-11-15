# 邮箱功能鲁棒性增强（2026-11-15）

## 出发点：证据，不是猜测

先盘点了 5 个渠道是否都有容错钩子：

| 渠道 | `is_transient` | `max_consecutive_errors` | `is_fatal_busy` |
|---|---|---|---|
| outlook / microsoft / mail_import | ❌ | ❌ | ❌ |
| icloud_local | ✅ | ✅ | ✅ |
| icloud_remote | ✅ | ✅ | ✅ |

三个 Outlook 渠道**一个都没有** —— 而生产 `mail_provider = icloud_remote`，
Outlook 池是回填/备用的主要来源。这就是最大的结构性缺口。

## 修了什么（6 项，每项带能先失败的测试 + 变异验证）

### 1. OAuth 失败不再「一种表情」

`probe_oauth_availability` 之前对**所有**失败都只回 `ok=False`（除
`service_abuse_mode`）→ 上层统一走「回退密码登录」。而回填号通常**没有密码**
（只有 client_id/refresh_token），于是直接失败、连轮询都不进。

- 新增 `_is_permanent_oauth_error(body, status)`：先看**错误码**
  （`invalid_grant`/`revoked`/`invalid_client` = 永久），再看状态
  （408/429/5xx = 可重试；401/403 = 永久）。
- `probe` 返回带 `retryable`；新增 `OutlookOAuthError(retryable=...)`；
  经 `_fetch_oauth_token_bundle` 透传到上层。
- **一次反向变异教会我的事**：最初把 4xx 一律判永久，测试仍全绿 ——
  因为 400+invalid_grant 与 400+invalid_request 的结论**恰好相同**。
  补了「同状态、不同错误码必须给不同结论」的对照后，双向变异都能捕获。

### 2. 瞬时 token 失败不再一次定生死

`OutlookGraphMailboxBackend.wait_for_code` 在进入轮询**之前**先取 token，
一次 503 就在这里抛出去 —— 连一次轮询都没发生，而轮询本身每 5 秒会重试。

- 改为：预检遇**可重试**失败只记日志并继续进轮询；**永久**失败仍然立刻抛。

### 3. 顺带修掉一个放大器级的缺陷：预检失败会降级到 IMAP

`_oauth_backend_capability` 只在**拿到 token 成功后**才被写入。预检失败时它没被写
→ 代码看到空串就「自动切换 IMAP」→ 而 OAuth-only 账号**没有密码**，IMAP 的
`AUTHENTICATE` 必然失败。**一次瞬时 503 被放大成「这个账号坏了」。**

- 改为：只有 `precheck_ok` 为真时才允许降级。

### 4. 轮询层真正拿到容错能力

三个 backend 的 `_run_polling_wait` 都接上 `is_transient=_is_transient_poll_error`
与 `max_consecutive_errors=4`（与 iCloud 同一口径）。

**但接上钩子还不够**：这些 `poll_once` 内部都 `except ... continue`，把异常吃掉了，
`_run_polling_wait` 根本看不到 —— 预算形同虚设。实测：60s 时限里干等 62s。

- 改为：记录 `last_error`，**整轮所有文件夹都失败**时把错误抛回轮询层。
- 实测改善：同样的持续故障，从「陪跑 62s」变成「约 15s 放弃」。

### 5. `in_use` 泄漏有兜底了

领取时标 `in_use` + `last_used`，复位只发生在正常收尾。进程被 kill / 任务在
收尾前被 stop / 代码抛在复位之前 → 这个号**永久**停在 `in_use`
（`find_claimable` 的候选状态从不包含它）。iCloud 有 2 小时租约兜底，Outlook 没有。

- 新增 `OutlookRepository.release_stale_claims(lease_hours=2)`。
- 接在 `_pop_account()` 取号前（与 `icloud_remote.py:135` 同一位置）。
- 租约内的号**不动**（有测试钉住，否则会出现同号被领两次）。

### 6. 门面导出补齐

新增的 `OutlookOAuthError` 要经 `core/mailboxes/channels/outlook/__init__.py`
和 `core/base_mailbox.py` 两处导出 —— 仓库有闸门测试
（`test_mailbox_facade_reexports_every_public_name`）先抓到了漏项。

## 变异验证记录

| 修复 | 变异方式 | 结果 |
|---|---|---|
| OAuth 分类（错误码） | 忽略 `_PERMANENT_OAUTH_MARKERS` | 4 条变红 ✓ |
| OAuth 分类（4xx 回退） | 4xx 一律永久 | 2 条变红 ✓ |
| 预检可重试 | 预检失败一律抛 | 1 条变红 ✓ |
| 预检成功才降级 | 无条件降级 | 1 条变红 ✓ |
| 轮询错误预算 | IMAP 继续吞异常 | 61s 后变红 ✓ |
| `in_use` 回收 | 永不回收 | 1 条变红 ✓ |

## 诚实的边界

1. **Outlook 的 `poll_once` 仍会吞掉「部分文件夹失败」**：只有一个文件夹读不到时
   不算整轮失败（这是对的，其余文件夹可能还有信）。只有**全部**失败才上报。
2. **`mailbox_otp_timeout_seconds = 60` 这个生产配置本身没改**。Kiro/AWS 已有 300
   下限兜底；iCloud/Outlook 会按配置走 60 秒。要调大是运维决策，不是我该替用户改的。
3. **没有实测真实的 Outlook 收信**：本轮验证全部走本地测试与注入故障，
   没有向真实微软端点发请求。真实链路的延迟特征仍需线上观察。
