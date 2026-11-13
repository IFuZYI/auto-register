// 真实执行 lib/maintenanceKeys.ts：LOCAL_KEYS 只发后端真实存在的键，且
// 没有面板的平台（AWS / Kiro）不许发 `*_sync_strategy`。
//
// 用法（frontend/ 下）：node scripts/run_maintenance_keys_checks.mjs
//      也可带一个 JSON 数组作为 argv[2]（pytest 传 core.config_schema.CONFIG_KEYS）；
//      不带时从 ../../core/config_schema.py 解析（供 `npm run check` 直接跑）。
//
// 变异守卫：把 `syncable ? [key] : []` 改成无条件展开 → 这里立刻红。
import { readFileSync } from 'node:fs'
import { rolldown } from 'rolldown'

const ENTRY = new URL('../src/lib/maintenanceKeys.ts', import.meta.url).pathname
const bundle = await rolldown({ input: ENTRY })
const { output } = await bundle.generate({ format: 'esm' })
const mod = await import('data:text/javascript;base64,' + Buffer.from(output[0].code).toString('base64'))

function configKeysFromSource() {
  const schema = readFileSync(new URL('../../core/config_schema.py', import.meta.url), 'utf8')
  return [...schema.matchAll(/ConfigKey\(\s*"([^"]+)"/g)].map((m) => m[1])
}

const passedKeys = process.argv[2] ? JSON.parse(process.argv[2]) : configKeysFromSource()
const configKeys = new Set(passedKeys)

const failures = []
let checked = 0
const ok = (cond, label) => {
  checked++
  if (!cond) failures.push(label)
}

const keys = mod.localKeysFor()

ok(passedKeys.length > 0, '没有拿到 CONFIG_KEYS（pytest 传参 / 源码解析失败）')

// ── 有面板的平台必须仍然读写 sync_strategy ──
ok(keys.includes('chatgpt_sync_strategy'), 'chatgpt 的 sync_strategy 丢了（它有面板，应该能配）')
ok(keys.includes('grok_sync_strategy'), 'grok 的 sync_strategy 丢了（它有面板，应该能配）')

// ── 没有面板的平台不许出现 sync_strategy（变异：无条件 spread 在这里红）──
ok(!keys.includes('aws_sync_strategy'), 'aws 没有面板，却仍在 LOCAL_KEYS 里发 sync_strategy')
ok(!keys.includes('kiro_sync_strategy'), 'kiro 没有面板，却仍在 LOCAL_KEYS 里发 sync_strategy')

// ── 每个发出去的键都必须登记过（否则 PUT /api/config 静默丢弃）──
const missing = keys.filter((key) => !configKeys.has(key))
ok(missing.length === 0, `这些键不在 core/config_schema.py 里，保存会被丢弃: ${missing.join(', ')}`)

// ── 每平台键数：有面板 11（10 + sync_strategy），没有面板 10 ──
const countOf = (prefix) => keys.filter((key) => key.startsWith(`${prefix}_`)).length
ok(countOf('chatgpt') === 11, `chatgpt 应有 11 个键，实际 ${countOf('chatgpt')}`)
ok(countOf('grok') === 11, `grok 应有 11 个键，实际 ${countOf('grok')}`)
ok(countOf('aws') === 10, `aws 应有 10 个键，实际 ${countOf('aws')}`)
ok(countOf('kiro') === 10, `kiro 应有 10 个键，实际 ${countOf('kiro')}`)
ok(new Set(keys).size === keys.length, 'LOCAL_KEYS 里有重复键')

// ── 平台表必须显式声明 syncable（类型上必填，这里钉取值）──
const flags = Object.fromEntries(mod.LOCAL_PLATFORMS.map((p) => [p.prefix, p.syncable]))
ok(flags.chatgpt === true && flags.grok === true, 'chatgpt / grok 必须标 syncable: true')
ok(flags.aws === false && flags.kiro === false, 'aws / kiro 必须标 syncable: false')

console.log(JSON.stringify({ passed: failures.length === 0, checked, failures }))
process.exit(failures.length ? 1 : 0)
