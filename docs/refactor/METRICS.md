# 重构度量

四个结构性指标 + 一个行为等价性检查。**行数不是指标** —— 死代码只有 92 行，
减行数没有意义；减路径数才有。

## 基线（阶段 0 实测，2026-11）

```
生产代码      52,454 行 / 207 文件
测试          55,807 行 / 211 文件 / 2,969 test
HTTP 端点     102
事故注释      293
兼容 shim     47
真死代码      92 行（0.18%）
```

| # | 指标 | 基线 | 阶段 1 后 | 阶段 2 后 | 阶段 3 后 |
|---|---|---:|---:|---:|---:|
| M1 | `from api import` 违规 | 12 | 12 | 12 | **0** |
| M2 | 字符串动作 id 散落点 | 17 | 17 | **≤ 8** | ≤ 8 |
| M3 | 鸭子类型 shim | 13 | **3** | 3 | 3 |
| M4 | 账号表示模型 | 2 | **1** | 1 | 1 |
| M5 | `except Exception` | 480 | ≤ 440 | ≤ 400 | ≤ 360 |
| M6 | 其中静默 `pass` | 80 | ≤ 60 | ≤ 40 | ≤ 20 |

M1–M4 是**结构性**的，必须达标；M5/M6 是渐进的，不达标不阻塞阶段推进。

## 命令

```bash
# M1 分层倒置
grep -rn "from api import\|import api" --include=*.py services/ platforms/ core/ | wc -l

# M2 字符串动作 id（同一 id 在 api/平台/services 各算一处）
grep -rn '"probe_local_status"\|"refresh_token"\|"upload_cpa"\|"sync_cliproxyapi_status"' \
  --include=*.py api platforms services | wc -l

# M3 鸭子类型 shim
grep -rn "class _[A-Z][A-Za-z]*:" --include=*.py api core services platforms | wc -l

# M4 账号表示（`class Account` + `class AccountModel`）
grep -rn "^class Account\b\|^class AccountModel\b" --include=*.py core | wc -l

# M5/M6 宽泛异常
grep -rn "except Exception" --include=*.py api core services platforms | wc -l
grep -rn -A1 "except Exception" --include=*.py api core services platforms | grep -c "pass"
```

## 行为等价性（比测试更强）

```bash
# 阶段 0 存基线
python scripts/dump_accounts.py --platform chatgpt > /tmp/accounts-before.json

# 每阶段末对比
python scripts/dump_accounts.py --platform chatgpt > /tmp/accounts-after.json
diff <(jq -S 'map(del(.updated_at))' /tmp/accounts-before.json) \
     <(jq -S 'map(del(.updated_at))' /tmp/accounts-after.json)
```

**空 diff = 数据层行为等价。** 阶段 1 结束时这个 diff **必须为空**（阶段 1 不改平台逻辑）。

阶段 2 起允许出现已知差异，但必须逐条解释并记录在 `docs/refactor/DIFF-LOG.md`：

| 允许的差异 | 原因 |
|---|---|
| `extra` 键从 `chatgpt_registration_mode: "refresh_token"` 变为 `"refresh_token_login"` | TokenPlan 归一 |
| `client_id` 从缺省变为实际签发者 | 修复 663/663 为空的历史 bug |
| 新增 `observations.*` 键 | 观测/意图分离 |

不允许的差异：`status` 值变化、`credentials` 任一字段丢失、`register_proxy` 变化。

## 每个阶段的"完成"判定

```
M1–M4 达标
AND 契约测试全绿（tests_A.txt 里全部文件）
AND dump diff 为空 或 差异已登记
```

三条缺一不可。**只有测试全绿不算完成** —— 测试只覆盖你想到的路径。
