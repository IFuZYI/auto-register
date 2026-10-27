// 「检测本地状态」必须走任务端点，不许退回同步批量调用。
import { readFileSync } from 'node:fs'
const failures = []
let checked = 0
const ok = (c, l) => { checked++; if (!c) failures.push(l) }
const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

ok(/\/tasks\/probe-status/.test(page), '检测本地状态没走任务端点 /tasks/probe-status')
ok(/kind === 'probe'/.test(page), '缺少 probe 分支判断')
// 任务分支必须在同步批量调用**之前** return，否则仍会走旧路径
const taskIdx = page.indexOf('/tasks/probe-status')
const batchIdx = page.indexOf('/actions/${currentPlatform}/${actionId}/batch')
ok(taskIdx > 0 && batchIdx > taskIdx, '任务分支没有在同步批量调用之前生效')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
