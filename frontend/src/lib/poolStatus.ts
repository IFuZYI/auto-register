/**
 * 号池状态的展示映射（iCloud / Outlook 共用一套语义）。
 *
 * 存储层是四态（`unpooled` / `available` / `in_use` / `used`，Outlook 另有
 * `failed`），但**展示层只有三态**（用户要求 2026-10-11）：
 *
 *   - 未入池：`unpooled` —— 生成/同步下来但还没被勾选「导入邮箱池」；
 *   - 已入池：`available` + `used` —— 进了池子（可领 / 已被领用）；
 *   - 使用中：`in_use` —— 正被某个注册任务持有。
 *
 * 「是否被某平台使用过」**不进这个标签** —— 那是「已注册平台」列的职责
 * （一个地址可以被多个平台使用，只显示「已使用/未使用」区分不出是哪个
 * 平台用过的）。
 *
 * 后端 `platforms/icloud/constants.py` 声明这是两边池页面共用的一套语义；
 * 这里做展示层单点定义（状态值本身的权威在后端）。
 */

/** 展示层三态。 */
export type PoolDisplayStatus = 'unpooled' | 'pooled' | 'in_use'

/** 展示层三态（+ Outlook 独有「失败」）→ antd Tag 的 color 与中文文案。 */
export const POOL_DISPLAY_META: Record<PoolDisplayStatus | 'failed', { color: string; text: string }> = {
  /**
   * iCloud / Outlook 共有：还没被勾选「导入邮箱池」的地址。
   * 注册取号会跳过它，用中性灰色，避免看起来像池里的可用号。
   */
  unpooled: { color: 'default', text: '未入池' },
  /** `available` + `used` 的合并：进了池子（可领 / 已被领用）。 */
  pooled: { color: 'green', text: '已入池' },
  in_use: { color: 'processing', text: '使用中' },
  /** Outlook 号池独有：导入时该地址就被判定不可用。 */
  failed: { color: 'red', text: '失败' },
}

/**
 * 存储层状态值 → 展示层三态。
 *
 * `failed` 原样保留：它不是「入没入池」的第三态，而是「不可用」的独立
 * 结论（Outlook 导入时判定），折叠进「已入池」会把它洗成可用号。
 */
export function poolDisplayStatus(value: string | null | undefined): PoolDisplayStatus | 'failed' {
  const raw = String(value || '').trim().toLowerCase()
  if (raw === 'unpooled') return 'unpooled'
  if (raw === 'in_use') return 'in_use'
  if (raw === 'failed') return 'failed'
  // available / used / 老库的空值 / 未知值 → 已入池（与后端 pool_summary 的兜底口径一致）
  return 'pooled'
}

/** 取状态对应的展示元数据；未知值按「已入池」处理。 */
export function poolStatusMeta(value: string | null | undefined) {
  return POOL_DISPLAY_META[poolDisplayStatus(value)]
}

/**
 * 池概览统计条要展示的固定顺序。
 *
 * `unpooled` 排在最前且与另两个分开理解：它统计的是「还没进池的地址」，
 * 不是「池里的号」。用户看到「未入池 168」应当明白注册取不到号是正常的，
 * 而不是以为池子有 168 个闲号却取不到。
 */
export const POOL_DISPLAY_ORDER = ['unpooled', 'pooled', 'in_use'] as const

/** 把后端四态计数折叠成展示层三态计数（统计条用）。 */
export function poolDisplaySummary(summary: {
  unpooled?: number
  available?: number
  used?: number
  in_use?: number
  total?: number
}) {
  return {
    unpooled: Number(summary.unpooled || 0),
    pooled: Number(summary.available || 0) + Number(summary.used || 0),
    in_use: Number(summary.in_use || 0),
    total: Number(summary.total || 0),
  }
}
