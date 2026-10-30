# 单一账号模型 + 全量凭据加密

账号现在有两套表示：`core.base_platform.Account`（DTO，`created_at: int`、
`status: Enum`、`extra: dict`）与 `core.db.models_account.AccountModel`（ORM，
`created_at: datetime`、`status: str`、`extra_json: str`）。两者靠 6 个手写转换点
和 13 个鸭子类型 shim 桥接；`password`、AT、RT、ST、IDT、SSO 全部明文落库。

**决定：引入 `AccountRecord` 作为唯一表示，`Credentials` 作为值对象；
在仓储边界做 AES-256-GCM 信封加密，覆盖全部凭据字段（含 `totp_secret` 与配置口令）。**

## 考虑过的替代方案

- **保留双模型，只加适配器**：被否。6 个转换点与 13 个 shim 就是双模型的产物，
  不合并表示就只是把适配器写得更整齐。
- **只加密 `password` 与配置口令**：被否。AT 比 password 更值钱 —— 可直接调用
  API；RT 可无限续期。加密覆盖面必须与风险面一致。
- **加密放在 API 层**：被否。导出、面板对比、状态探测都直接读仓储，放在 API 层
  等于每个读路径各自解密一次。
- **用 SQLCipher 整库加密**：被否。会把 `probe` / 调试 / 迁移工具全部锁死，
  且无法做字段级轮换。

## 后果

- `extra_json` 加密后**不可用 SQL 查询**。高频筛选字段（`platform` / `status` /
  `email` / `created_at`）已在独立列上，其余（`plus_status`、`has_rt`）走内存
  过滤或提成生成列 —— 这是本决定的真实成本，不是零。
- **顺序约束**：去重 → 唯一索引 → 分库合并 → 加密。一旦加密就无法用
  `lower(email)` 去重或建唯一索引。
- `totp_secret` 的写前日志（`_write_secret_journal`）必须同步加密，否则
  加密被这个明文 JSON 文件绕过。不许改成「直接写 DB」—— 绑定期间 DB 写会
  `database is locked`（注释有实测）。
- 密钥轮换依赖信封前缀（现有 `v1:`），旧密文保留可读一个版本。
