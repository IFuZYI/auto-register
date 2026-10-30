# 鲁棒性用错误分类表，不用分支

现在每个动作自己判错误：`api/actions.py:142-262` 有 9 个 `action_id` 分支，
`chatgpt_account_state.py` 有 6 个状态分支，`token_refresh.py` 有自己的
`permanent / configuration / retryable` 三分类，Grok 侧 `grok_account_state.py`
用同一套词汇又写了一遍。全仓 480 处 `except Exception`，其中 80 处直接 `pass`。

每加一种上游错误，就要在 4–5 个地方各加一个 `if "marker" in text`。

**决定：引入 `ErrorKind` 枚举 + 一张规则表 + 一张策略表。动作只返回
`Outcome(error_kind=...)`，状态与重试由策略表决定。**

```python
class ErrorKind(str, Enum):
    TRANSIENT = "transient"    # 408/425/429/5xx/网络 → 重试，不动状态
    PERMANENT = "permanent"    # invalid_grant / 号没了 → 不重试，落 invalid
    CONFIG    = "config"       # invalid_client / 缺 client_id → 不重试，不动状态
    UNKNOWN   = ""
```

## 考虑过的替代方案

- **保持 per-action if 链**：被否。这是过去一年分支膨胀的直接来源。
- **用 retry 装饰器统一重试**：被否。装饰器只能表达「重不重试」，表达不了
  「落不落状态」；`configuration` 类错误必须「不重试且不动状态」。
- **把所有异常包成自定义异常类**：被否。上游返回的是 HTTP 状态 + 文本，
  在边界处分类比在每个调用点抛不同异常便宜。

## 后果

- 新增一种上游错误 = 在 `_KIND_RULES` 加一行，不动任何 `if`。
- `configuration` 类**不动状态**这条不变量必须在策略表里显式表达 —— 它是
  「663 个账号 0 个有 client_id」那次事故的直接教训：把配错误判成号坏会误杀好号。
- 状态映射同样表驱动（`PROBE_STATE_TO_STATUS`），保留三条既有不变量：
  banned 粘性、只有 expired/invalid 可恢复、configuration 不动状态。
- `except Exception` 的收敛是渐进的，不阻塞阶段推进（见 METRICS 的 M5/M6）。
