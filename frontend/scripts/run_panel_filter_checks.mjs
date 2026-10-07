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

// ── 更新远程 / 更新本地两个方向（对称的一对：谁较新就动谁）──
// 更新远程 = 推（未上传 + 本地较新）；更新本地 = 拉（远端较新）。
// 两个方向互斥：同一行不可能同时在两边。
const dirRows = [
  { email: 'push@x.com', platform: 'grok', state: 'credential_diff', time_relation: 'local_newer', local_id: 101 },
  { email: 'pull@x.com', platform: 'grok', state: 'credential_diff', time_relation: 'remote_newer', local_id: 102 },
  { email: 'same@x.com', platform: 'grok', state: 'credential_diff', time_relation: 'time_synced', local_id: 103 },
  { email: 'unknown@x.com', platform: 'grok', state: 'credential_diff', time_relation: '', local_id: 104 },
  { email: 'nolocal@x.com', platform: 'grok', state: 'credential_diff', time_relation: 'local_newer', local_id: null },
  { email: 'only@x.com', platform: 'grok', state: 'local_only', local_id: 105 },
  { email: 'synced@x.com', platform: 'grok', state: 'synced', time_relation: 'time_synced', local_id: 106 },
  { email: 'remoteonly@x.com', platform: 'grok', state: 'remote_only', local_id: null },
]
const pushIds = mod.selectPushIds(dirRows)
const pullIds = mod.selectPullIds(dirRows)
// 更新远程：未上传（补传）+ 本地较新的凭证不同
eq(pushIds, [101, 105], 'push keeps local_newer diffs and local_only rows')
// 更新本地：只拉远端较新
eq(pullIds, [102], 'pull keeps only remote_newer diffs')
eq(pushIds.filter((id) => pullIds.includes(id)), [], 'the two directions are disjoint')
// 同小时 / 无法判定时间：两个方向都不动（不拿不确定的数据覆盖任何一边）
eq(pushIds.includes(103) || pullIds.includes(103), false, 'same-hour diffs are left alone')
eq(pushIds.includes(104) || pullIds.includes(104), false, 'unknown-time diffs are left alone')
// 没有本地 id 的行（远端独有）不是任何方向的对象
eq(pushIds.includes(106) === false && pullIds.includes(106) === false, true, 'synced rows are not targets')
eq(mod.selectPushIds([]), [], 'empty input stays empty (push)')
eq(mod.selectPullIds([]), [], 'empty input stays empty (pull)')

// ── 禁用的账号不参与同步（用户要求 2026-10-07）──
// 凭证已死：推上去污染远端面板，拉回来也救不活。两个方向都跳过。
const bannedRows = [
  { email: 'banned-push@x.com', platform: 'grok', state: 'local_only', local_id: 201, local_status: 'banned' },
  { email: 'banned-pull@x.com', platform: 'grok', state: 'credential_diff', time_relation: 'remote_newer', local_id: 202, local_status: 'banned' },
  { email: 'banned-newer@x.com', platform: 'grok', state: 'credential_diff', time_relation: 'local_newer', local_id: 203, local_status: 'banned' },
  { email: 'normal@x.com', platform: 'grok', state: 'local_only', local_id: 204, local_status: 'registered' },
]
eq(mod.selectPushIds(bannedRows), [204], 'banned rows are not push targets')
eq(mod.selectPullIds(bannedRows), [], 'banned rows are not pull targets')
eq(mod.selectPullIds([{ ...bannedRows[1], local_status: 'invalid' }]), [202], 'invalid rows still pull')
// 大小写不敏感（库里历史值可能带空白/大写）
eq(mod.selectPushIds([{ ...bannedRows[0], local_status: ' Banned ' }]), [], 'banned check is case-insensitive')

console.log(JSON.stringify({ passed: failures.length === 0, checked, failures }, null, 2))
process.exit(failures.length === 0 ? 0 : 1)
