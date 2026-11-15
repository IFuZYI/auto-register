/** 首页只需要任务状态计数，不读取日志或渲染任务列表。 */
export interface DashboardTask {
  status?: string | null
}

export interface DashboardTaskSummary {
  running: number
  pending: number
  failed: number
}

/** 等待中的任务尚未开始，不能显示成「正在运行」；已停止也不是失败。 */
export function summarizeDashboardTasks(tasks: DashboardTask[]): DashboardTaskSummary {
  const summary: DashboardTaskSummary = { running: 0, pending: 0, failed: 0 }
  for (const task of tasks) {
    if (task.status === 'running') summary.running += 1
    if (task.status === 'pending') summary.pending += 1
    if (task.status === 'failed') summary.failed += 1
  }
  return summary
}
