// Grok 有两个 probe 能力动作，批量必须走 probe_refresh。
//
// `probe` 是 CPA 探活：没有 AT 就直接返回失败。而 Grok 注册默认只产 Web SSO、
// 不产 AT —— 于是批量走 `probe` 会让整批新号显示失败。`probe_refresh` 用 RT
// 换一次凭证判定有效性，并写回轮换后的新值，才是批量该走的路径。
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (cond, label) => { checked++; if (!cond) failures.push(label) }

const grok = readFileSync(
  new URL('../../platforms/grok/plugin.py', import.meta.url), 'utf8')
const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

// 平台侧：probe_refresh 声明为批量默认
const probeRefreshBlock = grok.slice(grok.indexOf('"id": "probe_refresh"') - 60)
const block = probeRefreshBlock.slice(0, 320)
ok(/"id": "probe_refresh"/.test(grok), 'Grok 没有声明 probe_refresh')
ok(/"batch_default": True/.test(block), 'probe_refresh 没有标记为批量默认动作')
// 单账号 probe 菜单项要保留。**不匹配整行字面量** —— 格式化、加字段
// （比如给它也加个 `params` 项）都会让整行断言误红，而它想表达的是
// 「probe 这个 id 还在声明里，且没有被标成批量默认」。
const probeDecl = (() => {
  const m = grok.match(/\{[^{}]*"id":\s*"probe"[^{}]*\}/)
  return m ? m[0] : ''
})()
ok(probeDecl !== '', 'Grok 不再声明单向的 probe 菜单项')
ok(!/"batch_default"\s*:\s*True/.test(probeDecl),
   'probe 被标成了批量默认 —— 它没有 AT 时必然失败，批量会让整批新号显示失败')

// 前端：优先 batch_default，未标记时回落到第一个
ok(/batch_default === true/.test(page), '前端没有优先使用平台指定的批量默认动作')
ok(/candidates\.find\(\(a\) => a\.batch_default === true\) \|\| candidates\[0\]/.test(page),
   '缺少「未指定时回落第一个」的规则，会让没标记的平台没有入口')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)