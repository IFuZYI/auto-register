// 批量测活 / 批量刷新必须由平台声明的能力驱动，不能硬编码 chatgpt。
// 后端 `/tasks/batch-action` 是平台无关的；前端曾经只在 ChatGPT 分支渲染
// 「状态同步」与「刷新 Token」，导致 Grok / Kiro / AWS 只剩单账号入口。
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (cond, label) => { checked++; if (!cond) failures.push(label) }
const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

// 批量菜单必须从 platformActions 的能力推导，而不是平台名
ok(/batchCapabilities/.test(page), '批量菜单没有使用平台能力（capability）来推导可执行动作')
ok(/byCapability\('probe'\)/.test(page) && /byCapability\('refresh'\)/.test(page),
   '没有从平台动作里解析出探测 / 刷新能力')
ok(!/if \(currentPlatform !== 'chatgpt'\) return/.test(page),
   'handleBatchStatusSync 仍直接拒绝非 ChatGPT 平台')
ok(/kind === 'probe' && !action[\s\S]{0,220}没有声明可批量执行的检测动作/.test(page),
   '探测动作缺失时没有给出明确提示，而不是静默返回')
ok(/batchCapabilities\.refresh \?/.test(page),
   '「刷新 Token」仍被平台名锁死，没有按能力渲染')

// 批量动作必须打平台无关的通用端点，并带上当前平台
ok(/\/tasks\/batch-action/.test(page), '批量动作没有走 /tasks/batch-action')
ok(/platform: currentPlatform/.test(page), '批量动作没有把当前平台传给后端')
ok(/const dedicated = currentPlatform === 'chatgpt'/.test(page) && /dedicated\s*\n?\s*\? await apiFetch\('\/tasks\/refresh-token'/.test(page),
   'ChatGPT 的专用刷新端点应保留，其它平台才走通用端点')

// 勾选优先、否则当前筛选 —— 既有范围约定不能被新平台破坏
ok(/selectedRowKeys\.length > 0 \? 'selected' : 'all'/.test(page),
   '批量范围没沿用「勾选优先，否则筛选结果」')
ok(/all_filtered/.test(page), '批量范围没走 all_filtered（筛选结果）')

// 批量补绑 2FA：`/tasks/bind-2fa` 一直只有单行菜单入口，`batchCapabilities.bind2fa`
// 算出来之后没人用 —— 几百个号补 2FA 得一个个点。
ok(/bind2fa\s*\?/.test(page) || /batchCapabilities\.bind2fa/.test(page),
   '批量能力算了 bind2fa 却没有任何渲染引用')
ok(/\/tasks\/bind-2fa/.test(page), '没有调用既有的 /tasks/bind-2fa 端点')
ok(/批量补绑\s*2FA|补绑 2FA/.test(page), '没有「批量补绑 2FA」入口')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)