# 事故笔记索引

293 条带实测数据的注释，按主题归并成 12 篇。**重构前必须读完对应的篇目** ——
这些分支看起来冗余，每一条都是真实故障换来的。

抽取脚本见 [PROMPT-agent.md](./PROMPT-agent.md) 的 T0.1；`_index.json` 是原始行号索引。

---

## 1. Cloudflare 按浏览器家族封禁

**结论**：`warmup` 拿不到 `oai-did` 就必须失败退出，继续走必然 409。重试要**换家族**，不是换 IP。

**出处**
- `platforms/chatgpt/protocol/auth_flow.py:328-386`（26 轮 / 40+ IP 的对照表）
- `auth_flow.py:362-373`（`chrome136/142/146` 全 403；`mac_safari` / `ios_safari` / `firefox133` 全 200）
- `platforms/chatgpt/protocol/fingerprint.py`（`cross_family_impersonates`）

**重构约束**：不许把 4 次重试降到 1 次；不许把「换家族」改成「只换 IP」；判据必须保持「cookie jar 里有 `oai-did`」而不是 `status_code == 200`。

---

## 2. add-phone 墙

**结论**：Codex OAuth 拿 RT 时可能被要求先绑手机号。没配接码时这条路径必死，只能回落登录路线。

**出处**
- `platforms/chatgpt/protocol/mixins/codex.py:336-343`（`_codex_drive_login_from_log_in` 的 add-phone 分支）
- `codex.py:450-470`（直接落 `/add-phone` + SMS 绑号后重新 authorize）
- `platforms/chatgpt/registration_engine.py:155-177`（`_note_missing_refresh_token` 的解释）
- `platforms/chatgpt/protocol/mixins/add_phone.py`（429 行，接码唯一入口）

**重构约束**：`_sms_callback is None` 时必须能干净跳过，不许抛；这条分支的存在解释了「为什么 refresh_token 为空但账号可用」。

---

## 3. OTP 防串号

**结论**：一轮流程会发多封码值相同的邮件。必须同时用「发码时间窗 + 邮件 id 基线 + 排除已试码」三重过滤。

**出处**
- `auth_flow.py:635-694`（`otp_sent_at` 必须在发码**之前**取；401 重试要 `exclude_codes`）
- `core/mailboxes/base.py:94-129`（剥邮箱地址 → 语义正则 → 边界兜底，排除 `#202123` 与 `u24681357`）
- `protocol/mailbox_adapter.py:90-105`（`prime()` 打 id 基线）
- `protocol/mixins/signup.py:447-490`（已有账号只能 `resend`，`send_otp` 会让旧码失效）

**重构约束**：`issued_after` / `before_ids` / `exclude_codes` 三个参数一个都不能省；`prime()` 在第二段流程（绑 2FA）前必须重打。

---

## 4. client_id 必须跟着 RT 走

**结论**：两条 RT 路线用不同 `client_id` + `redirect_uri` + token 端点。刷新时用错 client 会得到 `401 invalid_client`，看起来像「RT 失效」。

**出处**
- `platforms/chatgpt/protocol/mixins/platform_rt.py:5-11`（两条路线对照表）
- `platforms/chatgpt/token_refresh.py:150-169`（Codex client → 401；platform client → ✅）
- `platforms/chatgpt/chatgpt_registration_mode_adapter.py:371-381`（663 个账号 0 个有 client_id 的事故）

**重构约束**：`client_id` 属于 `Credentials`，必须随 RT 一起落库、一起加密、一起导出；不许在代码里给默认值兜底（`app_EMoamEEZ73f0CkXaXp7hrann` 只允许出现在「构造 Codex authorize」那一处）。

---

## 5. 刷新的三个正交标志

**结论**：`success` / `verified` / `refreshed` 表达三件不同的事，缺一个就会误判。

**出处**
- `token_refresh.py:36-73`（三个字段的完整定义与各自的事故）
- `platforms/chatgpt/plugin.py:444-470`（三个兜底触发条件）
- `plugin.py:527-552`（落库字段与 `refreshed` 强制为 False）

**重构约束**：不许合并成一个 bool；`verify_access_token` 必须与 `validate_token` 分开（网络故障 ≠ 令牌失效）。

---

## 6. 代理粘性绑定

**结论**：同一账号反复走不同出口 IP 会被判异常登录。复用时要回到出生 IP；原代理不可用才换。

**出处**
- `core/proxy_pool.py:96-152`（`resolve_for_account` 的完整决策表）
- `proxy_pool.py:154-165`（住宅代理活着却 `is_active=0` 的事故）
- `services/task_runners.py:452-501`（`_resolve_proxy_for_account` + `_persist_register_proxy`）

**重构约束**：`fallback_provider` 必须是惰性 callable（`get_next()` 有推进游标的副作用）；「原代理在池外」视为可用。

---

## 7. 邮箱只对「用掉它的那个平台」一次性

**结论**：`used` 不是全局锁。同一个地址注册过 ChatGPT 仍可注册 Grok。

**出处**
- `services/icloud/aliases.py:110-143`（`claim_alias` 的筛选规则）
- `aliases.py:232-244`（`mark_alias_used` 的单向性）
- `core/mailboxes/channels/outlook/mailbox.py:140-161`（按平台排除已注册）

**重构约束**：`used_platforms` 的 `,grok,chatgpt,` 格式不许改；`_registered_platforms_for` 的跨库比对在分库塌缩后可以改成 SQL，但语义不能变。

---

## 8. 状态判定优先级与粘性

**结论**：禁用 > 过期 > 失效。`banned` 是粘性的，弱信号不得降级；只有 `expired` / `invalid` 能恢复为 `registered`。

**出处**
- `services/chatgpt_account_state.py:189-289`（`apply_chatgpt_status_policy` 全量规则）
- `platforms/chatgpt/status_probe.py:394-561`（探测态的产生）
- `services/chatgpt_account_state.py:101-140`（`classify_local_probe_state` 的 12 个分支）

**重构约束**：`configuration` 类错误（`invalid_client`）**不动状态** —— 那是我们的配错，不是号坏。

---

## 9. 分库的跨库陷阱

**结论**：SQLite 不跨库解析表名。`accounts` 在默认库、`outlook_accounts` 在池库，写成子查询直接 500。

**出处**
- `core/mailboxes/channels/outlook/mailbox.py:140-161`（`no such table: accounts` 事故）
- `core/db/registry.py:99-101`（`_lock` 非重入，锁内调 `engine_for` 自死锁）

**重构约束**：塌缩成单库后这两类问题消失，但 `platform_session()` 的**调用签名**要保留一个版本，避免 60 处调用点同时改。

---

## 10. 任务必须能被停止

**结论**：等码是整条链最长的一段。停止/跳过必须在 0.25 秒内生效。

**出处**
- `core/task_runtime.py`（`StopTaskRequested` / `SkipCurrentAttemptRequested` / `finish_attempt`）
- `core/mailboxes/base.py:34-74`（`_checkpoint` + 0.25s 切片）
- `services/task_runners.py:503-514`（`_wait_turn` 的 checkpoint 循环）

**重构约束**：任何新增的轮询/等待必须走 `_sleep_with_checkpoint` 或 `_run_polling_wait`，不许裸 `time.sleep(长秒数)`。

---

## 11. 2FA 密钥只下发一次

**结论**：TOTP 密钥在 enroll 响应里只出现一次。账号行此时还不存在，所以必须先写文件日志。

**出处**
- `platforms/chatgpt/registration_engine.py:416-441`（`_journal_secret` 的理由）
- `services/chatgpt_two_factor.py:237-267`（`_write_secret_journal`：原子替换 + fsync + 0600）
- `chatgpt_two_factor.py:301-316`（恢复后 `path.unlink`）

**重构约束**：journal 必须加密（阶段 1 T1.5）；**不许**改成「直接写 DB」—— 注释里记录了绑定期间 DB 写会 `database is locked`。

---

## 12. 面板的方向语义

**结论**：推送 / 拉回 / 登记是三个不同动作，历史上都叫「同步」，是排查时最容易走错方向的混淆源。

**出处**
- `services/panel_sync.py:1-24`（方向规则：远端较新才拉回）
- `services/panel_registry.py:13-28`（新增面板要同步注册的 4 处）
- `docs/adr/0002-panel-enrollment-and-bidirectional-sync.md`
- 根 `GLOSSARY.md`

**重构约束**：新增面板必须走 `PanelAdapter`（阶段 3 T3.3），不许再往 `FETCHERS` 里加分支。
