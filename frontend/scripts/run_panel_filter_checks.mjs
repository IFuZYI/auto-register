// 在 node 里真实执行 panelComparison.ts 的纯函数，打印断言结果。
//
// 与 `run_account_format_checks.mjs` 同一套做法：前端没有测试运行器，
// 用 vite 自带的 rolldown 把 TS 打成 ESM 再 import —— 断言作用在**真实执行
// 的函数**上，而不是对源码文本做正则（后者改了实现就可能失效，是假安全网）。
//
// 用法（在 frontend/ 下）：node scripts/run_panel_filter_checks.mjs
// 输出：一行 JSON {passed, checked, failures: [...]}；退出码 0/1。
import { rolldown } from 'rolldown'

const ENTRY = new URL('../src/lib/panelComparison.ts', import.meta.url).pathname

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

// ── 样本：CPA 那种两平台混在一起的行 ──
const rows = [
  { email: 'a@x.com', platform: 'chatgpt', state: 'local_only' },
  { email: 'b@x.com', platform: 'chatgpt', state: 'synced' },
  { email: 'c@x.com', platform: 'chatgpt', state: 'remote_only' },
  { email: 'a@x.com', platform: 'grok', state: 'local_only' },
  { email: 'd@x.com', platform: 'grok', state: 'credential_diff' },
]

// ── filterRowsByPlatform ──
eq(mod.filterRowsByPlatform(rows, '').length, 5, "filterRowsByPlatform('') keeps all")
eq(mod.filterRowsByPlatform(rows, 'grok').length, 2, 'filterRowsByPlatform(grok)')
eq(mod.filterRowsByPlatform(rows, 'chatgpt').length, 3, 'filterRowsByPlatform(chatgpt)')
eq(
  mod.filterRowsByPlatform(rows, 'grok').map((r) => r.email),
  ['a@x.com', 'd@x.com'],
  'filterRowsByPlatform(grok) preserves order',
)
eq(mod.filterRowsByPlatform(rows, 'gemini').length, 0, 'unknown platform filters everything out')
eq(mod.filterRowsByPlatform([], 'grok').length, 0, 'empty input stays empty')
// 不改原数组
const before = rows.length
mod.filterRowsByPlatform(rows, 'grok')
eq(rows.length, before, 'filterRowsByPlatform does not mutate input')

// ── countByPlatform ──
eq(mod.countByPlatform(rows).total, 5, 'countByPlatform.total')
eq(mod.countByPlatform(rows).chatgpt, 3, 'countByPlatform.chatgpt')
eq(mod.countByPlatform(rows).grok, 2, 'countByPlatform.grok')
eq(mod.countByPlatform([]).total, 0, 'countByPlatform([])')

// ── summarizeRows ──
eq(mod.summarizeRows(rows).total, 5, 'summarizeRows.total')
eq(mod.summarizeRows(rows).local_only, 2, 'summarizeRows.local_only')
eq(mod.summarizeRows(rows).synced, 1, 'summarizeRows.synced')
eq(mod.summarizeRows(rows).remote_only, 1, 'summarizeRows.remote_only')
eq(mod.summarizeRows(rows).credential_diff, 1, 'summarizeRows.credential_diff')
eq(mod.summarizeRows(rows).unknown_credential, 0, 'summarizeRows zero-fills missing states')
// 平台筛选后的计数（页面顶部的统计条要跟着筛）
const grokRows = mod.filterRowsByPlatform(rows, 'grok')
eq(mod.summarizeRows(grokRows).total, 2, 'summarizeRows(after platform filter).total')
eq(mod.summarizeRows(grokRows).local_only, 1, 'summarizeRows(after platform filter).local_only')

// ── shouldShowPlatformFilter ──
eq(mod.shouldShowPlatformFilter(['chatgpt', 'grok']), true, 'multi-platform shows the selector')
eq(mod.shouldShowPlatformFilter(['chatgpt']), false, 'single-platform hides it')
eq(mod.shouldShowPlatformFilter([]), false, 'no platforms hides it')
eq(mod.shouldShowPlatformFilter(undefined), false, 'undefined hides it')
// 去重后仍算多平台？重复项不该让单平台看起来像多平台
eq(mod.shouldShowPlatformFilter(['grok', 'grok']), false, 'duplicate platforms are not multi-platform')

console.log(JSON.stringify({ passed: failures.length === 0, checked, failures }, null, 2))
process.exit(failures.length === 0 ? 0 : 1)
