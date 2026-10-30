# 测试基线（阶段 0 结束时）

## 命令

```bash
cd /data/workspace/Register/.worktrees/refactor
/data/workspace/Register/.venv/bin/python -m pytest -q tests/
```

注意：项目 venv 在 `/data/workspace/Register/.venv`（Python 3.12.3）。
**不要用 `python3`** —— 那是 Hermes 运行时的 3.14 环境，连 `sqlmodel` 都没装。

## 结果

```
2934 passed, 11 failed, 16 skipped, 186 subtests passed  (约 165 秒)
```

## 这 11 个失败是**既有环境问题**，不是回归

在 base commit `5ba4bac` 的独立 worktree 上跑同一批测试，11 个**全部复现**：

| 测试 | 根因 |
|---|---|
| `test_executor_drives_path.py` × 4 | camoufox 浏览器未安装 |
| `test_grok_platform.py::test_mint_script_ported` | 同上 |
| `test_grok_pool_bookkeeping.py` × 4 | 同上（走 browser 路径） |
| `test_grok_send_code_rejection.py` × 2 | 同上 |

日志原文：

```
[Grok] 环境预检失败：camoufox 浏览器未安装（包配对 156.0.1-beta.36）
修复：运行 `python -m camoufox fetch`
```

## 后续阶段的门禁口径

**失败集合必须与这份清单逐条相等。** 新增任何一条 = 回归，停下来修。
修好环境（`python -m camoufox fetch`）后这份清单应清零，届时同步更新本文件。

## 阶段 0 期间的两次真实回归（已修）

| 回归 | 原因 | 处置 |
|---|---|---|
| `test_chatgpt_registration_engine::test_run_phone_also_mirrors` | T0.6 删了 `_run_phone`，测试还在 `inspect.getsource` 它 | 删除该测试（功能已删） |
| `test_config_dedup_and_registration_modes::test_chatgpt_declared_defaults_match_runtime` | T0.6 删了「注册流程」声明项，测试还在读 `by_key["chatgpt_register_flow"]` | 改为断言「不再声明」+ 运行时默认仍是 `email` |
