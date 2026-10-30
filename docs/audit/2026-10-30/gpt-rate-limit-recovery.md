# GPT 限流 / 异常账号恢复 —— 日志分析（2026-10-30）

数据源：`data/account_manager.db`（1057 账号 / 837 任务记录 / 1661 条任务日志）
方法：只读 SQL + 逐条统计 `extra_json` 里的 `chatgpt_token_refresh` / `chatgpt_local` / `sync_statuses`

## 一、当前状态分布

| 状态 | 数量 | 占比 |
|---|---|---|
| `registered` | 557 | 52.7% |
| `banned` | 423 | 40.0% |
| `invalid` | 69 | 6.5% |
| `expired` | 8 | 0.8% |
| `limited`（限流） | **0** | **状态值根本不存在** |

## 二、423 个 banned 的成因（按 `chatgpt_token_refresh.message`）

| 数量 | 原因 |
|---|---|
| 385 | `新 AT 被服务端拒绝(401)；登录流程: TOTP 验证失败: 403 - You do not have an account because it has been deleted or deactivated` |
| 15 | message 为空，但 stamp 是 `ok/verified/refreshed=True` |
| 13 | `缺 session_token 和 refresh_token；登录流程: 403 ...deleted or deactivated` |
| 8 | `OAuth token 刷新失败: HTTP 401；登录流程: 403 ...` |
| 1 | `只返还原本的 AT；发送 OTP 失败: 409 invalid_state` |
| 1 | `新 AT 被拒绝(401)；OTP 验证失败: 403 ...` |

**结论：385+13+8+1+1 = 408 条都有 OpenAI 的原文 `deleted or deactivated` —— 是真封禁**，判定正确。
补充验证：423 个 banned 的 AT **全部已过期**（`exp=0` 或 `exp<now`），且 423/423 都带 `totp_secret`
（说明登录链确实走到了 TOTP 那一步才被 403，不是提前放弃）。

## 三、发现的问题

### P0-1 「限流」没有状态位 —— 47 个账号的限流信息被丢掉了

- `services/panel_status_sync.py:85` 把面板的「限流 / limited」映射成 `limited`，
  但 `core/base_platform.py` 的 `AccountStatus` **只有 registered / expired / invalid / banned**。
- 实测：47 个账号的 `extra_json` 里带 `quota` / `limited` / `429` / `限流` 痕迹，
  但 `status` 列里没有一个是 `limited` —— 这些信息只活在 extra 里，界面筛不出来、
  恢复队列也排不进去。
- 后果：额度耗尽的号被当成「正常」，或者被同步写成 unknown，**既不复也不能筛**。

### P0-2 「异常账号尝试恢复」实际从未覆盖 65 个 invalid 账号

- 69 个 `invalid` 里 **65 个没有 `chatgpt_token_refresh` stamp** —— 一次恢复都没试过。
- 它们**都有 AT/RT/ST**（不是缺凭证），只是凭证被拒。`RETRY_STATUSES` 包含 `invalid`，
  但恢复依赖 `chatgpt_auto_refresh_enabled`，而配置里它是 **`false`**。
- 后果：用户要的「异常账号尝试恢复」在默认部署下**完全没跑**。

### P0-3 「不成功则禁用」没有实现 —— 尝试 3 次后静默停手，状态还是 invalid

- `services/chatgpt_maintenance.py:238` `record_auto_refresh_result`：连续 3 次失败只置
  `disabled: True`（「本 AT 周期内不再自动尝试」），**`status` 一个字节都不改**。
- 实测证据：`MAX_ATTEMPTS=3`、`_RETRY_BACKOFF_SECONDS=(1h,6h)`，第 3 次后 `disabled=True`，
  账号永远停在 `invalid`，界面上看不出「已经放弃」。
- 后果：用户看到的是「失效」而不是「已禁用」，分不清「还能救」和「救不回来了」。

### P1-1 banned 的「强证据恢复」不生效 —— 14 个账号探测确认可用却仍标禁用

- `services/chatgpt_account_state.py:260` 的正向恢复只对 `{expired, invalid}`：
  ```python
  if usable or probe_confirms_usable(local_probe) or remote_usable:
      current = ...
      if current in {"expired", "invalid"}:     # ← banned 不在里面
          setattr(account, "status", "registered")
  ```
- 实测：14 个 banned 账号的 `chatgpt_local` 探测结果是 `auth=access_token_valid` +
  `codex=usable`（2026-10-26 探的），而它们的 stamp 停在 2026-10-19/10-20 ——
  **探测发生在刷新之后、且确认可用，状态却仍是 banned**。
- 设计上「banned 是粘性、不因一次可用探测复活」是对的（防止弱信号洗掉封禁），
  但 `access_token_valid` + `codex=usable` **双证据**不该算弱信号。

### P1-2 `limited` 语义缺失带来的连带问题

- `AccountStatus.is_active()` 白名单只有 `registered` → limited 号会被判「不可用」（对）
- 但 `counts_as_registered()` 黑名单只有 `invalid/expired` → limited 号算「已注册」（对）
- 结论：只要加上 `LIMITED` 枚举值 + 前端文案，语义自动正确，但**必须同时确认**它进恢复队列。

### P2-1 恢复任务默认关闭，用户看不到「有没有在恢复」

- `chatgpt_auto_refresh_enabled = false`、`chatgpt2api_auto_sync_enabled = false`。
- 这是「用户没开」而不是 bug，但**恢复能力默认不生效**，与用户「异常账号尝试恢复」的
  期望相反。建议：把恢复与同步拆成两个开关，恢复默认开。

## 四、任务日志里的失败分布（最近 200 条 failed）

| 数量 | 关键词 |
|---|---|
| 86 | `deactivated` |
| 67 | `401` |
| 35 | `invalid_state` |
| 4 | 超时 |
| 7 | 其它（补 RT 各种断点） |

`invalid_state`（35 条）是可重试的（会话/state 不同步），**不能**判封禁 ——
`banned_signals.py` 的 docstring 已经写明这一点，判定正确。

## 五、改进清单（按优先级）

| # | 改动 | 依据 |
|---|---|---|
| 1 | 新增 `limited` 状态（枚举 + 前端文案 + 筛选） | P0-1：47 个号的限流信息没有落点 |
| 2 | `limited` 进恢复队列（额度会重置，可救） | P0-1/P1-2 |
| 3 | 恢复用尽 MAX_ATTEMPTS → 落 `banned`（「不成功则禁用」） | P0-3：现在只置 disabled 不改状态 |
| 4 | banned 允许**双证据**恢复（auth 有效 + codex usable） | P1-1：14 个号探测可用却仍禁用 |
| 5 | 恢复开关独立、默认开 | P2-1 |
