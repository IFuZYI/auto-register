# 重构后功能等价性验证

> 目的：回答一个问题 —— **重构完之后，原来能用的东西现在还能不能用。**
> 不是「测试绿了」，是「用户点下去还有反应，而且反应和以前一样」。

## 一、结论

| 层面 | 结果 |
|---|---|
| 单元/集成测试（主仓库，有 `node_modules`） | **3064 passed / 0 failed / 1 skipped**（222s，`tsc` 真跑） |
| 单元/集成测试（worktree，无 `node_modules`） | **3020 passed / 0 failed / 16 skipped**（175s） |
| 黑盒冒烟（sqlite 后端） | **45/45 步通过** |
| 黑盒冒烟（JSON 后端） | **45/45 步通过** |
| 与 base commit `5ba4bac` 对比 | base **44/44**，refactor **45/45**（多的一条是新端点 `/api/config/schema`） |
| 真 uvicorn 起服务 | 6 个主端点全 200，建账号/详情/列表契约正确 |
| 注册任务真跑 | 建任务 → 执行 → `status=done` / 5 条日志 / 完成摘要，**与 base 逐字一致** |
| pyflakes 静态检查 | `undefined name` **7 → 0**（其中 5 条是真 NameError） |
| **真实生产库 dump diff** | **1056 行，字段级差异 0**（仅 `id` 按 H4 变 UUID） |
| **生产库已迁移** | `c3f8a1d64b27 (head)`、1056/1056 UUID、明文凭据 0（见 `MIGRATION-LOG.md`） |

## 二、怎么验的

### 1. `scripts/e2e_smoke.py`（黑盒，不 mock）

真启动 `main.app`（走完整 lifespan：建表、加载平台、起调度器），用 `TestClient`
打真实 HTTP 路由。`DATA_DIR` 指向临时目录，绝不碰 `data/`。

```bash
python scripts/e2e_smoke.py --sweep                  # sqlite，44 步
python scripts/e2e_smoke.py --backend json --sweep   # JSON 后端另一条写路径
python scripts/e2e_smoke.py --quiet --out /tmp/r.json
```

覆盖的步骤：运行时信息 → 账号增/查/改/详情/列表 → 导出（两种格式）→
批量删除 → 动作清单 → 面板清单 → 配置读写 → 代理增删 → 任务列表 →
**注册任务创建 + 真跑 + 日志** → 全部无参 GET 路由扫描（24 条，5xx 即失败）。

`--sweep` 用 `app.openapi()` 枚举路由 —— 新版 FastAPI 把 `include_router`
变成惰性的 `_IncludedRouter`，`app.routes` 里只看得到 2 条直挂路由（实测）。

### 2. 与 base 对比

同一份脚本拷进 `/tmp/base-e2e`（`5ba4bac` 的独立 worktree）各跑一遍，
逐步比对 `status` 与关键字段。报告里账号 id 只记 `id_type`（`str` / `int`），
不记具体值，才能跨版本 diff。

### 3. 真 uvicorn

```bash
DATA_DIR=/tmp/x uvicorn main:app --port 8123
curl /api/runtime /api/accounts /api/actions/chatgpt /api/config /api/tasks /api/integrations/panels
```

全部 200；`POST /api/accounts` 返回 UUID；`GET /api/accounts/{id}` 带明文
`password` 且不带 `password_cipher`；列表只有 `has_password`。

### 4. 真实生产库的字段级 dump diff（最强的一条证据）

主仓库 `data/` 里是**真数据**：1056 个账号 / 1658 条任务日志 / 834 个任务 /
1941 条 iCloud 别名 / 5 个代理 / 143 项配置。

做法：把 `data/` 拷两份到 scratch（**原库只读，一个字节没动**），
一份用 base 代码 dump，一份用 refactor 代码迁移到 head 后 dump，按
`(platform, lower(email))` 对齐逐字段比。

```bash
cp -a data $S/prod-base && cp -a data $S/prod-refactor
cd /tmp/base-e2e        && DATA_DIR=$S/prod-base     python scripts/dump_accounts.py --indent 0 > /tmp/dump-base.json
cd <refactor worktree>  && DATA_DIR=$S/prod-refactor alembic stamp 77cc4baded71
                          DATA_DIR=$S/prod-refactor alembic upgrade head
                          DATA_DIR=$S/prod-refactor python scripts/dump_accounts.py --indent 0 > /tmp/dump-refactor.json
```

结果：

```
base 行数: 1056 | refactor 行数: 1056
键集合相同: True
只在 base: 0 | 只在 refactor: 0
=== 字段级差异 ===
无差异（id 除外）
ref 全是 UUID 字符串: True
```

即：**1056 行一行不少、一行不多，除 `id` 由整数变 UUID（H4 决策）外，
platform / email / password / token / status / cashier_url / user_id /
region / created_at / updated_at / extra 全部逐字相同。**

> 顺带发现并修掉一个**会毁掉生产部署**的文档错误：原报告写「老库先
> `alembic stamp head`」—— 那会把库标成已到最新，唯一索引与 UUID 两条迁移
> 一条都不跑。正确顺序是 `stamp 77cc4baded71` → `upgrade head`。已改
> `COMPLETION-REPORT.md` 与 `T0.4-PRODUCTION-STEPS.md`，并在副本上实跑验证。

## 三、这一步抓到的 4 个问题（单测全绿但功能是坏的）

| # | 问题 | 影响 | 来源 |
|---|---|---|---|
| 1 | `password` 从字段改成 property 后 pydantic 不再序列化 | 建账号/详情响应里没有密码，前端「复制密码」失效 | **本次重构引入**（T1.4b） |
| 2 | `BatchDeleteRequest.ids: list[int]` | UUID 被 422，**「批量删除」按钮完全失效** | **本次重构引入**（T1.0 漏改） |
| 3 | `get_many` / `delete_many` 缺 JSON 分支 | JSON 部署下导出、批量删 500（`no such table: accounts`） | base 上就有 |
| 4 | `pool_status_summary` 裸 SQL 查 `outlook_accounts` | JSON 部署下 `/api/outlook/pool-summary` 500 | base 上就有 |

1、2 是**这次重构自己造的回归**，3、4 是**本来就坏的**。四条都已修，并各自补了
守卫测试（`test_account_password_serialization.py`、`test_accounts_uuid_pk.py`、
`test_repository_backends.py`），mutation 验证过守卫真的会红。

**教训**：单测按功能点写，天然漏掉「端点接线」。功能等价性必须有一条
不 mock 的黑盒路径兜底。

## 四、契约对照（base vs refactor）

| 项 | base `5ba4bac` | refactor |
|---|---|---|
| 建账号返回体字段 | 13 个，含 `password` | **相同 13 个**，含 `password` |
| 详情返回体 | 含 `password` 明文 | 相同 |
| 列表返回体 | 不含 `password`，有 `has_password` | 相同 |
| 导出格式 | `email_pw` / `json` | 相同 |
| 账号 id 类型 | `int` | `str`（UUID，H4 决策） |
| `/api/accounts` 列表项 | — | 多一个 `capability`（纯加法） |
| 注册任务执行结果 | `done` / 5 条日志 / 同一条完成摘要 | **逐字相同** |
| JSON 后端导出 | **500** | 200 |
| JSON 后端号池汇总 | **500** | 200 |

## 五、camoufox：从 11 failed 到 0 failed

最初全量是 `2998 passed / 11 failed`，11 条全部同一个原因：

```
EnvironmentNotReadyError: camoufox 浏览器未安装（包配对 156.0.1-beta.36）
```

这是 `platforms/grok/plugin.py` 的 fail-fast 预检**故意**报的错。在 base commit
的独立 worktree 上逐条复现，**11 failed / 58 passed，一条不差** —— 所以它是环境，
不是重构。

装好浏览器（1.2 GB）之后：**10 条立刻转绿，剩 1 条**：

| 测试 | 真因 | 处理 |
|---|---|---|
| `test_grok_platform.py::test_mint_script_ported` | 断言里写死 `/home/Register/scripts/...`（作者机器路径） | 改成仓库根相对，断言内容不变 |

`camoufox fetch` 在本机装不上的原因也查清了：`camoufox_path()` 发现缓存目录
非空但缺 `.0.5_FLAG` 会 **`rmtree` 整个目录**，于是边下边删。手工装法（含
`version.json` 的字段陷阱）已写进 `register-repo-runbook` 技能。

最终：**3020 passed / 0 failed / 16 skipped**。

## 六、本机验不了的（需要生产侧）

| 项 | 为什么 |
|---|---|
| 字段级 dump diff | 本 worktree 无 `data/`，`find data -type f` 无输出 |
| 分库合并切流 | 同上，且需 `PLATFORM_DATABASE_URLS` |
| 真实注册（出号） | 需要真邮箱服务 + 代理 + 打码平台账号 |
| 面板推送 | 需要真面板地址与密钥 |
| 前端 `tsc` | 无 `frontend/node_modules`（仓库从未有过这道门） |
| Grok 真注册 | 浏览器已装、路径已通，但出号要真代理 + 真 xAI 账号 |
