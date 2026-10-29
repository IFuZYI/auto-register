# 账号主键改用 UUID

账号当前是 `AccountModel.id: int` 自增主键。分库塌缩、跨实例 bundle 导入、面板登记、
日志引用都指向「需要一个跨实例稳定、不依赖插入顺序的标识」。

**决定：`accounts.id` 改为 TEXT UUID（uuid4）主键，`AccountRecord.id: str`。
`(platform, email)` 的唯一约束保留 —— UUID 是身份，邮箱仍是注册判重的业务键。**

## 范围：只有 `accounts` 换主键

其余表**保留 int 主键**，因为 `ORDER BY id` 在这些地方是承重语义：

| 表 | 依赖 | 出处 |
|---|---|---|
| `icloud_aliases` | 取号队列按 id 升序（失败放回队首 → 又排第一） | `icloud_alias_repository.py:709-714` |
| `outlook_accounts` | 池内取号与列表顺序 | `outlook_repository.py:75, 163` |
| `icloud_accounts` | 列表与同步顺序 | `icloud_account_repository.py:65`、`services/icloud/accounts.py:263` |
| `task_logs` | 日志分页按 `id DESC` = 时间倒序 | `api/tasks.py:742` |
| `proxies` | 无外部引用 | — |

JSON 后端也依赖这条：`core/storage/json_backend.py:131` 明确写「数字主键按数值排，
其余按字符串 —— 与 SQLite 的 `ORDER BY id` 观感一致」。UUID 字符串的字典序是随机的，
一旦这些表换 UUID，队列公平性与日志倒序全部失效。

## 考虑过的替代方案

- **双键（`id` int + `uid` uuid）**：用户否 —— 两个身份概念，未来还要解释「用哪个」。
- **uuid7（时间有序）**：Python 3.12 无 `uuid.uuid7()`，需要额外依赖；当前量级
  （663 行）不需要插入局部性。若将来超过 10 万行再评估。
- **所有表统一 UUID**：被否 —— 上表的 `ORDER BY id` 是承重语义，改完要给每张表
  补一个 `seq` 列，收益为零。

## 后果

- 91 处 `account_id: int` 签名、`accounts` 相关 13 个路由、前端 5 处
  `account_ids: [acc.id]`、导出格式全部要改类型。
- **阶段 1 不再保证「零平台改动」**：签名改动会穿透到 `platforms/`。行为仍必须
  完全一致（dump diff 为空），只是改动面变大。
- 列表排序不能再用 `ORDER BY accounts.id` 当创建顺序 —— 用 `created_at`
  （已有索引，且模型注释写明列表默认按它倒序）。
- 迁移：逐行 backfill uuid，保留 `legacy_id` int 列一个版本用于回滚；一个版本后删除。
- 面板登记里的 `remote_id` 是**远端** id，与本决定无关；本地侧新增的引用一律用 UUID。
