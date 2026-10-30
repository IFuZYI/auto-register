# 阻塞项（C8：不确定就停下写这里，不许猜）

> 最后更新：阶段 3 结束时。
> 每条都写明「卡在哪、需要谁、怎么解」，没有「待定」这种条目。

## B-1 生产数据缺失 → T1.3 切流与 `dump diff` 无法验收

**状态**：代码已完成，验收待生产侧执行。

**事实**：本 worktree 的 `data/` 是空的（`find data -type f` 无输出）。
`T1.3` 的验收条件是「合并后行数守恒」，`equivalence` 的验收条件是
「字段级 dump diff 为空」—— 两条都需要真实账号数据。

**已就绪的代码**：

| 件 | 位置 |
|---|---|
| 合并实现（守恒校验 + 幂等 + dry-run） | `core/db/merge.py` |
| CLI | `scripts/merge_platform_dbs.py` |
| 快照导出（凭据打码） | `scripts/dump_accounts.py` |
| 测试（8 条） | `tests/test_db_merge.py` |

**解**（在生产机上依次跑）：

```bash
# 1) 基线快照
python scripts/dump_accounts.py --platform chatgpt > ~/accounts-before.json

# 2) 看有几个分库、各多少行
find data -name "*.db" -exec sqlite3 {} "select '{}', count(*) from accounts;" \;

# 3) 干跑，核对「源 N 行 = 新增 + 更新 + 跳过」
python scripts/merge_platform_dbs.py --target data/account_manager.db --dry-run

# 4) 真合并
python scripts/merge_platform_dbs.py --target data/account_manager.db

# 5) 清掉分库配置并重启
unset PLATFORM_DATABASE_URLS DATABASE_URL_GROK DATABASE_URL_CHATGPT

# 6) 对比
python scripts/dump_accounts.py --platform chatgpt > ~/after.json
diff <(jq -S 'map(del(.updated_at))' ~/accounts-before.json) \
     <(jq -S 'map(del(.updated_at))' ~/after.json)
```

`diff` 非空即回滚（`git checkout v0-refactor-baseline` + 恢复 `data/` 快照），
不要「先解释差异再继续」—— 这是数据丢失的唯一信号。

## B-2 M2a（API 面 17 → 7）需要产品决策，不是重构

**状态**：不做。已写进 `docs/refactor/METRICS.md` 的结论表。

**事实**：17 个动作 id 里，同一个能力下挂多个动作是**真实功能差异**，不是重复路径：

- `probe_local_status`（探本地状态，读 `data.probe`）
- `check_plus_trial`（查 Plus 订阅，不产出 probe）

两者同属 `PROBE`，但输入输出完全不同。把 API 面收成 6 个能力后，调用方必须再传
一个「子动作」参数才区分得开 —— 等于把 17 个 id 换成 6 个能力 + 11 个子参数，
调用方要记的东西更多。

**已经做的**：`GET /api/actions/{platform}` 每条动作带 `capability` 字段，
前端可以按能力分组渲染，不用硬编码 17 个 id。这是**加法**，不破坏任何调用方。

**要真做**（需要产品拍板 + 前端同步改）：把 `POST /actions/{platform}/{action_id}`
改成 `POST /actions/{platform}/{capability}`，子动作走 `params.panel` /
`params.mode`。前端 5 处硬编码要跟着改，且本机没有 `frontend/node_modules`，
`tsc` 跑不了 —— 盲改前端等于把 UI 交给运气。

## B-3 M5（`except Exception` 447 → ≤380）需要逐条证据

**状态**：不做。PLAN 自己写了「M5/M6 不达标不阻塞阶段推进」。

**事实**：M6（静默 `pass`）已从 71 降到 3，因为那条**只加日志、不动控制流**，
可以批量做。M5 是把宽捕获**收窄成具体类型** —— 没有证据就是盲改行为：
原来被吞掉的异常会开始往上冒，可能把「网络抖动重试一次就好」变成「任务失败」。

**要真做**：逐条读上下文，确认「这个 try 里只可能抛哪几种」，再改成
`except (A, B) as exc: logger.warning(...)`。这是长尾人工活，估 3–5 天。

## 不是阻塞、但要知道的三件事

1. **加密密钥必须与数据库一起备份**。`secrets/credential_key`（或
   `CREDENTIAL_ENCRYPTION_KEY`）丢了，所有 RT/密码不可恢复。`data_bundle`
   导出已经把密钥打进包里。
2. **`CREDENTIAL_ENCRYPTION=0` 是 kill switch**，只用于排障。关掉后新写入的是
   明文，再打开时老的明文仍能读（读侧对非 `v1:` 值原样通过），但**不会自动回填**。
3. **`auth_flow.py` 一行没动**（C6）。它的 3 处 `except Exception: pass` 保留原样，
   M6 的「3」就是这三处。
