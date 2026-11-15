// Kiro 派生判定不能只看前 200 条。
//
// `AuthorizedAccounts.tsx` 原来固定请求 `page=1&page_size=200`，用返回的
// items 建 `derivedMap`；而「补 Kiro」按钮靠这张图判断「这个 AWS 号有没有
// 派生过 Kiro」。超过 200 个 Kiro 账号时，后面那些一律被判成「未派生」，
// 点下去必然失败（重复注册同一来源）。
//
// 后端 `page_size` 上界刻意是 200（1000 时实测 988ms / 3.5MB），所以不能靠
// 调大参数解决 —— 要分页取全，且要有上限兜底，避免无限翻页。
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (cond, label) => { checked++; if (!cond) failures.push(label) }
const page = readFileSync(new URL('../src/pages/AuthorizedAccounts.tsx', import.meta.url), 'utf8')

ok(!/platform=kiro&page=1&page_size=200'/.test(page),
   '仍只取 Kiro 第一页 200 条 —— 超过 200 个派生账号会被误判为「未派生」')
ok(/fetchAllKiro|loadAllKiro|kiroPage|has_more|totalPages/.test(page),
   '没有分页取全 Kiro 账号的实现')
ok(/MAX_KIRO_PAGES|MAX_PAGES|while \(/.test(page),
   '分页没有上限兜底（后端异常时可能无限翻页）')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)