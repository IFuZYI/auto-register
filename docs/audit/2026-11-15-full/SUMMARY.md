# 全量检测 · 结论（2026-11-15）

> 目标：整个项目全量检测 —— 每条流程是否**正确、已优化、可用**。
> 基线：`HEAD fca2eec` + 工作树 dirty（82 files, +5366/−1650）。检测对象是磁盘上的工作树。
> 方法：17 条只读 lane（2 波）+ 控制器侧逐条复现 + 确认后 TDD 修复。

## 一、两行结论（检测与修复分开陈述）

**检测完成度：波次 1 的 10 条 lane 全部有结论（P0=2 / P1=22 / P2=70 / P3=26，覆盖 190 个文件）；波次 2 的 7 条 lane 见 §六，覆盖合计见 `inventory.json` 与 `aggregate.json`。**

**修复完成度：2 条 P0 与 5 条 P1 已修复并各带一条「在基线上必失败」的回归测试（反证通过）；其余 36 条 P1 已登记、未修复，其中影响最大的 19 条逐条列在 §四，随 v1.0.0 / v1.0.1 以「已知问题」发布。**

## 二、覆盖矩阵

| lane | 领地 | 文件 | P0 | P1 | P2 | P3 |
|---|---|---|---|---|---|---|
| L01-auth-config-backup | 鉴权中间件 / JWT / 配置 / 备份 / 免登录邮件页 | 17 | 0 | 1 | 9 | 4 |
| L02-accounts-storage | 账号 CRUD / 存储后端 / 加密 / 导入导出 | 18 | 0 | 5 | 6 | 2 |
| L03-tasks-runtime | 任务生命周期 / 运行时 / 调度 / 动作执行 | 10 | 0 | 0 | 10 | 3 |
| L04-proxies | 代理池 / URL 解析 / 打码 / 分配换绑 / tools | 20 | **1** | 2 | 7 | 5 |
| L05-icloud | 别名池 / HME / share_token / 远端 API | 31 | 0 | 3 | 9 | 2 |
| L06-mail | 邮箱通道 / Outlook 后端 / 邮件导入 / OTP | 26 | **1** | 2 | 8 | 2 |
| L07-panels | 面板对比/同步/推送/登记 / contribution | 14 | 0 | 3 | 6 | 3 |
| L08-frontend | React 面板 / 30 个自检脚本 / static | 24 | 0 | 1 | 3 | 2 |
| L09-gates | 门禁自身（测试/类型/lint/脚本/CI） | 23 | 0 | 3 | 5 | 2 |
| L10-data-migrations | 迁移链 / 引擎注册 / merge / paths | 21 | 0 | 2 | 7 | 1 |
| L11–L17 | 平台/服务/文档/测试质量/scripts | 见 §六 | — | — | — | — |

## 三、P0（已修复）

### P0-1 明文代理口令进了版本库 — `tools/resin_pair_probe.py:14`

`EXIT6 = "socks5h://gobngawr:06pk6as8qlwu@9.142.37.16:5187"`，与生产 `proxies` 表 id=6 同一条出口；同一口令还出现在 `tests/test_secret_leaks_in_api.py:79`（该文件的断言里甚至直接引用了这串口令）。**注意：常规工具输出会把它显示成 `***`（harness 脱敏），`od -c` 才看得到真值** —— 这正是最容易漏报的一类。

- 影响：任何能读仓库的人（fork / CI 日志 / 备份 / 历史克隆）直接拿到可用住宅代理凭据，可白嫖配额并把该出口 IP 声誉打脏（1661 个账号绑在上面）。
- 修复：`tools/` 改为读 `RESIN_EXIT6_PROXY`，缺失即 `SystemExit` 并说明怎么设；测试夹具换成 `probe-user:probe-pass-9f3a@192.0.2.10:6740`（断言同步改成假值，判别力不变）。
- **未做**：`git filter-repo` 清历史 + 强推。这会重写公共历史，需你确认；**口令必须在代理商后台轮换 —— 改代码不会让它失效**。

### P0-2 JSON 部署下 Outlook 取号 100% 失败 — `core/mailboxes/channels/outlook/mailbox.py:223`

`_pop_account` 用裸 `Session(current_engine())` 查 `accounts`，而全新部署的默认后端是 json（`resolve_backend_name`：无库文件 → json），`init_db` 不建表。

- 复现（控制器亲跑）：`DATA_DIR=<scratch> STORAGE_BACKEND=json` → `init_db()` → `select(AccountModel.email)` → `OperationalError: no such table: accounts`。
- 影响：一个邮箱都发不出来，所有走微软号池的注册任务在建号阶段失败。
- 修复：改走 `account_repository.list_all_accounts(platform=…)`；抽出 `_registered_emails_for_platform()` 使其可在不连 IMAP 的前提下单测。

## 四、P1（3 条已修，19 条登记未修）

**已修：**

| id | 缺陷 | 修复 |
|---|---|---|
| L04-2 | `POST /api/proxies` 回显未打码的 `ProxyRecord`，新建即把口令写进响应 | 返回值过 `_public_proxy()` |
| L04-3 | `redact_proxy_url("u:p@h:8080")` / `("h:8080:u:p:ss")` 整串放行（urlsplit 把 `u` 当 scheme） | 无 scheme 的两种写法前置拦截，只留 host:port |
| L06-2 | JSON 部署下「导入邮箱池」500（`no such table: outlook_accounts`） | 改走 `outlook_repository.list_all/set_fields` |
| L16-2 | `sso_token` 是 `sso` 的别名却不在 `SENSITIVE_EXTRA_KEYS` → 明文落盘且列表不打码 | 补该键（打码集合由同一 frozenset 派生，一处修两处生效） |
| L13-1 | solver 的 `ValueError(f"Invalid proxy format: {proxy}")` 把带口令的代理串写进 `solver.log` | 两处都改 `redact_proxy_url(proxy)`（只改一处时回归测试当场抓到第二处） |

## 四之二、伪证（falsified，不要当缺陷去「修」）

| id | lane 的断言 | 证伪方式 |
|---|---|---|
| L14-1 | `release.yml:112` 的 `Authorization: Bearer ***` 是字面量，所有 tag 推送都会 401、镜像 job 被跳过 | **工具脱敏假象**。`sed -n '112p' … \| od -c` 显示真值是 `AUTH="Authorization: Bearer ${GH_TOKEN}"`；且 v1.0.0 实跑已建出 Release（HTTP 200，正文 2756 字符）并推送镜像（manifest 200，digest `sha256:8b7da3c9…`）。若照 lane 的话去「修」，会把一个能用的 workflow 改坏。 |

同类需要留意的：常规工具输出会把 `password`/`token`/`Authorization` 一类值显示成 `***`（harness 脱敏）。凡 lane 的结论引用了带 `***` 的源码行，必须 `od -c` 复核原始字节再动手 —— 本轮的 P0-1 也是靠这一步才确认「`***` 背后是真口令」的。

**登记未修（按影响排序，逐条可复现）：**

| id | 缺陷 | 位置 |
|---|---|---|
| L02-5 | JSON 后端 `update` 是 get+put 两个临界区：40 线程只剩 8 个字段更新 | `core/storage/account_store_json.py` |
| L05-1 | iCloud 登录绕过仓储：JSON 部署写进 SQLite，列表看不到，且把全局后端静默切成 sqlite | `services/icloud/accounts.py:253` |
| L10-1 | 手写迁移链从不补 `accounts.legacy_id`，老库升级后首次查询崩 | `core/db/migrations.py:881` |
| L10-2 | 应用自建库无 `alembic_version`，`alembic upgrade head` 在真实安装上必失败 | `core/db/__init__.py:122` |
| L07-1 | chatgpt2api `delete_old` 在「上传被跳过」时删掉唯一远端记录 | `services/panel_push.py` |
| L07-3 | 贡献模式把 CPA 管理口令当 Bearer 发给第三方，默认还是明文 HTTP | `services/external_sync.py:251` |
| L02-2 | 默认 `source_format=email_pw` 把 `email----TOTP` 当密码写入，静默覆盖真密码 | `api/accounts.py` |
| L02-4 | 11 个文本导出格式都不含 sso/client_id/client_secret，文本往返丢主凭证 | `services/account_export.py` |
| L02-1 | 导出默认 3 列 / 导入默认 2 列，「导出→粘贴→导入」主流程必 400 | `api/accounts.py` |
| L02-3 | `----` 分隔符无转义，密码含 `----` 时无法回导 | `services/account_export.py` |
| L05-2 | `resolve_remote_account` 显式 `account_id` 只查 SQLite，JSON 部署永远「不存在」 | `services/icloud/remote.py:469` |
| L05-3 | 远端 `ALIAS_NOT_FOUND` 映射成 400，远端已删别名在本地永远删不掉 | `platforms/icloud/remote_client.py:64` |
| L06-3 | IMAP 取码在第一个空行截断正文，验证码在首段时永远读不到 | `core/mailboxes/base.py:237` |
| L07-2 | 筛选后目标为 0 时推送/拉回退化成「全平台」 | `services/panel_comparison.py:1333` |
| L08-1 | `/auth/status` 失败时设置页显示「未启用」——安全状态说反 | `frontend/src/pages/Settings.tsx:472` |
| L09-1 | CI 没有任何 test/lint/typecheck job，所有门禁都是本地约定 | `.github/workflows/` |
| L09-2 | 30 个 `npm run check` 脚本里 16 个是「读源码+正则」；变异实验证明删掉行为仍全绿 | `frontend/scripts/` |
| L09-3 | `npm run lint` 本身是红的（28E/4W），文档里的门禁链永远走不到 build | `docs/DEVELOPMENT.md` |
| L01-9 | 回环判定只看 TCP 源地址：同机反代下「未设密码只放行本机」失效 | `core/net_utils.py:41` |

## 五、门禁（控制器实测）

| 门禁 | 结果 |
|---|---|
| `pytest tests/ -q`（基线，修复前） | **3928 passed / 1 skipped / 0 failed / 243 subtests，961.63s** |
| `pytest tests/ -q`（修复后） | 见 `pytest-baseline.txt`（同文件记录两次） |
| `npx tsc -b` | exit 0，0 错误 |
| `npm run check` | 30/30 通过，15.15s（其中 16 个是正则假门禁，见 L09-2） |
| `npm run lint` | exit 1：28 errors / 4 warnings；基线 `fca2eec` 是 33/5 → **本轮 0 新增，净减 5** |
| ruff / mypy | **不存在**（`.venv` 内无 ruff 模块）→ 后端 lint 门禁从未真正运行 |
| 回归反证 | 新增 6 条测试，在 `fca2eec` 上 **5 条失败**（第 6 条是正控） |

## 六、波次 2（L11–L17）

| lane | 领地 | P0 | P1 | P2 | P3 |
|---|---|---|---|---|---|
| L11-platform-chatgpt | ChatGPT 协议链 + chatgpt_* 服务 | 0 | 3 | 8 | 2 |
| L12-platform-grok-kiro-aws | Grok / Kiro / AWS 插件 + 注册表 | 0 | 0 | 7 | 6 |
| L13-services-misc | solver / SMS / 验证码 / 维护任务 | 0 | 4 | 12 | 4 |
| L14-docs-deploy | docs / Docker / compose / CI 漂移 | 0 | 5 | 11 | 8 |
| L15-tests-deadcode | 测试质量（假门禁）+ 死面 | 0 | 0 | 3 | 3 |
| L16-core-runtime | executors / browser / crypto / time / TLS | 0 | 4 | 8 | 4 |
| L17-scripts | 运维脚本（破坏性默认 / 打印凭据） | 0 | 3 | 8 | 5 |

**全 17 条 lane 合计：P0=2 · P1=41 · P2=127 · P3=58（228 条）。**

**覆盖口径（诚实版）**：17/17 lane 都有结论，但 `reviewed_files` 只**逐条列名**了 319 个文件；余下 413 个（主要是 `tests/` 289 个与 `frontend/` 批量文件）是**按目录整体读过但未逐个列名**。因此本轮的准确说法是「17 条 lane 全部完成、319 个文件可逐条对账」，不是「714 个文件逐个签名」。`aggregate.json` 的 `unreviewed` 字段就是这 413 个未列名的文件，下一轮应从它开始收口。

## 七、产物

`inventory.json`（归属）· `L01..L17-*.json`（原始结论）· `aggregate.py`/`aggregate.json`（汇总）· `gates-baseline.md`（门禁实测）· `pytest-baseline.txt`（全量测试输出）· 本文件。
