// 在 node 里真实执行 accountFormat.ts 的纯函数，打印断言结果。
//
// 为什么这么绕：前端没有测试运行器（无 vitest/jest），但 vite 自带 rolldown，
// 可以把 TS 打成 ESM 再 import —— 于是能对**真实执行的函数**做断言，
// 而不是对着源码文本做正则（后者改了实现就可能失效，是假安全网）。
//
// 用法（在 frontend/ 下）：node scripts/run_account_format_checks.mjs
// 输出：一行 JSON {passed, failures: [...]}；退出码 0/1。
import { rolldown } from 'rolldown'

const ENTRY = new URL('../src/lib/accountFormat.ts', import.meta.url).pathname

const bundle = await rolldown({ input: ENTRY })
const { output } = await bundle.generate({ format: 'esm' })
const code = output[0].code
const mod = await import('data:text/javascript;base64,' + Buffer.from(code).toString('base64'))

const failures = []
function eq(actual, expected, label) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a !== e) failures.push(`${label}: got ${a}, want ${e}`)
}

// ── formatCreatedAt：空值 / 非法值 / 合法值 ──
eq(mod.formatCreatedAt(undefined), { date: '-', time: '' }, 'formatCreatedAt(undefined)')
eq(mod.formatCreatedAt(''), { date: '-', time: '' }, 'formatCreatedAt("")')
eq(mod.formatCreatedAt('not-a-date'), { date: 'not-a-date', time: '' }, 'formatCreatedAt(invalid)')

// ── formatSyncTime：空值原样 / 非法值原样 ──
eq(mod.formatSyncTime(undefined), '', 'formatSyncTime(undefined)')
eq(mod.formatSyncTime(''), '', 'formatSyncTime("")')
eq(mod.formatSyncTime('nope'), 'nope', 'formatSyncTime(invalid)')

// ── formatStructuredText：空 / 普通文本 / JSON 美化 / 坏 JSON 回退 ──
eq(mod.formatStructuredText(undefined), '', 'formatStructuredText(undefined)')
eq(mod.formatStructuredText('  hello  '), 'hello', 'formatStructuredText(trim)')
eq(mod.formatStructuredText('{"a":1}'), '{\n  "a": 1\n}', 'formatStructuredText(json)')
eq(mod.formatStructuredText('[1,2]'), '[\n  1,\n  2\n]', 'formatStructuredText(array)')
eq(mod.formatStructuredText('{broken'), '{broken', 'formatStructuredText(bad json)')

// ── 状态映射：抽样的 label/color 组合 ──
eq(mod.authStateMeta('access_token_valid'), { color: 'success', label: 'AT有效' }, 'authStateMeta(valid)')
eq(mod.authStateMeta('unknown-xyz'), { color: 'default', label: '未探测' }, 'authStateMeta(default)')
eq(mod.codexStateMeta('usable'), { color: 'success', label: '可用' }, 'codexStateMeta(usable)')
eq(mod.codexStateMeta('quota_exhausted'), { color: 'warning', label: '额度耗尽' }, 'codexStateMeta(quota)')
eq(mod.plusTrialMeta('TRIAL_ELIGIBLE'), { color: 'success', label: '可领首月免费' }, 'plusTrialMeta(uppercase)')
eq(mod.plusTrialMeta('banned'), { color: 'error', label: '封号' }, 'plusTrialMeta(banned)')
eq(mod.planMeta('PLUS'), { color: 'success', label: 'Plus' }, 'planMeta(uppercase)')
eq(mod.planMeta('nope'), { color: 'default', label: '未知' }, 'planMeta(default)')

// ── normalizeAccount：extra_json 解析与透传 ──
const na = mod.normalizeAccount({
  id: 1,
  email: 'a@b.c',
  extra_json: JSON.stringify({ totp_secret: 'S', register_proxy: 'socks5h://p', chatgpt_local: { x: 1 } }),
})
eq(na.totpSecret, 'S', 'normalizeAccount.totpSecret')
eq(na.registerProxy, 'socks5h://p', 'normalizeAccount.registerProxy')
eq(na.chatgptLocal, { x: 1 }, 'normalizeAccount.chatgptLocal')
eq(na.email, 'a@b.c', 'normalizeAccount passes through email')
// 坏 JSON 不能抛，且 extra 回空对象
const nb = mod.normalizeAccount({ extra_json: '{bad' })
eq(nb.extra, {}, 'normalizeAccount(bad json).extra')
eq(nb.totpSecret, '', 'normalizeAccount(bad json).totpSecret')
// 缺 extra_json
const nc = mod.normalizeAccount({})
eq(nc.extra, {}, 'normalizeAccount(no extra).extra')

console.log(JSON.stringify({ passed: failures.length === 0, checked: 24, failures }, null, 2))
process.exit(failures.length === 0 ? 0 : 1)
