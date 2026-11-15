# Task 10 — 平台插件：4 个平台的动作与注册模式声明
状态: 完成

## 结论

4 个平台共声明 **21 个动作**，全部都能被 `execute_action` 真正分发到实现（无「声明未实现」，也无「实现未声明」的孤儿动作）；`scope` 与面板注册表、前端过滤三方一致，无错标。`registration_modes` 声明的键全部在 `CONFIG_KEYS` 白名单里、默认值落在选项内、且运行时确实被读取，未发现「声明了但立刻抛错」的模式。

真正的问题在**环境检查**这一面：`check_environment` 的 `curl_cffi` 依赖只在 Kiro 上被检查，ChatGPT 只查 Node、AWS 完全没有钩子；基类也没有 `check_environment` 默认实现，于是「缺依赖」既不拦前置、也不被识别成环境错误（会白烧重试轮 + 白领邮箱）。另外 `core/registry.py` 对重复注册、非白名单注册、插件导入失败三种情况全部**静默**处理，只有 `get()` 会报错。

## 发现

### T10-1 — [Important] AWS 缺少 `check_environment`：缺 `curl_cffi` 时邮箱已被领走才炸，且不被判成环境错误

- 位置：`platforms/aws/plugin.py:92-99`（`_make_client`，`curl_cffi` 在 `platforms/aws/client.py:214` 首次 import）、`platforms/aws/plugin.py:133`（`mailbox.get_email()`）、`core/registry.py:427`（预检调用点）、`core/base_platform.py`（无 `check_environment` 默认实现）
- 复现（只读，不发网络请求，用 import 拦截模拟缺依赖）：

  `.venv/bin/python /tmp/t10_order.py`

  输出：
  ```
  aws.register raised ModuleNotFoundError: No module named 'curl_cffi'
  mailbox was touched BEFORE the dependency error: ['get_email']
  classified as environment error? False
  aws has check_environment hook? False
  ```
- 说明：`AwsPlatform` 没有 `check_environment` 类方法，`task_runners._run_register` 的预检段（`services/task_runners.py:427-435`）`getattr(...,"check_environment",None)` 拿到 `None` 直接放行。然后 `aws.register` 先 `mailbox.get_email()` 领邮箱（`plugin.py:133`），再 `self._make_client(proxy)`（`plugin.py:146`）→ `_new_session` → `from curl_cffi import requests`（`client.py:214`）才炸。同时 `core/environment._looks_environment_like` 只认 camoufox / geoip / Node 三类特征串，`No module named 'curl_cffi'` 判为**非环境错误**（可重试），于是每个账号、每一轮重试都重复「领一个邮箱 → 立刻失败」，与 `tests/test_environment_preflight.py` 记录的事故形态（camoufox 缺失烧重试轮）同构。
- 影响面：AWS 平台全部注册任务；一次 `register_retry_times=N` 的批量任务会消耗 N× 邮箱别名后才全数失败。
- 修复：给 `AwsPlatform` 加 `check_environment`，按 Kiro 同款实现（`platforms/kiro/plugin.py:625-645`）检查 `curl_cffi` + `cryptography`；并在 `core/environment._looks_environment_like` 补 `no module named 'curl_cffi'` / `'cryptography'` 分支，让漏到运行时的同错也判成不可重试。

### T10-2 — [Important] ChatGPT 的 `check_environment` 只验 Node，不验 `curl_cffi`：纯协议链路缺库时预检照常放行

- 位置：`platforms/chatgpt/plugin.py:102-111`（只调 `check_node_ready`）、`platforms/chatgpt/status_probe.py:12`（模块级 `from curl_cffi import requests`）、`platforms/chatgpt/cpa_upload.py:13-14`
- 复现：

  `.venv/bin/python /tmp/t10_env.py`

  输出（拦截 `curl_cffi` 后）：
  ```
  chatgpt preflight (normal): EnvironmentVerdict(ok=True, ...)
  chatgpt preflight (no curl_cffi): EnvironmentVerdict(ok=True, is_environment=False, non_retryable=False, ...)
  kiro preflight (no curl_cffi): EnvironmentVerdict(ok=False, is_environment=True, non_retryable=True, ...)
  ```
- 说明：`ChatGPTPlatform.check_environment` 只覆盖 Sentinel PoW 的 Node 依赖；而 ChatGPT 是**唯一纯 `curl_cffi` 协议**的平台（`plugin.py:60-66` 自己写明）。缺 `curl_cffi` 时预检仍 `ok=True`，任务继续跑，邮箱/代理已经分配。Kiro 的同类检查存在（`kiro/plugin.py:635`），两者不一致 —— 说明「该查什么」没有统一约定，而是各平台各写一份。
- 影响面：ChatGPT 注册任务；`pip` 依赖缺项（尤其升级/新机器）时，每个账号都要领邮箱、推代理后才在 `status_probe` 的模块级 import 处失败一次。
- 修复：把「纯协议公共依赖」抽成一个共享探针（如 `core/environment.check_protocol_deps()`），ChatGPT / Kiro / AWS 三处都调它；`BasePlatform` 加默认 `check_environment` 返回 `EnvironmentVerdict(ok=True)`，让「没实现」与「实现了但漏项」不再混同。

### T10-3 — [Important] `core/registry.py` 对重复注册、非白名单注册、插件导入失败全部静默，只有 `get()` 报错

- 位置：`core/registry.py:30-34`（`register`）、`core/registry.py:37-47`（`load_all`）、`core/registry.py:50-56`（`get`）
- 复现：

  `.venv/bin/python /tmp/t10_reg.py`（用内存桩类，不动生产代码）
  ```
  2) duplicate registration (same name twice):
     registry now: P2 | warnings emitted: none
  3) unsupported-name registration is silently dropped:
     decorator returned: P3 | in registry: False
  4) plugin whose import raises ModuleNotFoundError:
     load_all returned normally
     probe_plat registered? False
     (no warning printed above = silent skip)
  ```
  （第 4 项用临时包 `/tmp/t10pk/probe_plat/plugin.py` 造一个 `ImportError`，只读运行，不落仓库）
- 说明：
  1. **重复注册**：后注册者直接覆盖先注册者，无警告。前一个 `plugin.py` 的类被静默丢弃，`list_platforms()` 与 `get()` 都只看得到最后一个 —— 「同一个平台名被两个插件声明」这种真错会被吞掉。
  2. **非白名单注册**：`register()` 装饰器对 `is_platform_enabled(cls.name)` 为假的类**原样返回但不入册**（`registry.py:32-34` 无 else 分支）。写了插件、忘了加进 `SUPPORTED_PLATFORMS` 的人只会看到「平台不存在」，看不到「你的插件被白名单挡了」。
  3. **插件导入失败**：`load_all` 的 `except ModuleNotFoundError: pass`（`registry.py:46-47`）把**任何** `ModuleNotFoundError`（包括插件自己缺第三方依赖）当成「这个目录没有 plugin.py」吞掉；非 `ModuleNotFoundError` 的导入错误（`ImportError`/`SyntaxError`）则会**冒泡**整个 `load_all`，让其余平台一起加载不上。
     两种极端都不对，而当前实测的第三种行为更隐蔽：`pkgutil.iter_modules` 返回的元组解包在部分实现下会抛 `'bool' object has no attribute 'rsplit'`（见 `/tmp/t10_reg.py` 第 4 项首次尝试），说明这段代码的形状假设没有测试覆盖。
- 影响面：`main.py:148` 与 `core/scheduler.py:186` 都调 `load_all()`；插件少加载一个平台不会在启动期报错，只会在用户点「注册」时得到「平台未注册」。属验证/运维盲区。
- 修复：`register` 里对 `name in _registry` 且 `cls is not _registry[name]` 的情况打 warning 并保留首个（或直接抛）；对非白名单的类打一条明确 warning（说明 `SUPPORTED_PLATFORMS` 需同步）；`load_all` 把 `except ModuleNotFoundError` 收窄成「仅当 `<pkg>.plugin` 本身不存在」，其余一律带上模块名 re-raise 或聚合成一条启动错误。

### T10-4 — [Minor] `get_quota` 在 4 个平台里各自实现但无任何生产调用方；ChatGPT 未覆写，回落基类空 dict

- 位置：`core/base_platform.py:276-278`、`platforms/grok/plugin.py:701`、`platforms/kiro/plugin.py:611`、`platforms/aws/plugin.py:381`
- 复现：`grep -rn "get_quota" --include=*.py api/ services/ core/ frontend/src` → 除定义处与 `tests/test_grok_platform.py:149` 外**零命中**。
- 说明：基类返回 `{}`（不是 raise），所以「平台列表页/详情页」不会被打破 —— 这一项按 brief 第 5 条是**通过**的。但 ChatGPT 是唯一没有覆写的平台，若将来有页面按 `get_quota` 渲染配额，ChatGPT 会拿到空 dict 而其余三个有内容，属「隐性不一致」而非缺陷。
- 修复：要么删掉这个钩子（无消费方），要么在 ChatGPT 上补实现并加一条消费方测试。

### T10-5 — [Minor] `grok` 声明了已弃用动作 `refresh_oauth`，仅做转发（可接受，但无测试钉住转发）

- 位置：`platforms/grok/plugin.py:461`（声明）、`platforms/grok/plugin.py:669-675`（转发到 `refresh_token`）
- 复现：`platforms/grok/plugin.py:675` → `return self.execute_action("refresh_token", account, params)`；已用 mock 验证可达（`/tmp/t10_reach2.py`：`grok refresh_token: REACHED leaf`）。
- 说明：声明与实现一致（不是缺陷），但转发关系只靠代码注释说明，`tests/` 下没有断言「`refresh_oauth` 与 `refresh_token` 走同一实现」。老脚本若改用 id，行为等价但无人钉住 —— 属验证缺口。
- 修复：加一条测试断言 `execute_action("refresh_oauth", ...)` 与 `execute_action("refresh_token", ...)` 命中同一叶子函数。

## hypothesis

- **H1（未复现为主线缺陷）**：`services/action_execution.py:411-412` 的 `_PLATFORM_STATUS_POLICY` 只登记了 `chatgpt` / `grok`。`aws` / `kiro` 的 `probe` 动作返回 `data.usable`，但 `_apply_*_status` 不会被调用 —— 即「测活失败不改状态」。本次未能构造只读复现（需要 DB 行 + 端点），且 brief 范围是插件声明面而非状态策略，故只记为 hypothesis，不建议据此判 Important。
- **H2**：`core/registry.py:41` 的 `pkgutil.iter_modules(...)` 解包 `for _, module_name, _ in` 对返回 2 元组/`ModuleInfo` 的实现是脆的；本次在 monkeypatch 场景下实测抛 `'bool' object has no attribute 'rsplit'`（`/tmp/t10_reg.py` 第 4 项首跑）。真实环境（`FileFinder`）返回 3 元组，所以生产未触发 —— 属「测试之外无路可达」的脆点，非现状缺陷。

## 覆盖清单（动作声明 / 注册模式 / 环境检查 / 注册表健壮性）

### 1. 动作声明 ↔ 实现（21 项，全部可达）

验证方法：`load_all()` 后对每个平台实例化，逐条 `execute_action(id, account, params)`，并用 `unittest.mock.patch` 把**每个分支的叶子函数**替换成抛哨兵异常，确认请求确实落到了实现（脚本 `/tmp/t10_reach.py`、`/tmp/t10_reach2.py`）。

| 平台 | action id | scope | params | 实现位置 | 可达性证据 |
| --- | --- | --- | --- | --- | --- |
| chatgpt | `probe_local_status` | account | — | `plugin.py:386-404` → `status_probe.probe_local_chatgpt_status` | mock 命中叶子 |
| chatgpt | `check_plus_trial` | account | — | `plugin.py:406-423` → `status_probe.probe_plus_trial_status` | mock 命中叶子 |
| chatgpt | `sync_cliproxyapi_status` | **panel** | — | `plugin.py:425-446` → `cliproxyapi_sync.sync_chatgpt_cliproxyapi_status` | mock 命中叶子 |
| chatgpt | `sync_sub2api_status` | **panel** | — | `plugin.py:695-713` → `panel_status_sync.sync_panel_status_batch` | mock 命中叶子 |
| chatgpt | `sync_chatgpt2api_status` | **panel** | — | 同上（`ACTION_PANEL_KEYS` 分发） | mock 命中叶子 |
| chatgpt | `refresh_token` | account | — | `plugin.py:448-597` → `token_refresh.TokenRefreshManager` | mock 命中叶子 |
| chatgpt | `backfill_refresh_token` | account | — | `plugin.py:599-619` → `chatgpt_rt_backfill.backfill_account_data` | mock 命中叶子 |
| chatgpt | `bind_2fa` | account | — | `plugin.py:621-656` → `chatgpt_two_factor.bind_account_two_factor` | mock 命中叶子 |
| chatgpt | `upload_cpa` | **panel** | api_url, api_key | `plugin.py:658-669` → `cpa_upload.generate_token_json` | mock 命中叶子 |
| chatgpt | `upload_sub2api` | **panel** | api_url, api_key | `plugin.py:671-681` → `sub2api_upload.upload_to_sub2api` | mock 命中叶子 |
| chatgpt | `upload_chatgpt2api` | **panel** | api_url, api_key | `plugin.py:683-693` → `chatgpt2api_upload.upload_to_chatgpt2api` | mock 命中叶子 |
| grok | `probe` | account | — | `plugin.py:489-530` → `probe_token` | 无 AT 时明确返回错误（实测） |
| grok | `probe_refresh` | account | — | `plugin.py:532-604` → `token_refresh.refresh_via_grant` | mock 命中叶子 |
| grok | `refresh_token` | account | — | `plugin.py:606-667` → `token_refresh.refresh_via_device_flow` | mock 命中叶子 |
| grok | `refresh_oauth` | account | — | `plugin.py:669-675` 转发 → `refresh_token` | mock 命中叶子（转发生效） |
| grok | `sync_grok2api_status` | **panel** | — | `plugin.py:677-694` → `panel_status_sync` | mock 命中叶子 |
| grok | `upload_grok2api` | **panel** | api_url, username, password, nsfw | `plugin.py:696-697` → `_action_upload_grok2api` (`plugin.py:1011`) | 缺 SSO 时明确报错；`tests/test_grok_upload_grok2api_action.py::test_success_records_both_fields` 断言 `patch_data["grok2api_ingested"]` 为真 |
| kiro | `probe` | account | — | `plugin.py:580-586` → `probe_account_detail` | mock 命中叶子 |
| kiro | `refresh_token` | account | — | `plugin.py:588-604` → `probe.refresh_kiro` | 打真实 OIDC 端点前被 mock 截住；空凭据返回 `invalid_grant` |
| aws | `probe` | account | — | `plugin.py:346-356` → `probe_account_detail` | mock 命中叶子 |
| aws | `refresh_token` | account | — | `plugin.py:358-374` → `probe.refresh_builder_id` | 空凭据返回 `invalid_grant` |

- **声明未实现：0 项。实现未声明：0 项**（`execute_action` 的兜底 `NotImplementedError` 在 3 个平台实测触发正常，见 `/tmp/t10_reach2.py` 末尾三行）。
- **scope 正确性**：8 个 `panel` 动作与 `services/panel_registry.py` 的 `upload_actions` / `sync_actions` 逐条对齐（脚本 `/tmp/t10_panels.py` 输出 `scope_ok=True` ×8），且**没有孤儿 panel 动作**。反向证据：`tests/test_panel_management_actions.py::test_accounts_page_filters_panel_scoped_actions` 断言 `Accounts.tsx` 含 `a?.scope !== 'panel'` 且过滤作用在 `actions` 数据源上；`::test_account_actions_stay_on_the_account_page` 逐一断言账号类动作的 `scope != "panel"`。`tests/test_capability_registry.py::test_every_declared_action_is_registered` 断言每个动作 id 都在 `ACTION_CAPABILITIES` 里。

### 2. `registration_modes` 声明 ↔ 运行时

脚本 `/tmp/t10_modes.py`；每条键都在 `core/config_schema.py` 白名单内（否则 `PUT /api/config` 会静默丢弃），默认值都落在 `options` 内。

| 平台 | key | 类型 | 默认值 | 在 CONFIG_KEYS | 运行时读取点 |
| --- | --- | --- | --- | --- | --- |
| chatgpt | `chatgpt_registration_mode` | select ×3 | `refresh_token_login` | ✅ | `chatgpt_registration_mode_adapter.py:203-213`（`normalize` 三个值分别 → `refresh_token`/`refresh_token`/`access_token_only`，未知值回落默认，**不抛**） |
| grok | `grok_send_code_min_interval` | number (min 0) | `0` | ✅ | `plugin.py:774-776`，`_to_float(..., 0.0)` 与声明默认一致（`tests/test_config_dedup_and_registration_modes.py:285-291` 钉住这条一致性） |
| grok | `grok_oauth_exchange` | select ×2 | `"0"` | ✅ | `plugin.py:855`（`_truthy`，默认关） |
| kiro | `kiro_plan` | select ×2 | `Kiro FREE` | ✅ | `plugin.py:364`（`extra.get("kiro_plan") or DEFAULT_PLAN`） |
| aws | `aws_full_name` | select ×3 | `Test User` | ✅ | `plugin.py:114`（`extra.get("aws_full_name") or DEFAULT_FULL_NAME`） |

- **未发现「声明了但立刻抛错」的模式**：5 个键全部有实地读取点，且都带默认兜底。
- `tests/test_config_dedup_and_registration_modes.py::test_platforms_endpoint_exposes_registration_modes` 断言 `/api/platforms` 返回每平台 `registration_modes`；`tests/test_chatgpt_token_plan_settings.py:26-43` 断言 ChatGPT 的三个方案键齐备。
- 说明：`grok` 的 `supported_executors` 只有 `["browser"]`、`chatgpt`/`kiro`/`aws` 只有 `["protocol"]`，与各平台实现一致（`BasePlatform.__init__` 的回落逻辑 `base_platform.py:222-240` 实测只对**不受支持**的显式值生效）。

### 3. `check_environment`：缺依赖是否在分配资源**之前**拦截

| 平台 | 有钩子 | 检查内容 | 在 `_run_register` 预检段被调用 | 缺依赖时的实际顺序 |
| --- | --- | --- | --- | --- |
| chatgpt | ✅ `plugin.py:102` | 仅 Node（`check_node_ready`） | ✅ `task_runners.py:427-429` | 预检通过（漏 `curl_cffi`）→ 见 T10-2 |
| grok | ✅ `plugin.py:192` | camoufox 配对浏览器 | ✅ | 预检在 `mailbox.get_email()` 之前（`plugin.py:212-222` 早于 `plugin.py:734+` 的领邮箱）；`tests/test_environment_preflight.py::test_register_fails_fast_with_environment_hint` 断言顺序 |
| kiro | ✅ `plugin.py:625` | `curl_cffi` + `cryptography` | ✅ | 预检能正确拦下（实测 `EnvironmentVerdict(ok=False, is_environment=True, non_retryable=True)`）；且 `register` 里 `_source_claim` 在失败后未泄漏（实测 `source claim held after failure: False`） |
| aws | ❌ **无** | — | ❌（`getattr` 返回 None 直接放行） | 先 `mailbox.get_email()`（`plugin.py:133`）→ 再 `_make_client`（`plugin.py:146`）→ `curl_cffi` import 炸 → 见 T10-1 |

- 结论：**Grok 与 Kiro 顺序正确**；**AWS 违反了 brief 第 3 条**（资源先分配、依赖后检查）；**ChatGPT 顺序正确但检查项不全**。
- `core/base_platform.py` 没有 `check_environment` 默认实现，`task_runners.py:427` 的 `getattr(..., None)` 把「平台没写」当成「环境就绪」放行 —— 这是 AWS 漏检的结构性原因。
- 现有测试覆盖的是**分类**而非**顺序**：`tests/test_environment_preflight.py` 16 个用例中，`test_register_fails_fast_with_environment_hint`（Grok）与 `test_task_fails_fast_when_environment_not_ready`（runner 级）确实断言了「预检先于注册」，但没有任何用例覆盖 AWS 无钩子的场景 —— 这正是 T10-1 得以存在的原因。

### 4. 注册表健壮性

| 场景 | 当前行为 | 是否「响亮且可操作」 | 证据 |
| --- | --- | --- | --- |
| 未知平台名 `get("nosuch")` | `KeyError: 平台 'nosuch' 已下线` | ✅ 响亮 | `/tmp/t10_reg.py` 第 1 项 |
| 已下线平台 `get("icloud")` | 同上文案（但说「已下线」对从未存在的名字略误导） | ⚠️ 可改进 | 同上 |
| 空名 `get("")` | `KeyError: 平台 '' 已下线` | ✅ | 同上 |
| **重复注册同名平台** | 后者静默覆盖前者，无 warning | ❌ **静默** | `/tmp/t10_reg.py` 第 2 项 |
| **非白名单类注册** | 装饰器原样返回但不入册，无 warning | ❌ **静默** | `/tmp/t10_reg.py` 第 3 项 |
| **插件导入抛 `ModuleNotFoundError`** | `except ModuleNotFoundError: pass` 吞掉，平台缺席且无日志 | ❌ **静默** | `/tmp/t10_reg3.py`：`load_all returned normally` / `probe_plat registered? False` |
| 插件导入抛非 `ModuleNotFoundError` | 异常冒泡，整个 `load_all()` 失败，其余平台一起加载不上 | ⚠️ 过响（无隔离） | `/tmp/t10_reg2.py` C 项 |
| 大小写不对称 | `register` 接受 `name="CHATGPT"`（`is_platform_enabled` 归一），但入册用**原始** `cls.name`；`get("chatgpt")` 随后 `KeyError: 未注册，已注册: ['CHATGPT']` | ❌ **静默 + 自相矛盾** | `/tmp/t10_reg2.py` A 项 |

### 5. 基类默认值（不破坏平台列表页）

| 方法 | 基类行为 | 判定 |
| --- | --- | --- |
| `get_platform_actions()` | `return []`（`base_platform.py:263-268`） | ✅ 空列表，页面正常渲染「无动作」 |
| `get_quota()` | `return {}`（`base_platform.py:276-278`） | ✅ 空 dict，不 raise；4 平台中 3 个覆写、ChatGPT 回落空值（见 T10-4） |
| `execute_action()` | `raise NotImplementedError`（`base_platform.py:270-274`） | ✅ 由 `api/actions.py:483-485` 转成 HTTP 400，不是 500 |
| `check_environment()` | **不存在** | ❌ 见 T10-1 / T10-2（`getattr` 取不到 → 放行） |

---

## 附：最小复现（可粘贴，均只读、无上游调用）

T10-1（AWS 先领邮箱、后炸依赖）：

```python
import builtins; from core.registry import load_all, get
from core.base_platform import RegisterConfig; load_all()
class MB:
    calls = []
    def get_email(self): self.calls.append("get_email"); raise SystemExit
mb = MB(); aws = get("aws")(config=RegisterConfig(extra={}), mailbox=mb)
_r = builtins.__import__
builtins.__import__ = lambda n,*a,**k: (_ for _ in ()).throw(ModuleNotFoundError("x")) if n.startswith("curl_cffi") else _r(n,*a,**k)
try: aws.register()
except BaseException as e: print(type(e).__name__, e); print("mailbox:", mb.calls)
```

T10-3（插件导入失败被静默吞掉）：在任意可 import 的目录建 `probe_plat/plugin.py`，内容为一行 `import camoufox_not_installed_dep`，把该目录 append 到 `platforms.__path__`、把 `probe_plat` 加进 `SUPPORTED_PLATFORMS`，调 `core.registry.load_all()` → 返回 `None`，`'probe_plat' in _registry` 为 `False`，无任何输出。