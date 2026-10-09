// 日志清理的保留期选项 —— 「任务历史」与「任务运行」两页共用一份定义。
//
// 两页各写一份选项会出现「历史页有 90 天、运行页没有」这类分叉；保留期又是直接
// 传给清理端点的数值，写错一位就是删错数据。所以选项与端点路径都收在这里，
// 由 `scripts/run_log_cleanup_checks.mjs` 真实执行验证。

export interface RetentionOption {
  /** 传给清理端点的天数 */
  days: number
  label: string
}

/** 保留期选项（用户要求：「清理1天/周/月前的日志」）。 */
export const RETENTION_OPTIONS: RetentionOption[] = [
  { days: 1, label: '1 天前' },
  { days: 7, label: '1 周前' },
  { days: 30, label: '1 个月前' },
]

/** 清理端点：任务历史（task_logs）/ 任务运行记录（task_runs）。 */
export const CLEANUP_ENDPOINTS = {
  logs: '/tasks/logs/cleanup',
  runs: '/tasks/runs/cleanup',
} as const

/** 保留天数 → 展示文案；不在选项表里的值原样给出（不吞掉）。 */
export function retentionLabel(days: number): string {
  const hit = RETENTION_OPTIONS.find((option) => option.days === days)
  return hit ? hit.label : `${days} 天前`
}

/**
 * 下拉菜单 key → 选项对象。
 *
 * 菜单 key 是 `String(days)`，但反解不能走 `Number(key)`：将来若加入非数字
 * key，`Number` 会给出 `NaN` 并发起一次 `older_than_days: NaN` 的请求 ——
 * 后端 422，用户看到的是「清理失败」而不知道原因。查不到就返回 undefined，
 * 由调用方跳过。
 */
export function retentionOptionFromKey(key: string): RetentionOption | undefined {
  return RETENTION_OPTIONS.find((option) => String(option.days) === key)
}
