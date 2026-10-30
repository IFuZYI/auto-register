# 生产数据迁移记录（实跑）

> 对象：`/data/workspace/Register/data/`（**真实数据**，1056 个账号）
> 时间：2026-10-30
> 结论：**成功。1056 行一行不少，字段级 diff 为空，凭据全部密文化。**

## 一、备份（先做，做完才动库）

| 备份 | 位置 | 大小 | 校验 |
|---|---|---|---|
| 目录副本 | `/data/workspace/Register-data-backup-20261030-143134/` | 29 MB | 5 个 sqlite `pragma integrity_check` 全 `ok` |
| 异地打包 | `/root/Register-data-backup-20261030-143142.tgz` | 5.8 MB | 与上同源，另在 `/` 分区（与 `/data` 不同盘） |

两份都含 `secrets/credential_key`（丢了等于所有 RT/密码不可恢复）与
`secrets/totp_journal/`。

## 二、迁移前状态

```
accounts              1056 行
alembic_version       不存在（老库）
accounts.id           整数（243, 276, 280 …）
accounts.password     明文（y4EGtD, nzUVSb …）
configs 口令键        3 个非空且明文（cpa_api_key / grok2api_password / chatgpt2api_api_key）
totp_journal          1 个文件，老格式（明文 totp_secret）
```

## 三、执行的三步

```bash
cd /data/workspace/Register

# 1) 标基线（老库已有表，基线迁移只建表不能跑）
alembic stamp 77cc4baded71

# 2) 两条真迁移：去重+唯一索引 → accounts.id 换 UUID
alembic upgrade head

# 3) 存量凭据密文化（幂等，可 --dry-run）
python scripts/encrypt_existing_credentials.py
```

## 四、迁移后状态

```
alembic current       c3f8a1d64b27 (head)
accounts              1056 行（未增未减）
accounts.id           UUID 字符串（006067b3-…），旧值留在 legacy_id（1, 2, 3 …）
唯一索引              uq_accounts_platform_email 已建
accounts.password     v1: 密文，读回仍是明文
extra 六把凭证        3088 个字段全部密文化（0 个非空明文残留）
configs 口令          3 个 → v1: 密文（107 字符），GET /api/config 只回 <key>_set=true
totp_journal          1 个文件 → totp_secret_cipher
```

## 五、验证

| 项 | 结果 |
|---|---|
| 行数守恒 | 1056 → 1056 |
| 字段级 dump diff（按 `(platform, lower(email))` 对齐） | **0 差异**（仅 `id` 由整数变 UUID） |
| `encrypt_existing_credentials.py --verify` | 账号密码 0 / extra 凭证 0 / 配置口令 0 |
| 幂等 | 二次执行：待加密 0，已是密文 1056 行 |
| 真服务起在迁移后的库上 | `/api/runtime`、`/api/accounts`、`/api/config`、`/api/actions/chatgpt`、`/api/integrations/panels`、`/api/tasks` 全 200 |
| 列表契约 | `total=1056`，`has_password=true`，**不含** `password` |
| 详情契约 | 密码能解出（16 字符），**不含** `password_cipher`，`legacy_id` 保留 |
| 配置打码 | 三个口令键值回 `""`，`_set=true` |
| 2FA journal | 启动时读到了（`[2FA] 写前日志待补`：账号不在库，按设计保留文件） |

## 六、回滚

```bash
# 1) 停服务
# 2) 库回到迁移前
rm -rf data && cp -a /data/workspace/Register-data-backup-20261030-143134 data
#    或：tar xzf /root/Register-data-backup-20261030-143142.tgz -C /data/workspace/Register
# 3) 代码回到 main 的 c3445f6（重构前）
git checkout c3445f6
```

回滚后旧代码直接可用：备份库是**迁移前**的形态（整数 id、明文凭据、无
`alembic_version`）。

## 七、遗留（不影响运行，需要产品决策）

`SECRET_CONFIG_KEYS` 是 10 个键的既定集合。`maliapi_api_key`（27 字符）与
`gptmail_api_key`（15 字符）**不在**这个集合里，因此仍是明文、且不在
`GET /api/config` 的 69 键白名单中（界面看不到，只有后端内部读）。要不要
把它们也纳入口令集合，是产品决策，不是这次重构的范围。
