// 真实执行「今天要不要管」的判读逻辑：验证优先级、边界与「不谎报」。
//
// 为什么用可执行检查而不是正则读源码：这段逻辑的价值全在**判断顺序**上
// （失败 > 运行 > 队列 > 补货 > 正常），正则无法验证顺序，只能验证字面量。
//
// 同时钉住一条此仓库反复踩过的红线：**数据没到不能报「正常」**。
// 2026-11-15 实测过同类缺陷（批量删除失败却弹「已删除」），
// 静默谎报比报错更难查。
import { build } from 'rolldown'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join, resolve } from 'node:path'

const frontend = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const temp = await mkdtemp(join(tmpdir(), 'attention-'))
const bundle = join(temp, 'attention.mjs')
const failures = []
let checked = 0

const check = (name, condition, detail = '') => {
  checked += 1
  if (!condition) failures.push({ name, detail })
}

try {
  await build({
    input: resolve(frontend, 'src/lib/attention.ts'),
    output: { format: 'esm', file: bundle },
    platform: 'neutral',
  })
  const { readAttention, LOW_POOL_THRESHOLD } = await import(pathToFileURL(bundle).href)

  // ── 1. 数据没到：必须说「正在读取」，不能报正常 ─────────────
  const unknown = readAttention({ loaded: false, total: 1702, byStatus: { registered: 997 } })
  check(
    '数据未加载时不得报「正常」',
    unknown.level === 'unknown' && !/正常/.test(unknown.headline),
    `实际: ${unknown.level} / ${unknown.headline}`,
  )
  check(
    '未知状态不应给出动作按钮',
    unknown.href === '' && unknown.action === '',
    `href=${unknown.href} action=${unknown.action}`,
  )

  // ── 2. 失败优先于运行中：失败会静默吃库存 ──────────────────
  const failedWins = readAttention({ loaded: true, failed: 2, running: 5, total: 100 })
  check(
    '有失败任务时，即使有任务在跑也要先报失败',
    failedWins.level === 'action' && failedWins.headline.includes('失败'),
    `实际: ${failedWins.level} / ${failedWins.headline}`,
  )
  check(
    '失败态要给出去处',
    failedWins.href === '/history' && failedWins.action.length > 0,
    `href=${failedWins.href}`,
  )

  // ── 3. 运行中 ────────────────────────────────────────────
  const running = readAttention({ loaded: true, running: 3, total: 100 })
  check(
    '有任务运行时报告运行中',
    running.level === 'active' && running.headline.includes('3'),
    `实际: ${running.level} / ${running.headline}`,
  )
  check(
    '运行中带排队数时一并说明',
    readAttention({ loaded: true, running: 1, queued: 2 }).detail.includes('2'),
    '排队数没进 detail',
  )

  // ── 4. 队列空闲但有排队（可能卡住） ────────────────────────
  const queuedOnly = readAttention({ loaded: true, queued: 4, total: 100 })
  check(
    '只有排队没有运行时提示可能卡住',
    queuedOnly.level === 'watch' && queuedOnly.headline.includes('4'),
    `实际: ${queuedOnly.level} / ${queuedOnly.headline}`,
  )

  // ── 5. 空账号 ────────────────────────────────────────────
  const empty = readAttention({ loaded: true, total: 0 })
  check('没有账号时引导去注册', empty.href === '/register', `href=${empty.href}`)
  check('没有账号时不说「一切正常」', !/一切正常/.test(empty.headline), empty.headline)

  // ── 6. 可用号低于阈值 → 提醒补货 ──────────────────────────
  const low = readAttention({
    loaded: true,
    total: 200,
    byStatus: { registered: LOW_POOL_THRESHOLD - 1, banned: 150, invalid: 20 },
  })
  check(
    '可用数低于阈值要提醒补货',
    low.level === 'watch' && low.headline.includes(String(LOW_POOL_THRESHOLD - 1)),
    `实际: ${low.level} / ${low.headline}`,
  )
  check(
    '补货提醒要说明不可用占比',
    low.detail.includes('200') && low.detail.includes(String(200 - (LOW_POOL_THRESHOLD - 1))),
    low.detail,
  )

  // ── 7. 阈值边界：恰好等于阈值不算低 ────────────────────────
  const atThreshold = readAttention({
    loaded: true,
    total: 500,
    byStatus: { registered: LOW_POOL_THRESHOLD },
  })
  check(
    '可用数恰好等于阈值时视为正常',
    atThreshold.level === 'clear',
    `实际: ${atThreshold.level} / ${atThreshold.headline}`,
  )

  // ── 8. 全绿：明确说正常，且不给多余动作 ────────────────────
  const clear = readAttention({
    loaded: true,
    total: 100,
    byStatus: { registered: 90, banned: 10 },
  })
  check('全绿时明确说正常', clear.level === 'clear' && /正常/.test(clear.headline), clear.headline)
  check('全绿时不给动作按钮', clear.href === '' && clear.action === '', `href=${clear.href}`)
  check('全绿时 detail 仍给可用数', clear.detail.includes('90'), clear.detail)

  // ── 9. 脏数据不得抛错（后端可能返回 null / 字符串） ─────────
  let dirtyThrew = false
  try {
    readAttention({ loaded: true, total: null, byStatus: null, running: 'x', failed: undefined })
  } catch (error) {
    dirtyThrew = true
  }
  check('脏数据不得抛错', !dirtyThrew, 'readAttention 对 null/字符串抛了异常')

  // ── 10. 「只看 registered」是刻意的：过期/失效/封号都不可用 ──
  const mixed = readAttention({
    loaded: true,
    total: 100,
    byStatus: { registered: 10, expired: 40, invalid: 25, banned: 25 },
  })
  check(
    '只有 registered 计入可用（过期/失效/封号不算）',
    mixed.level === 'watch' && mixed.headline.includes('10'),
    `实际: ${mixed.level} / ${mixed.headline}`,
  )

  // ── 11. 部分数据到手就要用：任务比账号统计快，不能被后者拖住 ──
  //     实测 2026-11-15：/api/tasks 约 0.5s 返回、/api/accounts/stats 要 2–4s。
  //     「等两个都到齐」会让横条在最需要它的头几秒说「正在读取」，
  //     而右侧明明已经显示「1 个任务正在运行」——把已有信息藏起来。
  const tasksOnlyRunning = readAttention({
    tasksLoaded: true,
    statsLoaded: false,
    running: 1,
  })
  check(
    '任务已加载且正在运行 → 立即报运行中（不等账号统计）',
    tasksOnlyRunning.level === 'active' && tasksOnlyRunning.headline.includes('1'),
    `实际: ${tasksOnlyRunning.level} / ${tasksOnlyRunning.headline}`,
  )

  const tasksOnlyFailed = readAttention({
    tasksLoaded: true,
    statsLoaded: false,
    failed: 3,
  })
  check(
    '任务已加载且有失败 → 立即报失败（最高优先，不等账号统计）',
    tasksOnlyFailed.level === 'action' && tasksOnlyFailed.headline.includes('3'),
    `实际: ${tasksOnlyFailed.level} / ${tasksOnlyFailed.headline}`,
  )

  const statsOnly = readAttention({
    tasksLoaded: false,
    statsLoaded: true,
    total: 1702,
    byStatus: { registered: 997 },
  })
  check(
    '任务还没加载时，不得断言队列空闲',
    statsOnly.level === 'unknown' && !/空闲/.test(statsOnly.headline),
    `实际: ${statsOnly.level} / ${statsOnly.headline}`,
  )

  const nothingLoaded = readAttention({ tasksLoaded: false, statsLoaded: false })
  check(
    '都没加载时说正在读取',
    nothingLoaded.level === 'unknown' && /读取/.test(nothingLoaded.headline),
    nothingLoaded.headline,
  )

  const bothClear = readAttention({
    tasksLoaded: true,
    statsLoaded: true,
    total: 100,
    byStatus: { registered: 90 },
    running: 0, queued: 0, failed: 0,
  })
  check(
    '两个都加载且无任务时才能报正常',
    bothClear.level === 'clear',
    `实际: ${bothClear.level} / ${bothClear.headline}`,
  )

  // ── 12. 动作链接必须是**真实存在的路由** ─────────────────────
  // 实测 2026-11-15：我第一版把 href 写成 `/task-center/tasks` / `/task-center/history`，
  // 而 App.tsx 里的真实路由是 `/running-tasks` / `/history` —— 点了会落 404。
  // 断言方式：把每个可能出现的 href 收出来，逐一比对真实路由集合。
  const ROUTES = new Set([
    '/', '/accounts', '/register', '/running-tasks', '/history',
    '/panel-management', '/proxies', '/settings', '/mail', '/authorized-accounts',
  ])
  const hrefSamples = [
    readAttention({ loaded: true, failed: 1 }).href,
    readAttention({ loaded: true, running: 1 }).href,
    readAttention({ loaded: true, queued: 1 }).href,
    readAttention({ loaded: true, total: 0 }).href,
    readAttention({ loaded: true, total: 100, byStatus: { registered: 1 } }).href,
  ].filter(Boolean)
  check('至少有一种状态会给出链接', hrefSamples.length > 0, '一个链接都没有')
  for (const href of hrefSamples) {
    check(
      `动作链接 ${href} 存在于路由表`,
      ROUTES.has(href),
      `路由表里没有 ${href} —— 点了会 404`,
    )
  }

  if (failures.length > 0) {
    console.error(JSON.stringify({ checked, failures }, null, 2))
    process.exitCode = 1
  } else {
    console.log(`attention checks passed (${checked} assertions)`)
  }
} finally {
  await rm(temp, { recursive: true, force: true })
}