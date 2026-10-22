// 真实执行 lib/finishedTasks.ts 的纯函数（与 run_*.mjs 同一套做法）。
import { build } from 'rolldown'

const result = await build({
  input: 'src/lib/finishedTasks.ts',
  output: { format: 'esm', file: '/tmp/finishedTasks.bundle.mjs' },
  platform: 'neutral',
})
void result

const mod = await import('/tmp/finishedTasks.bundle.mjs?' + Date.now())
const { visibleFinishedTasks, FINISHED_PAGE_SIZE } = mod

const failures = []
let checked = 0
const check = (name, cond, detail = '') => {
  checked += 1
  if (!cond) failures.push({ name, detail })
}

const mk = (id, status) => ({ id, status, created_at: '2026-01-01T00:00:00Z' })

// 1) 25 条 done + 1 条刚 failed：failed 必须可见
const many = [...Array(25)].map((_, i) => mk(`done${i}`, 'done'))
const withFailure = [...many, mk('recent_failed', 'failed')]
const shown = visibleFinishedTasks(withFailure, false)
check('failed 不被折叠隐藏', shown.some((t) => t.id === 'recent_failed'),
  shown.map((t) => t.id).join(','))
check('done 被折叠到 20 条',
  shown.filter((t) => t.status === 'done').length === FINISHED_PAGE_SIZE,
  String(shown.filter((t) => t.status === 'done').length))

// 2) stopped 同样不受折叠影响
const withStopped = [...many, mk('s1', 'stopped')]
check('stopped 不被折叠隐藏',
  visibleFinishedTasks(withStopped, false).some((t) => t.id === 's1'))

// 3) 展开时全给
check('展开返回全部',
  visibleFinishedTasks(withFailure, true).length === withFailure.length)

// 4) 少于上限时原样返回
const few = [mk('a', 'done'), mk('b', 'failed')]
check('少于上限原样返回', visibleFinishedTasks(few, false).length === 2)

// 5) 保持后端相对顺序（不能因为分组把顺序打乱）
const orderIn = [mk('f1', 'failed'), mk('d1', 'done'), mk('f2', 'failed'), mk('d2', 'done')]
const orderOut = visibleFinishedTasks(orderIn, false).map((t) => t.id)
check('保持相对顺序', orderOut.join(',') === 'f1,d1,f2,d2', orderOut.join(','))

// 6) 全是 done 时不额外放行
const allDone = [...Array(30)].map((_, i) => mk(`d${i}`, 'done'))
check('全 done 时只给 20',
  visibleFinishedTasks(allDone, false).length === FINISHED_PAGE_SIZE)

console.log(JSON.stringify({ checked, failures }))
process.exit(failures.length ? 1 : 0)
