# 门禁基线（控制器侧实测，2026-11-15）

全部数字都是控制器自己跑出来的，不是 lane 的自述。基线取自 `git worktree add --detach <scratch> fca2eec`（不在工作树里 stash，避免动到未提交的改动）。

## 1. 前端类型检查 `tsc -b`

```
cd frontend && npx tsc -b        # exit 0，无输出
```
基线（fca2eec）与当前工作树都是 **0 错误**。当前树的 `frontend/tsconfig.json` 是 solution 式（references），`tsc -b` 会真的构建引用图，不是空跑。

## 2. 前端自检脚本 `npm run check`

```
cd frontend && npm run check
> for f in scripts/run_*.mjs; do node "$f" > /dev/null || exit 1; done && echo 'all checks passed'
all checks passed        # exit 0, 15.15s
```
30 个脚本全绿。**注意**：这批脚本里相当一部分是「读源码 + 正则」，不是行为断言 —— 全绿只能证明文本还在，不能证明行为对。L09 lane 负责逐条分类（行为断言 vs 正则）。

## 3. ESLint（**已知红，且不是本轮的锅**）

| 版本 | 文件数 | errors | warnings |
|---|---|---|---|
| 基线 `fca2eec` | 11 | **33** | 5 |
| 当前工作树 | 8 | **28** | 4 |

逐文件 delta（当前 − 基线），**没有任何文件变差**：

```
src/App.tsx                    2E/1W -> 0E/0W   -2E
src/pages/Dashboard.tsx        1E/0W -> 0E/1W   -1E
src/pages/Login.tsx            2E/0W -> 0E/0W   -2E
src/pages/PanelManagement.tsx  0E/1W -> 0E/0W    0E
```

剩余 28 errors 集中在 `src/pages/Accounts.tsx`（15E）与 `src/pages/Settings.tsx`（10E），这两个文件的错误数在基线与当前树**完全相同** —— 属于既有欠账（`no-explicit-any` 一类），不是本轮引入。

命令：
```
cd frontend && npx eslint . -f json -o /tmp/eslint.json      # 当前树  exit 1
cd <base-worktree>/frontend && npx eslint . -f json -o /tmp/eslint_base.json   # 基线 exit 1
```
（`-f compact` 在 ESLint 9 已移除，用它只会得到格式化器错误，别用。）

**结论**：lint 不是发布门禁（历史上就没绿过），本轮 0 新增错误、净减 5 个。`docs/releases/v0.1.0.md` 里写的「33 errors / 5 warnings 与既有基线一致」已过时，当前是 28/4 —— 属于文档漂移，L14 应记录。

## 4. ruff / mypy

```
.venv/bin/python -m ruff check .   # No module named ruff
```
**不存在**。仓库有 `.ruff_cache/` 但解释器里没装 ruff —— 即「lint 门禁」在后端侧从来没有真正跑起来过。属 L09 的发现面。

## 5. 全量 pytest

`.venv/bin/python -m pytest tests/ -q`（`pytest.ini` 固定 `testpaths = tests`，不能用裸 `pytest`：会递归进 `.worktrees/*/tests` 与 `reference/**/tests`，实测 2969 collected + 311 import mismatch）。

结果：见 `pytest-baseline.txt`（控制器单跑，不与 lane 的 focused 测试并发判定）。

## 6. CI / 发布

`.github/workflows/release.yml`：推 `v*` tag → 建 GitHub Release（正文优先取 `docs/releases/<tag>.md`）+ 构建并推 `ghcr.io/ifuzyi/auto-register`（semver `1.0.0` / `1.0` / `latest`）。本机无 `gh`、无 API token、无 registry 登录，**发布只能靠推 tag 触发 CI**。
