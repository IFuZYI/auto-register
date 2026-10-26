/**
 * 轮询间隔的自适应策略。
 *
 * 背景（实测）：`RunningTasks` 原本固定每 2.5 秒拉一次 `/api/tasks`。715 个任务里
 * **运行中 0 个**时，页面仍每 2.5 秒拉一次大响应 —— 60 秒 24 次请求，而内容完全
 * 没变。省掉的是「盯着空列表刷新」。
 *
 * 有运行中任务时保持快刷新（用户在看进度）；没有时退避到 15 秒
 * （新任务通常是用户自己发起的，切换页面即可立刻看到，不需要靠轮询发现）。
 */

/** 有运行中/等待中任务时的间隔：进度要实时。 */
export const POLL_INTERVAL_ACTIVE_MS = 2500
/** 没有运行中任务时的间隔：只为了发现「别的入口新起的任务」。 */
export const POLL_INTERVAL_IDLE_MS = 15000

/** 当前该用多大间隔轮询。 */
export function pollIntervalMs(hasActiveTasks: boolean): number {
  return hasActiveTasks ? POLL_INTERVAL_ACTIVE_MS : POLL_INTERVAL_IDLE_MS
}

/**
 * 是否需要每秒重渲染（刷新「已运行时长」）。
 *
 * 没有运行中任务时列表上没有任何「秒级变化的数字」，每秒 setState 只是白白
 * 让整棵树重渲染 —— 那是空转的 CPU。
 */
export function shouldTickSeconds(hasActiveTasks: boolean): boolean {
  return hasActiveTasks
}
