# deadcode-quality 审计

[MODE: SANDBOX]

- 仓库：`/data/workspace/Register`，分支 `main`，HEAD `c93b71f`（任务给的 `4680c8d` 是其祖先，之后落了 3 个提交，本文所有行号按 `c93b71f` 校准）
- 范围：`api/ core/ modules/ platforms/ services/ scripts/ main.py` —— **223 个 `.py`，56,034 行**
- ⚠️ 工作树状态：审计期间 `git status` 显示 `core/db/models_account.py`、`api/accounts.py`、`scripts/encrypt_existing_credentials.py`、`docs/audit/2026-10-30/frontend.md` 有**他人并发未提交修改**（另一路数据层审计正在改 AT 加密）。本文行号按「读取时的文件内容」校准；这两个 `.py` 的行号会随之漂移，复核请以符号名 + `git diff` 为准。
- 排除：`.venv/ .worktrees/ reference/ frontend/node_modules/ static/ tests/ migrations/`（tests 只作为「谁在引用」的证据，不计入死代码）
- 方法：
  1. `pyflakes 4.1.0`（venv 自带，无 ruff）全量扫描 → 255 条：**F401 223 / F811 25 / F841 5 / F-string 2**；
  2. 自写 AST 扫描：符号表 + 全仓 `Load` 引用计数（区分生产/测试两份语料）、import 图（按模块名而非文件名，覆盖 `sys.path` 式裸 import）、**不可达语句检测**（terminal 语句后的同层语句）、**归一化函数体哈希**（去名/去常量/去 docstring）、路由表提取；
  3. 每条结论都用 `grep` 反查字符串引用、`__all__`、装饰器注册、`importlib`/`pkgutil` 动态加载、前端 `frontend/src` 与 `tests/`；
  4. 三条既有守卫实跑通过：`test_exception_handlers_not_too_wide.py`、`test_dependency_direction.py`、`test_capability_registry.py`（**13 passed**）；另跑 `test_storage_coverage_gate.py`、`test_annotation_resolvability.py`、`test_contrast_gate_contract.py`（**20 passed**）。

**结论摘要**

| 类别 | 数量 | 体量 / 最值得先动的一条 |
|---|---|---|
| 整模块死（零 import、零调用） | 3 | `platforms/grok/upload.py` 197 行 —— CPA 下线后的整条残留上传链 |
| 只被测试引用的生产代码 | 3 簇 + 20 个符号 | `platforms/grok/oauth_device.py` 426 行死 / 437 行（只有 11 行是活的） |
| 真·不可达代码 | **1 处** | `protocol/mixins/trace.py:94-103`：`return` 之后 9 行永远不执行，**直接让 `AUTH_TRACE_DUMP` 功能失效** |
| 声明了但从不读的参数 | 2 | `api/integrations.py` 的 `account_ids`（前端在传，后端丢弃） |
| 死配置键 | 3 | `default_executor`（前端注释已写「已取消」） |
| 无消费者端点 | 3 + 1 | `GET /api/config/schema`；`POST /auth/logout` 前端根本没接 |
| 未使用 import / 变量 / 重定义 | 76 + 5 + 25 | 76 条是真未使用（其余 147 条是门面与测试重导出） |
| 重复实现 | 28 组（≥10 行） | 归一化后逐字节相同，合并可减 **482 行** |
| 宽捕获 | 453 处（226 未绑定） | 另有 **2 处裸 `except:`**（守卫报 0，见 §3.3） |
| 魔法数字 / 硬编码 | 5 类 | `timeout=30` 46 处；`SOLVER_PORT 8889` 6 处；URL 字面量 97 个/172 次 |
| 注释漂移 | 7 处 + 1 半漂 | `platforms/chatgpt/constants.py` 的注释为「已死的 client_id」辩护 |
| 分层越界 | 6 处 | `services/task_runners.py:44` 反向依赖 `api.tasks` 的 DTO |

---

## 1. 死代码

### 1.1 整模块死

**D1 `platforms/grok/upload.py:1-197` —— 197 行，生产零 import，只有测试在调**

- 证据：`grep -rn "from platforms.grok.upload\|grok.upload import"` 在非 `tests/` 下 **0 命中**；7 处 import 全在 `tests/`（`test_grok_upload_gaps.py:18`、`test_grok_platform.py:492,503,518`、`test_upload_proxy_switch.py:160`、`test_panel_push.py:208`、`test_panel_management_actions.py:574,597`）。
- 生产侧对应能力已经换人：Grok 上传走 `platforms/grok/grok2api.py` 的 `ingest_sso()`（`plugin.py:954-1015` 的 `_action_upload_grok2api`），CPA/Sub2API 两个方向自 2026-10-27 起对 Grok 关闭（`services/external_sync.py:32`、`services/panel_push.py:385` 的注释都写明）。
- 连带：它是 `oauth_device.cpa_auth_filename` 的唯一调用方（`upload.py:17,75`）—— 见 D2。
- 建议：整文件删除；同步删 `tests/test_grok_upload_gaps.py`、`tests/test_upload_proxy_switch.py` 里 Grok 那两条，以及 `tests/test_exception_handlers_not_too_wide.py:75` 的基线行（`platforms/grok/upload.py: 3`）。

**D2 `platforms/grok/oauth_device.py:1-437` —— 437 行里只有 12 行是活的**

- 活着的只有 `_decode_jwt_payload`（`:126-136`，11 行），被 `platforms/grok/plugin.py:1029` 以私有名 import。
- 其余全部零生产调用：
  - `sso_to_token`（`:150-387`，238 行）—— 只被 `tests/test_sub2api_upload_and_oauth_device.py:300,320,338,360,377,384,401` 调用；
  - `cpa_auth_filename`（`:390-394`）—— 唯一调用方是 D1 的死模块；
  - `token_to_auth_entry`（`:397-430`）—— 只被同测试文件 `:227,238` 调用；
  - `_NoRedirectSession`（`:39`）、`_device_location_error`（`:115`）、`_form_headers`（`:139`）、`_log`（`:107`）只服务 `sso_to_token`。
- 背景吻合：`platforms/grok/plugin.py:413-414`、`:623-629` 明说纯协议 device flow「被 CF 挡死（实测 403），从未成功过」，`refresh_oauth` 现在只转发到 `refresh_token`。
- 建议：删整个文件（426 行）；把 `_decode_jwt_payload` 并入 P1 的统一实现（`core/credential_fields.py` 已有带类型校验的版本）。连带删基线 `tests/test_exception_handlers_not_too_wide.py:70`（`platforms/grok/oauth_device.py: 10`，实际只有 8）。

**D3 `core/storage_separator.py:1-12` —— 12 行，且注释描述的两个消费者都不存在**

- 证据：全仓 `grep -rn "storage_separator"` 在源码/测试/其他文档里 **0 命中**（只剩 `__pycache__/*.pyc` 与本审计文本自身）。
- 注释（`:3-4`）称「`services/account_export.py` 与 `api/accounts.py` 都要用它」，实际：
  - `services/account_export.py:20` 自己又定义了一次 `SEPARATOR = "----"`；
  - `api/accounts.py:517` 是 `from services.account_export import SEPARATOR, ...`。
- 即「一个常量三处定义，其中一处没人用」。
- 建议：删除模块（`services → core` 是正确方向，但为一个常量维持依赖不值）。

**D4 `platforms/chatgpt/constants.py:1-13` —— 13 行，零 import，注释指向的消费者用的是另一个 client_id**

- 证据：无任何 `from platforms.chatgpt.constants import` / `import platforms.chatgpt.constants`；`grep "chatgpt.constants"` 只命中 docstring（`core/outlook_constants.py:8`、`tests/test_chatgpt_client_id_present.py:20`）与 `reference/`（不算本仓代码）。
- 注释（`:6`）写「现在只保留有真实消费者的 OAuth 客户端配置（`token_refresh.py` 刷新 AT 时用）」，但 `platforms/chatgpt/token_refresh.py:162-167` 取的是 `protocol/mixins/platform_rt.py:31` 的 `PLATFORM_CLIENT_ID = "app_2SKx67EdpoN0G6j64rFvigXD"`。
- 更糟：本文件的 `OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"`（`:11`）正是 `token_refresh.py:153` 注释里写明「→ 401 invalid_client」的 Codex client id。谁按注释来这里取常量，谁就复现那个 401。
- 建议：删除模块。若想保留端点表，就把它改成**真被 import** 的常量模块（见 M3），否则删。

### 1.2 只被测试引用的生产代码（生产零调用）

判定：全仓生产语料里该符号的 `Load` 引用数 = 0，且无装饰器注册；测试语料里 > 0。逐条 `grep` 反查过别名、`getattr`、注册表。

| # | 位置 | 行数 | 证据 / 说明 | 建议 |
|---|---|---|---|---|
| T1 | `services/cliproxyapi_sync.py:565-656` `sync_grok_cliproxyapi_status_batch` | 92 | 唯一调用方 `tests/test_panel_management_actions.py:482,502`；生产只有 `sync_chatgpt_cliproxyapi_status_batch`（`api/actions.py:235,257`） | 删；连带 `_probe_grok_remote_auth`（`:504-562`，59 行）只服务它，一起删 |
| T2 | `core/mailboxes/channels/outlook/mailbox.py:315-363` `requeue_account` | 49 | `grep "\brequeue_account\b"` 生产侧只命中定义与注释；测试 `test_outlook_mailbox_status_sync.py` 覆盖 | 删，或接到池调度真实路径 |
| T3 | `platforms/chatgpt/chatgpt_registration_mode_adapter.py:147-176` `resolve_chatgpt_token_plan` | 30 | 生产 0 引用，仅 `tests/test_chatgpt_token_plan_settings.py` | 删 |
| T4 | `core/proxy_utils.py:59-86` `is_authenticated_socks5_proxy` | 28 | 生产 0 引用，仅 `tests/test_proxy_utils.py` | 删 |
| T5 | `core/models/account.py:224-249` `AccountRecord.to_model_fields` | 26 | 与其 docstring 声称的「互转」相反：生产只走 `AccountModel.to_record()` 单向 | 删，或补真实写回路径 |
| T6 | `core/base_captcha.py:108-132` `LocalSolverCaptcha.start_solver` | 25 | 生产 0 引用；真正启动 solver 的是 `services/solver_manager.py:104` 直接起子进程 | 删（逻辑已由 solver_manager 承担） |
| T7 | `core/proxy_pool.py:431-453` `ProxyPool.reactivate_all` | 23 | 生产 0 引用（界面「检测全部代理」走逐条探活） | 删 |
| T8 | `core/db/outlook_repository.py:173-195` `count_by_status` | 23 | 生产 0 引用；池统计走 `OutlookMailbox.pool_status_summary()` | 删 |
| T9 | `services/mail_imports/microsoft_import_rules.py:231-253` `MicrosoftMailboxAvailabilityRule` | 23 | 未注册进 `mail_import_registry`（只注册了 `microsoft` provider），生产 0 引用 | 删或注册 |
| T10 | `modules/platforms/icloud.py:16-38` `ICloudAliasWorkflow` | 23 | 只在 `modules/__init__.py:19` / `modules/platforms/__init__.py:3` 的 `__all__` 里出现，无调用 | 删 |
| T11 | `core/mailboxes/channels/outlook/mailbox.py:666-680` `_fetch_oauth_token` | 15 | 生产 0 引用（Graph 刷新走 `backends.py`） | 删 |
| T12 | `core/db/models_account.py:207-220` `AccountModel.update_extra` | 14 | 生产 0 引用；仓储层用的是 `account_repository.update_extra`（`core/proxy_pool.py:250,309`、`services/task_runners.py:551`），不是模型方法。注意该文件正被并发重构（AT 加密），删前先 rebase | 删，或让仓储改调它 |
| T13 | `platforms/chatgpt/protocol/response_summary.py:61-73` `describe_page` | 13 | 生产 0 引用，仅测试 | 删 |
| T14 | `platforms/icloud/remote_client.py:284-292` `RemoteHMEClient.fetch_message` | 9 | 生产 0 引用；取信走 `web_mail` / `mailbox.py` | 删 |
| T15 | `core/base_platform.py:318-325` `_make_executor` | 8 | 生产 0 引用（`create_executor` 在 `:311` 才是活的那条） | 删 |
| T16 | `services/mail_imports/microsoft_import_rules.py:256-263` `parse_microsoft_import_record` / `:266-268` `parse_microsoft_import_line` | 8 | 生产 0 引用，仅 `test_mail_imports_service.py` | 删 |
| T17 | `platforms/grok/plugin.py:655-661` `GrokPlatform.get_quota` + `core/base_platform.py:263-265` 基类空实现 | 7 | 全仓**没有任何** `get_quota(...)` 调用点（`grep -rn "get_quota"` 只命中定义与测试） | 删两边，或做成真实配额入口 |
| T18 | `services/panel_enrollment.py:36-41` `is_enrolled`、`:44-48` `get_enrollment` | 10 | 同模块 `enrolled_panels`/`enroll`/`reconcile_with_remote` 都有调用方（`panel_sync.py:91,268`、`chatgpt_sync.py:114`、`panel_push.py:503`），只有这两个没有 | 删 |
| T19 | `core/db/icloud_alias_repository.py:120-154` `find_available`、`:370-389` `set_pool_status` | 55 | 生产 0 引用（别名池分配走 `services/icloud/aliases.py`）；`@staticmethod` 不带装饰器注册，不是框架钩子 | 删，或让服务层改调它 |
| T20 | 其余小项 | ~25 | `core/panel_tls.py:35 _reset_cache`（3）、`core/task_runtime.py:350 list_snapshots`（3）、`core/db/models_account.py:203-205 get_extra_value`（3）、`platforms/chatgpt/rt_backfill.py:368 _strategy_order`（2）、`api/auth.py:257 _reset_login_failures`（4，只有测试 reset 用） | 逐条删或补调用 |

合计约 **560 行**（含 T1 的 153 行）。

### 1.3 零引用符号（含框架钩子排除后的残留）

除 §1.2 外，另有以下符号全仓 **0 次 `Load` 引用**（连测试都没有），已排除 HTML 解析回调、日志 Handler、`@property`、路由与 `importlib` 动态加载：

| 位置 | 说明 | 建议 |
|---|---|---|
| `core/capabilities.py:84-89` `require_capability` | 注释说「用在『这里必须认识它』的位置」，但 0 调用点；能力表只经 `capability_of` 读 | 要么在动作分发入口真调它（未登记动作即报错），要么删 |
| `core/capabilities.py:92-98` `actions_with`、`:101-106` `group_by_capability` | 仅 `tests/test_capability_registry.py:16,18,49,56` 使用；界面动作清单来自 `/api/platforms` | 删，或接真实调用点 |
| `core/base_platform.py:278-286` `BasePlatform.db_session` | 文档写「新插件建议用它」（`docs/EXTENDING.md:351` 还给了示例），全仓 0 调用；插件直接 `platform_session(...)` | 删，或补真实调用点并把文档改成一致 |
| `core/base_platform.py:327-343` `_make_captcha` | 17 行验证码工厂，0 调用；支持 `yescaptcha/manual/local_solver`，其余值 `raise ValueError`，而 `:148` 的注释还写着支持 `2captcha` | 删；顺带修 `:148` 注释 |
| `core/mailboxes/base.py:191-209` `_build_search_text` | 基类的「拼可搜索文本」，子类各自实现 `wait_for_code`，0 调用（`docs/EXTENDING.md:248` 却当成扩展点介绍） | 删或落到实际渠道 |
| `core/task_runtime.py:270-276` `update_meta`、`:278-287` `append_meta_list` | 任务运行时两个写原语，0 调用（`append_log`/`set_progress` 有真实调用） | 删 |
| `core/scheduler.py:71-77` `register_job` | 扩展点，`core/scheduler.py:23-25` 自己写明「**尚无服务注册任务**」 | 保留，但注释已足够；不要再当「已有能力」宣传 |
| `modules/automation/pipeline.py:99-125` `from_application_defaults` | 类方法工厂 0 调用（见 §1.4 整簇） | 随簇删 |
| `platforms/grok/constants.py:88-97` `mail_retries`、`:80-85` `signup_retries`、`:72-77` `signup_cfg_ttl`、`:115-116` `cpa_include_sso` | 前三个只被 `plugin.py:37,41,42` import 后**从未使用**（F401 已点名），业务侧只读 env；`cpa_include_sso` 则是 CPA 下线后的孤儿，0 引用 | 删 4 个函数；`plugin.py` 的三行 import 一并删 |
| `platforms/grok/profile.py:21-23` `random_name` | 只被 `plugin.py:45` import（F401），`register_browser.py:38` 只 import `generate_password` | 删函数 + 删 import |
| `services/turnstile_solver/api_solver.py:562-592` `_find_turnstile_elements` | 29 行「智能查找 Turnstile 元素」，0 调用（同文件 `_optimized_route_handler` 才活） | 删（vendored 代码，改前确认上游） |
| `platforms/icloud/models.py:114-119` `ImportedSession.public_summary` | 0 调用 | 删 |
| `platforms/chatgpt/token_refresh.py:487-520` `validate_token` | 0 调用；同文件 `:459-460` 的注释还在解释「它与 `verify_access_token` 的区别」，而真正被用的是后者 | 删；注释一并删，或把区别写进 `verify_access_token` |
| `platforms/grok/grok2api.py:355-361` `Grok2ApiClient.test_connection` | 「设置页测试连接」入口，0 调用（设置页没有这个按钮） | 删 |

### 1.4 整簇死：`modules/` 的 5 个「可复用构件」

`modules/automation`、`modules/verification`、`modules/proxy`、`modules/platforms/icloud.py` 在生产侧**没有任何调用**，只被 `tests/test_reusable_modules.py:4-13` 使用。

- 体量：`modules/automation/pipeline.py` 217 + `modules/verification/challenges.py` 32 + `modules/proxy/pool.py` 50 + `modules/platforms/icloud.py` 38 = **337 行**。
- 它们之所以「看起来活着」，是因为 `modules/__init__.py:8-31` 的 barrel 导出把整簇拉进了 import 图，而 `platforms/chatgpt/plugin.py:11` 只需要其中的 `modules.platforms.registration`。也就是说**每次启动都会 import 一遍这 337 行死代码**。
- 对照 `docs/EXTENDING.md:29-38`：它把这 5 个包列为「可复用构件」并声称是扩展骨架，`docs/EXTENDING.md:154,160` 还给出 `RegistrationPipeline.from_application_defaults()` 的示例 —— 示例代码本身在仓内从未被调用。
- 建议二选一：① 删 4 个包 + 收窄 `modules/__init__.py`（只导出真在用的 `PlatformRegistrationService`/`BrowserExecutorFactory`/`MailboxFactory`/`RegistrationContextBuilder`），文档同步改；② 若确实要当对外扩展 API，就写明「本仓未使用」并加一条 smoke test，别让 barrel 在启动路径上白加载。

### 1.5 不可达代码（唯一 1 处，但直接废掉一个功能）

**D5 `platforms/chatgpt/protocol/mixins/trace.py:94-103` —— `return` 之后的 9 行永远不执行**

```python
94:        return "; ".join(f"{name}={value}" for name, value in cookie_pairs if name and value)
95:        if self._trace_dump_enabled:                      # ← 不可达
96:            try:
97:                os.makedirs("outputs", exist_ok=True)
98:                ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
99:                self._trace_dump_path = os.path.join("outputs", f"auth_trace_{ts}_{os.getpid()}.jsonl")
...
103:                self._trace_dump_enabled = False
```

- 全仓 AST 扫描（terminal 语句后同层还有语句）**只发现这一处**。
- 后果链：`_trace_dump_path` 只在 `:99` 被赋值，`auth_flow.py:192` 把它初始化为 `""`；而 `trace.py:194` 的写文件分支条件是 `if self._trace_dump_enabled and self._trace_dump_path:` → **永远为 False**。
- 也就是说 `AUTH_TRACE_DUMP=1`（`auth_flow.py:190`）这个开关**从不落盘**，而 `response_summary.py:6,15` 还在提示用户「完整报文开 `AUTH_TRACE_DUMP` 看」。
- 建议：把 `if self._trace_dump_enabled:` 块移到 `return` **之前**（或抽成 `_ensure_trace_dump_path()` 在 `__init__` 末尾调用）；补一条测试断言「开启后 `_trace_dump_path` 非空且文件被创建」。

### 1.6 声明了但从不读的参数

**D6 `api/integrations.py:82` `sync_panel_endpoint(..., account_ids: str = "")` + `:147-162` `push_panel_endpoint`（连参数都没有）**

- 前端在传：`frontend/src/components/settings/PanelComparisonPanel.tsx:598`（sync）与 `:635`（push）都会 `params.set('account_ids', ids.join(','))`，注释写明「按当前筛选出的 id 集合收窄（代码审查 I1）。不传的话后端会推该平台全部本地较新的行 ——『看到的 ≠ 被改的』」。
- 后端：`sync_panel_endpoint` 收了 `account_ids` 但**函数体里一次没读**；`push_panel_endpoint` 的签名里根本没有这个参数（FastAPI 会静默忽略未知 query 参数）。
- 后果：`I1` 只在「前端请求形状」上修了，**后端从未实现收窄** —— 用户筛了 3 个账号点「更新远程凭证」，实际会推该平台全部「本地较新」的行。
- 测试也没有覆盖：`tests/test_panel_sync_endpoint.py` 只测了 `platform` 参数（`:108 test_platform_param_scopes_the_pull`），没有 `account_ids` 用例。
- 建议：两个端点都解析 `account_ids`（逗号分隔 → `set`）并在 `local_rows` 上过滤；补 2 条测试。

### 1.7 死配置键

`core/config_schema.py` 是 71 个配置键的唯一定义处，`api/config.py` 由它派生 `CONFIG_KEYS`，因此**每个键都会出现在设置页**。逐键反查「非 schema 的字面引用」后：

| # | 位置 | 证据 | 建议 |
|---|---|---|---|
| D7 | `core/config_schema.py:45` `default_executor` | 后端 0 读取；前端 `frontend/src/lib/configFields.ts:13` 注释「default_executor 已取消」、`Accounts.tsx:534` 也写「不回落已取消的全局 default_executor」 | 从 schema 删（否则设置页永远显示一个无效下拉） |
| D8 | `core/config_schema.py:41` `twocaptcha_key` | `core/base_captcha.py` 只有 `YesCaptcha/ManualCaptcha/LocalSolverCaptcha` 三个类（`:25,58,67`），**没有 TwoCaptcha**；后端 `grep -i twocaptcha` 只命中 schema 本身；前端也只把它列进 `Settings.tsx:44` 的掩码数组 | 删，或真实现 TwoCaptcha（否则用户填的 key 永远不生效） |
| D9 | `core/config_schema.py:99` `grok_register_mode` | 作为**全局配置** 0 读取；唯一读取点是 `platforms/grok/plugin.py:216` 的 `extra.get("grok_register_mode")`（任务级 extra，不是 config）；`config_store.py:156` 只把它当迁移源 | 从 schema 删；保留 `config_store` 的迁移映射即可 |
| D10 | `core/config_schema.py:84-85` `cliproxyapi_base_url` / `cliproxyapi_management_key` | 仅作旧数据兜底（`core/config_store.py:139-140`、`services/cliproxyapi_sync.py:41`、`services/chatgpt_sync.py:46`），但仍渲染成两个可编辑字段，与 `cpa_api_url`/`cpa_api_key` 完全重复 | 设置页只留 `cpa_*`；别名键从 schema 移除、仅保留读取兜底 |

> 澄清一个容易误判的点：`chatgpt_executor` / `grok_executor` **不是死键** —— 前端用模板 `` `${platform}_executor` `` 动态读（`RegisterTaskPage.tsx:266`、`Accounts.tsx:539`、`RegisterSettingsPanel.tsx:173`）。后端不读它是因为前端把值塞进了任务请求的 `executor_type`（`api/tasks.py:82` → `services/task_runners.py:105,240`）。

### 1.8 端点无消费者

用 AST 提取 **106 条路由**，把 `{param}` 与前端模板串统一成「任意段」后在前端与测试里搜索：

| # | 位置 | 证据 | 建议 |
|---|---|---|---|
| D11 | `api/config.py:57-64` `GET /config/schema` | 端点 docstring 写「前端据此渲染设置页」；实测 `frontend/src` 里 `grep "config/schema"` **0 命中**，字段硬编码在 `PanelConfigPanel.tsx:70-100`；测试也 0 命中 | 删；或真让前端从 schema 渲染（那才能兑现 `core/config_schema.py:17` 的承诺） |
| D12 | `api/accounts.py:725` 起 `POST /accounts/check-all`（行号随并发修改漂移） | 前端 0 引用、测试 0 引用；批量测活已改走批量动作任务（`/actions/{platform}/{action_id}/batch`） | 删 |
| D13 | `api/icloud.py:317-332` `GET /icloud/accounts/{account_id}/messages` | 前端只用别名维度：`frontend/src/api/icloud.ts:325` `/icloud/aliases/${aliasId}/messages`；账号维度 0 引用 | 删（或前端补「按账号看信」入口） |
| D14 | `api/auth.py:445-453` `POST /auth/logout` | 后端实现是「换 JWT 密钥吊销全部令牌」（本 HEAD 刚加的安全修复），但前端**根本没调**：`App.tsx:462`、`Settings.tsx:367` 都是 `clearToken()` 只删 localStorage | **不要删**，补上前端调用（否则「退出登录」依然无法吊销服务端令牌，等于 F6 只修了一半） |

> 另有 13 条路由「前端无引用但测试有」（`/contribution/*`、`/sms/*`、`/outlook/*`、`/tasks/probe-status`、`/tasks/bind-2fa`、`/integrations/backfill`）：它们是给外部集成/脚本用的，不算死，但 `docs/API_REFERENCE.md` 应标注「无界面入口」。

### 1.9 未使用 import / 变量 / 重定义

`pyflakes` 255 条拆解：

- **F401 223 条 = 147 条刻意重导出 + 76 条真未使用**
  - 刻意：`services/icloud_service.py` 63、`services/sms_service.py` 16、`services/icloud/__init__.py` 25、`core/mailboxes/channels/outlook/__init__.py` 8、`core/db/__init__.py` 7（合计 119，注释都写明是兼容导入面）；`api/tasks.py:22-35` 28 条（`# noqa: E402` 缺 `F401`，测试按老名字 patch，见 `tests/test_backfill_rt_task_api.py:106,116`）。
  - **真未使用 76 条**，代表项：

```
api/accounts.py:3 select, func          api/accounts.py:5 account_repository（模块级，见 F811）
api/accounts.py:268 json as _json       api/actions.py:2 datetime, :6 json
api/actions.py:14 apply_chatgpt_status_policy   api/config.py:4 HTTPException
api/integrations.py:18 select, :21 AccountModel api/proxies.py:2 select, :4 Optional
core/base_platform.py:7 time            core/config_store.py:251 DATA_DIR, :252 BACKEND_JSON
core/proxy_pool.py:14 TAG_REGISTER      core/db/icloud_alias_repository.py:20 Iterable
core/db/merge.py:42 Optional            core/db/outlook_repository.py:174 any_of
core/db/task_repository.py:24 TaskRunModel
core/mailboxes/channels/outlook/mailbox.py:316,371,372 Session/select/OutlookAccountModel/_utcnow（4 处函数内）
main.py:178 services.chatgpt_maintenance ← **故意的副作用 import（注册周期任务），不要删**
platforms/chatgpt/protocol/mixins/platform_rt.py:20 base64, :25 Optional
platforms/grok/oauth_device.py:22 CPA_GROK_BASE_URL, CPA_GROK_HEADERS, CPA_TOKEN_ENDPOINT ← CPA 下线残骸
platforms/grok/plugin.py:22 json, :26 Path, :37 SIGNUP_PAGE_URL/mail_retries/signup_retries, :45 generate_password/random_name
services/action_execution.py:17 datetime, timezone, :22 AccountStatus, :26 update_account_model_cliproxy_sync
services/data_bundle.py:334,520 CREDENTIAL_KEY_FILE
services/icloud/{accounts,aliases,messages}.py 共 11 条
```

- **F841 5 条**（真死变量）：
  - `core/mailboxes/channels/outlook/mailbox.py:385` `session`
  - `platforms/chatgpt/protocol/mixins/add_phone.py:344` `phone_used`
  - `platforms/grok/plugin.py:607` `log_fn`
  - `platforms/grok/turnstile_mint.py:168` `exc`（只 `except ... as exc` 不记录类型，见 §3.2 E4）
  - `services/task_store_io.py:264` `_json_rows`
- **F811 25 条**：
  - `api/accounts.py` 有 **13 处函数内** `from core.db import account_repository`（`:98,135` 起，其余散见全文件；审计时最新一次读到的行号是 `98,135,376,423,463,512,595,697,735,747,771,781,791`），每一处都遮蔽 `:5` 的模块级 import —— 即**模块级那行才是死的**；
  - `services/panel_sync.py:32` 同理（`:195`、`:291` 局部重复 import）；
  - `services/action_execution.py:97,167,215,233,254` 局部重复 import `datetime/timezone/AccountStatus`（`:17,:22` 的模块级版本因此变死）；
  - `api/actions.py:211` 函数内 `import datetime`。
  - 这类重复 import 不是「无害噪声」：`:167` 与 `:233` 是两份 8 行相同的「更新时间戳」代码，属于复制粘贴痕迹（见 P5 同类）。
- 建议：CI 接入 `ruff check --select F401,F841,F811`；先把 5 个门面文件的 `# noqa: F401` 补齐（现在 `api/tasks.py:22` 只有 `E402`，所以 28 条噪声一直挂着），再把 76 条真未使用的清掉。

---

## 2. 重复实现

> 判定方法：AST 归一化（函数名/变量名/属性名/字符串常量/docstring 全部抹平）后对函数体哈希，仅在**生产代码**内比对，且只报 ≥10 行的组。得到 **28 组**，合并可减 **482 行**。

### 2.1 最大重复块

**P1 `services/task_runners.py:751-832` 与 `:923-1002` —— 两个 runner 近乎逐行相同（82 / 80 行）**

- 归一化哈希相同：`_run_probe_status` 与 `_run_refresh_token` 只差动作 id（`probe_local_status` vs `refresh_token`）与 3 条文案；内部 `_handle` 闭包（`:767-822` / `:935-992`，56 / 58 行）同样几乎相同。
- 而 `_run_batch_action`（`:835` 起）本来就是「动作 id 参数化」的通用实现，docstring 还写着「一个机制覆盖所有『批量点按钮』的场景」。
- 建议：两个 runner 直接委托 `_run_batch_action(task_id, ids, action_id=..., req=...)`，删掉两个闭包（约 **160 行**）。
- 附带格式伤：`:768` 的 `from services.action_execution import (` 之后，`:769-771` 的参数只有 8 空格（比 `from` 少一级缩进），是复制粘贴留下的。

**P2 `modules/mail/icloud_local.py` vs `modules/mail/icloud_remote.py` —— 约 190 行同构**

函数体哈希完全相同的函数：`wait_for_code`（`:269` 83 行 / `:241` 74 行）、`poll_once`（`:318` / `:282`，各 28）、`_message_text`（`:241` / `:219`）、`set_account_status`（`:166` / `:167`）、`_resolve`（`:147` / `:148`，各 18）、`get_current_ids`（`:210` / `:193`）、`_renew_lease`（`:299` / `:269`）。
连常量都各写一份：`LEASE_RENEW_INTERVAL_SECONDS = 60`（`icloud_local.py:42` / `icloud_remote.py:32`）。
建议：抽 `BaseICloudMailbox`（轮询/租约/文本提取/状态回写），子类只实现「生成/停用/取信」三个服务调用 —— 711 行可压到 ~500。

**P3 `services/chatgpt_sync.py` 的 3×2 面板写回 —— 9 个函数压成 2 个**

- `:215-231` / `:234-250` / `:253-270`：`update_account_model_{cpa,sub2api,chatgpt2api}_sync`，17 行 ×3，**唯一差别**是调哪个 `record_*_sync_result`；
- `:311-321` / `:324-334` / `:337-353`：`persist_{cpa,sub2api,chatgpt2api}_sync_result`，11 行 ×3，同上；
- 另有 `:273` `update_account_model_cliproxy_sync`（旧名，语义与 cpa 版相同）。
- 建议：`_SYNC_RECORDERS = {...}` + 两个泛型函数。

**P4 `core/db/icloud_{alias,account}_repository.py` —— 5 对同名方法同构**

`set_fields`（`:337` 31 行 / `:163` 24）、`delete`（`:865` / `:189`）、`get`（`:102` / `:78`）、`set_pool_status`（`:370`）、`find_available`（`:120`）。
建议：抽 `_JsonTableRepository` 基类（两者都已是「业务方法 + 统一 `_table()` 访问」的形状）。

### 2.2 同语义、多份实现（按影响排序）

**P5 JWT payload 解码 —— 7 份，行为不一致**

| 位置 | 差异 |
|---|---|
| `core/credential_fields.py:149` `_jwt_payload` | 带 `isinstance(dict)` 校验，返回 `Optional[dict]` |
| `platforms/chatgpt/cpa_upload.py:19` | **不校验** `isinstance(dict)`；`json.loads` 返回 list 时下游 `.get()` 会 AttributeError |
| `platforms/chatgpt/chatgpt2api_upload.py:63` | padding 用 `"=" * (-len % 4)` |
| `platforms/chatgpt/status_probe.py:60` | padding 同上，但先 `.encode("utf-8")` |
| `platforms/chatgpt/sub2api_upload.py:57` | padding 用 `4 - len % 4` + `if != 4` |
| `platforms/grok/oauth_device.py:126` | **要求 `len(parts) != 3` 才解**（两段式 token 直接 `{}`，其余 6 份接受 2 段） |
| `services/chatgpt_token_lifecycle.py:33` `decode_jwt_claims` | 与 chatgpt2api 版归一化后逐字节相同 |

建议：把 `core/credential_fields.py` 的实现提升为公开 `decode_jwt_payload()`，其余 6 份删除；`oauth_device` 的 3 段限制是行为差异，迁移时确认调用方是否需要。

**P6 时间工具 —— `_utcnow_iso` 6 份、`_utcnow` 5 份**

- `_utcnow_iso`：`platforms/chatgpt/status_probe.py:50`、`platforms/chatgpt/plugin.py:23`、`services/cliproxyapi_sync.py:26`、`services/chatgpt_sync.py:29`、`services/panel_status_sync.py:41`、`services/panel_comparison_cache.py:61` —— 6 份函数体相同。
- `_utcnow`：`core/db/base.py:35`、`services/chatgpt_sync.py:25`、`services/task_store_io.py:39`、`services/mail_imports/providers.py:36`、`services/icloud/_locks.py:23` —— 5 份相同；另有 `platforms/icloud/models.py:13` 的 `utcnow()`。
- `core/time_utils.py` 只有 `ensure_utc`/`UtcDatetime`，**没有 now 系列**，所以每个模块各自造一个。
- 建议：`core/time_utils.py` 增加 `utcnow()` / `utcnow_iso()`，全量替换 12 处。

**P7 `_get_config_value` —— 5 份，且已语义漂移**

| 位置 | 行为 |
|---|---|
| `platforms/chatgpt/cpa_upload.py:150-155` | **不 `str()`、不 `strip()`**，直接返回 `config_store.get(key, "")` |
| `platforms/chatgpt/chatgpt2api_upload.py:54-60` | `str(... or "").strip()` |
| `platforms/chatgpt/sub2api_upload.py:24-30` | 同上 |
| `services/cliproxyapi_sync.py:46-57` | 额外做别名兜底（`_CONFIG_ALIASES`） |
| `services/chatgpt_sync.py:33-40` | `strip()` + `or default` |

- 后果：同一个 `cpa_api_url` 若带尾随空格/换行，CPA 上传与 sub2api 上传拿到的 URL 不同（前者不 strip）。
- 5 份全部用 `except Exception: return ""` 包住 `config_store.get` —— 配置读失败静默变空串（见 §3.2 E3）。
- 建议：`core/config_store.py` 提供 `get_str(key, default="", aliases=())`，五处改调。

**P8 「是否 banned」谓词 —— 6 份**

`services/panel_sync.py:54`、`services/panel_push.py:50`、`services/panel_comparison.py:440`、`services/action_execution.py:90`（公开 `is_banned_status`）、`services/chatgpt_account_state.py`（走 `_lower_text`）、`services/grok_account_state.py:125`（内联 `== AccountStatus.BANNED.value`）。
`services/panel_comparison.py:443-444` 的 docstring 把重复制度化：「与 `panel_push._is_banned_status` / `panel_sync._is_banned_status` / `api/actions._is_banned_status` 同一口径（**各处一份的仓库惯例**）」—— 而 `api/actions.py:53` 现在只是 `services.action_execution.is_banned_status` 的别名，清单已过期。
建议：在 `core/models/account.py` 或 `AccountStatus` 上加 `is_banned(value)`，6 处收敛。

**P9 HTTP 错误解析 —— 3 对函数体相同**

`platforms/chatgpt/status_probe.py:107 _parse_loose_json` ↔ `services/cliproxyapi_sync.py:76 _parse_json_text`；`:118 _parse_header_error_json` ↔ `:87`（同名同体）；`:152 _extract_error_message` ↔ `:118`（11 行，归一化后相同）。
建议：抽 `core/http_json.py`。

**P10 刷新失败分类 —— 2 份，词表已不一致**

- `platforms/chatgpt/token_refresh.py:101-126`：返回 `permanent / configuration / retryable / ""`（未知 → **空串**），常量 `REFRESH_KIND_*` 在 `:76-78`。
- `platforms/grok/token_refresh.py:78-94`：返回 `permanent / configuration / retryable / "unknown"`（未知 → **"unknown"**），无常量、全裸字符串。
- 消费方也裸比较：`platforms/grok/plugin.py:534,538,540`。
- 后果：`kind == ""` 与 `kind == "unknown"` 在日志/统计里是两种东西；ADR-0006「用错误种类而不是分支」只落实一半。
- 建议：`core/errors.py` 定义 `RefreshKind` 枚举 + `classify_refresh_error(status, code, markers)`。

**P11 其余小项**

| 项 | 位置 | 行数 |
|---|---|---|
| `_decode_jwt_payload` 逐字同 | `platforms/chatgpt/chatgpt2api_upload.py:63` ↔ `services/chatgpt_token_lifecycle.py:33` | 13×2 |
| `_extract_auth` 逐字同 | `platforms/chatgpt/chatgpt2api_upload.py:78-80` ↔ `sub2api_upload.py:73-75` | 3×2 |
| `send_passwordless_otp` ↔ `resend_otp` | `platforms/chatgpt/protocol/mixins/signup.py:400-420 / :423-444` | 21+22 |
| `_log` 5 份 | `platforms/grok/{oauth_device:107,grok2api:382,oauth_browser:32,turnstile_mint:34,register_browser:155}` | 6×5 |
| `set_fields` 校验模板 6 次 | `core/db/icloud_{alias,account}_repository.py` | — |
| URL 归一化 32 处手写 `rstrip("/")` | 代表：`platforms/grok/upload.py:57,63,126,166`、`services/icloud/remote.py:83,119`、`services/panel_comparison.py:772,1160`、`platforms/icloud/remote_client.py:94`、`services/cliproxyapi_sync.py:61`；`api/contribution.py:49-54` 有 `_resolve_server_url` 但只本地用 | 32 处 |
| 时间口径两套 | 主流 `datetime.now(timezone.utc)`；残留 naive：`platforms/chatgpt/token_refresh.py:340`、`platforms/chatgpt/protocol/mixins/trace.py:98`（`strftime` 命名）、`:198`（`isoformat()+"Z"` 手工补时区） | 3 处 |
| 双存储后端 | `core/storage/__init__.py:1-25` 明说 SQLite/JSON 两套；`services/task_store_io.py:224 _finished_status_filter` 只服务 SQL 分支 | 结构性 |

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
| `scripts/` | 9 | 6 |
| `main.py` | 1 | 1 |
| **合计** | **462** | **232** |

- 既有守卫只扫 `api/services/core/platforms`（`tests/test_exception_handlers_not_too_wide.py:24`）：基线 **227**、实测 **225**，通过。
- 唯一松弛：`platforms/grok/oauth_device.py` 基线 10、实际 **8**（该文件若按 D2 删除，基线行应整行删掉）。`platforms/grok/upload.py` 基线 3，同理。
- **`except BaseException` 0 处**。

### 3.2 在吞关键错误的（按危害排序）

**E1 `core/db/repository.py:1032-1035` —— 判重查询吞掉所有 DB 异常**

```python
except Exception:
    # 该库没有 accounts 表（池库/新库）或库不可用 —— 当作没有记录。
    # 宁可少判重也不能让取号因为一次查询失败整体挂掉。
    return
```

- 位于 `AccountRepository.platforms_by_email`（`:971` 起）。注释只辩护了「没有 accounts 表」，但同时吞掉：SQL 语法错误、连接池耗尽、编码错误、磁盘满。
- 后果：邮箱→平台归属集合不完整 → 跨平台判重漏判 → **同一邮箱被重复注册**（正是这个函数要防的事）。
- 建议：只捕 `OperationalError` 且消息匹配 `no such table`；其余 `raise`（或至少 `logger.warning` 并标记结果不完整）。

**E2 `core/db/migrations.py:117-119` 与 `:199-200` —— 迁移逐库吞异常 `continue`**

- `:117` 收集「所有库里已用邮箱」、`:199` 重建 email→platform 归属；任一 engine 读失败就整段跳过。
- 后果：迁移「成功」退出，但归属少一整个平台 → 之后所有判重都错。
- 建议：收集失败列表，结束时 `logger.error` 并返回非零/抛出。

**E3 `_get_config_value` 的 5 份 `except Exception: return ""`（见 P7）**

- 位置：`platforms/chatgpt/cpa_upload.py:150`、`chatgpt2api_upload.py:54`、`sub2api_upload.py:24`、`services/cliproxyapi_sync.py:46`、`services/chatgpt_sync.py:33`。
- 后果：`config_store` 抛错（DB 锁、JSON 损坏）时上传目标 URL 变空串 → 报「未配置 API URL」，排查方向完全错。
- 建议：只在 `KeyError` 时返回默认值，其余记录并抛出。

**E4 `platforms/grok/turnstile_mint.py:168` —— `except ... as exc` 后既不记录类型也不用 `exc`**

- 与 F841 的 `exc` 是同一处：捕获了异常对象却只 `logger.warning("...")`，`type(exc)` 丢失。
- 建议：`logger.warning("...: %s: %s", type(exc).__name__, exc)`。

**E5 `core/scheduler.py:58-61` —— 周期任务间隔算不出来就永不运行**

```python
try:
    interval = float(self.interval() or 0)
except Exception:
    return 0.0
```

- `0.0` 的语义是「本次主动跳过」，配置写错的任务**永远不跑且无任何日志**。
- 建议：记一次 `logger.error`（带 job name），并用显式 `disabled` 状态区分。

**E6 `services/task_store_io.py:184-188` —— 任务快照落库失败静默返回**

- 后果：任务状态不落库、无日志；进程重启后任务从列表消失，用户只看到「任务没了」。
- 建议：`logger.warning("任务快照落库失败 task=%s", task_id, exc_info=True)`。

**E7 `platforms/grok/register_browser.py` 的选择器循环 `except Exception: continue`**

- `:222-232`、`:283-294`、`:333`、`:443-449`、`:516`：逐选择器 try/except，全失败后只报「找不到邮箱输入框（已重试 3 轮）」；真实原因（选择器语法错、页面已崩、context 被关）全丢。
- 建议：捕 `playwright.async_api.TimeoutError` 与 `Error` 两类；循环结束把 `last_error` 拼进错误消息。

**E8 `services/mail_imports/providers.py:396-400` —— 导入失败只 rollback + 计数**

- `errors.append(f"行 {record.line_number}: 创建失败: {str(exc)}")` 丢掉异常类型与 traceback；批量导入时无法区分「数据不合法」与「代码 bug」。
- 建议：`logger.warning(..., exc_info=True)`，`errors` 里带 `type(exc).__name__`。

### 3.3 守卫的盲区（含一条**实测错误**）

1. **扫描范围**：`SCAN_DIRS = ("api","services","core","platforms")` —— `modules/`（9 处）、`scripts/`（9 处）、`main.py`（1 处）不在守卫内，新增未绑定宽捕获不会被拦。
2. **只查「未绑定」**：`except Exception as exc:` 然后 `pass`/`continue`（绑定但不用）不拦。
3. **裸 `except:` 完全没拦**：实测 **2 处**，都带 `logger.debug` 兜底、影响可控，但 `except:` 会连 `KeyboardInterrupt`/`SystemExit` 一起吞：
   - `platforms/chatgpt/token_refresh.py:254`（解析 `expires` 字符串）
   - `services/turnstile_solver/api_solver.py:1080`（debug 模式下读 `data-sitekey`）

   建议守卫补一条 `ast.ExceptHandler.type is None` 的检查（基线 2，只减不增）。
4. 建议：把 `modules/`、`scripts/`、`main.py` 加进 `SCAN_DIRS`；再加一条「handler 体只有 `pass/continue/return` 且无 log 调用」的基线检查。

---

## 4. 魔法数字 / 硬编码路径 / 硬编码 URL

**M1 超时值**：`timeout=30` **46 处**、`timeout=20` 10 处、`timeout=15` 8 处。
全仓只有 iCloud 有常量：`platforms/icloud/constants.py:32 DEFAULT_TIMEOUT_SECONDS = 30`，而 `platforms/icloud/remote_client.py:48` 又重复定义了一份。
代表点：`platforms/chatgpt/cpa_upload.py:262`、`platforms/chatgpt/token_refresh.py:200,300,470,501`、`platforms/grok/grok2api.py:285`、`core/mailboxes/channels/outlook/mailbox.py:823`。
建议：`core/net.py: DEFAULT_TIMEOUT = 30` + 场景化 `HTTP_TIMEOUT_SHORT/LONG`。

**M2 `SOLVER_PORT` 默认 8889 散落 6 处**
`core/base_captcha.py:10`、`:109`（`port: int = 8889`）、`core/base_platform.py:340`、`platforms/grok/turnstile_mint.py:212`、`services/solver_manager.py:49`、`services/turnstile_solver/api_solver.py:1367`。
任何一处改默认值，其余五处静默不一致（且大多写成字符串 `'8889'`）。
建议：`services/solver_manager.py` 暴露 `solver_base_url()`，全部改调（`base_captcha.py:10` 已是函数，推广即可）。

**M3 硬编码 URL**
- 同一个第三方默认域名写死两遍：`api/contribution.py:14 DEFAULT_CONTRIBUTION_SERVER_URL = "http://new.xem8k5.top:7317/"` 与 `api/config.py:83` 的字面量（后者没 import 常量）。
- `api/config.py:87` `custom_contribution_url` 默认 `http://127.0.0.1:5000`（写死本地端口）。
- `platforms/chatgpt/constants.py:12` 与 `platforms/chatgpt/protocol/mixins/codex.py:66` 各写一遍 `http://localhost:1455/auth/callback`。
- 规模：生产代码 **97 个不同 URL 字面量、172 次出现、28 个域名**；其中 `auth.openai.com` 63 次跨 11 文件、`chatgpt.com` 30 次跨 9 文件、`api.openai.com` 12 次跨 6 文件。
- 建议：把 `platforms/chatgpt/constants.py`（D4，现在是死的）改造成真被 import 的端点表；`api/config.py` 改成 import `DEFAULT_CONTRIBUTION_SERVER_URL`。

**M4 硬编码路径**

| 位置 | 问题 |
|---|---|
| `platforms/chatgpt/protocol/mixins/trace.py:97-99` | `os.makedirs("outputs")` + `os.path.join("outputs", ...)` —— 相对 CWD，不在 `core/paths.py` 布局里（且整块不可达，见 D5） |
| `services/sms_service.py:55-60` | `Path(__file__).resolve().parents[1] / "data"` —— **绕过 `DATA_DIR`**：设了 `DATA_DIR` 时 SMS 号码缓存仍写进仓库 `data/` |
| `services/turnstile_solver/api_solver.py:184-187` | 自己复制「`DATA_DIR` 或 `parents[2]/data` + `account_manager.db`」的约定（独立进程可接受，但要与 `core/paths.py` 同步） |

正面项：`core/paths.py` 本身没有绝对路径硬编码，`_resolve_dir` 正确处理相对路径与 `~`；问题只在上述三处绕过它。

**M5 语义阈值写死，绕过既有统一入口**
`mailbox_otp_timeout_seconds` 有统一解析函数 `core/base_platform.py:104 resolve_mailbox_otp_timeout`，但仍有写死 180 的地方：`platforms/chatgpt/protocol/auth_flow.py:657,714,1024`、`platforms/chatgpt/protocol/mixins/add_phone.py:110,401`、`platforms/chatgpt/login_refresh.py:194`。
后果：用户把 OTP 超时改成 240，这几条路径仍 180 秒超时。建议全部改调 `resolve_mailbox_otp_timeout(...)`。

---

## 5. 注释与代码漂移（核对 12 处）

| # | 位置 | 注释主张 | 现状 | 判定 |
|---|---|---|---|---|
| C1 | `platforms/chatgpt/constants.py:6` | 「只保留有真实消费者的 OAuth 客户端配置（token_refresh.py 刷新 AT 时用）」 | 模块零 import；`token_refresh.py:162` 用 `PLATFORM_CLIENT_ID`；本文件 `OAUTH_CLIENT_ID` 恰是注释里被否定的 Codex id（`token_refresh.py:153` 实测 401） | **漂移（严重）** |
| C2 | `core/storage_separator.py:3-4` | 「`services/account_export.py` 与 `api/accounts.py` 都要用它」 | 两者都不用它；`account_export.py:20` 另定义一份 | **漂移** |
| C3 | `core/config_schema.py:17-18` + `api/config.py:59-62` | 「`GET /api/config/schema` 把这张表原样吐给前端…前端不必再维护第三份键名列表」 | `frontend/src` 里 `schema` 0 命中；字段硬编码在 `PanelConfigPanel.tsx:70-100`；该端点无消费者（D11） | **漂移** |
| C4 | `services/panel_comparison.py:443-444` | 「与 `panel_push._is_banned_status` / `panel_sync._is_banned_status` / `api/actions._is_banned_status` 同一口径（各处一份的仓库惯例）」 | `api/actions.py:53` 已改成 `from services.action_execution import is_banned_status as _is_banned_status`，那份「拷贝」不存在了 | **漂移（轻微）** |
| C5 | `core/base_platform.py:148` | `captcha_solver: str = "yescaptcha"  # yescaptcha \| 2captcha \| manual` | 没有 TwoCaptcha 实现（`core/base_captcha.py` 只有 3 个类）；`_make_captcha` 对 `"2captcha"` 会 `raise ValueError` | **漂移** |
| C6 | `docs/EXTENDING.md:248` | 「`_build_search_text(message)` 把各渠道 JSON 拼成可搜索文本」 | 该方法是死代码（§1.3） | **漂移** |
| C7 | `docs/EXTENDING.md:154,160` | 「`RegistrationPipeline.from_application_defaults()` 是扩展入口」 | 仓内 0 调用，整簇只有测试用（§1.4） | **漂移** |
| C8 | `api/tasks.py:637-639` | 「SQLite 版原来返回 `TaskLog`、JSON 版返回 dict…现在两种后端返回同一形状」 | `core/db/task_log_repository.py:27 _from_sqlite` 确实转 dict；`api/tasks.py:640-657` 两分支同形 | 准确 |
| C9 | `core/base_captcha.py:33-39` | 「此前是 `verify=False` + `disable_warnings()`…现在 `verify=True`」 | 同文件 `:39`、`:47` 都是 `verify=True`，无 `disable_warnings` | 准确 |
| C10 | `main.py:72-75` | 「`git diff` 不接受 `--untracked-files`（实测 exit 129）」 | 实测 `git diff --untracked-files=no` → exit **129** | 准确 |
| C11 | `platforms/chatgpt/protocol/http_client.py:151-153` | 「代理链路 5.4% 偶发 TLS 瞬断，原 session 重试实测 8/8 一次即恢复」 | `_TlsRetrySession` 存在并包住 session（`:158`），`_is_tls_handshake_error` 在 `sentinel.py:90`、`sentinel_quickjs.py:327` 复用 | 准确 |
| C12 | `core/config_store.py:152-155` | 「旧键保留原值（仍有代码/脚本读它）」 | 唯一「读」是迁移映射本身与 `plugin.py:216` 的任务 extra，不是全局配置 | **半漂移** |

小结：12 处里 **7 处已漂（C1-C7）**、1 处半漂（C12）、4 处准确（C8-C11）。按「注释为已死代码辩护」计，最严重的是 C1/C2/C6/C7。共同模式是「注释描述的重构又重构了一轮，注释没跟上」——C1/C2/C6/C7 属于**为死代码辩护**，比单纯过期更有害。

---

## 6. 模块分层越界

契约见 `docs/EXTENDING.md:43-62`：`platforms → modules → core` 是主骨架，core 不得反向 import（仅 `core/registry.py`、`core/mailboxes/registry.py` 两个加载器例外）；platforms 与 services 双向引用是既定现状。

实测（AST import 图，含 `from x import y` 的 `y` 归属）：

- `core → services`：**0**；`core → modules`：0；`core → platforms`：**1**（`core/registry.py:39`，文档点名的加载器例外）。
- `platforms → api`：0。
- `services → api`：**1**（L1）。
- `api → platforms`：3（`api/icloud.py:11`、`api/shared_mail.py:17,18`）—— 属既定现状，不算越界。

| # | 位置 | 越界内容 | 影响 / 建议 |
|---|---|---|---|
| L1 | `services/task_runners.py:44-51` | `if TYPE_CHECKING: from api.tasks import (BackfillRtTaskRequest, ...)`（6 个请求 DTO） | 服务层认识 HTTP 层 DTO，方向反了。运行时不炸（`from __future__ import annotations`），但类型检查会把 api 拖进 services 依赖图。建议下沉到 `core/schemas.py` 或 `services/task_contracts.py` |
| L2 | `api/tasks.py:648` | `from core.db.task_log_repository import _from_sqlite`（**私有**符号） | API 依赖 core 内部实现；`_from_sqlite` 改名就断。建议在仓储暴露公开 `to_dict(row)` |
| L3 | `api/outlook.py:69,77` | `from core.mailboxes.channels.outlook import OutlookMailbox`，直接调具体渠道类方法（`pool_status_summary` / `import_accounts_to_pool`） | 绕过 `core/mailboxes/registry.py` 的渠道抽象；换实现要改 API。建议加门面函数 |
| L4 | `api/tasks.py:650-657`、`:676-682`、`:828-833` | 3 处 `with Session(current_engine())` + `select(TaskLog)` 裸 SQL，且同时存在 JSON/SQLite 两分支 | API 层直接写 SQL。建议收进 `core/db/task_log_repository.py` |
| L5 | `api/accounts.py:5` vs 函数内 13 处（`:98,135` 起） | 13 处函数内 `from core.db import account_repository`，遮蔽模块级 import | 模块级 import 实际是死的（F811×13）；「每进一个函数就重新 import 一次 DB 层」是重构残留。建议只留模块级 |
| L6 | `services/panel_comparison.py:429 _match_key` | 被 4 个模块以**私有名**导入：`services/panel_sync.py:228`、`services/panel_status_sync.py:210`、`services/panel_push.py:192`、`services/panel_comparison_cache.py:284` | 事实上的公共 API 挂着下划线，且住在 1302 行的 `panel_comparison` 里（导入它=拖进整条对比链）。建议移到 `core/models/account.py` 或 `services/panel_keys.py` 并去掉下划线 |

**附带（同类味道，不算越界）**

- `api/tasks.py:216-233` 从 `services.task_store_io` / `services.task_runners` 导入 **23 个私有函数**（`_ensure_task_exists`、`_run_register`…），只为让测试按老名字 patch（`tests/test_backfill_rt_task_api.py:106,116`）。这是「测试耦合倒逼生产 API 形状」，比分层越界更难拆。
- `platforms/grok/plugin.py:1029` 的 `from .oauth_device import _decode_jwt_payload`（跨模块私有 import）—— 并入 P5 后自然消失。
- `main.py:178` 的 `from services import chatgpt_maintenance  # noqa: F401` 是**故意的副作用 import**（注册周期任务），别当 F401 清掉。

---

## 7. 建议处置顺序

**第一批（纯删，风险低，约 1,300 行）**

1. D1 `platforms/grok/upload.py`（197）+ D2 `platforms/grok/oauth_device.py`（426，先搬走 `_decode_jwt_payload`）+ T1 `sync_grok_cliproxyapi_status_batch` 与 `_probe_grok_remote_auth`（151）—— 这三处是同一件事：**Grok CPA 下线后的残骸**，一次清完。
2. D3 `core/storage_separator.py`（12）、D4 `platforms/chatgpt/constants.py`（13，端点表先并入 M3 再说）。
3. §1.3 的 19 个零引用符号（含 3 个扩展点，建议保留）+ §1.2 的 20 个测试专用符号（约 200 行）；`modules/automation`、`modules/verification`、`modules/proxy`、`modules/platforms/icloud.py` 整簇（337 行）。
4. D7-D9 三个死配置键；D11-D13 三个无消费者端点（先确认无外部脚本）。
5. 同步下调守卫基线：`tests/test_exception_handlers_not_too_wide.py:70`（oauth_device 10→删行）、`:75`（grok/upload 3→删行）。

**第二批（修正确性，优先于「删得更多」）**

6. **D5 不可达块**（`trace.py:94-103`）—— 一处挪动修好 `AUTH_TRACE_DUMP`，并补测试。
7. **D6 `account_ids` 未实现**（`api/integrations.py:82`、`:147`）—— 前端已在按筛选收窄，后端必须兑现，否则「看到的 ≠ 被改的」。
8. **D14 `/auth/logout` 前端未接** —— 后端吊销能力已就绪，前端 `App.tsx:462`/`Settings.tsx:367` 仍只 `clearToken()`。
9. E1（判重吞异常）与 E2（迁移吞异常）—— 这两条是**数据正确性**，优先级高于体量更大的 P2。

**第三批（重复收敛，风险中）**

10. P1 两个 runner 委托 `_run_batch_action`（约 -160 行）→ P3 同步写回 9→2 → P4 仓储基类。
11. P5 JWT 7→1、P6 时间工具 12→2、P7 `_get_config_value` 5→1（顺手修 strip 不一致）、P8 banned 谓词 6→1、P9 HTTP 解析 6→2。
12. P2 `modules/mail/icloud_{local,remote}.py` 抽基类（约 -190 行）。
13. L6 `_match_key` 先归位（4 个调用方，改动面小），再 L1-L5。

**第四批（防回归）**

14. CI 接入 `ruff check --select F401,F841,F811,E722,DTZ003`：先把 76 条真未使用 import、5 条 F841、2 条裸 `except:` 清到 0，再按文件加 `# noqa` 白名单（`icloud_service`/`sms_service`/`api/tasks`/各 `__init__`）。
15. 守卫扩围：`SCAN_DIRS` 加 `modules`、`scripts`、`main.py`；新增「裸 except」与「handler 只有 pass/continue」两条基线。

**不需要动的**

- `services/icloud_service.py` / `services/sms_service.py` 的 79 个 F401：注释写明是「保持旧导入面」，刻意的兼容层。
- `api/tasks.py:216-233` 的 23 个私有重导出：测试 patch 目标，属技术债但不宜在死代码清理里动。
- `core/paths.py`：路径单一真相源做得对，问题只在三处绕过它的代码（M4）。
- `platforms/` 与 `services/` 的双向引用：`docs/EXTENDING.md:47-53` 已明确是既定现状。

---

## 附：本轮复核修正（与仓内既有草稿的出入）

审计开始时 `docs/audit/2026-10-30/deadcode-quality.md` 已存在一版（随 `4680c8d..c93b71f` 一起提交）。下面这些结论在按 `c93b71f` 复核后**不成立**，已在上文改正：

| 旧结论 | 实测 | 说明 |
|---|---|---|
| 「`chatgpt_executor` 是死配置键」 | **活的** | 前端用 `` cfg[`${platform}_executor`] `` 动态读（`RegisterTaskPage.tsx:266`、`Accounts.tsx:539`、`RegisterSettingsPanel.tsx:173`），后端只经 `executor_type` 中转 |
| 「无 `except:` 裸捕获（0 处）」 | **2 处** | `platforms/chatgpt/token_refresh.py:254`、`services/turnstile_solver/api_solver.py:1080` |
| 「URL 字面量 114 个 / 196 次 / 43 域名」 | **97 个 / 172 次 / 28 域名** | 按「纯 URL 字符串常量」重算（旧口径应含了 `reference/` 或拼接串） |
| 「`core/db/repository.py:979` 判重吞异常」 | **`:1032`** | 979 是函数 docstring 区；`except Exception:` 在 1032 |
| 「`_is_banned_status` 6 份含 `api/actions`」 | 5 份 + 1 个别名 | `api/actions.py:53` 已改为 `services.action_execution.is_banned_status` 的别名 |
| 「`platforms/grok/upload.py` 未提及（只提 oauth_device）」 | **197 行整模块死** | 复核中发现的更大一块；`oauth_device` 的 `cpa_auth_filename` 正是靠它才「看起来活着」 |
| 「`module/automation` 只是测试用」 | 准确，但漏了 **整簇 337 行** | `modules/__init__.py` 的 barrel 让它在启动路径上被白加载 |
| 「重复 48 组 / 12 组 ≥14 行」 | **28 组 ≥10 行，可减 482 行** | 旧口径未排除测试、未设最小行数，短函数归一化后易误报 |

另外新增了三条旧稿没有的发现：**D5 不可达块（功能失效）**、**D6 `account_ids` 死参数（前后端契约断裂）**、**D14 `/auth/logout` 无前端调用（安全修复只做了一半）**。
