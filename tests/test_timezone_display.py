"""时区显示契约：本地/远端时间按浏览器本地时区显示 + 时区标注。

背景（用户报的问题）：「本地和远程面板服务器时区可能不同」。
实测：grok2api 用 `+08:00` 写时间（`createdAt`），CPA 用 `+08:00`（`modtime`），
本地库写 UTC。后端比较前已归一（`services/panel_comparison._iso`），但**显示**
如果直接截断 ISO 串，对 +08:00 的用户每个时间都差 8 小时且无标注。

修复：`frontend/src/lib/time.ts` 把时间转成浏览器本地时区显示，界面加
`UTC+8` 这样的标注。本测试：
1. 静态断言（接线在、关键函数被使用）；
2. 行为验证（有 node 时编译 time.ts 并在三个时区下跑断言 —— 没有 node 跳过）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
TIME_MODULE = FRONTEND / "src" / "lib" / "time.ts"
PANEL = FRONTEND / "src" / "components" / "settings" / "PanelComparisonPanel.tsx"

_BEHAVIOR_JS = """
const mod = require(process.argv[2]);
const tz = process.env.TZ || '';
let failed = 0;
function check(name, cond, detail) {
  if (!cond) { console.log('FAIL ' + name + ' ' + (detail || '')); failed++; }
}
const local = mod.formatLocalTime('2026-10-04T11:00:00+00:00');
if (tz === 'UTC') check('utc passthrough', local === '2026-10-04 11:00', local);
if (tz === 'Asia/Shanghai') check('+8 shift', local === '2026-10-04 19:00', local);
if (tz === 'America/New_York') check('-4 shift', local === '2026-10-04 07:00', local);
// 远端 +08:00 串 == 11:00Z
const remote = mod.formatLocalTime('2026-10-04T19:05:31.613931+08:00');
if (tz === 'UTC') check('remote +08 normalized', remote === '2026-10-04 11:05', remote);
if (tz === 'Asia/Shanghai') check('remote +08 roundtrip', remote === '2026-10-04 19:05', remote);
// naive 按 UTC（后端写的是 UTC）
const naive = mod.formatLocalTime('2026-10-04 11:00:00');
if (tz === 'UTC') check('naive as utc', naive === '2026-10-04 11:00', naive);
if (tz === 'Asia/Shanghai') check('naive as utc +8', naive === '2026-10-04 19:00', naive);
// 标注格式
const label = mod.localTimezoneLabel();
check('label format', /^UTC[+-]\\d{1,2}(:\\d{2})?$/.test(label), label);
// 保底
check('empty fallback', mod.formatLocalTime('') === '\\u2014');
const s = mod.parseTimeValue(1743393600), ms = mod.parseTimeValue(1743393600000);
check('epoch s==ms', s && ms && s.getTime() === ms.getTime());
process.exit(failed > 0 ? 1 : 0);
"""


class TimezoneDisplayStaticTests(unittest.TestCase):
    """接线：时间模块存在且被对比面板使用。"""

    def test_time_module_exists_with_key_functions(self):
        src = TIME_MODULE.read_text(encoding="utf-8")
        for fn in ("formatLocalTime", "localTimezoneLabel", "parseTimeValue"):
            self.assertIn(f"export function {fn}", src, f"time.ts 缺少 {fn}")

    def test_panel_uses_local_time_formatting(self):
        src = PANEL.read_text(encoding="utf-8")
        self.assertIn("formatLocalTime", src, "对比面板没接本地时区格式化")
        self.assertIn("localTimezoneLabel", src, "对比面板没显示时区标注")
        # 旧实现（直接截断 ISO 串、丢掉时区信息）不能回来
        self.assertNotIn(
            "replace(/(\\+\\d{2}:\\d{2}|Z)$/, '')",
            src,
            "旧的截断式时间显示又回来了 —— 它会把 UTC 时钟直接摆给 +08:00 的用户",
        )

    def test_panel_shows_timezone_annotation(self):
        src = PANEL.read_text(encoding="utf-8")
        self.assertIn("时区 {localTimezoneLabel()}", src, "界面没有时区标注")


class TimezoneDisplayBehaviorTests(unittest.TestCase):
    """行为：三个时区下编译并执行 time.ts（无 node 环境跳过）。"""

    def _compile_time_module(self, workdir: Path) -> Path:
        out = workdir / "out"
        result = subprocess.run(
            [
                "npx", "tsc", str(TIME_MODULE),
                "--outDir", str(out),
                "--module", "commonjs",
                "--target", "es2020",
                "--skipLibCheck",
            ],
            cwd=FRONTEND,
            capture_output=True,
            text=True,
            timeout=180,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        compiled = out / "time.js"
        self.assertTrue(compiled.exists(), f"编译产物不存在: {compiled}")
        return compiled

    def test_formatting_across_timezones(self):
        if shutil.which("npx") is None or not (FRONTEND / "node_modules").exists():
            self.skipTest("未安装 node/npx 或前端依赖 —— 跳过时区行为验证")
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            compiled = self._compile_time_module(workdir)
            script = workdir / "run.js"
            script.write_text(_BEHAVIOR_JS, encoding="utf-8")
            for tz in ("UTC", "Asia/Shanghai", "America/New_York"):
                env = {"TZ": tz, "PATH": shutil.os.environ.get("PATH", "")}
                result = subprocess.run(
                    ["node", str(script), str(compiled)],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=env,
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"TZ={tz} 行为断言失败:\n{result.stdout}\n{result.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
