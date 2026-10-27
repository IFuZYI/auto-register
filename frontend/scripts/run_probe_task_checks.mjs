// 批量动作必须跑成任务（通用端点），不许退回同步批量调用。
//
// 用户要求「整个项目大部分功能都统一基于任务」。用**通用**端点
// (`/tasks/batch-action`) 而不是逐个动作写专用端点 —— 新增动作时前端不用改。
import { readFileSync } from 'node:fs'
const failures = []
let checked = 0
const ok = (c, l) => { checked++; if (!c) failures.push(l) }
const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

ok(/\/tasks\/batch-action/.test(page), '批量动作没走通用任务端点')
ok(/action_id: actionId/.test(page), '任务请求没带 action_id')
ok(!/\/actions\/\$\{currentPlatform\}\/\$\{actionId\}\/batch/.test(page),
   '仍在调同步批量端点 —— 任务化没生效')
// 结果展示：任务模式下不再有同步返回的 items 可展示
ok(!/showBatchActionResult/.test(page), '同步批量的结果展示函数还在（死代码）')

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
