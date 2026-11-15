export interface AccountStats {
  total?: number
  by_platform?: Record<string, number>
  by_status?: Record<string, number>
}

export interface TaskStatusItem {
  status?: string | null
}

export interface DashboardDataSnapshot {
  stats: AccountStats | null
  statsLoaded: boolean
  statsFailed: boolean
  statsError: string
  tasks: TaskStatusItem[]
  tasksLoaded: boolean
  tasksFailed: boolean
  tasksError: string
}

export interface DashboardLoadResult {
  applied: boolean
  statsError?: string
  tasksError?: string
}

interface DataSource {
  fetchStats: (signal: AbortSignal) => Promise<AccountStats>
  fetchTasks: (signal: AbortSignal) => Promise<TaskStatusItem[]>
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

/**
 * 每类数据独立 settle，不让较慢的接口阻塞另一块。
 * 请求代数 + AbortController 双保险：忽略 abort 的旧请求也不能覆盖新数据。
 */
export function createDashboardDataLoader(source: DataSource) {
  let snapshot: DashboardDataSnapshot = {
    stats: null,
    statsLoaded: false,
    statsFailed: false,
    statsError: '',
    tasks: [],
    tasksLoaded: false,
    tasksFailed: false,
    tasksError: '',
  }
  let generation = 0
  let controller: AbortController | null = null
  /**
   * 还挂在路上的请求。
   *
   * 为什么需要：`/api/accounts/stats` 实测要 3.5–6s，而工作台在有任务时每 15s
   * 轮询一次。只要**上一次请求还没回来**就再发一次，新的 `abort()` 会把旧的
   * 掐掉 —— 那一半数据永远不落地，界面停在骨架屏（实测抓到
   * `net::ERR_ABORTED`）。
   *
   * 所以：同一种数据还有请求在路上时不重复发起，让旧的自然完成。
   * 两种数据各自独立判断 —— 任务快、统计慢，不能因为一个慢就把快的也冻住。
   */
  const inFlight: { stats: boolean; tasks: boolean } = { stats: false, tasks: false }

  return {
    getSnapshot: () => snapshot,
    /** 还有数据在路上吗？调用方（轮询）据此决定这次要不要真发。 */
    isBusy: () => inFlight.stats || inFlight.tasks,
    load(onSnapshot?: (next: DashboardDataSnapshot) => void): Promise<DashboardLoadResult> {
      const current = ++generation
      controller?.abort()
      controller = new AbortController()
      const { signal } = controller
      const publish = () => onSnapshot?.(snapshot)
      let statsError: string | undefined
      let tasksError: string | undefined

      inFlight.stats = true
      inFlight.tasks = true

      const statsPromise = source.fetchStats(signal).then(value => {
        if (current !== generation) return
        snapshot = { ...snapshot, stats: value, statsLoaded: true, statsFailed: false, statsError: '' }
        publish()
      }).catch(error => {
        if (current !== generation) return
        statsError = errorMessage(error, '账号数据加载失败')
        snapshot = { ...snapshot, statsLoaded: true, statsFailed: true, statsError }
        publish()
      }).finally(() => { inFlight.stats = false })

      const tasksPromise = source.fetchTasks(signal).then(value => {
        if (!Array.isArray(value)) throw new Error('任务接口返回格式不正确')
        if (current !== generation) return
        snapshot = { ...snapshot, tasks: value, tasksLoaded: true, tasksFailed: false, tasksError: '' }
        publish()
      }).catch(error => {
        if (current !== generation) return
        tasksError = errorMessage(error, '任务状态加载失败')
        snapshot = { ...snapshot, tasksLoaded: true, tasksFailed: true, tasksError }
        publish()
      }).finally(() => { inFlight.tasks = false })

      return Promise.all([statsPromise, tasksPromise]).then(() => ({
        applied: current === generation,
        statsError,
        tasksError,
      }))
    },
    dispose() {
      generation += 1
      controller?.abort()
      controller = null
    },
  }
}
