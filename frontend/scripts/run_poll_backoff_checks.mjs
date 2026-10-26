// 真实执行 pollBackoff.ts 的纯函数。
// 用法（frontend/ 下）：node scripts/run_poll_backoff_checks.mjs
import { rolldown } from 'rolldown'

const ENTRY = new URL('../src/lib/pollBackoff.ts', import.meta.url).pathname
const bundle = await rolldown({ input: ENTRY })
const { output } = await bundle.generate({ format: 'esm' })
const mod = await import('data:text/javascript;base64,' + Buffer.from(output[0].code).toString('base64'))

const failures = []
let checked = 0
const eq = (a, b, label) => { checked++; if (JSON.stringify(a) !== JSON.stringify(b)) failures.push(`${label}: got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`) }

eq(mod.pollIntervalMs(true), 2500, '有运行中任务 → 2.5s')
eq(mod.pollIntervalMs(false), 15000, '无运行中任务 → 15s（退避）')
eq(mod.shouldTickSeconds(true), true, '有运行中任务 → 每秒 tick')
eq(mod.shouldTickSeconds(false), false, '无运行中任务 → 不 tick')
checked++
if (mod.POLL_INTERVAL_IDLE_MS <= mod.POLL_INTERVAL_ACTIVE_MS) failures.push('退避间隔必须大于活跃间隔')
checked++
if (mod.POLL_INTERVAL_IDLE_MS < 10000) failures.push('退避至少 10 秒，否则省不下请求')

// 页面源码里不该再有写死的 2500 字面量（说明真的用了纯函数）
import { readFileSync } from 'node:fs'
const page = readFileSync(new URL('../src/pages/RunningTasks.tsx', import.meta.url), 'utf8')
checked++
if (/\},\s*2500\s*\)/.test(page)) failures.push('RunningTasks 里仍有写死的 2500 间隔字面量')
checked++
if (!/pollIntervalMs/.test(page)) failures.push('RunningTasks 没有使用 pollIntervalMs')
checked++
if (!/shouldTickSeconds/.test(page)) failures.push('RunningTasks 没有使用 shouldTickSeconds')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
