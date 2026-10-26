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
def test_chatgpt_token_plan_has_three_options():
    """「Token 方案」必须是三选一（用户要求：合并成三种）。

    检查脚本真实执行 `lib/chatgptTokenPlan.ts`，覆盖方案数量、取值顺序、
    默认值、以及每个方案落成的后端键。
    """
    script = os.path.join(FRONTEND, "scripts", "run_chatgpt_token_plan_checks.mjs")
    assert os.path.exists(script), f"缺少执行脚本: {script}"
    proc = subprocess.run(
        ["node", script], cwd=FRONTEND, capture_output=True, text=True, timeout=180
    )
    assert proc.returncode == 0, f"脚本失败: {proc.stdout[-800:]}{proc.stderr[-800:]}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not result["failures"], result["failures"]
    assert result["checked"] >= 13


@pytest.mark.skipif(
    not os.path.isdir(os.path.join(FRONTEND, "node_modules")),
    reason="frontend/node_modules 未安装，跳过真实 TS 执行",
)
def test_form_hints_live_in_tooltips_not_on_the_page():
    """说明文字走 tooltip，不常驻占版面（用户要求：注释太乱，收进文字提示）。

    检查脚本断言：三个 ChatGPT 控件不再渲染常驻 `<Text type="secondary">`、
    任务页/账号页用 `Form.Item` 的 tooltip、全局配置页的 `mode.desc` 保持走
    tooltip，以及**邮箱导入的「两步流程」警告必须留在版面**（那是操作步骤，
    不是注释，收进 tooltip 用户会漏看）。
    """
    script = os.path.join(FRONTEND, "scripts", "run_form_hint_checks.mjs")
    assert os.path.exists(script), f"缺少执行脚本: {script}"
    proc = subprocess.run(
        ["node", script], cwd=FRONTEND, capture_output=True, text=True, timeout=180
    )
    assert proc.returncode == 0, f"脚本失败: {proc.stdout[-800:]}{proc.stderr[-800:]}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not result["failures"], result["failures"]
    assert result["checked"] >= 12


@pytest.mark.skipif(
    not os.path.isdir(os.path.join(FRONTEND, "node_modules")),
    reason="frontend/node_modules 未安装，跳过真实 TS 执行",
)
@pytest.mark.parametrize("script_name", [
    "run_poll_backoff_checks.mjs",
    "run_accounts_list_token_checks.mjs",
])
def test_frontend_script_passes(script_name):
    """把每个前端检查脚本都接入 pytest —— 否则它们永远不会被执行。

    复审指出：脚本躺在 `frontend/scripts/` 下，既不在 package.json 的
    scripts、也不在 CI、也不在 pytest 里，等于死代码（`run_poll_backoff_checks`
    就是这种情况，`pollIntervalMs` 的取值守卫从没跑过）。
    """
    script = os.path.join(FRONTEND, "scripts", script_name)
    assert os.path.exists(script), f"缺少执行脚本: {script}"
    proc = subprocess.run(
        ["node", script], cwd=FRONTEND, capture_output=True, text=True, timeout=180
    )
    assert proc.returncode == 0, (
        f"{script_name} 失败: {proc.stdout[-600:]}{proc.stderr[-600:]}"
    )
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert not result["failures"], result["failures"]


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
