/**
 * 已完成任务的展示切片。
 *
 * 为什么单独抽出来：后端排序是「先状态档位（done→failed→stopped）再时间倒序」，
 * 直接 `slice(0, 20)` 会把**刚失败**的任务挤到后面藏起来 —— 而失败恰恰是
 * 最需要被看见的。这里改成「失败/停止优先，其余按时间」，并对两类各自限量。
 */

export type FinishedTask = {
  id: string
  status: string
}

/** 每个分组默认渲染多少条。 */
export const FINISHED_PAGE_SIZE = 20

const NEEDS_ATTENTION = new Set(['failed', 'stopped'])

/** 这条任务是不是「需要关注」（失败/停止）。 */
export function needsAttention(task: FinishedTask): boolean {
  return NEEDS_ATTENTION.has(String(task.status || '').toLowerCase())
}

/**
 * 计算要渲染的已完成任务。
 *
 * 规则：**需要关注的不受折叠影响**（失败/停止全展示，它们数量通常很少），
 * 其余（done）按给定顺序取前 `pageSize` 条。
 */
export function visibleFinishedTasks<T extends FinishedTask>(
  finished: T[],
  showAll: boolean,
  pageSize: number = FINISHED_PAGE_SIZE,
): T[] {
  if (showAll) return finished
  const attention: T[] = []
  const rest: T[] = []
  for (const task of finished) {
    if (needsAttention(task)) attention.push(task)
    else rest.push(task)
  }
  // 保持后端给出的相对顺序，只对 done 施加折叠
  const kept = new Set(attention.map((t) => t.id))
  const merged: T[] = []
  let restShown = 0
  for (const task of finished) {
    if (kept.has(task.id)) {
      merged.push(task)
      continue
    }
    if (restShown < pageSize) {
      merged.push(task)
      restShown += 1
    }
  }
  return merged
}
