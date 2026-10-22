"""已完成任务的渲染上限：失败/停止的任务不能被「最近 20 条」挤掉。

背景（复审实测）：上限按**后端排序**切片，而后端先按状态档位
（`done:2` → `failed:3` → `stopped:4`）再按时间倒序。于是 25 条 done
会把刚失败的那条挤到第 26 位、藏进「展开其余 N 条」后面 ——
最需要被看见的任务默认看不见。

这里跑真实 TS（与 `run_*.mjs` 同一套做法），不靠读源码。
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(ROOT, "frontend")


@pytest.mark.skipif(
    not os.path.isdir(os.path.join(FRONTEND, "node_modules")),
    reason="frontend/node_modules 未安装，跳过真实 TS 执行",
)
def test_failed_tasks_are_not_hidden_behind_the_cap():
    """25 条 done + 1 条刚 failed → 那条 failed 必须可见。"""
    script = os.path.join(FRONTEND, "scripts", "run_finished_cap_checks.mjs")
    assert os.path.exists(script), f"缺少执行脚本: {script}"
    proc = subprocess.run(
        ["node", script], cwd=FRONTEND, capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, f"脚本失败: {proc.stderr[:800]}"
    result = json.loads(proc.stdout)
    assert not result["failures"], result["failures"]
    assert result["checked"] >= 3
