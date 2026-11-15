// 真实执行首页数据加载器：验证接口独立更新、乱序保护与异常响应。
import { build } from 'rolldown'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join, resolve } from 'node:path'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const temp = await mkdtemp(join(tmpdir(), 'dashboard-data-'))
const bundle = join(temp, 'dashboardData.mjs')
try {
  await build({
    input: resolve(frontend, 'src/lib/dashboardData.ts'),
    output: { format: 'esm', file: bundle },
    platform: 'neutral',
  })
  const { createDashboardDataLoader } = await import(pathToFileURL(bundle).href)
  const failures = []
  let checked = 0
  const check = (name, condition, detail = '') => {
    checked += 1
    if (!condition) failures.push({ name, detail })
  }
  const deferred = () => {
    let resolvePromise
    let rejectPromise
    const promise = new Promise((res, rej) => { resolvePromise = res; rejectPromise = rej })
    return { promise, resolve: resolvePromise, reject: rejectPromise }
  }
  const settle = () => new Promise(resolvePromise => setTimeout(resolvePromise, 0))
  const stats = total => ({ total, by_platform: {}, by_status: { registered: total } })
  const task = status => ({ status })

  // React 订阅必须在单项接口返回时就收到快照，不能等 Promise.all。
  const pendingStats = deferred()
  const readyTasks = deferred()
  const subscribed = createDashboardDataLoader({
    fetchStats: () => pendingStats.promise,
    fetchTasks: () => readyTasks.promise,
  })
  const published = []
  const subscribedLoad = subscribed.load(snapshot => published.push(snapshot))
  readyTasks.resolve([task('running')])
  await settle()
  check('快接口先通知订阅者', published.length === 1)
  check('先发布的任务能独立渲染', published[0]?.tasks[0]?.status === 'running')
  check('先发布时统计仍在加载', published[0]?.statsLoaded === false)
  pendingStats.resolve(stats(12))
  await subscribedLoad
  check('慢接口随后再发布一次', published.length === 2 && published[1].stats.total === 12)

  // 慢统计不能挡住先返回的任务；两个区块的加载状态必须独立。
  const slowStats = deferred()
  const fastTasks = deferred()
  const loader = createDashboardDataLoader({
    fetchStats: () => slowStats.promise,
    fetchTasks: () => fastTasks.promise,
  })
  const initial = loader.load()
  fastTasks.resolve([task('running')])
  await settle()
  check('任务先返回时立即可见', loader.getSnapshot().tasks[0]?.status === 'running')
  check('统计仍在自己的加载态', loader.getSnapshot().statsLoaded === false)
  slowStats.resolve(stats(8))
  check('两部分均成功应用', (await initial).applied === true)
  check('统计随后独立更新', loader.getSnapshot().stats.total === 8)

  // 旧请求即使晚到、忽略 abort，也不得覆盖最新成功响应。
  const first = { stats: deferred(), tasks: deferred() }
  const second = { stats: deferred(), tasks: deferred() }
  const calls = { stats: 0, tasks: 0 }
  const signals = []
  const racing = createDashboardDataLoader({
    fetchStats: signal => { signals.push(signal); return (++calls.stats === 1 ? first : second).stats.promise },
    fetchTasks: signal => (++calls.tasks === 1 ? first : second).tasks.promise,
  })
  const oldLoad = racing.load()
  const newLoad = racing.load()
  check('新请求会取消旧请求', signals[0].aborted === true)
  second.stats.resolve(stats(22))
  second.tasks.resolve([task('running'), task('pending')])
  check('最新请求结果生效', (await newLoad).applied === true)
  first.stats.resolve(stats(1))
  first.tasks.resolve([])
  check('被替代的请求不会应用', (await oldLoad).applied === false)
  check('旧统计不覆盖新统计', racing.getSnapshot().stats.total === 22)
  check('旧任务不覆盖新任务', racing.getSnapshot().tasks.length === 2)

  const failingTasks = deferred()
  const malformed = createDashboardDataLoader({
    fetchStats: () => Promise.resolve(stats(3)),
    fetchTasks: () => failingTasks.promise,
  })
  const failed = malformed.load()
  failingTasks.reject(new Error('连接中断'))
  await failed
  check('任务接口失败明确标记', malformed.getSnapshot().tasksFailed === true)
  check('任务失败不伪造成空列表', malformed.getSnapshot().tasksError === '连接中断')

  const wrongShape = createDashboardDataLoader({
    fetchStats: () => Promise.resolve(stats(3)),
    fetchTasks: () => Promise.resolve({ tasks: [] }),
  })
  const wrongResult = await wrongShape.load()
  check('异常任务结构也是失败', wrongShape.getSnapshot().tasksFailed === true)
  check('异常响应说明可诊断', wrongResult.tasksError === '任务接口返回格式不正确')

  const late = deferred()
  const disposed = createDashboardDataLoader({
    fetchStats: () => Promise.resolve(stats(5)),
    fetchTasks: () => late.promise,
  })
  void disposed.load()
  disposed.dispose()
  late.resolve([task('running')])
  await settle()
  check('卸载后不再应用任务响应', disposed.getSnapshot().tasksLoaded === false)
  disposed.dispose()

  // ── 慢接口自取消保护 ─────────────────────────────────────────
  // 实测 2026-11-15：`/api/accounts/stats` 要 3.5–6s，而工作台在有任务时每 15s
  // 轮询一次。只要上一次还没回来就再发一次，新的 abort 会掐掉旧的 —— 那一半
  // 数据永远不落地，界面停在骨架屏（playwright 抓到 net::ERR_ABORTED）。
  //
  // 设计取舍：loader 保留「新取消旧」（既有测试钉住，也对：用户点刷新就该
  // 立刻换新）。所以**不重复发起**的责任在调用方 —— loader 暴露 `isBusy()`，
  // 轮询看到它就别发。这里钉住 `isBusy` 的语义：
  //   · 请求在路上 → true（调用方应跳过这次轮询）
  //   · 全部落地   → false
  const patientStats = deferred()
  let statsStarts = 0
  const patient = createDashboardDataLoader({
    fetchStats: () => { statsStarts += 1; return patientStats.promise },
    fetchTasks: () => Promise.resolve([task('running')]),
  })
  check('初始空闲', patient.isBusy() === false, '未发起时就报占用')
  const patientLoad = patient.load()
  await settle()
  check(
    '统计在路上时 isBusy 为真（轮询据此跳过）',
    patient.isBusy() === true,
    '请求还没回来就报空闲 —— 轮询会重发并 abort 掉它',
  )
  patientStats.resolve(stats(5))
  await patientLoad
  check('统计落地后 isBusy 恢复为假', patient.isBusy() === false)
  check('统计最终仍然落地', patient.getSnapshot().stats?.total === 5)
  check('统计被标记为已加载', patient.getSnapshot().statsLoaded === true)

  console.log(JSON.stringify({ checked, failures }))
  process.exitCode = failures.length ? 1 : 0
} finally {
  await rm(temp, { recursive: true, force: true })
}
