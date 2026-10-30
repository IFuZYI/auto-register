# deadcode-quality 审计

[MODE: SANDBOX]

- 仓库：`/data/workspace/Register`，分支 `main`，HEAD `4680c8d`
- 范围：`api/ core/ modules/ platforms/ services/ scripts/ main.py`（228 个 `.py`，55,852 行）
- 排除：`.venv/ .worktrees/ reference/ frontend/node_modules/ tests/`（tests 只作为「谁在用」的证据）
- 方法：
  1. `pyflakes 4.1.0`（venv 内置）全量扫描 → 223 个 F401 / 5 个 F841 / 25 个 F811；
  2. 自写 AST 扫描：模块级符号表 + 全仓词引用计数、类方法引用计数、import 图（按模块名而非文件名）、`except` 处理器结构、关键字参数里的数字字面量、URL 字面量；
  3. 每条「死」结论都用 `grep` 交叉验证过（含字符串引用、`__all__`、注册表/importlib 加载、测试引用）；
  4. 三个既有守卫实测通过：`tests/test_dependency_direction.py`、`tests/test_exception_handlers_not_too_wide.py`、`tests/test_capability_registry.py`（13 passed）。

**结论摘要**

| 类别 | 条数 | 最值得先动的一条 |
|---|---|---|
| 整模块死 | 2 | `core/storage_separator.py`（注释声称有消费者，实际零引用） |
| 函数/方法死 | 12 个符号（3 函数 + 9 方法） | `platforms/grok/oauth_device.py` 的 `sso_to_token`（238 行，生产零调用） |
| 配置键死 | 4 + 2 个重复面 | `default_executor` / `chatgpt_executor` / `twocaptcha_key` |
| 端点无消费者 | 4 | `GET /api/config/schema`（前端硬编码字段，从不读它） |
| 重复实现 | 14 组 | JWT payload 解码 7 份、`_get_config_value` 5 份且已语义漂移 |
| 宽捕获 | 453 处（226 未绑定） | `core/db/repository.py:979` 判重查询吞掉所有 DB 异常 |
| 魔法数字/硬编码 | 5 类 | `SOLVER_PORT` 默认 8889 散落 6 处；`timeout=30` 46 处 |
| 注释漂移 | 核对 12 处（要求 10 + 追加 2）：4 漂 + 1 半漂 | `platforms/chatgpt/constants.py` 注释与实际消费者完全不符 |
| 分层越界 | 6 处 | `services/task_runners.py:36` 反向依赖 `api.tasks` 的 DTO |

---

## 1. 死代码

### 1.1 整模块死（零 import、零字符串引用）

**D1 `core/storage_separator.py:1-10` —— 死模块，且注释描述的两个消费者都不存在**

- 证据：全仓 `grep -rn "storage_separator"` **0 命中**（含 tests、scripts、docs）。
- 它声称「`services/account_export.py` 与 `api/accounts.py` 都要用它」，实际：
  - `services/account_export.py:20` 自己又定义了一遍 `SEPARATOR = "----"`；
  - `api/accounts.py:508` 是 `from services.account_export import SEPARATOR, parse_delimited_line`。
- 即：**一个常量三处定义，其中一处（本模块）无人使用**。
- 建议：删除该模块；若想保留单一真相，让 `account_export` 从 `core` 导入（services→core 是正确方向），但为一个常量维持这条依赖不值，删掉更干净。

**D2 `platforms/chatgpt/constants.py:1-12` —— 死模块，且注释指向的消费者用的是另一个 client_id**

- 证据：全仓没有任何 `from ...chatgpt.constants import` / `import platforms.chatgpt.constants`；`grep "chatgpt.constants"` 只命中两处 docstring（`core/outlook_constants.py:8`、`tests/test_chatgpt_client_id_present.py:20`）。
- 注释（第 6 行）写「现在只保留有真实消费者的 OAuth 客户端配置（`token_refresh.py` 刷新 AT 时用）」，但 `platforms/chatgpt/token_refresh.py:162-167` 用的是 `protocol/mixins/platform_rt.py:31` 的 `PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"`。
- 更糟：本模块的 `OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"` 正是 `token_refresh.py:153` 注释里写明「→ 401 invalid_client」的 Codex client id。谁按注释来这里取常量，谁就复现那个 401。
- 建议：删除模块（`OAUTH_REDIRECT_URI` 也零引用，见 D3）。

### 1.2 函数/方法死（全仓零引用，非框架钩子）

| # | 位置 | 说明 | 建议 |
|---|---|---|---|
| D3 | `core/capabilities.py:84-89` `require_capability` | 注释说「用在『这里必须认识它』的位置」，但没有任何调用点；能力登记表只经 `capability_of` 读取 | 要么在动作分发入口真正调用它（新增动作未登记即报错），要么删 |
| D4 | `services/panel_enrollment.py:44-47` `get_enrollment` | 同模块 `enroll/is_enrolled/revoke/reconcile_with_remote` 都有调用方，只有它没有 | 删 |
| D5 | `platforms/grok/constants.py:115-116` `cpa_include_sso` | 读环境变量 `CPA_INCLUDE_SSO`，Grok 的 CPA 支持已于 2026-10-27 删除（见 `migrate_sso_only.py`），函数成孤儿 | 删（连同这个已无意义的环境变量） |
| D6 | `core/base_platform.py:278-289` `BasePlatform.db_session` | 文档写「新插件建议用它」，全仓 0 调用；插件都直接 `platform_session(...)` | 删或补一个真实调用点 |
| D7 | `core/base_platform.py:327-345` `BasePlatform._make_captcha` | 19 行验证码工厂，0 调用；`BasePlatform` 从不创建 captcha（各平台自己在协议层做）。它支持 `yescaptcha/manual/local_solver`，其余值 `raise ValueError`，而 `core/base_platform.py:148` 的注释还写着支持 `2captcha` | 删；顺带修 `captcha_solver` 的注释 |
| D8 | `core/mailboxes/base.py:191-209` `_build_search_text` | 基类里的「把邮件 JSON 拼成可搜索文本」，子类各自实现 `wait_for_code`，无人调用 | 删 |
| D9 | `core/task_runtime.py:270-276` `update_meta` / `:278-287` `append_meta_list` | 任务运行时的两个写原语，0 调用（`append_log`/`set_progress` 都有） | 删 |
| D10 | `modules/automation/pipeline.py:99-124` `RegistrationPipeline.from_application_defaults` | 类方法工厂 0 调用；更进一步：**整个 `modules/automation` 包只被测试导入**（全仓唯一 import 是 `tests/test_reusable_modules.py:4`），而 `docs/EXTENDING.md:154` 把它当作对外扩展骨架 | 删，或明确它是给外部扩展用的公开 API 并在文档里写「本仓未使用」 |
| D11 | `platforms/grok/grok2api.py:355-361` `Grok2ApiClient.test_connection` | 「设置页测试连接」入口，0 调用（设置页没有这个按钮） | 删 |
| D12 | `platforms/icloud/models.py:114-119` `ImportedSession.public_summary` | 0 调用 | 删 |
| D13 | `services/turnstile_solver/api_solver.py:562-590` `_find_turnstile_elements` | 29 行「智能查找 Turnstile 元素」，0 调用（同文件 `_optimized_route_handler` 才是活的） | 删（这是 vendored 代码，改前先确认上游） |

### 1.3 测试专用（生产零引用，只有测试在跑）

**D14 `platforms/grok/oauth_device.py`：437 行里约 280 行是纯测试资产**

- `sso_to_token`（`:150-387`，238 行，纯协议 device flow）—— 生产零调用；只有 `tests/test_sub2api_upload_and_oauth_device.py:300,320,338,360,377,384,401` 调它。
- `token_to_auth_entry`（`:397-430`）—— 同样只被测试调用。
- 两者都在 `:432-436` 的 `__all__` 里，看起来像公开 API。
- 生产实际只用了 `cpa_auth_filename`（`:390-394`，`platforms/grok/upload.py:17,75`）和 `_decode_jwt_payload`（`:126-136`，`platforms/grok/plugin.py:1029` 私有 import）。
- 背景吻合：`platforms/grok/plugin.py:413-416` 与 `:623-628` 明说 `refresh_oauth` 走纯协议 device flow「被 CF 挡死（实测 403），从未成功过」，现在只转发到 `refresh_token`。
- 建议：删 `sso_to_token`/`token_to_auth_entry` 与 `__all__` 中对应项；把 `_decode_jwt_payload` 换成 P1 的统一实现；该文件可缩到 ~40 行。

**D15 `core/capabilities.py:92-98 actions_with` / `:101-106 group_by_capability`** —— 仅 `tests/test_capability_registry.py:16,18,49,56` 使用。二者是「按能力分组动作」的展示逻辑，界面并未使用（前端动作清单来自 `/api/platforms`）。建议删，或接到真正的调用点。

### 1.4 配置键死（schema 里能填、代码里没人读）

`core/config_schema.py` 是配置键的「唯一定义处」，`api/config.py` 由它派生 `CONFIG_KEYS`，因此**每个 schema 键都会出现在设置页**。以下键没有任何后端读取：

| # | 位置 | 证据 | 建议 |
|---|---|---|---|
| D16 | `core/config_schema.py:45` `default_executor` | 后端 0 读取；前端 `frontend/src/lib/configFields.ts:13-17` 注释明说「default_executor 已取消」，`Accounts.tsx:522` 也写「不回落已取消的全局 default_executor」 | 从 schema 删（否则设置页永远显示一个无效下拉） |
| D17 | `core/config_schema.py:51` `chatgpt_executor` | 全仓唯一提及是 `tests/test_register_settings_ui.py:37`（断言这个字段存在）；前端用模板 `` `${platform}_executor` `` 动态取，但 `chatgpt_executor` 在 `frontend/src` 里 0 命中 | 删，或补上真正的读取点 |
| D18 | `core/config_schema.py:41` `twocaptcha_key` | `core/base_captcha.py` 只有 `YesCaptcha/ManualCaptcha/LocalSolverCaptcha` 三个类，没有 TwoCaptcha；`grep -i twocaptcha` 在后端只命中 schema 本身 | 删（或实现 TwoCaptcha，否则用户填的 key 永远不生效） |
| D19 | `core/config_schema.py:99` `grok_register_mode` | 作为**全局配置** 0 读取；唯一读取点是 `platforms/grok/plugin.py:216` 的 `extra.get("grok_register_mode")`（任务级 extra，不是 config） | 从 schema 删；保留 `config_store` 的迁移映射即可 |
| D20 | `core/config_schema.py:84-85` `cliproxyapi_base_url` / `cliproxyapi_management_key` | 仅作旧数据兜底（`core/config_store.py:139-140`），但仍作为独立分组渲染成两个可编辑字段，与 `cpa_api_url`/`cpa_api_key` 完全重复。`services/cliproxyapi_sync.py:40-42` 与 `services/chatgpt_sync.py:46,52` 仍读别名 | 设置页只留 `cpa_*`，别名键从 schema 移除、仅保留读取兜底 |

> 说明：`chatgpt_executor` 与 `grok_executor` 的值实际由前端拼进任务请求的 `executor_type`（`api/tasks.py:80-82`），所以「配置键」这一层是多余的中间层——要么让后端读它，要么别让它出现在设置页。

### 1.5 端点无消费者（前端 0 引用 + 测试 0 引用）

用 AST 提取 106 条路由，把 `{param}` 换成「模板字符串或任意段」的正则后在前端源码与测试里搜索：

| # | 位置 | 证据 |
|---|---|---|
| D21 | `api/config.py:58-62` `GET /config/schema` | 端点自己的 docstring（`:59-62`）写「前端据此渲染设置页」；实测 `frontend/src` 里 `grep "schema"` **0 命中**，字段列表硬编码在 `frontend/src/components/settings/PanelConfigPanel.tsx:70-100`。这条端点存在的理由（见 C3）已经不成立 |
| D22 | `api/accounts.py:717-725` `POST /accounts/check-all` | 前端 0 引用；批量测活已改走批量动作任务（`/actions/{platform}/{action_id}/batch`） |
| D23 | `api/icloud.py:318-330` `GET /icloud/accounts/{id}/messages` | 前端只用别名维度：`frontend/src/api/icloud.ts:325` `/icloud/aliases/${aliasId}/messages` |
| D24 | `api/auth.py:402-410` `POST /auth/logout` | 前端 0 引用（JWT 无状态，登出=前端丢 token） |

建议：确认没有外部脚本/移动端调用后删除；若要保留，至少在 `docs/API_REFERENCE.md` 标注「无界面入口，供外部集成」。

### 1.6 未使用 import / 变量

`pyflakes` 共 255 条，拆开看：

- **F401 223 条**：79 条在两个「门面」`services/icloud_service.py`、`services/sms_service.py`（注释明确说明是刻意保留旧导入面，合理）；62 条在 `api/tasks.py:22-35,216-233` 的重导出块与各 `__init__.py`（也是刻意，为了测试 patch 名字）；**剩下 82 条是真未使用**。
- **F841 5 条**（真正的死变量）：
  - `core/mailboxes/channels/outlook/mailbox.py:385` `session` 赋值后未用
  - `platforms/chatgpt/protocol/mixins/add_phone.py:344` `phone_used`
  - `platforms/grok/plugin.py:607` `log_fn`
  - `platforms/grok/turnstile_mint.py:168` `exc`
  - `services/task_store_io.py:264` `_json_rows`
- **F811 25 条**：`api/accounts.py` 有 **13 处函数内** `from core.db import account_repository`（`:98,135,367,414,454,503,586,688,726,738,762,772,782`），每一处都遮蔽 `api/accounts.py:5` 的模块级 import —— 也就是说**模块级那行才是死的**。`services/panel_sync.py:32` 同理（`:195,:291` 局部重复 import）。

值得单独点名的真未使用 import（不是门面、不是重导出）：

```
api/actions.py:2 datetime        api/actions.py:6 json
api/proxies.py:2 select          api/proxies.py:4 Optional
api/integrations.py:18 select    api/integrations.py:21 AccountModel
core/base_platform.py:7 time     core/proxy_pool.py:14 TAG_REGISTER
core/config_store.py:251 DATA_DIR, :252 BACKEND_JSON
platforms/grok/plugin.py:37 SIGNUP_PAGE_URL, mail_retries, signup_retries
platforms/grok/oauth_device.py:22 CPA_GROK_BASE_URL, CPA_GROK_HEADERS, CPA_TOKEN_ENDPOINT
platforms/grok/plugin.py:45 generate_password, random_name（register_browser.py:38 用的是另一条 import）
```

最后一行是「CPA 支持删除后留下的残骸」的典型样本：`oauth_device.py:22` 导入了三个 CPA 常量，文件里一次没用。

建议：CI 加 `ruff check --select F401,F841,F811`（注意 `services/icloud_service.py`、`services/sms_service.py`、`api/tasks.py` 的既有 `# noqa` 要补齐——现在 `api/tasks.py:22` 那个块只有 `E402` 没有 `F401`，所以 28 条噪声一直挂着）。

---

## 2. 重复实现

> 判定方法：AST 归一化（去函数名/变量名/docstring/字符串常量）后对函数体做哈希，全仓比对；再人工核对语义。

### 2.1 同一语义、多份实现（按影响排序）

**P1 JWT payload 解码 —— 7 份，且行为不一致**

| 位置 | 差异 |
|---|---|
| `core/credential_fields.py:149` `_jwt_payload` | 带 `isinstance(dict)` 校验，返回 `Optional[dict]` |
| `platforms/chatgpt/cpa_upload.py:19` | **不校验** `isinstance(dict)`；`json.loads` 返回 list 时下游 `.get()` 会 AttributeError |
| `platforms/chatgpt/chatgpt2api_upload.py:63` | padding 用 `"=" * (-len % 4)` |
| `platforms/chatgpt/status_probe.py:60` | padding 同上，但先 `.encode("utf-8")` |
| `platforms/chatgpt/sub2api_upload.py:57` | padding 用 `4 - len % 4` + `if != 4` |
| `platforms/grok/oauth_device.py:126` | **要求 `len(parts) != 3` 才解**（两段式 token 直接返回 `{}`，其它 6 份都接受 2 段） |
| `services/chatgpt_token_lifecycle.py:33` `decode_jwt_claims` | 与 chatgpt2api 版逐行相同（哈希相同） |

- 建议：`core/credential_fields.py` 已有实现，把它提升为公开的 `decode_jwt_payload()`，其余 6 份删除。注意 `oauth_device` 的 3 段限制是行为差异，迁移时要确认调用方是否需要。

**P2 时间工具 —— `_utcnow_iso` 6 份、`_utcnow` 5 份**

- `_utcnow_iso`：`platforms/chatgpt/status_probe.py:50`、`platforms/chatgpt/plugin.py:23`、`services/cliproxyapi_sync.py:26`、`services/chatgpt_sync.py:29`、`services/panel_status_sync.py:41`、`services/panel_comparison_cache.py:61` —— 6 份函数体完全相同（`datetime.now(timezone.utc).isoformat()`）。
- `_utcnow`：`core/db/base.py:35`、`services/chatgpt_sync.py:25`、`services/task_store_io.py:39`、`services/mail_imports/providers.py:36`、`services/icloud/_locks.py:23` —— 5 份相同。
- 另有 `platforms/icloud/models.py:13` 的 `utcnow()`。
- `core/time_utils.py` 只有 `ensure_utc`/`UtcDatetime`，**没有 now 系列**，所以每个模块各自造一个。
- 建议：`core/time_utils.py` 增加 `utcnow()` / `utcnow_iso()`，全量替换 12 处（6 + 5 + `platforms/icloud/models.py:13`）。

**P3 `_get_config_value` —— 5 份，且已经语义漂移（有真实后果）**

| 位置 | 行为 |
|---|---|
| `platforms/chatgpt/cpa_upload.py:150-155` | **不 `str()`、不 `strip()`**，直接返回 `config_store.get(key, "")` |
| `platforms/chatgpt/chatgpt2api_upload.py:54-60` | `str(... or "").strip()` |
| `platforms/chatgpt/sub2api_upload.py:24-30` | 同上 |
| `services/cliproxyapi_sync.py:46-57` | 额外做别名兜底（`_CONFIG_ALIASES`） |
| `services/chatgpt_sync.py:33-40` | `strip()` + `or default` |

- 后果：同一个 `cpa_api_url` 值若带尾随空格/换行，CPA 上传拿到的 URL 与 sub2api 上传拿到的不同（前者不 strip，`requests` 可能把空格带进 URL）。
- 5 份全部用 `except Exception: return ""` 包住 `config_store.get` —— 配置读失败静默变成空串（见 E5）。
- 建议：`core/config_store.py` 提供 `get_str(key, default="", aliases=())`，五处全部改调。

**P4 「是否 banned」谓词 —— 6 份**

`services/panel_sync.py:54`、`services/panel_push.py:50`、`services/panel_comparison.py:439`、`services/action_execution.py:91`（公开 `is_banned_status`）、`services/chatgpt_account_state.py:149`（走 `_lower_text`）、`services/grok_account_state.py:125`（内联 `== AccountStatus.BANNED.value`）。

- `services/panel_comparison.py:442-443` 的 docstring 写「与 `panel_push._is_banned_status` / `panel_sync._is_banned_status` / `api/actions._is_banned_status` 同一口径（**各处一份的仓库惯例**）」—— 这条注释把重复制度化了，而且它列的 `api/actions` 那份现在只是 `services.action_execution.is_banned_status` 的别名（`api/actions.py:53`）。
- 建议：`core/models/account.py` 或 `core/base_platform.AccountStatus` 上加一个 `is_banned(value) -> bool`，6 处收敛。

**P5 面板同步结果写回 —— 3 组 ×3 份，共 9 个函数**

- `services/chatgpt_sync.py:215-231` / `:234-250` / `:253-270`：`update_account_model_{cpa,sub2api,chatgpt2api}_sync`，17 行 ×3，**唯一差别**是调 `record_*_sync_result` 哪一个。
- `services/chatgpt_sync.py:311-321` / `:324-334` / `:337-355`：`persist_{cpa,sub2api,chatgpt2api}_sync_result`，11 行 ×3，唯一差别同上。
- 另有 `:273` 的 `update_account_model_cliproxy_sync`（旧名，语义与 cpa 版相同）。
- 建议：`_SYNC_RECORDERS = {"cpa": record_cpa_sync_result, "sub2api": ..., "chatgpt2api": ...}` + 两个泛型函数，9 → 2。

**P6 iCloud 本地 / 远程两个渠道 —— 约 80% 相同（最大的单块重复）**

- `modules/mail/icloud_local.py`（374 行）vs `modules/mail/icloud_remote.py`（337 行）：`diff` 只有 106/78 行差异。
- 函数体哈希完全相同的：`wait_for_code`（74 行）、`poll_once`（28）、`_message_text`（21）、`set_account_status`（19）、`_resolve`（18）、`get_current_ids`（14）、`_renew_lease`（12）—— 合计约 186 行。
- 连常量都各写一份：`LEASE_RENEW_INTERVAL_SECONDS = 60`（`icloud_local.py:42` / `icloud_remote.py:32`）。
- 建议：抽 `BaseICloudMailbox`（轮询/租约/文本提取/状态回写），子类只实现「生成/停用/取信」三个服务调用。这一处能把 711 行压到 ~450 行。

**P7 `services/task_runners.py` 两个 56 行闭包逐行相同**

- `:756-812`（`_run_probe_status` 内的 `_handle`）与 `:924-980`（`_run_refresh_token` 内的 `_handle`）：只差动作 id（`probe_local_status` vs `refresh_token`）与三条文案。
- 而 `_run_batch_action`（`:815` 起）本来就是「动作 id 参数化」的通用实现，docstring 还写着「一个机制覆盖所有『批量点按钮』的场景」。
- 建议：这两个 runner 直接委托 `_run_batch_action(task_id, ids, action_id=..., req=...)`，删掉两个闭包（约 110 行）。
- 附带：`:755-761` 的 `from services.action_execution import (...)` 缩进错位（import 内容比 `from` 多缩进 4 格），是复制粘贴留下的格式伤。

**P8 刷新失败分类 —— 2 份，词表已经不一致**

- `platforms/chatgpt/token_refresh.py:101-126`：返回 `permanent / configuration / retryable / ""`（未知 → **空串**），常量 `REFRESH_KIND_*` 定义在 `:76-78`。
- `platforms/grok/token_refresh.py:78-94`：返回 `permanent / configuration / retryable / "unknown"`（未知 → **"unknown"**），无常量、全是裸字符串。
- 消费方也裸比较：`platforms/grok/plugin.py:534,538,540` 用 `result.kind == "permanent"`。
- 后果：`kind == ""` 与 `kind == "unknown"` 在日志/统计里是两种东西；ADR-0006「用错误种类而不是分支」的意图只落实了一半。
- 建议：`core/errors.py` 定义 `RefreshKind` 枚举 + `classify_refresh_error(status, code, markers)`，两个平台各自提供 markers 表。

**P9 凭证状态分类 —— 2 份近同构**

- `services/chatgpt_account_state.py:94-127` `classify_local_probe_state` 与 `:130-146` `classify_remote_sync_state`：三分支（401 → deactivated → 403）逐行对应，只差取值 key 与返回前缀（`auth_` / `codex_` / `remote_`）。
- 建议：一个 `_classify_credential_state(state, status, error_code, message, prefix)`，两处传不同参数。

**P10 HTTP 错误消息/JSON 解析 —— 3 对函数体相同**

| status_probe | cliproxyapi_sync | 行数 |
|---|---|---|
| `platforms/chatgpt/status_probe.py:107 _parse_loose_json` | `services/cliproxyapi_sync.py:76 _parse_json_text` | 9 |
| `platforms/chatgpt/status_probe.py:118 _parse_header_error_json` | `services/cliproxyapi_sync.py:87 _parse_header_error_json` | 同 |
| `platforms/chatgpt/status_probe.py:152 _extract_error_message` | `services/cliproxyapi_sync.py:118 _extract_error_message` | 11 |

- 建议：抽 `core/http_json.py`（`parse_json_loose` / `extract_error_message`）。

**P11 `_extract_auth` 逐字相同**：`platforms/chatgpt/chatgpt2api_upload.py:78-80` 与 `platforms/chatgpt/sub2api_upload.py:73-75`（3 行 ×2）。建议放 `cpa_upload.py`（它已有 `_get_auth_info`）或 core。

**P12 URL 归一化 —— 32 处手写 `rstrip("/")`，没有公共函数**

代表点：`platforms/grok/upload.py:57,63,126,166`、`services/icloud/remote.py:83,119`、`services/panel_comparison.py:772,1160`、`platforms/icloud/remote_client.py:94`、`platforms/grok/grok2api.py:65`、`services/cliproxyapi_sync.py:61`。

- `api/contribution.py:49-54` 有一个 `_resolve_server_url`（补 scheme + 去尾斜杠 + 补回斜杠），但只在本地用。
- 建议：`core/urls.py: normalize_base_url(url, *, ensure_scheme=False)`，逐处替换。

**P13 时间口径两套（重复之外的不一致）**

- 主流：`datetime.now(timezone.utc)`（56 处）。
- 残留 naive：`platforms/chatgpt/token_refresh.py:340` `datetime.utcnow() + timedelta(...)`；`platforms/chatgpt/protocol/mixins/trace.py:98`（`strftime` 命名）与 `:198`（`isoformat() + "Z"` 手工补时区标记）。
- `token_refresh` 的 `expires_at` 会与 JWT `exp`（aware）比较，naive/aware 混用是 `TypeError` 的经典来源；`isoformat()+"Z"` 则把 naive 伪装成 UTC。
- 建议：并入 P2 的 `utcnow()`，禁止 `utcnow()`（`ruff` 的 `DTZ003` 规则）。

**P14 双存储后端（已知且文档化，风险提示）**

- `core/storage/__init__.py:1-25` 明说 SQLite / JSON 两套实现（`core/db/repository.py` vs `core/storage/account_store_json.py` + `json_table.py`）。
- 同一批业务语义（判重、状态过滤、分页）要在两处维护；`services/task_store_io.py` 里已经出现「JSON 版 / SQLite 版」分叉（`:224 _finished_status_filter` 只服务 SQL 分支）。
- 建议：给两套实现跑同一组契约测试（同一批输入 → 同一批输出），否则漂移是时间问题。

### 2.2 重复度量化

- 归一化后函数体完全相同的分组：**48 组**，其中 ≥14 行的 12 组（见上）。
- 单文件内重复最重：`services/task_runners.py`（2×56 行）、`services/chatgpt_sync.py`（3×17 + 3×11 行）。
- 跨文件重复最重：`modules/mail/icloud_{local,remote}.py`（约 186 行）。

---

## 3. 宽捕获（`except Exception`）

### 3.1 分布

| 目录 | `except Exception/BaseException` | 其中未绑定异常对象 |
|---|---|---|
| `platforms/` | 226 | 122 |
| `services/` | 145 | 57 |
| `core/` | 54 | 39 |
| `api/` | 18 | 7 |
| `modules/` | 9 | 0 |
| `main.py` | 1 | 1 |
| **合计** | **453** | **226** |

- 既有基线守卫 `tests/test_exception_handlers_not_too_wide.py` 允许 227、实测 225（`api/services/core/platforms`），**唯一漂移**：`platforms/grok/oauth_device.py` 基线 10、实际 8 —— 该文件的 device flow 若按 D14 删除，基线应直接归零。
- 无 `except:` 裸捕获（0 处）、无 `except BaseException`（0 处）—— 这两项是干净的。

### 3.2 在吞关键错误的（按危害排序）

**E1 `core/db/repository.py:979-982` —— 判重查询吞掉所有 DB 异常**

```python
except Exception:
    # 该库没有 accounts 表（池库/新库）或库不可用 —— 当作没有记录。
    # 宁可少判重也不能让取号因为一次查询失败整体挂掉。
    return
```

- 注释只辩护了「没有 accounts 表」这一种情况，但 `except Exception` 同时吞掉：SQL 语法错误、连接池耗尽、编码错误、磁盘满。
- 后果：`email_used_by_platform` 返回不完整集合 → 跨平台判重漏判 → **同一邮箱被重复注册**（正是这个函数要防的事）。
- 建议：只捕获 `OperationalError` 且消息匹配 `no such table`；其余 `raise`（或至少 `logger.warning` 并标记结果不完整）。

**E2 `core/db/migrations.py:117-119` 与 `:199-200` —— 迁移里逐库吞异常 `continue`**

- `:117` 是「收集所有库里已用邮箱」，`:199` 是「重建 email→platform 归属」；任一 engine 读失败就整段跳过。
- 后果：迁移「成功」退出，但 `email_used_by_platform` 少一整个平台的归属 → 之后所有判重都错。
- 建议：收集失败列表，迁移结束时 `logger.error` 并返回非零/抛出；至少不要静默。

**E3 `services/task_store_io.py:184-188` —— 任务快照落库失败静默返回**

```python
try:
    snapshot = _store().snapshot(task_id)
except Exception:
    return
_upsert_task_run(snapshot)
```

- 后果：任务状态不落库、无日志；进程重启后任务从列表消失，用户只看到「任务没了」。
- 建议：`logger.warning("任务快照落库失败 task=%s", task_id, exc_info=True)`。

**E4 `core/scheduler.py:58-61` —— 周期任务间隔算不出来就永不运行**

```python
try:
    interval = float(self.interval() or 0)
except Exception:
    return 0.0
```

- `0.0` 的语义是「本次主动跳过」，于是配置写错的任务**永远不跑且无任何日志**。
- 建议：记一次 `logger.error`（带 job name），并按一个显式 `disabled` 状态处理。

**E5 `_get_config_value` 的 5 份 `except Exception: return ""`（见 P3）**

- 位置：`platforms/chatgpt/cpa_upload.py:150`、`chatgpt2api_upload.py:54`、`sub2api_upload.py:24`、`services/cliproxyapi_sync.py:46`、`services/chatgpt_sync.py:33`。
- 后果：`config_store` 抛错（DB 锁、JSON 损坏）时，上传目标 URL 变空串 → 报「未配置 API URL」，排查方向完全错。
- 建议：只在 `KeyError` 时返回默认值，其余记录并抛出。

**E6 `platforms/grok/register_browser.py` 的 6 处选择器 `except Exception: continue`**

- `:222-232`、`:283-294`、`:333`、`:443-449`、`:516` —— 逐选择器 try/except，全失败后只报「找不到邮箱输入框（已重试 3 轮）」。
- 真实原因（选择器语法错、页面已崩、context 被关）全部丢失，只能靠猜。
- 建议：捕获 `playwright.async_api.TimeoutError` 与 `Error` 两类；循环结束后把 `last_error` 拼进错误消息。

**E7 `services/mail_imports/providers.py:394-398` —— 导入失败只 rollback + 计数**

- `errors.append(f"行 {record.line_number}: 创建失败: {str(exc)}")` 丢掉了异常类型与 traceback；批量导入时无法区分「数据不合法」与「代码 bug」。
- 建议：`logger.warning(..., exc_info=True)`，`errors` 里带 `type(exc).__name__`。

**E8 `platforms/chatgpt/protocol/auth_flow.py:1294-1295` —— `except Exception: pass`**

- 从 AT 里回填 email 失败就静默（注释有说明，影响可控），但属于「绑定异常对象也不用」的写法。
- 建议：至少 `logger.debug`。

### 3.3 守卫本身的盲区

1. **扫描范围**：`tests/test_exception_handlers_not_too_wide.py:24` 的 `SCAN_DIRS = ("api","services","core","platforms")` —— `modules/`（9 处）与 `main.py`（1 处）不在守卫内，新增未绑定宽捕获不会被拦。
2. **只查「未绑定」**：`except Exception as exc:` 然后 `pass`/`continue`（绑定但不用）不拦。实测这类静默块 **61 处**，最集中的是 `platforms/grok/register_browser.py`（7 处，全是选择器探测 `except Exception: continue`）。
3. 建议：把 `modules/`、`main.py` 加进 `SCAN_DIRS`；再加一条「handler 体只有 `pass/continue/return None` 且无 log 调用」的基线检查（先按现有 61 处定基线，只减不增）。

---

## 4. 魔法数字 / 硬编码路径 / 硬编码 URL

**M1 超时值：`timeout=30` 46 处、`timeout=20` 10 处、`timeout=15` 8 处**

- 全仓只有 iCloud 平台有常量：`platforms/icloud/constants.py:32 DEFAULT_TIMEOUT_SECONDS = 30`（而且 `platforms/icloud/remote_client.py:48` 又重复定义了一份）。
- 代表点：`platforms/chatgpt/cpa_upload.py:262`、`platforms/chatgpt/token_refresh.py:200,300,470,501`、`platforms/grok/grok2api.py:285`、`core/mailboxes/channels/outlook/mailbox.py:823`。
- 建议：`core/net.py: DEFAULT_TIMEOUT = 30` + 按场景的 `HTTP_TIMEOUT_SHORT/LONG`。

**M2 `SOLVER_PORT` 默认 8889 散落 6 处**

`core/base_captcha.py:10`、`:109`、`core/base_platform.py:340`、`platforms/grok/turnstile_mint.py:212`、`services/solver_manager.py:49`、`services/turnstile_solver/api_solver.py:1367`。

- 任何一处改默认值，其余五处静默不一致（且都写成字符串 `'8889'`）。
- 建议：`core/paths.py` 或 `services/solver_manager.py` 暴露 `solver_base_url()`，全部改调（`base_captcha.py:10` 已经是函数，推广即可）。

**M3 硬编码 URL**

- **同一个第三方默认域名写死两遍**：`api/contribution.py:14` `DEFAULT_CONTRIBUTION_SERVER_URL = "http://new.xem8k5.top:7317/"` 与 `api/config.py:83` 的字面量。后者没有 import 常量（`DEFAULT_CONTRIBUTION_SERVER_URL` 只在本文件 `:50` 与测试里用）。
- `api/config.py:87` `custom_contribution_url` 默认 `http://127.0.0.1:5000`（写死的本地端口）。
- `platforms/chatgpt/constants.py:12` 与 `platforms/chatgpt/protocol/mixins/codex.py:66` 各写一遍 `http://localhost:1455/auth/callback`。
- 规模：生产代码共 **114 个不同 URL 字面量、196 次出现**（43 个域名）；其中 `auth.openai.com` 64 次跨 11 个文件、`chatgpt.com` 30 次跨 9 个文件、`api.openai.com` 12 次跨 6 个文件。
- 建议：把 `platforms/chatgpt/constants.py`（D2，现在是死的）改成真正被 import 的端点表；`api/config.py` 改成 import `DEFAULT_CONTRIBUTION_SERVER_URL`。

**M4 硬编码路径**

| 位置 | 问题 |
|---|---|
| `platforms/chatgpt/protocol/mixins/trace.py:97-99` | `os.makedirs("outputs", exist_ok=True)` + `os.path.join("outputs", ...)` —— 相对 CWD 的路径，不在 `core/paths.py` 布局里；从别处启动就写到别处，容器里可能不可写 |
| `services/sms_service.py:57-60` | `Path(__file__).resolve().parents[1] / "data"` —— **绕过 `DATA_DIR`**：设了 `DATA_DIR` 时，SMS 号码缓存仍写进仓库 `data/`，与「所有持久化路径的唯一真相源」冲突 |
| `services/turnstile_solver/api_solver.py:184-187` | 自己复制「`DATA_DIR` 或 `parents[2]/data` + `account_manager.db`」的约定（独立进程可接受，但要与 `core/paths.py` 同步） |

- 正面项：`core/paths.py` 本身没有绝对路径硬编码，`_resolve_dir` 正确处理了相对路径与 `~`。问题只在上述绕过它的三处。

**M5 语义阈值写死，绕过既有统一入口**

- `mailbox_otp_timeout_seconds` 有统一解析函数 `core/base_platform.py:104 resolve_mailbox_otp_timeout`（3 个调用方），但仍有写死 180 的地方：`platforms/chatgpt/protocol/auth_flow.py:657`、`:714`、`:1024`，`platforms/chatgpt/protocol/mixins/add_phone.py:110`、`:401`，`platforms/chatgpt/login_refresh.py:194`。
- 后果：用户在设置页把 OTP 超时改成 240，这几条路径仍然 180 秒超时。
- 建议：全部改调 `resolve_mailbox_otp_timeout(self.extra_config)`；`login_refresh.py:187` 那套 `for key in (...)` 的兜底链也并进去。

---

## 5. 注释与代码是否漂移（12 处核对：要求的 10 处「实测/踩过」+ 追加 2 处）

| # | 位置 | 注释主张 | 现状 | 判定 |
|---|---|---|---|---|
| C1 | `platforms/chatgpt/constants.py:6` | 「只保留有真实消费者的 OAuth 客户端配置（token_refresh.py 刷新 AT 时用）」 | 模块零 import；`token_refresh.py` 用 `PLATFORM_CLIENT_ID`；本文件的 `OAUTH_CLIENT_ID` 恰是注释里被否定的 Codex id（`token_refresh.py:153` 实测 401） | **漂移（严重）** |
| C2 | `core/storage_separator.py:3-4` | 「`services/account_export.py` 与 `api/accounts.py` 都要用它」 | 两者都不用它；`account_export.py:20` 另定义一份 | **漂移** |
| C3 | `core/config_schema.py:17-18` + `api/config.py:59-62` | 「`GET /api/config/schema` 把这张表原样吐给前端…前端可以据此渲染表单，不必再维护第三份键名列表」 | `frontend/src` 里 `schema` 0 命中；字段仍硬编码在 `PanelConfigPanel.tsx:70-100`；该端点无消费者（D21） | **漂移** |
| C4 | `services/panel_comparison.py:442-443` | 「与 `panel_push._is_banned_status` / `panel_sync._is_banned_status` / `api/actions._is_banned_status` 同一口径（各处一份的仓库惯例）」 | `api/actions.py:53` 已改为 `from services.action_execution import is_banned_status as _is_banned_status`，那份「拷贝」不存在了；清单过期 | **漂移（轻微）** |
| C5 | `api/tasks.py:637-639` | 「SQLite 版原来返回 `TaskLog` 模型、JSON 版返回 dict…现在两种后端返回同一形状」 | `core/db/task_log_repository.py:27 _from_sqlite` 确实把行转成 dict；`api/tasks.py:640-657` 两分支同形 | 准确 |
| C6 | `core/base_captcha.py:33-39` | 「此前这里是 `verify=False` + `disable_warnings()`…现在 `verify=True`」 | 同文件 `:39`、`:47` 都是 `verify=True`，无 `disable_warnings` | 准确 |
| C7 | `main.py:72-75` | 「`git diff` 不接受 `--untracked-files`（实测 exit 129）」 | 实测 `git diff --untracked-files=no` → exit **129** | 准确 |
| C8 | `platforms/chatgpt/protocol/http_client.py:151-153` | 「代理链路 5.4% 偶发 TLS 瞬断，原 session 重试实测 8/8 一次即恢复」 | `_TlsRetrySession` 存在并包住 session（`:158`），`_is_tls_handshake_error` 在 `sentinel.py:90`、`sentinel_quickjs.py:327` 被复用 | 准确 |
| C9 | `platforms/grok/plugin.py:413-416` | 「已弃用：纯协议 device flow 被 CF 挡死（实测 403）…保留 id 转发到 refresh_token」 | `:623-628` 确实是 `if action_id == "refresh_oauth": return self.execute_action("refresh_token", ...)` | 准确（但对应实现 `sso_to_token` 仍留着，见 D14） |
| C10 | `core/mailboxes/channels/outlook/mailbox.py:316-319` | 「这个 import 原来漏了…一进 `requeue_account` 就 NameError」 | `:319` 确有 `from core.db.outlook_repository import outlook_repository` | 准确 |
| C11 | `core/db/migrations.py:677-679` | 「SQLite 3.35+ 支持 DROP COLUMN（实测 3.53）；列不存在或库不支持时安静跳过」 | `:699-702` 就是 try/except + `logger.debug` | 准确 |
| C12 | `core/config_store.py:152-155` | 「旧键保留原值（仍有代码/脚本读它，见 `platforms/grok/plugin.py` 的兜底链）」 | `plugin.py:214-220` 读的是**任务 extra** 的 `grok_register_mode`，从不读全局配置；所谓「兜底链」在 `base_platform._executor_explicit`，与这个键无关 | **半漂移** |

小结：12 处里 4 处已漂（C1/C2/C3/C4）+ 1 处半漂（C12）。共同模式是**「注释描述的重构已经又重构了一轮，注释没跟上」**——尤其 C1/C2 属于「注释为已死的代码辩护」，比单纯过期更有害。

---

## 6. 模块分层越界

分层契约见 `docs/EXTENDING.md:43-62`：`platforms → modules → core` 是主骨架，core 不得反向 import（仅 `core/registry.py`、`core/mailboxes/registry.py` 两个加载器例外）；platforms 与 services 双向引用是既定现状。

**实测结果**

- `core → services`：**0 处**（`tests/test_dependency_direction.py` 通过）。`core/registry.py:39` 的 `import platforms` 属文档点名的加载器例外。
- `core → modules`：0 处。
- `platforms → api`：0 处。
- `services → api`：**1 处**（L1）。
- `api → core` 越界：**4 类**（L2-L5）。

| # | 位置 | 越界内容 | 影响 / 建议 |
|---|---|---|---|
| L1 | `services/task_runners.py:36-43` | `if TYPE_CHECKING: from api.tasks import RegisterTaskRequest, ...`（6 个请求 DTO） | 服务层认识 HTTP 层 DTO，方向反了。运行时无影响（`from __future__ import annotations`），但类型检查会把 api 拖进 services 的依赖图。建议把这 6 个请求模型下沉到 `core/schemas.py` 或 `services/task_contracts.py` |
| L2 | `api/tasks.py:648` | `from core.db.task_log_repository import _from_sqlite`（**私有**符号） | API 依赖 core 的内部实现细节；`_from_sqlite` 一改名就断。建议在 `task_log_repository` 暴露公开的 `to_dict(row)` |
| L3 | `api/outlook.py:69`、`:77` | `from core.mailboxes.channels.outlook import OutlookMailbox` —— API 直接调用具体渠道类的类方法（`pool_status_summary` / `import_accounts_to_pool`） | 绕过 `core/mailboxes/registry.py` 的渠道抽象；换渠道实现要改 API。建议加 `core/mailboxes` 门面函数或 `services/mailboxes.py` |
| L4 | `api/tasks.py:650-657`、`:676-682`、`:828-833` | 3 处 `with Session(current_engine())` + `select(TaskLog)` 裸 SQL | API 层直接写 SQL，且同时存在 JSON/SQLite 两分支（`:641-657`）。建议全部收进 `core/db/task_log_repository.py` |
| L5 | `api/accounts.py:5` vs `:98,135,367,414,454,503,586,688,726,738,762,772,782` | 13 处函数内 `from core.db import account_repository`，遮蔽模块级 import | 模块级 import 实际是死的（F811×12）；且「API 每进一个函数就重新 import 一次 DB 层」是重构残留。建议只留模块级 |
| L6 | `services/panel_comparison.py:429 _match_key` | 被 4 个模块以**私有名**导入：`services/panel_sync.py:228`、`services/panel_status_sync.py:210`、`services/panel_push.py:192`、`services/panel_comparison_cache.py:284` | 事实上的公共 API 挂着下划线，且住在 1302 行的 `panel_comparison` 里（导入它=拖进整条对比链）。建议移到 `core/models/account.py` 或 `services/panel_keys.py` 并去掉下划线 |

**附带（不算越界，但同类味道）**

- `api/tasks.py:216-233` 从 `services.task_store_io` / `services.task_runners` 导入 **23 个私有函数**（`_ensure_task_exists`、`_run_register`…），只为让测试按老名字 patch。这是「测试耦合倒逼生产 API 形状」，比分层越界更难拆；建议后续把 patch 目标改到服务模块本身。
- `platforms/grok/plugin.py:1029` 的 `from .oauth_device import _decode_jwt_payload`（跨模块私有 import）—— 并入 P1 后自然消失。

---

## 7. 建议的处置顺序

**第一批（低风险、纯删，约 450 行）**

1. 删 D1 `core/storage_separator.py`、D2 `platforms/chatgpt/constants.py`（删前把 `auth.openai.com` 等端点表搬进去复活它，见 M3）。
2. 删 D14 `sso_to_token` / `token_to_auth_entry`（约 280 行）+ `oauth_device.py` 里三个 CPA 常量 import。
3. 删 D6-D13 九个死方法、D3-D5 三个死函数、D16-D19 四个死配置键。
4. 删 D21-D24 四个无消费者端点（先确认外部集成）。
5. `core/config_store.py` 里把 `platforms/grok/oauth_device.py` 的异常基线从 10 调到 8（或随文件删除归零）。

**第二批（重复收敛，风险中）**

6. P1 JWT 解码 7→1、P2 时间工具 12→2、P3 `_get_config_value` 5→1（顺手修 strip 不一致）、P4 banned 谓词 6→1。
7. P5 同步写回 9→2、P10 HTTP 解析 6→2、P11 `_extract_auth` 2→1。
8. P7 两个 56 行闭包 → 委托 `_run_batch_action`（约 -110 行）。
9. P8/P9 两对分类器各自合一。

**第三批（结构性，收益最大也最费工）**

10. P6 `modules/mail/icloud_{local,remote}.py` 抽基类（约 -186 行）。
11. E1/E2 收窄判重与迁移的宽捕获（这条是**正确性问题**，优先级应高于体量更大的 P6）。
12. L1-L6 分层归位；L6 的 `_match_key` 先做（4 个调用方，改动面小）。
13. CI 接入 `ruff --select F401,F841,F811,DTZ003`，把 82 个真未使用 import 与 `datetime.utcnow()` 钉住。

**不需要动的**

- `services/icloud_service.py` / `services/sms_service.py` 的 79 个 F401：注释已说明是「保持旧导入面」，属于刻意的兼容层，不是死代码。
- `core/paths.py`：路径单一真相源做得对，问题只在三处绕过它的代码（M4）。
- `platforms/` 与 `services/` 的双向引用：`docs/EXTENDING.md:47-53` 已明确是既定现状，不算违规。
