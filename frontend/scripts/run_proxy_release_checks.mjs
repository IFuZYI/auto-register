// 代理绑定必须在界面上可逆：账号详情里要能解绑。
//
// 后端 `PATCH /accounts/{id}/proxy`（`proxy: ""` = 解绑）在 2026-11-14 补上，
// 此前整个系统没有任何路径能把账号从代理上摘下来 —— 绑错之后只能靠
// `rebalance` 挪到另一个带 bind 标签的代理，没有可绑定代理时永久卡死。
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (cond, label) => { checked++; if (!cond) failures.push(label) }
const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

ok(/PATCH\(|\/proxy`/.test(page) || /\/proxy`/.test(page), '账号页没有调用代理绑定端点')
ok(/\/accounts\/\$\{[^}]*\}\/proxy|\/accounts\/[^`]*\/proxy/.test(page),
   '没有调用 /accounts/{id}/proxy')
ok(/解除绑定|解绑/.test(page), '绑定代理区域没有「解绑」入口')
ok(/proxy:\s*(""|'')/.test(page), '解绑没有传空串（后端约定空串 = 解绑）')

// 只在真正绑定了代理时才显示解绑，避免对未绑定账号给一个无意义按钮。
const block = page.slice(page.indexOf('title="绑定代理"'), page.indexOf('title="绑定代理"') + 1400)
ok(/bindProxyRelease|handleReleaseProxy|releaseProxy/.test(block),
   '「绑定代理」区域没有接上解绑处理函数')

// 被误停用的代理要能一键复活（后端 /proxies/reactivate，2026-11-14 补的入口）。
const proxies = readFileSync(new URL('../src/pages/Proxies.tsx', import.meta.url), 'utf8')
ok(/\/proxies\/reactivate/.test(proxies), '代理页没有调用 /proxies/reactivate')
ok(/恢复停用|复活/.test(proxies), '代理页没有「恢复停用」入口')

// 检测全部是后台串行任务（8s/条），固定 3 秒刷新会显示过期结果。
ok(!/setTimeout\(\s*\(\s*\)\s*=>\s*\{[^}]*load\(\)[^}]*\},\s*3000\s*\)/.test(proxies),
   '「检测全部」仍在固定 3 秒后刷新 —— 20 个代理要 160 秒，看到的是过期结果')
ok(/\/proxies\/check-status|checkingFromServer|轮询|poll/i.test(proxies),
   '「检测全部」没有改为按真实进度轮询')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)