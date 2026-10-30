# 重构完成报告（v1.0-refactored）

> 分支 `refactor-v2`，base `5ba4bac`，33 个提交，4 个阶段 tag。
> 每个提交单独可 revert；数据层有两个 `alembic downgrade` 回滚点。

## 一句话

把「同一语义的 N 条实现路径」收敛成一条：**服务层不再认识 HTTP 层（M1=0）、
账号只有一种表示（M4=1）、鸭子类型空壳清零（M3=0）、凭据零明文落盘、
配置键一处定义**。外部行为不变 —— 2,998 条测试全绿，唯一的 11 个失败是
camoufox 浏览器未装（base commit 上逐条复现过）。

## 指标

| 指标 | 基线 | 终态 | 目标 | |
|---|---:|---:|---:|---|
| M1a 服务层反向依赖 API | 14 | **0** | 0 | ✅ |
| M1b 私有 helper 跨层引用 | 5 | **0** | 0 | ✅ |
| M2 策略层动作 id 分支 | 17 | **5** | ≤8 | ✅ |
| M3 账号/代理鸭子类型空壳 | 6 | **0** | 3 | ✅ |
| M4 账号表示模型 | 2 | **1** | 1 | ✅ |
| M5 `except Exception` | 471 | 447 | ≤380 | 见 BLOCKERS B-3 |
| M6 静默 `pass` | 80 | **3** | ≤25 | ✅ |
| 配置键定义处 | 3 | **1** | 1 | ✅ |
| 凭据明文落盘 | 7 类 | **0 类** | 0 | ✅ |
| 生产代码 | 52,454 行 | 53,105 行 | — | 涨 651 行（新增能力层/schema/加密/合并工具） |
| 测试 | 2,929 通过 | **2,998 通过** | — | +69 条 |

## 对运维有影响的四处变化

### 1. 数据库

| 变更 | 回滚 |
|---|---|
| `accounts.id` 从自增整数换成 UUID 字符串，旧值留在 `legacy_id` | `alembic downgrade b7c1e4a92d30` |
| `(platform, lower(email))` 表达式唯一索引 | 同上 |
| `accounts.password` 列**名不变**，值变成 `v1:` 密文 | 设 `CREDENTIAL_ENCRYPTION=0` 后重写即可（读侧兼容明文） |
| `extra_json` 里六把凭证（AT/RT/IDT/ST/SSO/totp_secret）变成密文 | 同上 |

**升级顺序**（老库，已有数据、无 `alembic_version`）：

```bash
alembic stamp 77cc4baded71     # 只写版本行，不建表 —— 表已经在了
alembic upgrade head           # 跑 b7c1e4a92d30（去重+唯一索引）+ c3f8a1d64b27（UUID）
alembic current                # 必须是 c3f8a1d64b27 (head)
```

⚠️ **不要 `alembic stamp head`**：那会把库标成「已到最新」，两条真正的迁移
（唯一索引、UUID）一条都不跑，服务启动后模型要 UUID 而库里还是整数。
空库才直接 `alembic upgrade head`（基线迁移自己建表）。

实测：拿生产库副本走这条路径，1056 行全部完成 UUID 化，`alembic check`
报 `No new upgrade operations detected`。

**密钥**：`secrets/credential_key`（或 `CREDENTIAL_ENCRYPTION_KEY`）。
**丢了等于所有 RT/密码不可恢复**，必须与数据库一起备份。`data_bundle` 导出已包含。

### 2. 接口

- `GET /api/actions/{platform}` 每条动作多一个 `capability` 字段（`probe` /
  `refresh` / `backfill` / `bind_2fa` / `upload` / `sync_status`）。**纯加法**，
  老调用方不受影响。
- `GET /api/config/schema` 新增：返回 69 个配置键的身份（键名/分组/是否口令），
  **不含任何值**。前端可据此渲染表单。
- 账号相关路径参数从 `int` 变 `str`（UUID）。批量接口的 `account_ids` 接受
  `list[str | int]`，传旧整数不 422，但会作为 `missing` 返回（不再解析到账号）。

### 3. 分库

`PlatformDatabaseRegistry` 接口保留；没有 `PLATFORM_DATABASE_URLS` 时恒返回默认
engine。合并工具见 `scripts/merge_platform_dbs.py`，切流步骤见 `BLOCKERS.md` B-1。

### 4. 删掉的功能

**纯手机号注册**（−2,195 行）。手机**绑定**保留（`services/sms/*`、
`mixins/add_phone.py` 一行没动）—— Codex RT 撞 add-phone 墙时它是唯一过关路径。

## 怎么验

```bash
/data/workspace/Register/.venv/bin/python -m pytest -q tests/     # 2,998 passed
python -m compileall -q core services platforms api
alembic check                                                     # No new upgrade operations
```

门禁数字（阶段末）：

```bash
grep -rn 'from api import' --include=*.py services/ | wc -l        # 0
grep -rnE '^[[:space:]]*class _(A|SyncAccount|UploadAccount|JsonProxy)\b' \
  --include=*.py api core services platforms | wc -l               # 0
grep -rn '^class Account\b\|^class AccountModel\b' --include=*.py core | wc -l   # 1
```

## 怎么回滚

| 粒度 | 做法 |
|---|---|
| 单个任务 | `git revert <sha>` |
| 整个阶段 | `git checkout v0-refactor-baseline`（或其余 tag） |
| 数据库 | `alembic downgrade b7c1e4a92d30` → `77cc4baded71` |
| 加密 | `CREDENTIAL_ENCRYPTION=0`（读侧兼容明文，不会自动回填） |

## 已知限制（不是 bug，是取舍）

1. **M5 没做**：447 处宽捕获保持原样，收窄需要逐条证据（`BLOCKERS.md` B-3）。
2. **M2a 没做**：API 面仍是 17 个动作 id；收成 6 个能力需要产品决策 + 前端同步改
   （`BLOCKERS.md` B-2）。能力分类已经以 `capability` 字段暴露。
3. **`auth_flow.py` 一行没动**（C6），它的 3 处 `except Exception: pass` 是 M6 的「3」。
4. **`legacy_id` 保留一版**：`alembic downgrade` 靠它还原整数主键。下个大版本可以删。
5. **旧整数链接不再解析**：`normalize_account_ids` 放行正整数（不 422），但查不到，
   会作为 `missing` 返回。没有做 `legacy_id` 回查 —— 双读会让每次 miss 多打一次库。
