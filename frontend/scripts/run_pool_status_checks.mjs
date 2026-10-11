// 在 node 里真实执行 poolStatus.ts / icloud.ts 的纯函数，打印断言结果。
//
// 与 `run_account_format_checks.mjs` 同一套做法：前端没有测试运行器，
// 用 vite 自带的 rolldown 把 TS 打成 ESM 再 import —— 断言作用在**真实执行
// 的函数**上，而不是对源码文本做正则（后者改了实现就可能失效，是假安全网）。
//
// 覆盖用户要求（2026-10-11）：号池标签三态化 —— 未入池 / 已入池 / 使用中。
// 存储层 available / used 在展示层合并为「已入池」，「是否使用」由
// 「已注册平台」列负责；旧标签「未使用 / 已使用」在任何取值下都不再出现。
//
// 用法（在 frontend/ 下）：node scripts/run_pool_status_checks.mjs
// 输出：一行 JSON {passed, checked, failures: [...]}；退出码 0/1。
import { rolldown } from 'rolldown'

async function bundle(entryPath) {
  const bundle = await rolldown({ input: entryPath })
  const { output } = await bundle.generate({ format: 'esm' })
  const code = output[0].code
  return await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))
}

const pool = await bundle(new URL('../src/lib/poolStatus.ts', import.meta.url).pathname)
const icloud = await bundle(new URL('../src/lib/icloud.ts', import.meta.url).pathname)

const failures = []
let checked = 0
function eq(actual, expected, label) {
  checked++
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a !== e) failures.push(`${label}: got ${a}, want ${e}`)
}
function ok(cond, label) {
  checked++
  if (!cond) failures.push(label)
}

try {
  // ── 三态标签本体 ──
  eq(pool.POOL_DISPLAY_META.unpooled.text, '未入池', 'unpooled label')
  eq(pool.POOL_DISPLAY_META.pooled.text, '已入池', 'pooled label')
  eq(pool.POOL_DISPLAY_META.in_use.text, '使用中', 'in_use label')
  eq(pool.POOL_DISPLAY_ORDER, ['unpooled', 'pooled', 'in_use'], 'summary order')
  const colors = new Set(pool.POOL_DISPLAY_ORDER.map((key) => pool.POOL_DISPLAY_META[key].color))
  eq(colors.size, 3, 'three labels use distinct colors')

  // ── 行级映射：available / used 合并为「已入池」 ──
  eq(pool.poolStatusMeta('available').text, '已入池', "meta('available')")
  eq(pool.poolStatusMeta('used').text, '已入池', "meta('used')")
  eq(pool.poolStatusMeta('available').color, pool.poolStatusMeta('used').color, 'available/used share one tag')
  eq(pool.poolStatusMeta('unpooled').text, '未入池', "meta('unpooled')")
  eq(pool.poolStatusMeta('in_use').text, '使用中', "meta('in_use')")
  eq(pool.poolStatusMeta('failed').text, '失败', "meta('failed') — Outlook 独有")
  eq(pool.poolStatusMeta(undefined).text, '已入池', 'unknown falls back to pooled')
  eq(pool.poolStatusMeta(null).text, '已入池', 'null falls back to pooled')

  // 旧标签在任何取值下都不再出现（用户要求）
  for (const value of ['unpooled', 'available', 'in_use', 'used', 'failed', '', null, 'bogus']) {
    const text = pool.poolStatusMeta(value).text
    ok(
      !text.includes('未使用') && !text.includes('已使用'),
      `legacy label must not appear for ${JSON.stringify(value)}: ${text}`,
    )
  }

  // ── poolDisplayStatus 折叠 ──
  eq(pool.poolDisplayStatus('unpooled'), 'unpooled', 'display(unpooled)')
  eq(pool.poolDisplayStatus('in_use'), 'in_use', 'display(in_use)')
  eq(pool.poolDisplayStatus('available'), 'pooled', 'display(available)')
  eq(pool.poolDisplayStatus('used'), 'pooled', 'display(used)')
  eq(pool.poolDisplayStatus(''), 'pooled', 'display(empty)')
  eq(pool.poolDisplayStatus('bogus'), 'pooled', 'display(unknown)')

  // ── 折叠计数（统计条） ──
  const folded = pool.poolDisplaySummary({ unpooled: 4, available: 3, used: 2, in_use: 1, total: 10 })
  eq(folded, { unpooled: 4, pooled: 5, in_use: 1, total: 10 }, 'fold available+used into pooled')
  eq(pool.poolDisplaySummary({}), { unpooled: 0, pooled: 0, in_use: 0, total: 0 }, 'fold empty pool')
  eq(pool.poolDisplaySummary({ available: 5 }).pooled, 5, 'fold tolerates missing fields')

  // ── 筛选折叠：真实执行 filterAliases ──
  const base = { status: 'active', label: '', note: '', account_email: 'a@icloud.com', source: 'local' }
  const rows = [
    { ...base, address: 'used@icloud.com', pool_status: 'used', used_platforms: ',chatgpt,', registered_platforms: ['chatgpt'] },
    { ...base, address: 'avail@icloud.com', pool_status: 'available', used_platforms: '', registered_platforms: [] },
    { ...base, address: 'unpool@icloud.com', pool_status: 'unpooled', used_platforms: '', registered_platforms: [] },
    { ...base, address: 'busy@icloud.com', pool_status: 'in_use', used_platforms: '', registered_platforms: [] },
  ]
  eq(
    icloud.filterAliases(rows, { poolStatus: 'pooled' }).map((r) => r.address),
    ['used@icloud.com', 'avail@icloud.com'],
    "filter 'pooled' = available + used",
  )
  eq(
    icloud.filterAliases(rows, { poolStatus: 'unpooled' }).map((r) => r.address),
    ['unpool@icloud.com'],
    "filter 'unpooled'",
  )
  eq(
    icloud.filterAliases(rows, { poolStatus: 'in_use' }).map((r) => r.address),
    ['busy@icloud.com'],
    "filter 'in_use'",
  )
  eq(icloud.filterAliases(rows, { poolStatus: 'used' }).length, 0, "legacy 'used' value matches nothing")
  eq(icloud.filterAliases(rows, {}).length, 4, 'no filter keeps all')
} catch (error) {
  failures.push(`unexpected error: ${error && error.stack ? error.stack : error}`)
}

const passed = failures.length === 0
console.log(JSON.stringify({ passed, checked, failures }, null, 2))
process.exit(passed ? 0 : 1)
