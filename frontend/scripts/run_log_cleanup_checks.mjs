// 在 node 里真实执行 logCleanup.ts 的纯函数，打印断言结果。
// 用法（在 frontend/ 下）：node scripts/run_log_cleanup_checks.mjs
// 输出：一行 JSON {passed, failures: [...]}；退出码 0/1。
import { rolldown } from 'rolldown'

const ENTRY = new URL('../src/lib/logCleanup.ts', import.meta.url).pathname

const bundle = await rolldown({ input: ENTRY })
const { output } = await bundle.generate({ format: 'esm' })
const code = output[0].code
const mod = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))

const failures = []
let checked = 0
function eq(actual, expected, label) {
  checked++
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a !== e) failures.push(`${label}: got ${a}, want ${e}`)
}

// ── 保留期选项：用户要求「清理1天/周/月前的日志」──
eq(
  mod.RETENTION_OPTIONS.map((o) => o.days),
  [1, 7, 30],
  '保留期天数',
)
eq(
  mod.RETENTION_OPTIONS.map((o) => o.label),
  ['1 天前', '1 周前', '1 个月前'],
  '保留期文案',
)
// 天数必须是正整数 —— 0 会被后端 422 拒绝，负数更不可能
if (mod.RETENTION_OPTIONS.some((o) => !Number.isInteger(o.days) || o.days < 1)) {
  failures.push('保留期出现非正整数天数')
}
// 天数不得重复（重复会在下拉里出现两个一样的选项）
if (new Set(mod.RETENTION_OPTIONS.map((o) => o.days)).size !== mod.RETENTION_OPTIONS.length) {
  failures.push('保留期天数重复')
}

// ── 清理端点：两页共用一份，避免各自硬编码分叉 ──
eq(mod.CLEANUP_ENDPOINTS.logs, '/tasks/logs/cleanup', '任务历史清理端点')
eq(mod.CLEANUP_ENDPOINTS.runs, '/tasks/runs/cleanup', '任务运行清理端点')

// ── 文案函数：已知值走选项表，未知值原样显示（不吞掉）──
eq(mod.retentionLabel(1), '1 天前', 'label(1)')
eq(mod.retentionLabel(7), '1 周前', 'label(7)')
eq(mod.retentionLabel(30), '1 个月前', 'label(30)')
eq(mod.retentionLabel(3), '3 天前', 'label(未知值)')

console.log(JSON.stringify({ passed: failures.length === 0, checked, failures }, null, 2))
process.exit(failures.length === 0 ? 0 : 1)
