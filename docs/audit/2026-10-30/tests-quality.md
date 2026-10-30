# tests-quality 审计

[MODE: SANDBOX]

- 审计对象：`/data/workspace/Register`（Python 3.12，pytest 9.1.1）
- 审计基线：起点 `4680c8d`，审计过程中 HEAD 被并行提交推进到 **`c93b71f`**（`fix(auth): 堵住 setup 抢注接管 …`）。本文行号以 `c93b71f` 为准；期间被上游修掉的瞬时问题会单独标注。
- 口径：只读审计。除本文件外未改动任何仓库文件；验证用的 mutation 插件放在仓库外的 scratch 目录。
- 规模：`tests/` 下 **224 个 `test_*.py`**、AST 统计 **3043 个 test 函数**、pytest 收集 **3107 条**（含参数化/subTest），`--collect-only` 4.6s。

## 0. 结论摘要

| 维度 | 结论 |
| --- | --- |
| 关键路径兜底 | 六条主链都有测试文件，但**注册协议链、RT 的两条执行函数、平台登录的邮箱 OTP 分支、任务停止端点、通用批量动作 runner** 只有「源码字符串断言」或「只测端点不测执行体」——见 §1 |
| 钉错误行为 | 存在把「已知盲区/旧契约」写成硬断言的用例（`test_panel_comparison.py:801` 最典型）；审计中还抓到一条安全加固前的旧契约测试（已在上游 `c93b71f` 修掉） |
| 脆弱测试 | 真实 `sleep`/墙钟、模块级单例（`_task_store`、scheduler `_jobs`、logging handler、config_store）、**全 session 共享一个 SQLite**（74 个文件碰库，仅 3 个文件自建隔离库）、源码切片断言 |
| 断言强度 | 36 条「只断言 status_code」、17 条「零断言」、1226 条「单断言」；其中若干条 **500/404 也能过** |
| 运行时间 | 绝大多数逻辑测试毫秒级；**5 个文件吃掉约 100s+**（npm lint 26s、tsc 25s、PBKDF2 600k 约 24s、真实 sleep 6.4s、嵌套 pytest 收集 8s） |
| 假通过风险 | 6 处「把被测逻辑本身替掉」：`hasattr` 当测试、`404` 也算通过、源码字符串断言（mutation 实测：注入历史 bug 后 109 条仍全绿） |

**建议的修复顺序（P0）**：给 `AuthFlow.run_register` 补一条 `FakeHttp` 驱动的端到端测试 → 给 `platform_rt` 的 `email_otp_verification` 分支补行为测试 → 把 `_run_codex_rt` / `_run_login_rt` 的源码断言换成行为断言 → 补 `/tasks/{id}/stop`、`/skip-current` 端点测试 → 补 `_run_probe_status` / `_run_batch_action` runner 测试 → 补 `looks_like_proxy_error` + 任务时代理换绑分支。

---

## 1. 关键路径覆盖矩阵

### 1.1 注册链路

| 环节 | 覆盖文件（test 数） | 实际测到什么 |
| --- | --- | --- |
| `ChatGPTPlatform.register` 接线 | `tests/test_chatgpt_plugin.py`（6） | 邮箱/密码/`_log_fn` 透传；**adapter 被 `_RecordingAdapter` 整个替换**（`tests/test_chatgpt_plugin.py:25,67`），插件从未走到引擎 |
| 引擎结果映射 / 抢救 / 2FA 钩子 | `tests/test_chatgpt_registration_engine.py`（32） | `AuthFlow` 被 `mock.patch(..., return_value=flow)` 整个替换（`:235-239`），只验证 result 映射与 `_salvage` 分支 |
| 任务层：重试 / 停止 / 断管 / 进度 | `tests/test_register_task_controls.py`（18）、`tests/test_environment_preflight.py` | 真实 `_run_register` + 假平台；覆盖「整轮重试」「0 轮」「BrokenPipe」 |
| 落库 → 列表 → 导出 | `tests/test_end_to_end_flows.py`（3） | 真实任务 + 真实 DB + 真实 API，但 `core.registry.get` 被换成 `_E2EPlatform`（`:109,158`）——**没有一行 ChatGPT 协议代码** |
| 协议链顺序/守卫 | `tests/test_registration_chain_ordering.py`（16）、`tests/test_rt_route_reaches_protocol_flow.py`（11） | 全部是 `inspect.getsource` + 字符串/切片断言，见 §2.2 |

**缺口（点名）**

- **`AuthFlow.run_register`（`platforms/chatgpt/protocol/auth_flow.py:567`）从未被任何测试执行。** 全仓 `grep "run_register("` 只命中 mock 的 `return_value/side_effect`（`tests/test_chatgpt_registration_engine.py:244,265,277,291`）与源码读取（`tests/test_registration_chain_ordering.py:41-44`）。这条 ~400 行的注册主链（signup → sentinel → OTP → 设密码 → callback → RT）没有任何行为兜底。
- **`tests/route_harness.py`（437 行、`FakeHttp`/`FakeResponse`/`assert_account_persisted`）只有自测在用。** `grep -rl route_harness tests/*.py` → 仅 `tests/test_route_harness.py`；其 docstring 明说「Task 2-7 复用」，但没有任何线路测试接入（`tests/route_harness.py:1-30`）。这套夹具正是为解决「2978 条全绿时注册仍在失败」而写的，现在等于建好没通电。
- `platforms/chatgpt/plugin.py:118` 的 `register()` 只被 adapter 替身覆盖；`create_application_platform_registration_service(...).register_chatgpt(...)`（`plugin.py:148-153`）这条真实装配链没有测试。

**建议**：用 `route_harness.FakeHttp` 驱动 `AuthFlow.run_register` 跑一条 happy path + 一条 OTP 超时 + 一条 RT 交换失败；断言最终 `AuthResult`、账号落库（`assert_account_persisted`）与「日志无明文凭证」（`assert_no_secret_leak`）。这条测试的价值高于再补 50 条源码断言。

### 1.2 RT 三条路线

按仓库实现拆成三条：① Codex OAuth、② platform 密码登录、③ 会话复用。

| 路线 | 覆盖文件 | 实际测到什么 |
| --- | --- | --- |
| ① Codex 交换 | `tests/test_chatgpt_registration_engine.py:396-445` | 只执行到 `_build_codex_authorize` 抛错为止（抢跑名额逻辑），**authorize → callback → `/oauth/token` 全程未跑** |
| ① Codex（注册链内选路） | `tests/test_chatgpt_platform_rt_route.py:304-343` | `_run_codex_rt` / `_run_login_rt` 被 lambda 替身替换（`:314-315`），只验证「选哪条」 |
| ② platform 密码登录 | `tests/test_chatgpt_platform_rt_route.py:130-245` | 真实 `acquire_rt_via_platform_login` 编排，但 `authorize_continue`/`login_password_verify`/`submit_mfa_totp` 全部打桩；URL/POST 参数另有 2 条记录型测试 |
| ② 的邮箱 OTP 分支 | **只有源码断言**：`tests/test_registration_chain_ordering.py:234-280` | `assert "wait_for_otp" in src or "verify_otp" in src` —— 注释里出现这两个词也算过 |
| ③ 会话复用 | `tests/test_chatgpt_rt_backfill.py:102-210` | 真实 `RefreshTokenBackfiller` + `_FakeFlow`（自写替身） |
| RT 续期（刷新 Token） | `tests/test_chatgpt_token_refresh_verification.py`（51） | 真实 `TokenRefreshManager` + FakeSession；登录兜底用 `AuthFlow` mock |

**缺口（点名）**

- **`platform_rt` 的 `email_otp_verification` 分支（`platforms/chatgpt/protocol/mixins/platform_rt.py:232-262`）没有行为测试。** 这正是 2026-10-28 真实事故（AT/session 有、RT 空、`partial=True`）的修复点；现在唯一的守卫是 `test_platform_rt_waits_for_email_otp` 的字符串包含。
- **`_run_codex_rt`（`platforms/chatgpt/registration_engine.py:289`）/ `_run_login_rt`（`:302`）没有行为测试。** 前者是 2026-10-28「`self.mail_provider` 拼错 → AttributeError 被吞 → Codex 换 RT 从未执行」的现场；后者是 2026-10-30「漏传 mail_provider → 撞验证码必然失败」的现场。两者的守卫都是 `assertIn("self.mailbox", src)`（`tests/test_registration_chain_ordering.py:323,332`）。
- `oauth_codex_rt_exchange`（`platforms/chatgpt/protocol/mixins/codex.py:377`）的真实授权/换 token 段无测试；`test_chatgpt_registration_engine.py` 只覆盖前置守卫。

**mutation 证据（本次实测）**：把 `_run_codex_rt` / `_run_login_rt` 改成「保留 `self.mailbox` 字面量、实际传 `mail_provider=None`」，再把 `acquire_rt_via_platform_login` 的 `mail_provider` 强制置 `None`（等价于 OTP 分支失效），跑 `test_chatgpt_registration_engine.py + test_chatgpt_platform_rt_route.py + test_registration_chain_ordering.py + test_chatgpt_rt_backfill.py` → **109 passed**。即：三条 RT 路线同时坏掉，现有测试仍然全绿。

**建议**：
1. `tests/test_chatgpt_platform_rt_route.py` 增一条 `page_type=email_otp_verification` 的编排测试（`mail_provider.wait_for_otp` 返回码 → `verify_otp` → `_exchange_platform_code` 被调、code 不被重复消费）。
2. 用 `FakeHttp` 给 `oauth_codex_rt_exchange` 补一条「authorize 302 → callback code → token 200」的端到端。
3. `_run_codex_rt` / `_run_login_rt` 用 `mock.Mock()` flow 断言 `mail_provider is self.mailbox`（行为断言，替换 `getsource`）。

### 1.3 补 RT

| 环节 | 覆盖文件 | 结论 |
| --- | --- | --- |
| 策略顺序 / 跳过原因 / 中断 | `tests/test_chatgpt_rt_backfill.py:102-210`、`tests/test_chatgpt_platform_rt_route.py:346-438` | 较好；含「缺 totp 不再跳过」「缺密码跳过」「中断不落到下一策略」 |
| 协议开关（session 去 prompt、login 只要 RT、OTP 超时） | `tests/test_chatgpt_rt_backfill.py:286-346` | 较好；`overrides[0]` 取第一条策略（`:345`），顺序变了会静默测错对象（见 §3.5） |
| 结果落库 / partial 清理 | `tests/test_chatgpt_rt_backfill.py:348-405`、`tests/test_chatgpt_platform_rt_route.py:441-490`、`tests/test_chatgpt_rt_backfill.py:449-514` | 覆盖 `apply_backfill_result`、partial 标记清理、`_apply_action_result` |
| 任务 API + runner + 停止开关 | `tests/test_backfill_rt_task_api.py`（12） | 真实 `_run_backfill_rt`（TestClient 后台任务），引擎 `backfill_account_data` 被 mock —— 合理接缝 |
| 真实 `backfill_account_data` 装配 | `tests/test_chatgpt_rt_backfill.py:407-447` | 只测「mailbox 解析后传给 engine」，engine 是 Mock |

**缺口**：`RefreshTokenBackfiller.run()` 的**并发/多线程**路径（`protocol_log_relay` 的线程隔离只测了日志镜像，没测多号并发时 `_task_control`/`attempt_id` 是否串号）；`backfill_account_data` 里 `MailboxUnavailableProvider` 的真实等待行为只在 `_FakeFlow` 层验证。

### 1.4 代理绑定 / 均衡

| 环节 | 覆盖文件 | 结论 |
| --- | --- | --- |
| 标签解析 / `get_next(tag)` / 回退 | `tests/test_proxy_tags_balance.py:63-88` | 有；回退用例只断言「非空」（见 §4.3） |
| 绑定规则（保留 bind、无标签换、已删换、空绑定给一个） | `tests/test_proxy_tags_balance.py:91-129` | 4 条，行为级 |
| 负载均衡 / 上限 / dry-run | `tests/test_proxy_tags_balance.py:131-210` | 3 条；读全局账号表（见 §3.4） |
| 账号侧复用（`resolve_for_account`） | `tests/test_account_proxy_reuse.py:45-191` | 19 条，行为级，质量较好 |
| 端点（probe 时补绑/换死绑） | `tests/test_account_proxy_rebind_endpoints.py`（4） | 行为级 |
| 基础工具 | `tests/test_proxy_utils.py`（8）、`tests/test_proxy_pool.py`（3）、`tests/test_proxy_record.py`、`tests/test_proxy_repository_backends.py` | 归一化/脱敏/仓储 |

**缺口（点名）**

- **`looks_like_proxy_error`（`core/proxy_utils.py:224`）零测试。** 它是「当场摘代理 + 换绑」的唯一闸门；判据写错会误摘好代理或漏判死代理。`tests/test_proxy_utils.py` 只覆盖 `redact/normalize/build_*`。
- **任务时代理异常自动换绑分支（`services/task_runners.py:588-604`）零测试。** `report_proxy_error` 只在 `tests/test_proxy_tags_balance.py:212` 被直接调用；`rebind()` 与「old → new」日志从未在任务上下文里跑过。
- 最新提交 `4680c8d` 的说明写「测试：test_proxy_tags_balance.py 14 条」，但这 14 条**没有一条**覆盖第 3 节「任务时代理异常 → 换绑」，属于提交信息与测试范围不符。

### 1.5 面板推送

| 环节 | 覆盖文件 | 结论 |
| --- | --- | --- |
| 方向判定（plan_push） | `tests/test_panel_push.py`（16） | 强：未上传/本地新/远端新/同小时/时区/无凭证/封禁 |
| 拉取方向（plan_pull） | `tests/test_panel_sync.py`（18） | 强，含 `iat` 秒级判定 |
| 端点编排 / 删除旧记录 | `tests/test_panel_push_endpoint.py`（8）、`test_panel_sync_endpoint.py`（6） | 行为级；上传/删除函数被 mock |
| 推送结果落库 | `tests/test_panel_push_persist.py`（5） | 行为级 |
| 自动上传（注册后） | `tests/test_external_sync_contribution_mode.py`（7）、`tests/test_chatgpt2api_panel.py:636+` | 覆盖开关与 payload |

**缺口**：`delete_chatgpt2api_account` / `delete_sub2api_account`（`services/panel_push.py:252,293`）只被 `mock.patch`（`tests/test_panel_push_endpoint.py:208,246`），它们的 URL 拼接、鉴权头、非 200 处理、响应解析**从未执行**。`_push_account_uploader`（`panel_push.py:353`）同理，真实 uploader 只在各平台自己的测试里单测。

### 1.6 任务控制

| 环节 | 覆盖文件 | 结论 |
| --- | --- | --- |
| `RegisterTaskControl`（stop/skip 语义） | `tests/test_task_runtime.py:11-50` | 行为级 |
| 邮箱等待循环被 stop/skip 打断 | `tests/test_mailbox_task_control.py`（2） | 行为级，但都是「先置位再进循环」 |
| 整任务重试 / 停止 / 断管 | `tests/test_register_task_controls.py`（18） | 行为级 |
| 任务存储 / 孤儿收尾 / 409 守卫 | `tests/test_task_store_io.py`（38，自带隔离库） | 行为级 |
| 任务可见性 / 类型标签 | `tests/test_task_kinds_and_visibility.py`（12） | 行为级 |

**缺口（点名）**

- **`POST /tasks/{task_id}/stop`（`api/tasks.py:626`）与 `/skip-current`（`:615`）没有任何测试调用。** 全仓 `grep "skip-current\|/stop"` 在 `tests/` 下 0 命中；只有 `_task_store.request_stop()` 的进程内测试。
- **没有「运行中停止」的用例。** 现有 stop 用例都是任务启动前先置位（`tests/test_register_task_controls.py:333-350`），无法证明「注册进行到一半点停止，任务在账号间隙停下并标记 stopped」。
- `_ensure_task_mutable` 的 409 分支有测试（`tests/test_task_store_io.py:489-515`），但端点层的「已结束任务 → 409」未接线验证。

### 1.7 凭据加密

| 环节 | 覆盖文件 | 结论 |
| --- | --- | --- |
| 字段级加解密 / 幂等 / 明文兼容 | `tests/test_credential_crypto.py`（11） | 强：落盘无明文、读回明文、老明文可读、开关 |
| 仓储落盘（`save_account` → `extra_json`） | `tests/test_chatgpt_two_factor_persistence.py:98-110` | 有（totp_secret 密文 + 读回明文） |
| 密码列 | `tests/test_account_password_serialization.py`（5） | 强：DB 密文、响应明文、`password_cipher` 不泄露 |
| 配置密钥 | `tests/test_config_secret_encryption.py`（6） | 有 |
| TOTP 待写入日志 | `tests/test_totp_journal_crypto.py`（3） | 有（journal 明文/恢复/不可解密保留） |
| 密钥文件权限 | `tests/test_chatgpt_two_factor_persistence.py:325-326` | 只覆盖 TOTP journal 的 0600；凭据密钥文件本身没有 |
| 列表/日志不泄露 | `tests/test_list_payload_no_secrets.py`、`tests/test_no_credential_in_logs.py`（2） | 见下 |

**缺口（点名）**

- **密钥文件 0600 没有断言。** `core/secret_box.py:85` 用 `os.open(..., 0o600)` 创建密钥，但没有任何测试检查 `stat().st_mode`（对比：TOTP journal 有，`tests/test_chatgpt_two_factor_persistence.py:325`）。
- **「密钥被换掉」时 API 的行为无测试。** `SecretBox.decrypt` 抛 `InvalidTag`，`decrypt_text` 包成 `CredentialKeyError`（`core/credential_crypto.py:87-92`）；此时 `GET /api/accounts` 是 500 还是有可读错误，没有用例。`tests/test_docker_credential_key_persistence.py:68` 只断言原语会抛。
- **`test_no_credential_in_logs.py:46-68` 名不副实**：它只正则扫「凭据变量切片」（`token[:20]`），不检测 `logger.info("token=%s", token)` 这种整串打印，也不检测 `%r`、`repr()`、异常消息里带 token。docstring 写「日志里不许出现凭据（哪怕是前缀）」，实际契约窄得多。

---

## 2. 钉「错误的行为」的测试

### 2.1 把已知盲区写成硬契约（最典型）

`tests/test_panel_comparison.py:801` `test_the_blind_spot_would_have_been_reported_as_synced`

```python
state, _ = compare_credentials(
    {"refresh_token": "RT-SAME"},                      # 本地只有 RT
    {"access_token": "AT-DIFFERENT", "refresh_token": "RT-SAME"},
)
self.assertEqual(state, "synced")                      # ← 把「漏读列上的 AT」判成已同步
```

- **证据**：`:810-811` 的注释自己写着「现状（只读 extra）确实是 synced —— 所以必须靠上面的合并来避免」。这条断言把 `compare_credentials` 的**信息缺失当 synced** 固化成了契约。
- **危害**：任何把「本地缺该字段」改成 `unknown` / `credential_diff` 的安全修复（这正是防「死凭证被当成已同步」的方向）都会让这条测试变红，reviewer 会以为改坏了。
- **建议**：改成 `assertIn(state, {"synced", "unknown"})` 并把「盲区」作为注释；真正的契约由上面的「合并列后判出 credential_diff」承担。

### 2.2 源码字符串断言：既钉实现，又放过 bug

`tests/test_registration_chain_ordering.py` 全文件 16 条测试，**全部基于 `inspect.getsource`**（13 处 `getsource` 调用 + 3 个 helper）。

| 用例 | 断言 | 问题 |
| --- | --- | --- |
| `:323` `test_codex_rt_gets_the_mailbox` | `assertIn("self.mailbox", getsource(_run_codex_rt))` | 注释/死代码里出现该串即通过；传 `None` 也通过（本次 mutation 实测） |
| `:332` `test_login_rt_gets_the_mailbox` | 同上 | 同上 |
| `:306` `test_engine_has_no_phantom_mail_provider_attribute` | 扫 `self.mail_provider` 字面量 | 只防这一个拼写；改成 `self.mail_box`/`self.mailboxx` 依旧 AttributeError 被吞，测试全绿 |
| `:266` `test_platform_rt_waits_for_email_otp` | `"wait_for_otp" in src or "verify_otp" in src` | 分支被删、注释保留 → 仍绿 |
| `:47` `test_hook_comes_after_get_auth_session` | `src.index("self.get_auth_session()")` | 调用改成 `self.get_auth_session(timeout=…)` → `ValueError`（error 而非 fail）；提取成 helper → 误报 |
| `:82-86` `test_guard_checks_the_hook` | `src[start-600:start+300]` | 魔法窗口：代码上下移动 600 字符就切不到守卫，断言结果随机 |

- **mutation 证据**：注入「`self.mailbox` 字面量保留、实际传 `None`」后，`test_registration_chain_ordering.py` 仍 **16 passed**（与其余 3 个文件合计 109 passed）。这说明这些断言只能防「字面量被删」，防不住「行为被改」。
- **建议**：保留 1-2 条「不许裸调 Codex」的结构守卫（这类确实需要 AST/正则），其余全部改成行为断言：`Mock()` flow → 调 `_run_codex_rt` → `assert flow.oauth_codex_rt_exchange.call_args.kwargs["mail_provider"] is engine.mailbox`。

### 2.3 注册/代理路径的「表达式级」契约

`tests/test_account_proxy_reuse.py:197-222`

```python
src = inspect.getsource(tasks_mod._run_register)
assert 'account.extra["register_proxy"] = _proxy' in src
```

- **证据**：同一文件 `:224-256` 还断言 `"_resolve_action_proxy(" in src`、`"instance.config.proxy = action_proxy" in src`、`"register_proxy" in src`；`:262-270` 断言 `"redact_proxy_url(chosen)" in src`。
- **问题**：把变量名、赋值形式、函数名钉死；把赋值挪进 helper、改名 `proxy_url`、或把赋值放在 `if False:` 里，测试分别表现为「误报」或「漏报」。而 `_run_register` 本身在这些用例里**从未执行**。
- **建议**：用真实 `_run_register` + 假平台跑一轮，断言落库账号的 `extra["register_proxy"]` 等于任务代理；日志脱敏用 `assertNotIn(密码, caplog.text)`。

### 2.4 日志文案/级别被当契约

- `tests/test_register_task_controls.py:186-195`：`assertIn("开始注册第 1/1 个账号\n", joined + "\n")`、`assertNotIn("失败重试轮数", joined)`。
- `tests/test_chatgpt_registration_engine.py:447-461`：`assertIn('logger.info("Codex RT 本轮已尝试过，跳过重复尝试")', getsource(...))`。
- **问题**：改一个字/换行就红；而真正要保证的「用户能在任务日志里看到跳过原因」并没有被验证（没有 caplog 断言 level=INFO 且消息进入任务日志）。
- **建议**：用 `assertLogs`/`caplog` 断言「存在一条 INFO 且包含关键词」，不要断言整句字面量。

### 2.5 已修复的历史案例（说明这是反复出现的模式）

- `tests/test_chatgpt_two_factor_persistence.py:105-110` 的注释明确记录：「T1.4：`totp_secret` 落盘必须是密文（**原先这条断言的是明文**，也就是把泄漏当成了契约）」。现已改正，但说明「按现状写断言」在本仓真实发生过。
- 审计期间在 `tests/test_api_proxies_and_auth.py` 抓到一条**仍在钉修复前契约**的用例：`test_setup_requires_token_when_password_already_set` 断言「只有 token、没有旧密码也能改密（200）」，而 `api/auth.py:348-351` 已要求 `old_password` → 该用例当时是红的。上游 `c93b71f` 已把它替换为 `test_setup_requires_token_and_old_password_when_password_already_set`（`:353`）。
- **建议**：安全加固类提交必须同步 `grep` 旧契约测试；CI 里保留「全绿」红线，红灯不允许合入（这次红灯存续时间未知，说明本地/CI 没有强制）。

---

## 3. 脆弱测试（flaky 来源）

### 3.1 真实 sleep / 墙钟

| 位置 | 证据 | 为什么会 flaky |
| --- | --- | --- |
| `tests/test_task_runtime.py:105-131` | `time.sleep(0.2)` ×2 + `time.sleep(1.5)`，断言「只剩 1 个 `_loop` 线程」 | 依赖线程在 1.5s 内完成 join/退出；CI 负载高时旧线程可能还没醒，或新线程还没起 |
| `tests/test_task_runtime.py:178-200` | `_loop_interval_seconds=0.2`，`sleep(0.8)` 断言「4 个 tick 内不触发」 | 机器慢时 0.8s 可能只跑 1-2 个 tick；机器快时逻辑本身没问题，但边界由墙钟决定 |
| `tests/test_task_runtime.py:204-226` | `sleep(1.6)` 断言「1s 间隔的任务至少跑一次」 | 同上；且 `register_job` 写的是**全局** `_jobs`（见 3.3） |
| `tests/test_panel_comparison_concurrency.py:135-168` | 两个线程 `sleep(0.3)`/`sleep(0.05)`，断言 `elapsed < 0.6` 且 `order[0]=="sub2api"` | ① 单核/高负载下线程启动抖动；② 串行执行也只要 0.35s < 0.6s —— **这条阈值根本区分不了并行与串行**；③ `order[0]` 取决于调度 |

- **量化**：`tests/test_task_runtime.py` 15 条用例 6.40s，其中 5.4s 是纯 `sleep`（84%）。
- **建议**：给 scheduler 注入可控 clock/Event（仓库已有 `_stop_event`，可加 `_now_fn`）；并发测试用「事件栅栏 + 记录开始/结束时间戳」判断重叠，而不是总耗时阈值。

### 3.2 全局单例 / 进程级状态

| 位置 | 证据 | 风险 |
| --- | --- | --- |
| `api.tasks._task_store` | 6 个测试文件直接操作私有 `_records` 清理：`tests/test_backfill_rt_task_api.py:110-113`、`tests/test_bind_2fa_task_api.py:110-113`、`tests/test_batch_scope_consistency.py:120-123,172-175,271-274`、`tests/test_end_to_end_flows.py:100-103,225-228`、`tests/test_refresh_token_task.py:56-59,130-133`、`tests/test_log_cleanup.py:112` | `tests/test_bind_2fa_task_api.py:97-113` 的 docstring 自己写明：runner 被 mock 时任务停在 `pending`，`has_active()` 恒真，**别的测试文件**（`services/data_bundle.py` 导入）会失败；「单跑本文件全绿」 |
| `core.scheduler._jobs` | `tests/test_task_runtime.py:194-198,216-220` 用 `register_job("late_job_test"/"late_due_test", …)` 注册后**从不注销** | 之后任何 `Scheduler.start()` 都会带上这两个测试任务；`registered_jobs()` 的断言随执行顺序变化 |
| logging handlers | `tests/test_chatgpt_rt_backfill.py:214-227` 快照/恢复 `platforms.chatgpt.protocol` 与 `services.sms_service` 的 handlers | 恢复是「赋回原 list 对象」；若测试失败在 tearDown 前被跳过、或 pytest 并行，其他测试的日志会被吞/串 |
| `config_store` | `tests/test_mail_import_source_config.py:149` 写入 `mail_provider="applemail"` 不恢复；`tests/test_chatgpt_auto_maintenance.py:952-961` setUp 清空 4 个全局键不恢复 | 后续 `create_mailbox()` 读到空/旧 provider，表现为「别的文件随机失败」 |
| `core.db.engine` | `tests/test_task_store_io.py:87-95` 替换全局 engine（有恢复） | 若 setUp 失败，tearDown 不跑 → 后续所有用例连到临时库 |

### 3.3 共享数据库（最大的系统性脆弱点）

- `tests/conftest.py:39-40`：整个 pytest session 共用一个临时 SQLite（`DATABASE_URL` 只设一次）。
- **74 个测试文件**直接使用 `core.db` / `account_repository`；**只有 3 个文件**自建隔离库：`tests/test_db_modular.py:27`、`tests/test_account_repository_batch.py:19`、`tests/test_task_store_io.py:84`。
- **27 处 `session.exec(delete(AccountModel))`** 在 setUp 里清空全表，例如 `tests/test_account_password_serialization.py:31`、`tests/test_backfill_rt_task_api.py:23,83`、`tests/test_batch_scope_consistency.py:51,100,152,251`、`tests/test_chatgpt_auto_maintenance.py:404,811,1195`。
- **后果**：任何并行化（`pytest-xdist`）、随机顺序、或「单文件跑绿、全量跑红」都由此产生；`tests/test_proxy_tags_balance.py:82-87` 的注释直接承认「共享库里可能有别的用例建的代理」，并因此把断言弱化成「非空」。
- **建议**：把 conftest 的「一个 session 库」改成 `function` 级 fixture（`tmp_path` + `monkeypatch core.db.engine` + `platform_database_registry.configure`），仓库里已有三份可复用的实现，抽成 `tests/conftest.py` 的 `isolated_db` 即可。

### 3.4 顺序 / 文件级依赖

- 没有 `pytest.ini`/`pyproject.toml`，pytest 按**文件名字典序**执行；重命名文件即改变顺序。
- `tests/test_proxy_tags_balance.py:131-210` 的 `rebalance` 会遍历**全库账号**并把「不合规绑定」搬走；如果别的测试文件留下的账号带 `register_proxy`，会被本测试改写。
- `tests/test_backfill_rt_task_api.py:100-113` 的 tearDown 注释：「`_task_store` 是模块级单例，跨测试文件共享……表现为别的测试文件失败、单跑本文件却全绿」。
- `tests/test_grok_platform.py:542-577` 直接改 `os.environ`（有 try/finally，但断言失败时 finally 仍执行，尚可）；`tests/test_banned_upload_exclusion.py:26` 在 import 期 `os.environ.setdefault("DATA_DIR", mkdtemp())`，与其他文件的 DATA_DIR 假设耦合。

### 3.5 源码切片 / 私有属性

- `tests/test_registration_chain_ordering.py:82-86`：`src[start-600:start+300]`。
- `tests/test_registration_chain_ordering.py:47-52`：`src.index(...)` 找不到即 `ValueError`（pytest 记为 error，不是 assertion failure，排查成本更高）。
- `tests/test_rt_route_reaches_protocol_flow.py:150`：`outside.index(f"def {name}(")` 同样会在重命名时抛异常。
- `tests/test_chatgpt_rt_backfill.py:345`：`overrides[0]["OTP_TIMEOUT"]` 依赖策略顺序；文件自己 `:66-70` 也承认「默认路线变了这些用例前提全部失效」。
- `tests/test_account_status_removal.py:167`、`tests/test_solver_idle_reaping.py:211`：直接改私有字段（`_loop_interval_seconds`、`_last_used_at`）。

### 3.6 环境依赖（同一套测试在不同机器覆盖不同）

| 位置 | 证据 | 后果 |
| --- | --- | --- |
| `tests/test_maintenance_doc.py:108-128` | `subprocess.run(["npm","run","lint"], timeout=300)`，无 npx 则 `skipTest` | 有 node 的机器跑 26s 并可能因 eslint 版本差异红；没 node 的机器直接跳过 → 「文档数字」无人守 |
| `tests/test_maintenance_doc.py:138-156` | 在 pytest 内再 `subprocess.run([sys.executable,"-m","pytest","tests/","--collect-only"])` | 8s；且子进程继承环境，若外层用了 `-k/-m` 过滤，内层仍全量收集，语义漂移 |
| `tests/test_frontend_layout_contract.py:36-56` | `npx tsc -b`（实测 25.1s），无 npx 则 skip | 同上 |
| `tests/test_frontend_running_tasks_cap.py`、`test_pool_status_contract.py`、`test_timezone_display.py` 等 | 调 `node frontend/scripts/*.mjs` | 前端脚本存在性/Node 版本成为 Python 测试的前置条件 |

### 3.7 并发用例

- `tests/test_panel_comparison_concurrency.py:30-64`：`threading.Barrier(n)` + `join(timeout=10)`；Barrier 在 6 线程下若一个线程启动失败会 `BrokenBarrierError`，表现为 error 而非断言失败。
- `tests/test_account_unique_index.py:102-106`：20 线程并发 upsert SQLite，依赖「一个赢、其余 IntegrityError」；SQLite 的 `database is locked` 与 busy_timeout 会让结果随机器变化（该用例在慢盘上可能 20 个全失败或全成功）。

---

## 4. 断言强度

### 4.1 统计（AST 口径，`c93b71f`）

- test 函数 3043；**只断言 `status_code` 的 36 条**；**零断言的 17 条**；**只有 1 条断言的 1226 条（40%）**。
- `status_code` 精确等于 200 的仅 25 处 —— 本仓整体不是「只测 200」，问题集中在少数「接受一个区间」的用例。

### 4.2 「500 也能过」/「404 也能过」

| 位置 | 断言 | 问题 |
| --- | --- | --- |
| `tests/test_batch_action_task.py:45-53` | `assertIn(r.status_code, (200,400,404))` + `assertNotEqual(…,405)` | **端点不存在（404）也算通过**；用例名 `test_endpoint_exists` 却无法证明端点存在 |
| `tests/test_probe_status_task.py:24-35` | 同上，且用例名 `test_endpoint_creates_a_task` | 全程没有断言「task 被创建」；404 也过 |
| `tests/test_api_integration.py:124,133` | `assertGreaterEqual(r.status_code, 400)` | **500 Internal Server Error 也满足**；校验逻辑崩了照样绿 |
| `tests/test_backup_import_bounds.py:25` | `assertIn(r.status_code, (400,411,413,422))` | 尚可（排除了 500） |
| `tests/test_batch_action_task.py:73-79` | `assertTrue(hasattr(task_runners, "_run_batch_action"))` | 只证明名字存在，不证明能跑 |

### 4.3 「非空即通过」

- `tests/test_proxy_tags_balance.py:79-88`：`assertTrue(seen, "回退后不该返回 None")`。若回退实现返回任意一个活跃代理（哪怕标签过滤完全失效）也通过。建议补一条「存在 tagged 代理时只可能返回 tagged」的强断言（`test_register_tag_filters` 已部分覆盖，但两条用例没有互相约束）。
- `tests/test_panel_comparison_concurrency.py:166`：`assertLess(elapsed, 0.6)` —— 串行 0.35s 也通过（见 3.1）。

### 4.4 happy path only

| 文件 | 证据 | 缺什么 |
| --- | --- | --- |
| `tests/test_end_to_end_flows.py` | 只有 3 条：注册→落库→列表→导出、重复邮箱、刷新链成功 | 没有「注册失败」「OTP 超时」「RT 缺失」「停止」路径；文件名叫 end_to_end 但平台是 `_E2EPlatform` 假体 |
| `tests/test_chatgpt_plugin.py` | 6 条里 4 条是「传参正确」，失败路径只有「无邮箱」「engine 报错」 | 没有 adapter 抛异常/部分成功的组合 |
| `tests/test_panel_push_endpoint.py` | 8 条中 7 条 happy/方向判定 | 没有「upload 抛异常」「delete 抛异常」「部分成功」 |
| `tests/test_account_proxy_rebind_endpoints.py` | 4 条全是「能绑/能复用/能换死绑」 | 没有「池空」「rebind 抛异常」 |

### 4.5 零断言用例

`tests/test_task_store_io.py:469,474,489,511`（`_ensure_task_exists/_ensure_task_mutable` 的「不抛」用例）、`tests/test_console_broken_pipe.py:38,99,125`、`tests/test_data_paths.py:84` 等 17 条。多数是「不抛异常即通过」的合理写法，但 `_ensure_task_exists` 若退化成 no-op，这 4 条仍绿（好在同文件有 404 负例兜底）。

---

## 5. 运行时间分布

> 方法：按要求只跑单个大文件（`--durations`），另补测几个可疑的重文件。机器为本次审计容器，绝对值不代表 CI。

### 5.1 单文件采样

| 文件 | 用例数 | 总耗时 | 最慢用例 |
| --- | --- | --- | --- |
| `tests/test_chatgpt_auto_maintenance.py`（1224 行，指定的大文件） | 66 | **9.15s** | 1.72s `test_ordinary_refresh_failure_does_not_mark_banned`；1.25s `test_expired_uses_small_jitter` |
| `tests/test_panel_comparison.py`（1001 行） | 79 | 0.40s | 0.01s —— 纯逻辑，极快 |
| `tests/test_chatgpt2api_panel.py`（1019 行） | 45 | 1.40s | 0.63s（配置键白名单） |
| `tests/test_task_runtime.py` | 15 | **6.40s** | 1.91s / 1.71s / 0.90s —— 全是 `sleep` |
| `tests/test_api_proxies_and_auth.py` | 28 | **24-31s** | 11.01s `test_change_password_requires_auth_and_validates`；6.42s `test_hash_pw_is_salted_slow_kdf` |
| `tests/test_maintenance_doc.py` | 6 | **34.2s** | 25.98s `test_eslint_baseline_matches_the_doc`（npm run lint）；8.03s `test_collected_test_count…`（嵌套 pytest） |
| `tests/test_frontend_layout_contract.py` | 36 | **25.4s** | 25.14s `test_typescript_still_compiles`（`npx tsc -b`） |

### 5.2 解读

1. **业务逻辑测试本身很快**：`panel_comparison` 79 条 0.4s、`chatgpt2api_panel` 45 条 1.4s，平均 5-30ms。
2. **耗时集中在与业务无关的三类**：
   - 文档/前端契约：`test_maintenance_doc.py`（34s）+ `test_frontend_layout_contract.py`（25s）≈ **59s**，占 4 分钟全量的 ~25%。其中 npm lint 26s、tsc 25s、嵌套收集 8s。
   - 真实 KDF：`api/auth.py:115` 的 PBKDF2 **600,000 轮**在测试里按真实成本跑。本机单次 ≈1.2s：`test_hash_pw…` 5 次运算 6.4s，`test_change_password…` 约 9 次 11s；`test_api_proxies_and_auth.py` 合计 ~24s，`test_auth_hardening.py + test_auth_fail_closed.py` 又 21s。**约 45s 花在密码哈希上**。
   - 真实 sleep：`test_task_runtime.py` 6.4s 里 5.4s 是 `sleep`。
3. **`test_chatgpt_auto_maintenance.py` 的 1.72s 单条**：用例本身是纯逻辑（mock 了 `execute_action`），耗时来自 setUp 的 `delete(AccountModel)` + 首次 import/DB 初始化；说明「每条用例清空全表」在 1000+ 行文件里累积成主要成本。

### 5.3 建议

- 给 `slow` / `contract` / `frontend` 打 marker（`pytest.ini` 里注册），默认 `-m "not slow"` 跑 2 分钟内的主套件，CI 再跑全量。
- `test_maintenance_doc.py` 的 npm lint 与嵌套 pytest 改为 `@pytest.mark.contract` + 环境变量开关；文档数字可以用脚本在 CI 单独校验。
- PBKDF2：给 `hash_password` 加一个**仅测试**可注入的迭代数（如 `AUTH_PBKDF2_ITERATIONS` 环境变量，默认 600k），并保留一条测试断言「默认值 ≥ 600k」；否则每加一条登录用例就多 1-2s。
- `test_task_runtime.py` 的 sleep 改为可注入 clock（见 3.1）。

---

## 6. Mock 把被测逻辑也 mock 掉（假通过）

### 6.1 明确的假通过

| 位置 | 做法 | 为什么是假通过 |
| --- | --- | --- |
| `tests/test_batch_action_task.py:73-79` + `:90-106` | `hasattr(task_runners, "_run_batch_action")` + `getsource` 里找 `execute_action` / `_apply_action_result` | `_run_batch_action`（`services/task_runners.py:835`）**从未被执行**；整个「通用批量动作」机制（9 个动作）没有一条行为用例。若 runner 里 `execute_action` 调用被注释掉、字符串留在注释里，测试仍绿 |
| `tests/test_probe_status_task.py:38-43` + `:63-…` | `hasattr(_run_probe_status)` + `getsource` | `_run_probe_status`（`services/task_runners.py:751`）从未执行；端点用例 404 也过（§4.2） |
| `tests/test_account_proxy_reuse.py:197-270` | 4 条 `getsource` 断言 | `_run_register` / `_run_account_batch_task` 在这 4 条里没有执行；`register_proxy` 是否真的写进账号、日志是否真的脱敏，均未观察 |
| `tests/test_registration_chain_ordering.py:306-339` | `assertIn("self.mailbox", src)` | mutation 实测：改成传 `None` 仍 109 passed（§2.2） |
| `tests/test_end_to_end_flows.py:109,158` | `mock.patch("core.registry.get", return_value=_E2EPlatform)` | 「端到端」只到任务层；注册协议、插件、adapter 全部绕过。文件名的承诺大于实际 |
| `tests/test_chatgpt_plugin.py:67-77` | `_RecordingAdapter` 替换 `adapter_builder` | `plugin.register` 从未创建真实 adapter/engine；「注册参数透传」是唯一断言 |

### 6.2 合理接缝（不算假通过，但要知道边界）

- `tests/test_chatgpt_registration_engine.py:235-239`：`AuthFlow` 整体 Mock —— 这是引擎单测的合理边界，但**必须**另有 `run_register` 的端到端来兜底（目前没有，见 1.1）。
- `tests/test_chatgpt_rt_backfill.py:36-101` 的 `_FakeFlow`：方法签名与真实 `AuthFlow` 对得上（`oauth_codex_rt_exchange(mail_provider=None)`、`from_existing_credentials(session_token, access_token, device_id)`、`seed_device_id`），但**没有任何机制保证签名不漂移**。真实类加一个必填参数时，测试仍绿、生产崩。
- `tests/test_backfill_rt_task_api.py:116` / `tests/test_bind_2fa_task_api.py:121`：端点测试 patch 掉 runner，但同文件另有 runner 行为用例（`test_runner_persists…`），属于合理分工。
- `tests/test_panel_push_endpoint.py:208,246`：`delete_*` 被 mock —— 端点逻辑合理，但 `delete_*` 本体无测试（§1.5）。

### 6.3 建议

1. **禁止 `hasattr` 当测试**：`_run_batch_action` / `_run_probe_status` 至少各补一条「建任务 → 轮询 snapshot → 断言 success/failed 与落库」的用例（可照抄 `tests/test_bind_2fa_task_api.py:156-230` 的写法）。
2. **源码字符串断言全部降级为「补充守卫」**，主断言必须是行为；每个 `getsource` 用例旁边注明「这条只防 X 字面量回归，行为由 Y 覆盖」。
3. **给 `_FakeFlow` 加契约测试**：用 `inspect.signature(AuthFlow.oauth_codex_rt_exchange)` 与 `_FakeFlow` 的签名比对（`tests/test_login_refresh_flow_contract.py` 已有「对真实类构造」的先例，可推广）。

---

## 7. 逐项核对表（对应任务 6 问）

| 问题 | 结论 | 关键证据 |
| --- | --- | --- |
| (1) 关键路径有无测试兜底 | 六条都有文件，但 6 处关键执行体无行为测试 | `auth_flow.py:567` 无执行；`platform_rt.py:232-262` 仅源码断言；`api/tasks.py:615/626` 无调用；`task_runners.py:751/835` 无执行；`proxy_utils.py:224` 无测试；`panel_push.py:252/293` 仅 mock |
| (2) 是否钉错误行为 | 有，1 处明确 + 1 处历史遗留已修 | `test_panel_comparison.py:801` 把盲区判 synced 写成契约；`test_api_proxies_and_auth.py` 旧 F10 契约（`c93b71f` 已修） |
| (3) 脆弱测试 | 四类齐全 | 真实 sleep（`test_task_runtime.py:105-226`）；全局单例（`_task_store`/`_jobs`/config_store）；共享 DB（`conftest.py:39`，74 文件碰库仅 3 隔离）；源码切片（`test_registration_chain_ordering.py:82`） |
| (4) 断言强度 | 36 条仅 status、17 条零断言、1226 条单断言；4 条「500/404 也过」 | `test_batch_action_task.py:45-53`、`test_probe_status_task.py:24-35`、`test_api_integration.py:124,133` |
| (5) 运行时间分布 | 双峰：逻辑测试 ms 级；5 个文件 ~100s+ | 见 §5.1 表；npm lint 26s、tsc 25s、PBKDF2 ~45s、sleep 5.4s |
| (6) mock 是否 mock 掉被测逻辑 | 6 处假通过 | 见 §6.1；mutation 实测 109 passed 是最硬的一条 |

---

## 8. 附：本次审计做过的验证

1. `pytest tests/test_chatgpt_auto_maintenance.py -q --durations=25` → 66 passed / 9.15s。
2. `pytest tests/test_task_runtime.py -q --durations=10` → 15 passed / 6.40s（sleep 占 5.4s）。
3. `pytest tests/test_panel_comparison.py -q --durations=8` → 79 passed / 0.40s。
4. `pytest tests/test_chatgpt2api_panel.py -q --durations=6` → 45 passed / 1.40s。
5. `pytest tests/test_api_proxies_and_auth.py -q --durations=8` → 28 passed / ~24s（PBKDF2 主导）。
6. `pytest tests/test_maintenance_doc.py -q -k "not eslint"` → 5 passed / 8.21s；`-k eslint` → 1 passed / 26.15s。
7. `pytest tests/test_frontend_layout_contract.py -q --durations=3` → 36 passed / 25.44s（tsc 25.14s）。
8. `pytest tests --collect-only -q` → 3107 tests / 4.55s。
9. **mutation 实验 1**（`mut_rt_bug.py`）：把 `_run_codex_rt` 改回 `self.mail_provider`、`_run_login_rt` 去掉 `mail_provider` → `test_chatgpt_registration_engine + test_chatgpt_platform_rt_route + test_registration_chain_ordering` → **2 failed**（只有源码断言红，行为测试全绿）。
10. **mutation 实验 2**（`mut_semantic.py`）：保留 `self.mailbox` 字面量、实际传 `None`，并让 `acquire_rt_via_platform_login` 永远拿不到 mail_provider → 上述 3 个文件 + `test_chatgpt_rt_backfill` → **109 passed**。
11. 代理/任务/凭据相关 8 个文件回归：`test_proxy_tags_balance + test_account_proxy_reuse + test_account_proxy_rebind_endpoints + test_register_task_controls + test_mailbox_task_control + test_credential_crypto + test_account_password_serialization + test_batch_action_task` → 79 passed / 13.40s。

> 说明：审计期间 HEAD 从 `4680c8d` 推进到 `c93b71f`（并行提交），第 10 项之后又新增了认证加固提交；上表耗时是各文件在对应提交上的实测值，量级结论不受影响。
