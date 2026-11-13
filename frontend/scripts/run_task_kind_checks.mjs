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

// ── 前缀型来源：自动维护（AWS / Kiro 也走这条路）与批量动作 ──
// 后端真实创建过的 source：`auto_probe_<platform>` / `auto_refresh_<platform>`
// （services/local_maintenance.py）与 `action:<id>`（api/tasks.py 的批量动作）。
// 没有这些文案时任务页会直接显示 `auto_probe_aws` 这种原始 id。
eq(mod.taskKindMeta('auto_probe_aws'), { label: '自动检测', color: 'cyan' }, 'auto_probe_aws')
eq(mod.taskKindMeta('auto_probe_kiro'), { label: '自动检测', color: 'cyan' }, 'auto_probe_kiro')
eq(mod.taskKindMeta('auto_refresh_aws'), { label: '自动刷新', color: 'cyan' }, 'auto_refresh_aws')
eq(mod.taskKindMeta('auto_refresh_kiro'), { label: '自动刷新', color: 'cyan' }, 'auto_refresh_kiro')
eq(mod.taskKindMeta('auto_pull_cpa'), { label: '自动同步', color: 'magenta' }, 'auto_pull_cpa')
eq(mod.taskKindMeta('action:probe'), { label: '批量测活', color: 'blue' }, 'action:probe')
eq(mod.taskKindMeta('action:refresh_token'), { label: '批量刷新 Token', color: 'blue' }, 'action:refresh_token')
eq(mod.taskKindMeta('action:unknown-op'), { label: '批量动作', color: 'blue' }, 'action:unknown-op')

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
