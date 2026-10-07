// 在 node 里真实执行 taskKinds.ts 的纯函数，打印断言结果。
// 用法（在 frontend/ 下）：node scripts/run_task_kind_checks.mjs
// 输出：一行 JSON {passed, failures: [...]}；退出码 0/1。
import { rolldown } from 'rolldown'

const ENTRY = new URL('../src/lib/taskKinds.ts', import.meta.url).pathname

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

// ── 已知类型：label 与颜色 ──
eq(mod.taskKindMeta('manual'), { label: '注册', color: 'blue' }, 'manual')
eq(mod.taskKindMeta('api'), { label: 'API 注册', color: 'geekblue' }, 'api')
eq(mod.taskKindMeta('schedule'), { label: '定时注册', color: 'geekblue' }, 'schedule')
eq(mod.taskKindMeta('backfill_rt'), { label: '补 RT', color: 'orange' }, 'backfill_rt')
eq(mod.taskKindMeta('bind_2fa'), { label: '绑 2FA', color: 'purple' }, 'bind_2fa')
eq(mod.taskKindMeta('refresh_token'), { label: '刷新 Token', color: 'green' }, 'refresh_token')
eq(mod.taskKindMeta('auto_refresh'), { label: '自动刷新', color: 'cyan' }, 'auto_refresh')
eq(mod.taskKindMeta('chatgpt2api_sync'), { label: '凭证同步', color: 'magenta' }, 'chatgpt2api_sync')

// ── 空值 / 未知值：不吞掉，原样显示 ──
eq(mod.taskKindMeta(''), { label: '未知', color: 'default' }, 'empty')
eq(mod.taskKindMeta(undefined), { label: '未知', color: 'default' }, 'undefined')
eq(mod.taskKindMeta('legacy-x'), { label: 'legacy-x', color: 'default' }, 'unknown')
// 空白容错
eq(mod.taskKindMeta('  manual  '), { label: '注册', color: 'blue' }, 'whitespace')

// ── 每个已知类型的颜色必须互不相同（「不同标签方便区分」）──
const sources = ['manual', 'api', 'schedule', 'backfill_rt', 'bind_2fa', 'refresh_token', 'auto_refresh', 'chatgpt2api_sync']
const colors = new Set(sources.map((s) => mod.taskKindMeta(s).color))
if (colors.size < 5) failures.push(`颜色区分度不足: ${colors.size} 种`)

console.log(JSON.stringify({ passed: failures.length === 0, checked, failures }, null, 2))
process.exit(failures.length === 0 ? 0 : 1)
