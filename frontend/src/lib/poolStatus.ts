/**
 * 号池状态的展示映射（iCloud / Outlook 共用一套语义）。
 *
 * 后端 `platforms/icloud/constants.py` 明确声明这是「两边池页面共用的一套
 * 语义」，但前端一度把同一份 { color, text } 映射在三个地方各写一遍
 * （iCloud 别名列、iCloud 池概览 Tag、邮箱导入面板的状态列）。改一个状态
 * 的中文名或颜色要同时改三处，漏一处两个页面就会对同一状态显示不同标签。
 *
 * 这里做单点定义。注意这是**展示层**映射：状态值本身的权威在后端。
 */

/** 号池状态值 → antd Tag 的 color 与中文文案。 */
export const POOL_STATUS_META: Record<string, { color: string; text: string }> = {
  /**
   * iCloud 独有：生成/同步下来但还没被勾选「导入邮箱池」的别名。
   * 注册取号会跳过它，所以既不是 available 也不是 failed —— 用一个中性的
   * 灰色，避免和「未使用」（绿）看起来一样让人以为可以直接取号。
   */
  unpooled: { color: 'default', text: '未入池' },
  available: { color: 'green', text: '未使用' },
  in_use: { color: 'processing', text: '使用中' },
  used: { color: 'blue', text: '已使用' },
  /** Outlook 号池独有：导入时该地址就被判定不可用。 */
  failed: { color: 'red', text: '失败' },
}

/** 取状态对应的展示元数据；未知值按「未使用」处理（与后端默认一致）。 */
export function poolStatusMeta(value: string | null | undefined) {
  return POOL_STATUS_META[String(value || 'available')] ?? POOL_STATUS_META.available
}

/**
 * 池概览统计条要展示的固定顺序。
 *
 * `unpooled` 排在最前且与另三个分开理解：它统计的是「还没进池的地址」，
 * 不是「池里的可用号」。用户看到「未入池 168」应当明白注册取不到号是正常的，
 * 而不是以为池子有 168 个闲号却取不到。
 */
export const POOL_SUMMARY_ORDER = ['unpooled', 'available', 'in_use', 'used'] as const
