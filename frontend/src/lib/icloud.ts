import type { ICloudAlias, ICloudRegion } from '@/api/icloud'

export const DEFAULT_ICLOUD_IMAP_HOST = 'imap.mail.me.com'
export const DEFAULT_ICLOUD_IMAP_PORT = 993

/** Apple 对每个主号限制每滚动小时最多成功生成 5 个隐私邮箱。 */
export const ICLOUD_HOURLY_ALIAS_LIMIT = 5

export const ICLOUD_REGION_OPTIONS: { value: ICloudRegion; label: string }[] = [
  { value: 'global', label: '国际版（icloud.com）' },
  { value: 'china', label: '中国大陆（icloud.com.cn）' },
]

export function getICloudRegionLabel(region?: string): string {
  return ICLOUD_REGION_OPTIONS.find((item) => item.value === region)?.label ?? region ?? '-'
}

/**
 * 导出格式沿用仓库里邮箱池导入那套 `----` 分隔：
 *   mail_url → 隐私邮箱----邮件 URL（默认，正好是邮箱导入里 `邮箱----mailapi_url` 那一行）
 *   account  → 隐私邮箱----所属主号（想知道每个别名挂在哪个 Apple ID 下时用）
 */
export type AliasExportMode = 'mail_url' | 'account'

export const ALIAS_EXPORT_FILENAME = 'icloud_aliases.txt'

export interface AliasExportRecord {
  address: string
  account_email: string
  share_token?: string
}

/**
 * 补 share_token 之前建的老别名没有免登录链接，这种行导不出 URL，只能整行跳过：
 * 写一行末尾空着的 `邮箱----` 反而会让对面的导入器报格式错。
 */
export function formatAliasExport(
  aliases: AliasExportRecord[],
  mode: AliasExportMode = 'mail_url',
): string {
  const lines: string[] = []
  for (const alias of aliases) {
    if (mode === 'account') {
      lines.push(`${alias.address}----${alias.account_email}`)
      continue
    }
    const url = aliasMailUrl(String(alias.share_token || ''))
    if (url) lines.push(`${alias.address}----${url}`)
  }
  return lines.join('\n')
}

/** 导出前先算能导出几条，好在没有链接的别名被跳过时如实告诉用户。 */
export function countExportableAliases(
  aliases: AliasExportRecord[],
  mode: AliasExportMode = 'mail_url',
): number {
  if (mode === 'account') return aliases.length
  return aliases.filter((alias) => String(alias.share_token || '').trim()).length
}

export function downloadTextFile(filename: string, content: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: 'text/plain;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

/**
 * 隐私邮箱的免登录邮件链接：打开就是这个地址的最新一封邮件正文。
 * 链接本身就是权限，复制给谁谁都能看，所以走的是后端那串随机 share_token。
 */
export function aliasMailUrl(shareToken: string): string {
  return shareToken ? `${window.location.origin}/m/${shareToken}` : ''
}

export function formatDateTime(value?: string | null): string {
  if (!value) return '-'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? '-' : parsed.toLocaleString()
}

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** 邮件列表里用相对时间，扫起来比一串完整时间戳快得多。 */
export function formatRelativeTime(value?: string | null): string {
  if (!value) return '-'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '-'
  const diff = Date.now() - parsed.getTime()
  if (diff < 0) return parsed.toLocaleDateString()
  if (diff < MINUTE) return '刚刚'
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)} 分钟前`
  if (diff < DAY) return `${Math.floor(diff / HOUR)} 小时前`
  if (diff < 7 * DAY) return `${Math.floor(diff / DAY)} 天前`
  return parsed.toLocaleDateString()
}

// --------------------------------------------------------------- 隐私邮箱筛选

/** 隐私邮箱列表的筛选条件。空值 = 不限。 */
export interface AliasFilter {
  /** 关键词，匹配地址 / 标签 / 备注 / 所属主号（大小写不敏感） */
  keyword?: string
  /** 号池状态。未选平台时按池口径匹配，选了平台按 `aliasPlatformStatus` 的平台口径。 */
  poolStatus?: string
  /** 启用状态：active / inactive */
  status?: string
  /** 平台（chatgpt / grok）。选了之后号池状态按「在这个平台有没有注册」解释。 */
  platform?: string
}

/**
 * 平台口径状态（选了平台筛选时用）。
 *
 * - `registered`：该平台已注册（`accounts` 表有账号，或池里记过这个平台）
 * - `available`：该平台没注册过、池里可领
 * - `in_use`：正在被某个任务领用
 * - `unpooled`：还没入池
 *
 * 规则与取号逻辑（`claim_alias` / `_pop_account`）一致：证据优先，池里的
 * `used_platforms` 记账作补充（任务中途崩掉会让记账缺项）。
 */
export type AliasPlatformStatus = 'unpooled' | 'available' | 'in_use' | 'registered'

/** `,chatgpt,grok,` → ['chatgpt','grok']（大小写归一）。 */
export function parsePlatformField(field?: string | null): string[] {
  return String(field || '')
    .split(',')
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean)
}

/** 这个地址在该平台是否已注册（记账 ∪ accounts 表证据）。 */
export function aliasRegisteredOn(
  alias: Pick<ICloudAlias, 'used_platforms' | 'registered_platforms'>,
  platform: string,
): boolean {
  const name = String(platform || '').trim().toLowerCase()
  if (!name) return false
  if (parsePlatformField(alias.used_platforms).includes(name)) return true
  return (alias.registered_platforms || []).some((item) => String(item).trim().toLowerCase() === name)
}

/** 该地址已注册过的全部平台（记账 ∪ 证据，去重排序）。 */
export function aliasRegisteredPlatforms(
  alias: Pick<ICloudAlias, 'used_platforms' | 'registered_platforms'>,
): string[] {
  const names = new Set(parsePlatformField(alias.used_platforms))
  for (const item of alias.registered_platforms || []) {
    const name = String(item || '').trim().toLowerCase()
    if (name) names.add(name)
  }
  return Array.from(names).sort()
}

/**
 * 按平台口径算一行别名的状态。`platform` 为空时原样返回池状态
 * （不筛选 = 显示有没有入池）。
 */
export function aliasPlatformStatus(
  alias: Pick<ICloudAlias, 'pool_status' | 'used_platforms' | 'registered_platforms'>,
  platform?: string,
): AliasPlatformStatus {
  const name = String(platform || '').trim().toLowerCase()
  if (!name) return alias.pool_status as AliasPlatformStatus
  if (aliasRegisteredOn(alias, name)) return 'registered'
  if (alias.pool_status === 'in_use') return 'in_use'
  if (alias.pool_status === 'unpooled') return 'unpooled'
  // available / used(只被别的平台用过) 对**本平台**都是可领的
  return 'available'
}

/**
 * 按条件筛选隐私邮箱。
 *
 * 抽成纯函数是为了能单测（Python 侧在 `tests/test_icloud_alias_filter.py`
 * 复刻同一套规则，两边行为必须一致）。
 *
 * 关键词覆盖四个字段：用户找某个别名时，记得的可能是邮箱本身，也可能是
 * 当初填的备注或它在哪个主号下 —— 只搜地址会让人以为"搜不到"。
 *
 * `platform` 给定后 `poolStatus` 按平台口径匹配（`aliasPlatformStatus`）。
 */
export function filterAliases<
  T extends Pick<
    ICloudAlias,
    'address' | 'label' | 'note' | 'account_email' | 'pool_status' | 'status' | 'used_platforms' | 'registered_platforms'
  >,
>(aliases: T[], filter: AliasFilter): T[] {
  const keyword = String(filter.keyword || '').trim().toLowerCase()
  return aliases.filter((alias) => {
    if (filter.poolStatus) {
      const status = filter.platform
        ? aliasPlatformStatus(alias, filter.platform)
        : alias.pool_status
      if (status !== filter.poolStatus) return false
    }
    if (filter.status && alias.status !== filter.status) return false
    if (!keyword) return true
    return [alias.address, alias.label, alias.note, alias.account_email]
      .some((field) => String(field || '').toLowerCase().includes(keyword))
  })
}

/** 是否设了任一筛选条件（用于「筛选出 N/M 个 + 清除」的显示判断）。 */
export function hasAliasFilter(filter: AliasFilter): boolean {
  return Boolean(
    String(filter.keyword || '').trim() || filter.poolStatus || filter.status || filter.platform,
  )
}

/** 平台口径的号池计数（选了平台筛选时顶部的统计条用）。 */
export function aliasPlatformSummary<T extends Parameters<typeof aliasPlatformStatus>[0]>(
  aliases: T[],
  platform?: string,
): Record<AliasPlatformStatus | 'total', number> {
  const summary: Record<AliasPlatformStatus | 'total', number> = {
    unpooled: 0,
    available: 0,
    in_use: 0,
    registered: 0,
    total: aliases.length,
  }
  for (const alias of aliases) {
    summary[aliasPlatformStatus(alias, platform)] += 1
  }
  return summary
}

/** 平台口径状态的展示映射（与池状态的 Tag 配色保持同一套观感）。 */
export const PLATFORM_STATUS_META: Record<AliasPlatformStatus, { color: string; text: string }> = {
  unpooled: { color: 'default', text: '未入池' },
  available: { color: 'green', text: '该平台未注册' },
  in_use: { color: 'processing', text: '使用中' },
  registered: { color: 'blue', text: '该平台已注册' },
}
