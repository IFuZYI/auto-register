"""本地维护面板的「键表」契约：LOCAL_KEYS 只能发后端真实存在的键。

前端没有测试运行器，但可以用 node + rolldown **真实执行**
`frontend/src/lib/maintenanceKeys.ts` 的纯函数（与 `run_account_format_checks.mjs`
同一套做法）。这里驱动 `frontend/scripts/run_maintenance_keys_checks.mjs`：

1. **没有面板的平台不得读写 `*_sync_strategy`**。AWS / Kiro 没有可同步的面板
   （后端只有 probe / refresh），多一个键就是多一个永远无效的控件。
   变异守卫：把 `syncable ? [key] : []` 改成无条件展开 → 这里立刻红。
2. **发出去的每个键都必须登记过**。`PUT /api/config` 对未知键是「容忍但丢弃」
   （回 `ignored`），界面照样弹「已保存」—— 少了这条断言，多写一个键
   谁也不会发现。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from core.config_schema import CONFIG_KEYS

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
HARNESS = FRONTEND / "scripts" / "run_maintenance_keys_checks.mjs"


class LocalMaintenanceKeysContractTests(unittest.TestCase):
    def test_computed_keys_exclude_sync_strategy_without_a_panel(self):
        if shutil.which("node") is None:
            self.skipTest("未安装 node —— 跳过前端纯函数行为检查")
        if not (FRONTEND / "node_modules" / "rolldown").exists():
            self.skipTest("frontend/node_modules/rolldown 不存在（未 npm install）")

        result = subprocess.run(
            ["node", str(HARNESS), json.dumps(list(CONFIG_KEYS))],
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            timeout=300,
        )
        self.assertTrue(result.stdout.strip(), f"harness 没有输出：{result.stderr[-500:]}")
        report = json.loads(result.stdout)
        self.assertTrue(
            report["passed"],
            "本地维护键表不符：\n" + "\n".join(report["failures"]),
        )
        self.assertGreaterEqual(report["checked"], 8, "断言条数太少，网太稀")


if __name__ == "__main__":
    unittest.main()
