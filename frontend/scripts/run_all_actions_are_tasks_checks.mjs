// 所有作业都必须是任务 —— 包括单个账号的动作。
//
// 用户要求（2026-10-28）：「所有作业都是任务，包括刷新token，检测状态，
// 包括单个的也是。都会弹出任务日志窗口，在任务日志中显示。」
//
// 之前的实现：只有 `backfill_refresh_token` / `bind_2fa` 两个白名单动作走
// 任务，其余（refresh_token / probe_local_status / check_plus_trial /
// sync_* / upload_* / grok 的 probe、refresh_oauth）都走同步端点 +
// Toast 弹窗 —— 用户看不到任务日志，也没法在任务页里追踪/停止。
import { readFileSync } from 'node:fs'

const failures = []
let checked = 0
const ok = (c, l) => { checked++; if (!c) failures.push(l) }

const page = readFileSync(new URL('../src/pages/Accounts.tsx', import.meta.url), 'utf8')

// 1) 不许再有「同步动作端点」的调用（单个动作也必须走任务）
ok(!/\/actions\/\$\{[^}]*\}\/\$\{[^}]*\}\/\$\{actionId\}/.test(page),
   '仍有单个动作直接调同步端点 /actions/{platform}/{id}/{actionId}')

// 2) 必须走通用任务端点
ok(/\/tasks\/batch-action/.test(page), '没有走 /tasks/batch-action')

// 3) 单个动作也要弹任务日志窗口（复用 runAsTask 的 modal）
ok(/setTaskModalKind/.test(page) && /setActionTaskId/.test(page),
   '单个动作没有打开任务日志窗口（setTaskModalKind / setActionTaskId 缺失）')

// 4) 不许用「动作白名单」把某些动作排除在任务之外
ok(!/TASK_BACKED_ACTIONS/.test(page),
   '仍有 TASK_BACKED_ACTIONS 白名单 —— 部分动作被排除在任务之外')

// 5) **单个动作**执行完不许用 Toast 报「完成/失败」（结果在任务日志里看）。
//
// 注意范围：批量入口的 toast 是允许的 —— 任务是后台跑的，用户需要一条
// 「已开始，去任务运行页看」的提示才知道去哪看进度。这里只禁单动作路径。
const runAsTaskBody = (() => {
  const s = page.indexOf('const runAsTask = async')
  const e = page.indexOf('const handleAction = async', s)
  return s === -1 || e === -1 ? '' : page.slice(s, e)
})()
ok(!/message\.success/.test(runAsTaskBody),
   '单动作成功后仍用 Toast —— 结果应在任务日志窗口里显示')
ok(!/message\.loading/.test(runAsTaskBody),
   '单动作仍在用 Toast loading —— 任务日志窗口自带进度')
// 失败时允许 Toast：那是「任务都没建成」，没有任务日志可看

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
