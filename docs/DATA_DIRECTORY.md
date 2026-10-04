# 数据目录规范

运行时产生的所有数据都放在仓库根的 **`data/`** 下。代码侧的唯一真相源是
[`core/paths.py`](../core/paths.py) —— 新增一类数据时只改那一处，业务代码引用
`core.paths` 的常量，不要自己拼路径字符串。

## 目录布局

```
data/
├── account_manager.db        默认库：任务、任务日志、代理、全局配置
├── platforms/                平台分库（DATABASE_URL_<PLATFORM> 指到这里）
│   └── <platform>.db
├── secrets/
│   └── credential_key        凭据加密密钥（AES-256-GCM）
├── import_backups/           导入备份包前自动留存的数据快照（可丢弃）
│   └── <时间戳>-<随机后缀>-import/
└── logs/                     应用自身日志（Turnstile solver 等）
```

| 路径 | 内容 | 生命周期 |
|---|---|---|
| `account_manager.db` | 跨平台基础设施：`task_runs` / `task_logs` / `proxies` / `configs` | **必须备份** |
| `platforms/*.db` | 各平台账号库，按需创建 | **必须备份** |
| `secrets/credential_key` | 加密账号凭据的密钥 | **必须备份**，丢了凭据全废 |
| `import_backups/` | 每次导入前自动留存的一份快照（用于回退） | 可丢弃（占用随时间累积，可手动清） |
| `logs/` | 应用自身运行日志（Turnstile solver 等） | 可丢弃 |

> **一键迁移**：界面「全局配置 → 数据迁移」可把上述**必须备份**的三项（各库 +
> 凭据密钥）打成一个 ZIP 导出，在另一台机器导入即完成迁移 —— 不用手工搬文件。
> 实现见 `services/data_bundle.py`，接口见 `docs/API_REFERENCE.md` 的
> `/api/backup/*`。日志与 `import_backups/` 刻意不进包。

## 配置方式

所有路径都由 `DATA_DIR` 派生，默认 `<仓库根>/data`。容器部署只需挂这一个卷：

```yaml
environment:
  DATA_DIR: /runtime
volumes:
  - ./data:/runtime
```

单个路径也可用环境变量覆盖（优先级高于 `DATA_DIR` 派生值）：

| 变量 | 作用 |
|---|---|
| `DATA_DIR` | 数据根目录 |
| `DATABASE_URL` | 默认库地址；不设则用 `$DATA_DIR/account_manager.db` |
| `DATABASE_URL_<PLATFORM>` / `PLATFORM_DATABASE_URLS` | 平台分库，建议指到 `$DATA_DIR/platforms/` |
| `CREDENTIAL_ENCRYPTION_KEY_FILE` | 密钥文件；不设则用 `$DATA_DIR/secrets/credential_key` |
| `CREDENTIAL_ENCRYPTION_KEY` | 直接给密钥本体（base64/hex），优先于密钥文件 |

**路径一律解析成绝对路径。** 相对路径交给 SQLite 时会按*进程工作目录*解析，
从别处启动（`python /path/to/main.py`、systemd、supervisor）就会连到另一个库上，
症状是"数据凭空消失"。这也是 `core/paths.py` 里所有常量都是绝对路径的原因。

## 旧布局迁移

历史版本把数据散放在仓库各处，启动时 `migrate_legacy_paths()` 会自动搬到
`data/` 下（在 `init_db()` 之前执行，见 `main.py` 的 lifespan）：

| 旧位置 | 新位置 |
|---|---|
| `./account_manager.db` | `data/account_manager.db` |
| `./.secrets/credential_key` | `data/secrets/credential_key` |
| `./services/turnstile_solver/solver.log` | `data/logs/solver.log` |

（该表必须与 `core/paths.py` 的 `_LEGACY_MOVES` 保持一致 —— 上面三条是它当前
的全部条目；`./mail/`、`./services/external_logs/`、`./_ext_targets/` 的搬运
随 AppleMail 渠道与本地插件管理一起删除。）

迁移语义（三条都很重要）：

1. **只搬不删。** 搬成功才移除旧位置的副本；有冲突就旧文件原地保留，
   由使用者确认后自行清理。
2. **绝不覆盖。** 目标位置已有同名文件时，保留目标那份 —— 新位置的数据
   永远优先于旧位置的残留。搬目录时逐项合并，同名项跳过并在日志里说明。
3. **失败不阻断启动。** 数据目录有问题时应用仍会起来，并在启动日志里打印
  带 `[数据]` 前缀的原因（例如 `失败 …：disk full`），而不是静默失败。

迁移是幂等的：跑第二次不会做任何事。

### 配置里存了旧路径怎么办

`configs` 表里的路径类配置**不会**被迁移改动（它们是用户数据）。相对值改成
按仓库根解析后，历史默认名会指向已不存在的旧目录。`core/applemail_pool.py`
曾把 `mail` / `data/mail` 映射到 `MAIL_DIR` 兜住这种情况，但该模块与
`data/mail/` 目录已随 AppleMail（小苹果）渠道一起删除；**新增路径类配置项时
仍要留意同样的坑** —— 表现是"数据突然用不了"且不报错，排查成本很高。

## 备份与恢复

整个 `data/` 就是需要备份的全部内容：

```bash
# 备份（数据库是 SQLite，热备前建议先停服务或用 .backup）
tar czf backup-$(date +%F).tar.gz data/

# 恢复
tar xzf backup-YYYY-MM-DD.tar.gz
```

`secrets/credential_key` 和数据库**必须一起备份**：密钥单独丢了，库里的
`credentials_cipher` 全部解不开（`decrypt` 抛 `InvalidTag`），且无补救手段；
反过来库丢了而密钥还在，只是白留一把钥匙。

## 新增数据种类时

1. 在 `core/paths.py` 加常量（并在 `ensure_data_dirs()` 里建目录）；
2. 业务代码 `from core.paths import <常量>`，**不要**写 `Path("xxx")` 这类
   相对路径；
3. 如果是旧位置的搬迁，往 `_LEGACY_MOVES` 加一条；
4. 在 `tests/test_data_paths.py` 补一条"默认值落在 data/ 下"的断言。

第 3、4 步容易漏 —— 漏了第 3 步表现为老用户的数据找不到，漏了第 4 步表现为
下次有人改路径时无声跑偏。
