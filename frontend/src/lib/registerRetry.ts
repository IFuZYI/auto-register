/** 整条注册流程失败后再开几轮：每一轮都是全新的邮箱 / 号码 / 会话。 */
export const DEFAULT_REGISTER_RETRY_TIMES = 1
export const MAX_REGISTER_RETRY_TIMES = 10

export function normalizeRegisterRetryTimes(value: unknown): number {
  const parsed =
    typeof value === 'number' ? value : Number.parseInt(String(value ?? ''), 10)
  if (!Number.isFinite(parsed) || parsed < 0) return DEFAULT_REGISTER_RETRY_TIMES
  return Math.min(Math.trunc(parsed), MAX_REGISTER_RETRY_TIMES)
}
