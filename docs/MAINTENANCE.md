# 维护与升级手册

本文是**改动这个仓库时**的操作清单：改完代码要同步动哪些文件、加一个新东西要
改哪几处、哪些地方漏了会出现「看着正常、实际没生效」的静默故障。

与另外几篇的分工：

| 文档 | 回答的问题 |
| --- | --- |
| [README.md](../README.md) | 这是什么、怎么跑起来、怎么用（核心功能 + 快速开始 + 使用教程） |
| [FEATURES.md](FEATURES.md) | 平台能力、邮箱服务、面板对接、导出格式的完整说明 |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Docker 部署、环境变量、数据目录与一键迁移 |
| [EXTENDING.md](EXTENDING.md) | 新增平台 / 注册流程 / 邮箱渠道**怎么写**（代码骨架） |
| **本文** | 改完之后**还要动哪些文件**（接线清单）、升级与迁移步骤、验证门禁 |
| [API_REFERENCE.md](API_REFERENCE.md) | 每个接口的请求/响应形状 |
| [DATABASE_MODULARITY.md](DATABASE_MODULARITY.md) | 分库、邮箱唯一键、跨库查询 |
| [DATA_DIRECTORY.md](DATA_DIRECTORY.md) | 数据目录布局与路径规则 |

**核心原则：这个仓库的大部分契约是「两处以上保持一致」，而漏改一处几乎都不会
报错。** 下面每节都写了「漏了会怎样」——那是判断该不该改的依据。

---

## 1. 升级 / 更新部署

### 1.1 本地直跑（`python main.py`）

```bash
git pull
pip install -r requirements.txt          # 依赖有变动时
cd frontend && npm install && npm run build && cd ..   # 前端有变动时（产物进 static/）
kill <旧进程 PID> && python main.py      # 必须重启：改了代码不重启还跑旧代码
```

**改完代码必须重启进程。** 这个服务是手工拉起的，没有热重载（`APP_RELOAD=1`
才有）。判断有没有生效看版本戳：

```bash
curl -s http://127.0.0.1:8000/api/runtime
# {"code_version":"<进程启动时加载的提交>","disk_version":"<磁盘当前 HEAD>",
#  "stale":false,"booted_at":"...","pid":12345}
```

`stale: true` = 磁盘上有新提交但进程还是旧代码。**这个端点就是为这件事存在的**
（实测踩过：修复提交后没重启，之后三次任务全在旧代码上跑，症状与修复前一模一样）。

> 别用 `pkill -f main.py` —— 开发机上会误匹配其它同名进程（Hermes 自己也有
> 一个 `main.py`）。按 PID kill。

### 1.2 Docker Compose

```bash
git pull
docker compose up -d --build     # 重建镜像并重启
docker compose logs -f app       # 看日志
```

构建相关的两个坑（都实测过，写在 Dockerfile 注释里）：

- **基础镜像可换**：Docker Hub 不可达时（TLS 失败 / 连接重置），在 `.env` 里设
  `NODE_IMAGE` / `PYTHON_IMAGE` 为可达镜像源，不用改 Dockerfile。
- **浏览器版本跟随 pip 包**：构建脚本读 `camoufox` 包自带的 `browser-pin.json`，
  `CAMOUFOX_VERSION` / `CAMOUFOX_RELEASE` 只是包不带 pin 时的兜底。升级
  `camoufox` 依赖后浏览器自动跟着升，**不要**手动去改这两个值。

### 1.3 数据库迁移（自动）

`init_db()` 在每次启动时跑 `run_migrations()`，对**默认库 + 每个平台库**各跑一遍：
补列、建唯一索引、合并重复行、回填数据。迁移是幂等的，不需要手工执行 SQL。

**新增列时的硬约束**（踩过，见 `core/db/migrations.py` 的注释）：

> 补列（`ALTER TABLE ... ADD COLUMN`）**必须排在任何用 ORM 读那张表的迁移之前**。
> SQLModel 的 `select()` 会把新列一起写进 SELECT，列还没补上时整条语句直接抛
> `no such column`，**启动就炸**。

同理，加新字段到 `AccountModel` 时：老库里没有那一列，`SELECT` 会失败 ——
迁移是唯一的补救路径，别指望 `create_all`（它不改动已存在的表）。

### 1.4 数据迁移（换机器）

界面「全局配置 → 数据迁移」：导出把**默认库 + 平台分库 + 凭据密钥**打成一个
ZIP，在另一台机器导入即完成迁移。实现见 `services/data_bundle.py`。

需要知道的边界：

| 事项 | 行为 |
| --- | --- |
| 打包内容 | 默认库、`data/platforms/*.db`、`secrets/credential_key` |
| 不打包 | `data/logs/`（可丢弃）、`data/import_backups/`（防套娃） |
| 导入前 | 自动备份到 `data/import_backups/<时间戳>-<随机后缀>-import/` |
| 导入时 | 有运行中任务 → **409 拒绝**（先把任务停掉） |
| 密钥 | 必须一起导 —— 只导库不导密钥，导入端所有加密字段都解不开 |
| 版本 | `manifest.format_version` 高于程序支持的版本 → 拒绝（先升级程序） |

> ⚠️ **平台库的发现方式是「glob `data/platforms/*.db`」。** 如果你把某个平台库
> 用 `DATABASE_URL_<PLATFORM>` 指到了**别的目录**（不在 `data/platforms/` 下），
> 那个库**不会**进导出包 —— 迁移过去就少一个平台的账号。要迁移自定义位置的库，
> 先手工拷文件，或把它挪回 `data/platforms/`。

### 1.5 升级后的自检

```bash
curl -s http://127.0.0.1:8000/api/runtime            # stale 必须是 false
curl -s http://127.0.0.1:8000/api/auth/status        # 200 = 后端活着
curl -s http://127.0.0.1:8000/api/solver/status      # {"running":true}
curl -s -o /dev/null -w '%{http_code}\n' \
     http://127.0.0.1:8000/api/config                # 设了密码时应当是 401
```

---

## 2. 改完之后要同步哪些文件（接线清单）

### 2.1 新增一个**面板**

以 `foo` 面板为例。**五处，漏一处症状都很难查**（这份清单的出处是加
chatgpt2api 时漏了三处、面板页直接 404）：

| # | 文件 | 改什么 | 漏了会怎样 |
| --- | --- | --- | --- |
| 1 | `services/panel_registry.py` | 加一项（`key` / `label` / `desc` / `url_key` / `github` / `secret_key` / `platform` / `upload_action` / `sync_action`） | 面板不存在，侧栏没有这一项 |
| 2 | `services/panel_comparison.py` 的 `FETCHERS` | 加一个远端拉取函数 | 面板页报「未知面板: foo」（404） |
| 3 | `services/panel_comparison_cache.py` 的 `_PANEL_PLATFORMS` 与 `_panel_credentials` | 本地平台映射 + 凭据读取分支 | 对比拉不到数据（本地 0 / 远端 0） |
| 4 | 平台插件的 `get_platform_actions` | 声明上传动作，标 `scope="panel"` | 动作冒回账号页菜单；或面板页没有上传按钮 |
| 5 | `api/config.py` 的 `CONFIG_KEYS`（+ `SECRET_CONFIG_KEYS` 若是口令）<br>前端 `PanelConfigPanel.tsx` 的 `PANEL_SECTIONS` | 配置键白名单 + 表单字段 | 保存被静默忽略（响应里的 `ignored` 会有它）；口令会明文回填进表单 |

**多平台面板**（同时服务多个平台，如 CPA 托管 ChatGPT + Grok）额外要：

- 注册表里声明 `platforms: ['chatgpt', 'grok']` 与 `upload_actions` /
  `sync_actions`（平台 → 动作 id 映射）；
- 对比行的 `platform` 字段必须带上（匹配键是 **(平台, 邮箱)**，不带就全显示成
  「未上传 + 仅远端」—— grok2api 面板实测踩过：22 个本地账号全部显示未上传，
  而两边邮箱 100% 重合）；
- 前端会自动出现平台选择器（`shouldShowPlatformFilter`），单平台面板不渲染。

**面板动作接线**（每个面板三个动作，用户要求四个面板都有）：

| 动作 | 实现位置 | 说明 |
| --- | --- | --- |
| 同步远端状态 | `services/panel_status_sync.py`（CPA 走 `services/cliproxyapi_sync.py`） | 读远端状态回写本地；CPA 必须探活（列表无状态），其余读列表自带状态 |
| 更新远程凭证 | `POST /api/integrations/panels/{key}/push`（`services/panel_push.py`） | 推「未上传 + 本地较新」；新建式面板（sub2api/chatgpt2api）配 `_PANEL_PUSH_KIND`（`api/integrations.py`）与删除器 |
| 更新本地凭证 | `POST /api/integrations/panels/{key}/sync`（`services/panel_sync.py`） | 拉「远端较新」 |

新增面板时三处都要有对应实现（对比页按钮按 `sync_action` + 端点存在与否渲染）。

**上传代理开关**（可选）：面板导入格式支持代理字段时，在
`services/chatgpt_sync.py` 的 `_UPLOAD_PROXY_SWITCHES` 加一项 +
`api/config.py` 白名单 + `PanelConfigPanel.tsx` 的字段与 `BOOLEAN_KEYS`。
上传路径要把 `upload_proxy_for(panel, extra)` 传给上传函数。

**自动上传**（注册完自动推过去）在 `services/external_sync.py` 的 `sync_account()`
里加分支，并按需加 `<panel>_enabled` 开关键。

**测试**：`tests/test_panel_registry.py`（形状）、`tests/test_panel_management_actions.py`
（动作存在性、scope、多平台）、`tests/test_secret_config_contract.py`
（口令必须进打码清单）。`test_panel_management_actions.py` 的 `_rows_for()`
是**逐面板点名**的假响应工厂 —— 新面板要在那里补一段，否则那条通用测试会 fail
并提示「测试没有为面板 X 准备假响应」。

### 2.2 新增一个**平台**

代码骨架见 [EXTENDING.md §1](EXTENDING.md)。接线部分：

| # | 文件 | 改什么 | 漏了会怎样 |
| --- | --- | --- | --- |
| 1 | `core/registry.py` 的 `SUPPORTED_PLATFORMS` | 加平台名 | 插件被**静默忽略**（有 `@register` 也不加载） |
| 2 | 前端 `frontend/src/lib/platforms.ts` 的 `PLATFORMS` | 加一条（label / color / tagColor） | 侧栏、标签、筛选项缺这一平台；`getPlatformLabel` 回落成原始英文名 |
| 3 | 前端 `frontend/src/App.tsx` | 一般不用改（平台列表来自 `/api/platforms`） | — |
| 4 | 配置键 `api/config.py` | 加 `<platform>_executor`（若支持执行器） | 执行器设置保存被忽略 |
| 5 | `core/paths.py`（可选） | 想默认分库就加进 `DEFAULT_PLATFORM_DB_FILES` | 与默认库共库（也能跑，只是不好单独备份） |

> `frontend/src/lib/platforms.ts` 里的 `PLATFORMS` 是**唯一**需要手工补的平台
> 清单（颜色/图标这类展示信息后端不下发）。`PLATFORM_OPTIONS` /
> `PLATFORM_FILTER_OPTIONS` 由它派生，任务页、账号页、历史页、iCloud 页都读它。

**测试**：`tests/test_config_dedup_and_registration_modes.py`
（`registration_modes` 形状与默认值一致性）、`tests/test_db_modular.py`（分库）。

### 2.3 新增一个**配置键**

| # | 文件 | 改什么 | 漏了会怎样 |
| --- | --- | --- | --- |
| 1 | `api/config.py` 的 `CONFIG_KEYS` | 加键 | **`PUT /api/config` 静默丢弃**（响应里回 `ignored` 列表 + 打日志）。实测踩过：加了键忘进白名单，界面保存成功却完全不生效 |
| 2 | `api/config.py` 的 `SECRET_CONFIG_KEYS` | 若是口令 | `GET /api/config` **回明文**，页面一打开就把口令塞进 DOM |
| 3 | 前端表单（`PanelConfigPanel.tsx` / `Settings.tsx` / `RegisterSettingsPanel.tsx`） | 加字段 | 界面上没有这一项 |
| 4 | 前端 `BOOLEAN_KEYS`（存 `"0"`/`"1"` 的开关） | 若是开关 | 读出来是字符串 `"0"`，界面按真值渲染（开关恒亮） |

**口令类的三条硬规则**（`tests/test_secret_config_contract.py` 钉住）：

1. 前端提交前要摘掉 `<key>_set` 只读标记；
2. **口令留空 = 不修改**（空值不能提交，否则保存一次就把口令抹掉）；
3. 服务端对「空串」也拦一道（脚本/curl 直接提交空串同样不写库）。

**平台专属旋钮**（只对某个平台有意义的参数）不要写进前端表单 —— 由插件在
`registration_modes` 里声明（形状见 EXTENDING），界面通用渲染。写进前端就等于
「某平台的设置长在别处」，且插件与界面两份定义会分叉。

### 2.4 新增一种**邮箱渠道**

代码骨架见 [EXTENDING.md §3](EXTENDING.md)。两个必须做的动作：

1. 在 `core/mailboxes/__init__.py` 里 `import` 一次（触发自注册；渠道包
   `core/mailboxes/channels/__init__.py` 只维护 `__all__` 清单）；
2. 若是依赖 `services/` 的渠道（要读库里的主号凭据），放 `modules/mail/`，
   并把模块名加进 `core/mailboxes/registry.py` 的 `_OPTIONAL_PROVIDER_MODULES`。

**漏了 import 的症状**：渠道文件存在但查不到，`create_mailbox()` 报
「未知邮箱提供商」—— 失败点在注册任务的深处，看不到任何「渠道没注册」的字样。

前端侧：邮箱服务页的分组定义在 `frontend/src/lib/mailboxSections.ts`
（新渠道若要有自己的配置页，在那里加分组 + 在 `App.tsx` 加路由）。

**别名**：同一个 builder 可以挂多个 provider 名（`outlook` / `microsoft` /
`mail_import` 是同一条渠道的三个名字 —— 见
`core/mailboxes/channels/outlook/__init__.py` 的注册处）。老库里存过的旧名
要么挂别名、要么进 `_LEGACY_PROVIDER_ALIASES` 收敛，否则升级后直接报未知。

**测试**：`tests/test_reusable_modules.py::test_every_channel_module_imports_and_registers`
会自动发现「文件存在但没注册」；新渠道补一条 `available_providers()` 断言。

### 2.5 新增一个**周期任务**

```python
# services/my_service.py 末尾
from core.scheduler import register_job
register_job("my_job", interval_seconds=lambda: ..., runner=lambda: ...)
```

然后在 `main.py` 的 `lifespan` 里 `import services.my_service` 一次（触发注册）。
**不要**把业务任务写进 `core/scheduler.py` —— core 不能反向依赖 services。

`interval_seconds()` 返回 0 = 本次不跑；`runner()` 抛异常时**不推进** `last_run_at`
（下个 tick 就重试，这是刻意的重试策略）。细节见 EXTENDING §4.1。

### 2.6 新增一类**数据**（要进备份包）

| # | 文件 | 改什么 | 漏了会怎样 |
| --- | --- | --- | --- |
| 1 | `core/paths.py` | 加常量（+ `ensure_data_dirs()` 里建目录） | 路径散落各处，从别处启动就读另一个位置 |
| 2 | `core/paths.py` 的 `_LEGACY_MOVES`（若是搬迁） | 加一条 | 老用户的数据找不到 |
| 3 | `services/data_bundle.py` 的 `_collect_export_files()` | 让它进导出包 | **备份/迁移静默丢这一类数据** |
| 4 | `tests/test_data_paths.py` | 加「默认值落在 data/ 下」断言 | 下次有人改路径时无声跑偏 |

> 第 3 步是本文写完之后**唯一没有自动门禁**的一条（见 §4）。导出、备份、预览
> 共用 `_collect_export_files()` 这一处枚举 —— 新增数据种类只改那里，但漏改
> 不会报错，只会在换机器时发现少东西。

### 2.7 新增一个**平台动作**（账号级）

在插件的 `get_platform_actions()` 里声明，`execute_action()` 里实现。
`scope` 字段决定它出现在哪：

- 不写 / `scope != "panel"` → 账号页菜单（对账号本身的操作）；
- `scope="panel"` → 只在「面板管理」页（目标是外部面板的操作）。

**落库**：动作结果要写回账号时，`api/actions.py::_apply_action_result()` 是
落库的唯一入口。上传类动作走 `_UPLOAD_SYNC_WRITERS` 表（加一行即可）——
漏加的症状是「界面永远不显示上传状态」，在 sub2api / chatgpt2api 上各踩过一次。
动作返回的 `data` 里的凭证字段由 `core/credential_fields.py` 的
`canonical_writes()` 归一后落 extra（只收凭证字段，message/status 等展示字段不落）。

### 2.8 新增 / 修改一个**凭证字段**（AT / RT / SSO …）

凭证字段**全系统一处定义**：`core/credential_fields.py` 的 `CREDENTIAL_FIELDS`
（规范名 + camelCase 别名 + 短标签）。改这里等于改全系统凭证口径，消费方包括：

| 消费方 | 用注册表的什么 |
| --- | --- |
| `services/panel_comparison.py` | `compare_aliases()`（比对哪些字段） |
| `services/panel_sync.py` / `panel_push.py` | `sync_aliases()`（拉/推哪些字段） |
| `services/account_export.py` / `api/accounts.py` | `export_names()`（导出/导入字段） |
| `api/actions.py` | `canonical_writes()`（动作结果落库归一） |
| `platforms/grok/plugin.py` 等读侧 | `get_credential()` / `token_column_credential()` |

新增字段的步骤：① 注册表加一项；② 按需把规范名加进 `account_export` 的
`EXPORT_FIELDS` 与 `api/accounts._IMPORT_EXTRA_KEYS`（往返不丢）；③ 跑
`tests/test_credential_fields.py`（注册表契约 + 往返测试）。

**`token` 列（历史遗留列）的镜像规则**：token 列 = **平台主凭证的镜像**
（chatgpt → AT，grok → SSO），规则表在注册表的 `_TOKEN_COLUMN_FIELDS`。
写侧一律走 `sync_token_column()`；读侧兜底走 `token_column_credential()`
（grok 列上是 OAuth 形态 JWT 时拒绝——那是被 AT 盖过的脏值，启动迁移
`_normalize_grok_token_column` 会修库）。加新平台时要在映射表里登记，
漏登记的后果是凭证合并静默不生效（`test_credential_fields.py` 会点名）。

---

## 3. 仓库自带的「契约测试」是什么

前端没有单元测试运行器，很多跨文件的一致性靠 **pytest 里的契约测试**守着
（读源码/构建产物做断言）。改前端时这些测试会告诉你哪里漏了：

| 测试文件 | 守什么 | 改了什么时候会红 |
| --- | --- | --- |
| `test_frontend_layout_contract.py` | 表格列宽和 `scroll.x` 的匹配、按钮组换行、tsc 编译、全站 90% 缩放（`--ui-scale`/`--app-vh`/form-grid 等分）、内容上限随视口增长（`max(1440px, 75vw)` / `--w-page: max(1200px, 62.5vw)`，防低缩放窄条孤岛）、AT/Plus 列日期不截断 | 改列宽忘了同步 `scroll.x`；TS 类型错误；整屏高度用了裸 `100vh`；form-grid 档位不再等分；内容上限回退固定值 / theme.ts 又注入 `--w-page` |
| `test_secret_config_contract.py` | 口令「输入后不可查看」的前后端约定 | 新口令字段没进打码清单 / 忘了摘 `_set` 标记 |
| `test_panel_registry.py` | 面板注册表形状（key/url_key 唯一性、github 链接） | 面板字段改名、url_key 撞车 |
| `test_panel_management_actions.py` | 面板动作存在性、`scope`、多平台接线 | 新面板没补 `_rows_for()`；动作漏标 scope |
| `test_deploy_config_contract.py` | compose/Dockerfile/.env.example 的一致性 | 新增 `${VAR}` 插值却没写进 `.env.example` |
| `test_config_dedup_and_registration_modes.py` | 注册方式声明与运行时兜底一致 | 插件改了默认值没同步声明 |
| `test_contrast_gate_contract.py` | 主题色板两套齐、preset 标签钉住 | 加新颜色没做对比度处理 |
| `test_data_paths.py` | 数据路径默认落在 `data/` 下 | 新增路径常量写错基准 |
| `test_credential_fields.py` | 凭证字段注册表（字段/别名/标签、token 列镜像、导出往返含 SSO、启动迁移把被 AT 盖过的 grok token 列修回 SSO 镜像） | 改 `core/credential_fields.py` 的字段表；消费方不再同源 |
| `test_panel_time_single_side.py` | 时间判定的单侧回落：一侧有 iat、另一侧回落记录时间（`mixed` 档）仍能判方向；iat 两侧可比时优先于记录时间 | 改 `compare_credential_time` 的回落口径 |
| `test_account_status_removal.py` | 账号状态四值（registered/expired/invalid/banned）、`AccountStatus.normalize` 读侧兜底、启动迁移归一历史行并删除 `trial_end_time` 列 | 改状态枚举或删除迁移 |
| `test_status_semantics.py` | 状态语义分档（正常/过期/失效/禁用）：AT 过期判「过期」、被拒未过期判「失效」、封禁措辞（`deleted or deactivated`）判「禁用」、sign-in session 措辞**不判封禁**、正向恢复；前端标签映射与动作接线 | 改 `services/chatgpt_account_state.py` 的判定优先级；前端状态标签改回英文/旧文案 |
| `test_grok_status_semantics.py` | grok 状态判定对齐 grok2api：SSO 被拒/`invalid_grant` 判「失效」、`blocked-user` 判「禁用」、402/403 无额度仍有效、正向恢复 | 改 `services/grok_account_state.py` |
| `test_chatgpt_token_refresh_verification.py` | 刷出的 AT 真校验（`/backend-api/me`）、**只返还原本 AT（未换发）→ 走登录流程换发**、登录链兜底、封禁识别、轮换 ST 不被冲空 | 改 `platforms/chatgpt/token_refresh.py` 的校验或登录兜底 |
| `test_environment_preflight.py` | 环境预检与错误分类：camoufox 配对探测、Node 运行时探测、任务级预检（runner 在分配邮箱前拦截）、环境错误判不可重试（dead-end 提前收手） | 改 `core/environment.py` 的判定；平台去掉 `check_environment` 钩子 |
| `test_chatgpt_auto_maintenance.py` | ChatGPT Token 自动维护：临期窗口内随机时刻（至少提前 1h）、封禁/达失败上限不重复尝试、失败退避 1h→6h、未换发算失败、每轮限量防高并发、stale 计划清理、scheduler 注册与配置白名单、前端开关接线 | 改 `services/chatgpt_maintenance.py` 的窗口/退避/限量逻辑；去掉周期任务注册；开关漏进 CONFIG_KEYS 或前端 BOOLEAN_KEYS |
| `test_runtime_version_stamp.py` | 版本戳语义（未跟踪文件不算脏） | 改 `_read_git_version` |
| `test_frontend_layout_contract.py::test_typescript_still_compiles` | `npx tsc -b` 0 错误 | 前端类型错误（无 node 时跳过） |

**前端可执行的行为测试**（真实跑 TS 函数，不是正则）：

| 脚本 | 跑什么 |
| --- | --- |
| `frontend/scripts/run_account_format_checks.mjs` | `lib/accountFormat.ts` 的纯函数（53 条断言，含状态标签 正常/过期/失效/禁用） |
| `frontend/scripts/run_panel_filter_checks.mjs` | `lib/panelComparison.ts` 的筛选/计数（32 条断言） |
| `tests/test_timezone_display.py` | 三个时区下编译并执行 `lib/time.ts` |

新写前端纯逻辑时照这个模式：**逻辑放 `frontend/src/lib/`，配一个
`frontend/scripts/run_*.mjs` 用 rolldown 打包后真实执行**。留在组件里的逻辑
只能靠对源码做正则来测 —— 那种测试改了实现就可能失效，是假安全网。

---

## 4. 验证门禁

### 4.1 改动后跑什么

```bash
# 后端全量（约 90 秒，1500+ 用例）
python -m pytest tests/ -q

# 前端三件套
cd frontend && npx tsc -b && npm run lint && npm run build
```

**eslint 的基线是 55 problems（50 errors + 5 warnings）** —— 这是历史存量，
不是「0 才算过」。改动不应让这个数字变大；顺手修可以，但别为了清零做大改。

### 4.2 测试没有覆盖到的（要人工确认）

诚实清单 —— 这些**没有**自动门禁，靠本节的人工步骤：

| 缺口 | 怎么确认 |
| --- | --- |
| 自定义位置的平台库不进备份包 | 导出后解开 ZIP，核对 `platforms/` 下是否每个库都在（见 §1.4） |
| 面板远端接口的真实形状 | 加面板/改 fetcher 后，在面板页点「重新拉取对比」，确认行数与远端实际数量对得上 |
| 浏览器路径（注册/登录） | 需要真实网络与代理；按平台写一次性脚本，别指望单测 |
| 前端交互（点击、筛选、批量动作） | 起服务后用浏览器实测：改平台筛选要核对**表格行数、筛选条计数、按钮上的数字**三者一致 |
| 容器内行为（Xvfb、PID 1、浏览器） | `docker compose up -d --build` 后 `docker ps` 看 health，再跑一次注册任务 |

### 4.3 提交前

```bash
git status --short          # 确认没有把 data/、.env、凭据带进来
git diff --stat
```

**脱敏规则**（公开仓库）：真实住宅代理凭据（IP / 用户名 / 密码）、个人邮箱
地址、真实出口 IP、各类 API Key 一律不得进仓库 —— 用 `proxy.example` /
`sample123456` 这类示例值。`data/`、`.env`、`.worktrees/`、`reference/` 都在
`.gitignore` 里，别用 `git add -f` 绕过。

---

## 5. 常见「静默故障」速查

改代码时最值得记住的一张表 —— 这些都是**不报错**的：

| 现象 | 根因 | 修法 |
| --- | --- | --- |
| 改了代码行为没变 | 进程没重启 | 看 `/api/runtime` 的 `stale`；重启 |
| 面板保存成功但不生效 | 键不在 `CONFIG_KEYS` | 看 `PUT /api/config` 响应里的 `ignored` |
| 界面永远不显示上传状态 | 动作没进 `_UPLOAD_SYNC_WRITERS` | 加一行（§2.7） |
| 插件加了但不加载 | 不在 `SUPPORTED_PLATFORMS` | 加白名单（§2.2） |
| 邮箱渠道「文件存在但查不到」 | 忘了 `core/mailboxes/__init__.py` 里 import | 加 import（§2.4） |
| 面板对比全是「未上传 + 仅远端」 | 远端行没带 `platform` | fetcher 里打上平台（§2.1） |
| 面板页 404「未知面板」 | `FETCHERS` 没加 | 加 fetcher（§2.1） |
| 口令保存一次就没了 | 前端提交了空口令 | 留空 = 不修改（§2.3） |
| 换机器后少一个平台的账号 | 库在 `data/platforms/` 之外 | 见 §1.4 |
| 分库后账号「消失」 | 裸 `select(AccountModel)` 漏了平台库 | 走 `account_repository` |
| 启动报 `no such column` | 新列的迁移排在 ORM 读之后 | 调整迁移顺序（§1.3） |
| 前端「保存了但没生效」 | 表单字段名与配置键不一致 | 两处对照（§2.3） |

---

## 6. 删除功能 / 清理时的规则

按用户要求删过几轮（本地插件管理、协议注册路径、临时邮箱渠道、一次性脚本…），
沉淀下来的规则：

1. **用显式清单，不用 glob** —— 删之前 `git ls-files` 全量核对、`grep` 验证零引用；
2. **删完 grep 全仓**：注释、文档、测试、`.env.example`、前端 placeholder 里
   可能还留着它的名字（一个残留的旧名字会被读成「这功能还在」）；
3. **已删除的值要有读侧兜底**：老库里可能存过已删除的 provider / 面板 key，
   读取路径要收敛（`_LEGACY_PROVIDER_ALIASES` / `resolve_panel_key` 就是这个作用），
   否则老用户升级后直接报「未知」；
4. **别把别的服务塞进归一表**：`team_manager` / `codex_proxy` 已整体删除，
   把它们归一到 `cpa` 会让调用方静默拿到另一个服务的配置 —— 宁可报错；
5. **删完跑全量**：测试计数应当「减去随删除退场的用例」，数目对不上说明
   删多了或删漏了。

---

## 7. 版本戳与发布

`main.py` 在 import 期算一次版本戳（`_BOOT_CODE_VERSION`），`/api/runtime` 返回：

- `code_version`：**进程启动时**加载的提交（刻意在 import 期算好 —— 现查磁盘的话
  提交之后它会跟着变，把「没重启」藏起来）；
- `disk_version`：磁盘当前 HEAD；
- `stale`：两者不一致。

版本戳只算**已跟踪文件**的改动（`--untracked-files=no`），脏时附改动内容的短指纹。
原因：根目录长期有未跟踪目录（`.hermes/` 这类本地状态），把它们算作脏会让戳被
永久钉成 `+dirty`，此后真改了已跟踪文件戳也不变 —— `stale` 恒为 false，
**恰好漏掉这个端点要防的情况**。
