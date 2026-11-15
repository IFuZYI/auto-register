/**
 * 「今天要不要管」的判读 —— 把一堆数字压成一句话。
 *
 * 为什么需要它：工作台现在给你四张等权卡片（账号可用性 / 任务状态 /
 * 各平台 / 状态明细），每个都能点、都有数字，但**没有一个回答**
 * 「我现在需要做什么」。运维每天打开它的第一个问题就是这个，而
 * 1702 个账号里 607 个已封、68 个失效 —— 这些数字摆在那里，却要人
 * 自己算、自己判断「算不算多」。
 *
 * 这个模块只做判读，不碰 DOM，纯函数，便于钉住行为。
 *
 * 判读顺序（先看做不了的，再看要做的，最后看好的）：
 * 1. 有任务在跑 → 告知进度（这是「正在发生」的事）
 * 2. 有任务失败 → 最高优先，因为失败会静默吃掉库存
 * 3. 号池/可用号少 → 提醒补货（可注册的号才是产能源）
 * 4. 全绿 → 明确说「正常」，不留模糊
 */

export type AttentionLevel = 'action' | 'active' | 'watch' | 'clear' | 'unknown'

export interface AttentionReading {
  level: AttentionLevel
  /** 一句话主结论，<= 24 字，直接渲染，不含标点尾巴。 */
  headline: string
  /** 支撑结论的一行细节；空串表示不需要。 */
  detail: string
  /** 要去的页面（相对路由）；空串表示不需要动作。 */
  href: string
  /** 动作按钮文案；随 href 一起出现。 */
  action: string
}

export interface AttentionInput {
  /** 账号总数 */
  total?: number
  /** 按状态计数，键是后端状态名 */
  byStatus?: Record<string, number>
  /** 运行中的任务数 */
  running?: number
  /** 等待中的任务数 */
  queued?: number
  /** 失败的任务数 */
  failed?: number
  /** 任务数据是否已加载。任务接口快（约 0.5s），先用它下结论。 */
  tasksLoaded?: boolean
  /** 账号统计是否已加载。它慢（实测 2–4s），不能拖住任务结论。 */
  statsLoaded?: boolean
  /** 兼容旧调用：等价于两个都已加载。 */
  loaded?: boolean
}

/** 可正常使用的状态。后端四态：registered / expired / invalid / banned。 */
const USABLE_STATUSES = ['registered'] as const

/** 低于这个可用数就提醒补货：一次批量任务通常 20–100 个，留一档余量。 */
export const LOW_POOL_THRESHOLD = 35

function num(value: unknown): number {
  const parsed = Number(value ?? 0)
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : 0
}

/**
 * 判读。
 *
 * 注意 `loaded === false` 时**绝不能说「一切正常」** —— 数据还没到，
 * 说正常等于给了一个用户会信任的错误结论（这类「静默谎报」在本仓库
 * 已经修过一次：批量删除失败却弹「已删除」）。未知就明说未知。
 */
export function readAttention(input: AttentionInput): AttentionReading {
  // 两个接口速度差一个数量级（任务 0.5s / 账号统计 2–4s）。**已到手的信息先用**：
  // 任务在跑就该立刻说在跑，不能因为账号统计没回来而让横条停在「正在读取」
  // —— 那等于把已知的事藏起来，而用户最关心的恰恰是「有没有任务在跑」。
  const legacy = input.loaded
  const tasksLoaded = input.tasksLoaded ?? legacy ?? false
  const statsLoaded = input.statsLoaded ?? legacy ?? false

  const running = num(input.running)
  const queued = num(input.queued)
  const failed = num(input.failed)

  // ── 任务相关结论：只要任务数据到了就能下 ──────────────────────
  if (tasksLoaded) {
    if (failed > 0) {
      return {
        level: 'action',
        headline: `${failed} 个任务失败，需要查看`,
        detail: '失败会静默消耗邮箱与代理，越早处理损失越小',
        href: '/history',
        action: '查看失败任务',
      }
    }

    if (running > 0) {
      return {
        level: 'active',
        headline: `${running} 个任务正在运行`,
        detail: queued > 0 ? `另有 ${queued} 个在排队` : '可以查看实时日志',
        href: '/running-tasks',
        action: '查看进度',
      }
    }

    if (queued > 0) {
      return {
        level: 'watch',
        headline: `${queued} 个任务在排队`,
        detail: '队列还没开始跑，检查是否有并发上限卡住',
        href: '/running-tasks',
        action: '查看队列',
      }
    }
  }

  // ── 账号相关结论：必须两个都到齐 ─────────────────────────────
  // 「队列空闲」要同时知道任务与账号；只到一半就说「一切正常」是谎报
  // （实测：任务未加载时曾报「还没有账号」，把「不知道」说成了「没有」）。
  if (!statsLoaded || !tasksLoaded) {
    return {
      level: 'unknown',
      headline: '正在读取当前状态',
      detail:
        !statsLoaded && tasksLoaded
          ? '任务状态已就绪，账号统计还在读'
          : '数据还没回来，先不下结论',
      href: '',
      action: '',
    }
  }

  const total = num(input.total)
  const usable = USABLE_STATUSES.reduce(
    (sum, key) => sum + num((input.byStatus ?? {})[key]),
    0,
  )
  const dead = total - usable

  if (total === 0) {
    return {
      level: 'clear',
      headline: '还没有账号',
      detail: '发起第一个注册任务，或在账号页导入已有凭据',
      href: '/register',
      action: '注册账号',
    }
  }

  if (usable < LOW_POOL_THRESHOLD) {
    return {
      level: 'watch',
      headline: `可用账号只剩 ${usable} 个`,
      detail: `已有 ${total} 个账号，其中 ${dead} 个不可用；低于 ${LOW_POOL_THRESHOLD} 就该补货`,
      href: '/register',
      action: '补注册',
    }
  }

  return {
    level: 'clear',
    headline: '一切正常，没有待处理事项',
    detail: `${usable} 个可用 / 共 ${total} 个；队列空闲`,
    href: '',
    action: '',
  }
}
