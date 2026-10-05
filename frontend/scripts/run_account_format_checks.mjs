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
let checked = 0
function eq(actual, expected, label) {
  checked++
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

// ── AT 生命周期：从 JWT 解出生成/到期时间 ──
// 与后端 `services/chatgpt_token_lifecycle.py` 同口径（三档 valid/expiring/
// invalid + unknown）。前端算一份是为了详情弹窗即时显示 —— 不必为看一眼
// 到期时间再发一次请求。
eq(mod.atLifecycleMeta(''), { status: 'invalid', label: '无 AT', color: 'default', issuedText: '', expiresText: '', remainingText: '' }, 'atLifecycleMeta(empty)')
eq(mod.atLifecycleMeta('garbage'), { status: 'unknown', label: '无法解析', color: 'default', issuedText: '', expiresText: '', remainingText: '' }, 'atLifecycleMeta(garbage)')

// 造一个只有 payload 的假 JWT（header/签名随便填）
function fakeJwt(payload) {
  const b64 = (obj) => Buffer.from(JSON.stringify(obj)).toString('base64url')
  return `${b64({ alg: 'RS256' })}.${b64(payload)}.sig`
}
// 未来 10 天 → valid
const future = Math.floor(Date.now() / 1000) + 10 * 86400
const past = Math.floor(Date.now() / 1000) - 100
const metaValid = mod.atLifecycleMeta(fakeJwt({ iat: 1000, exp: future }))
eq(metaValid.status, 'valid', 'atLifecycleMeta(valid).status')
eq(metaValid.color, 'success', 'atLifecycleMeta(valid).color')
eq(metaValid.issuedText !== '', true, 'atLifecycleMeta(valid).issuedText non-empty')
// 剩 1 小时 → expiring
const metaExpiring = mod.atLifecycleMeta(fakeJwt({ iat: 1000, exp: Math.floor(Date.now() / 1000) + 3600 }))
eq(metaExpiring.status, 'expiring', 'atLifecycleMeta(expiring).status')
eq(metaExpiring.color, 'warning', 'atLifecycleMeta(expiring).color')
// 已过期 → invalid
const metaInvalid = mod.atLifecycleMeta(fakeJwt({ iat: 1000, exp: past }))
eq(metaInvalid.status, 'invalid', 'atLifecycleMeta(invalid).status')
eq(metaInvalid.color, 'error', 'atLifecycleMeta(invalid).color')
// 没有 exp → unknown（不误判成失效）
eq(mod.atLifecycleMeta(fakeJwt({ iat: 1000 })).status, 'unknown', 'atLifecycleMeta(no exp)')

// ── 列表行 AT 摘要（atListSummary）：列表「AT 有效期」列用 ──
// 参考实现（chatgpt2api）在列表行内直接显示凭据状态；我们的列表此前
// 只在详情弹窗里有 AT 生成/到期，用户看不到。这个函数产出列表行的
// 一行摘要：状态标签 + 到期日 + 剩余时间。
const listValid = mod.atListSummary(fakeJwt({ iat: 1000, exp: future }))
eq(listValid.label, '有效', 'atListSummary(valid).label')
eq(listValid.color, 'success', 'atListSummary(valid).color')
eq(listValid.expiresShort.includes('-'), true, 'atListSummary(valid).expiresShort 是短日期')
eq(listValid.remainingText !== '', true, 'atListSummary(valid).remainingText non-empty')
// tooltip 全量信息：生成 + 到期 + 剩余
eq(listValid.tooltip.includes('生成于'), true, 'atListSummary.tooltip 含生成时间')
eq(listValid.tooltip.includes('到期'), true, 'atListSummary.tooltip 含到期时间')
// 即将过期（剩 1 小时）→ warning
eq(mod.atListSummary(fakeJwt({ iat: 1000, exp: Math.floor(Date.now() / 1000) + 3600 })).label, '即将过期', 'atListSummary(expiring).label')
// 已过期 → error
eq(mod.atListSummary(fakeJwt({ iat: 1000, exp: past })).label, '已过期', 'atListSummary(invalid).label')
// 空 token → 无 AT，expiresShort 为空串（列表不显示日期行）
eq(mod.atListSummary('').label, '无 AT', 'atListSummary(empty).label')
eq(mod.atListSummary('').expiresShort, '', 'atListSummary(empty).expiresShort 空')

console.log(JSON.stringify({ passed: failures.length === 0, checked, failures }, null, 2))
process.exit(failures.length === 0 ? 0 : 1)
