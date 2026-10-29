# 塌缩平台分库：保留接口，只留一个库

平台分库（`DATABASE_URL_<PLATFORM>` / `PLATFORM_DATABASE_URLS`）把一个库拆成
「默认库 + 每平台一个库」。代价已经具体发生过：

- `outlook/mailbox.py:140-161` 写成跨库子查询，直接 `no such table: accounts` →
  500 发生在导入/取号那一刻，看起来像「导入功能坏了」
- `icloud/aliases.py:159-164` 只能在 Python 里做跨库比对（先查主库拿名单）
- `core/db/registry.py:99-101` 的 `_lock` 非重入，锁内调 `engine_for` 自死锁，
  注释专门警告
- 建表要传表对象维持依赖方向（`register_schema(platform, tables)`）
- 迁移要做 N 次（`migrations.py` 791 行 × 平台数）
- 会话工厂两套：`current_engine()` 与 `platform_session()`

而量级是 **663 个 chatgpt 账号**（`chatgpt_registration_mode_adapter.py:375` 的注释）。
SQLite 单表百万行毫无压力。

**决定：塌缩成单库。保留 `platform_session()` / `PlatformDatabaseRegistry` 的
调用签名（内部恒返回默认 engine），`DATABASE_URL_<PLATFORM>` 读但不生效并 warn 一次。**

## 考虑过的替代方案

- **把分库做完（ATTACH + 跨库视图）**：被否。为了一个不存在的规模问题引入
  更多 SQLite 特性，调试成本更高。
- **直接删掉 registry，改 60 处调用点**：被否。一次改 60 处无法逐段验证，
  保留签名可以一个版本内灰度。
- **保持现状，只修跨库查询**：被否。跨库是结构问题，修一处还会有下一处。

## 后果

- 合并脚本要把 N 个 db 文件按 `(platform, email)` upsert 进单库；冲突行默认保留
  `updated_at` 最新的一条（dry-run 先出冲突清单）。
- `uq_accounts_platform_email` 唯一索引终于可以建（分库时每个库各建一份，
  跨库唯一性无人保证）。
- `mailbox_pool_session()` 塌缩成 `Session(current_engine())`，Outlook 池的
  「池库」概念消失，`_pop_account` 里的跨库注释随之失效。
- 未来真需要分库时，`url_for()` 改回读配置即可，调用方零改动。
