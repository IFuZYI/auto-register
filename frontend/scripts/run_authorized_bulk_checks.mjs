// AWS 授权页也要有批量测活 / 批量刷新：它不在 /accounts/{platform} 路由下，
// 用的是独立页面，所以账号页的批量入口补不到这里。
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (cond, label) => { checked++; if (!cond) failures.push(label) }
const page = readFileSync(new URL('../src/pages/AuthorizedAccounts.tsx', import.meta.url), 'utf8')

ok(/批量测活/.test(page), 'AWS 授权页缺「批量测活」入口')
ok(/批量刷新凭据/.test(page), 'AWS 授权页缺「批量刷新凭据」入口')
ok(/runBulkAction/.test(page), '批量动作没有独立处理函数')
ok(/all_filtered: true/.test(page), '批量范围没走当前筛选结果')
ok(/platform: 'aws'/.test(page), '批量动作没有指定 aws 平台')
ok(/action_id: actionId/.test(page), '批量动作没有传动作 id')
ok(/\/tasks\/batch-action/.test(page), '批量动作没走平台无关的通用端点')
ok(/payload\.email = email/.test(page) && /payload\.status = status/.test(page),
   '筛选条件没有透传给后端（处理数会大于显示数）')
ok(/actionId: 'probe' \| 'refresh_token'/.test(page), '批量动作类型应与行内动作同源')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)