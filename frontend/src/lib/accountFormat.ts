// 从 Accounts.tsx 抽出的纯格式化函数（无副作用，可单测/复用）。

/**
 * 后端 `/api/accounts` 返回的账号行。
 *
 * 只声明本文件真正读到的字段 —— 其余字段靠 `...account` 原样透传，所以不需要
 * 在这里穷举（后端加字段时这里不必跟着改）。`extra_json` 是 JSON 字符串，
 * 由 `parseExtraJson` 解析。
 */
export interface AccountLike {
  id?: number
  email?: string
  extra_json?: string
  [key: string]: unknown
}

function parseExtraJson(raw: string | undefined) {
  if (!raw) return {}
  try {
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

export function normalizeAccount(account: AccountLike) {
  const extra = parseExtraJson(account.extra_json) as Record<string, unknown>
  const chatgptLocal = extra.chatgpt_local && typeof extra.chatgpt_local === 'object' ? extra.chatgpt_local : {}
  const plusCheck = extra.plus_check && typeof extra.plus_check === 'object' ? extra.plus_check : {}
  const totpSecret = String(extra.totp_secret || '')
  // 注册这个账号时用的出口代理。复用时优先回到它（见后端
  // `proxy_pool.resolve_for_account`），原代理不可用会被换成新的。
  const registerProxy = String(extra.register_proxy || '')
  // `extra.sync_statuses`（面板上传/同步状态）不在这里解析：面板状态在
  // 「面板管理」页看（那里有本地 ↔ 远端对比），账号页只管账号本身。
  return {
    ...account,
    extra,
    chatgptLocal,
    plusCheck,
    totpSecret,
    registerProxy,
  }
}

export function formatSyncTime(value?: string) {
  if (!value) return ''
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

export function formatCreatedAt(value?: string) {
  if (!value) return { date: '-', time: '' }
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return { date: value, time: '' }
  }
  return {
    date: date.toLocaleDateString(),
    time: date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
  }
}

export function authStateMeta(state?: string) {
  switch (state) {
    case 'access_token_valid':
      return { color: 'success', label: 'AT有效' }
    case 'account_deactivated':
      return { color: 'error', label: '已失效' }
    case 'access_token_invalidated':
      return { color: 'error', label: 'AT失效' }
    case 'unauthorized':
      return { color: 'error', label: '未授权' }
    case 'missing_access_token':
      return { color: 'default', label: '缺少AT' }
    case 'banned_like':
      return { color: 'error', label: '疑似封禁' }
    case 'probe_failed':
      return { color: 'warning', label: '探测失败' }
    default:
      return { color: 'default', label: '未探测' }
  }
}

export function codexStateMeta(state?: string) {
  switch (state) {
    case 'usable':
      return { color: 'success', label: '可用' }
    case 'account_deactivated':
      return { color: 'error', label: '已失效' }
    case 'access_token_invalidated':
      return { color: 'error', label: 'AT失效' }
    case 'unauthorized':
      return { color: 'error', label: '未授权' }
    case 'payment_required':
      return { color: 'warning', label: '需付费/权限' }
    case 'quota_exhausted':
      return { color: 'warning', label: '额度耗尽' }
    case 'skipped_auth_invalid':
      return { color: 'default', label: '未测' }
    case 'probe_failed':
      return { color: 'warning', label: '探测失败' }
    default:
      return { color: 'default', label: '未探测' }
  }
}

export function plusTrialMeta(status?: string) {
  switch ((status || '').toLowerCase()) {
    case 'trial_eligible':
      return { color: 'success', label: '可领首月免费' }
    case 'plus_active':
      return { color: 'processing', label: 'Plus 生效中' }
    case 'free':
      return { color: 'default', label: 'Free' }
    case 'banned':
      return { color: 'error', label: '封号' }
    case 'token_invalid':
      return { color: 'warning', label: '凭证失效' }
    default:
      return { color: 'default', label: '未检测' }
  }
}

export function planMeta(plan?: string) {
  switch ((plan || '').toLowerCase()) {
    case 'plus':
      return { color: 'success', label: 'Plus' }
    case 'team':
      return { color: 'processing', label: 'Team' }
    case 'enterprise':
      return { color: 'processing', label: 'Enterprise' }
    case 'pro':
      return { color: 'processing', label: 'Pro' }
    case 'free':
      return { color: 'default', label: 'Free' }
    default:
      return { color: 'default', label: '未知' }
  }
}

export function formatStructuredText(value?: string) {
  if (!value) return ''
  const trimmed = String(value).trim()
  if (!trimmed) return ''
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      return JSON.stringify(JSON.parse(trimmed), null, 2)
    } catch {
      return trimmed
    }
  }
  return trimmed
}

// ── 账号状态（用户要求 2026-10-06：正常 / 过期 / 失效 / 禁用）──

/**
 * 账号状态 → 展示元数据（标签 + 颜色）。
 *
 * 语义（与后端 `services/chatgpt_account_state.py` 同口径）：
 * - `registered` 正常：正常能使用的账号；
 * - `expired` 过期：AT 已过期（刷新可能救回）；
 * - `invalid` 失效：需要重新登录的（凭证被拒），走流程登录；
 * - `banned` 禁用：被封了的账号。
 *
 * 此前账号列表直接渲染英文原值（`<Tag>{status}</Tag>` → "registered"），
 * 且旧文案是「已注册 / 已过期 / 已失效 / 已封禁」—— 统一收敛到这里，
 * Accounts 与 Dashboard 共用一处映射。
 */
export function accountStatusMeta(status?: string): { label: string; color: string } {
  switch (String(status || '').trim().toLowerCase()) {
    case 'registered':
      return { label: '正常', color: 'success' }
    case 'expired':
      return { label: '过期', color: 'warning' }
    case 'invalid':
      return { label: '失效', color: 'error' }
    case 'banned':
      return { label: '禁用', color: 'default' }
    default:
      // 历史库可能带任意字符串 —— 原样显示比吞掉强（能看到真实值）
      return { label: String(status || '未知'), color: 'default' }
  }
}

// ── AT 生命周期（与后端 `services/chatgpt_token_lifecycle.py` 同口径）──

/** 到期前多久算「即将过期」：24 小时（与后端 / 参考实现一致）。 */
export const AT_EXPIRING_SKEW_SECONDS = 24 * 60 * 60

export interface AtLifecycleMeta {
  status: 'valid' | 'expiring' | 'invalid' | 'unknown'
  label: string
  color: string
  /** 生成时间（浏览器本地时区，无值时空串） */
  issuedText: string
  /** 到期时间（浏览器本地时区，无值时空串） */
  expiresText: string
  /** 剩余时间的人话（「3 天 4 小时」/「已过期」/空串） */
  remainingText: string
}

function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const parts = String(token || '').split('.')
    if (parts.length < 2) return {}
    const payload = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = payload + '='.repeat((4 - (payload.length % 4)) % 4)
    const json = atob(padded)
    const claims = JSON.parse(json)
    return claims && typeof claims === 'object' ? claims : {}
  } catch {
    return {}
  }
}

function formatRemaining(seconds: number): string {
  if (seconds <= 0) return '已过期'
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days > 0) return `${days} 天 ${hours} 小时`
  if (hours > 0) return `${hours} 小时 ${minutes} 分`
  return `${minutes} 分`
}

/**
 * AT → 展示用元数据（状态 / 标签 / 颜色 / 生成与到期时间 / 剩余时间）。
 *
 * 与后端同口径：`valid` / `expiring`（剩余 ≤ 24h）/ `invalid`（已过期）/
 * `unknown`（没有 exp claim，不误判成失效）。空 token 单独标「无 AT」。
 */
export function atLifecycleMeta(accessToken?: string): AtLifecycleMeta {
  const token = String(accessToken || '').trim()
  if (!token) {
    return {
      status: 'invalid', label: '无 AT', color: 'default',
      issuedText: '', expiresText: '', remainingText: '',
    }
  }
  const claims = decodeJwtPayload(token)
  const issuedAt = Number(claims.iat) > 0 ? Number(claims.iat) : null
  const expiresAt = Number(claims.exp) > 0 ? Number(claims.exp) : null
  if (issuedAt === null && expiresAt === null) {
    return {
      status: 'unknown', label: '无法解析', color: 'default',
      issuedText: '', expiresText: '', remainingText: '',
    }
  }
  const now = Math.floor(Date.now() / 1000)
  const remaining = expiresAt !== null ? expiresAt - now : null
  let status: AtLifecycleMeta['status'] = 'unknown'
  let label = '未知'
  let color = 'default'
  if (remaining !== null && remaining <= 0) {
    status = 'invalid'
    label = '已过期'
    color = 'error'
  } else if (remaining !== null && remaining <= AT_EXPIRING_SKEW_SECONDS) {
    status = 'expiring'
    label = '即将过期'
    color = 'warning'
  } else if (remaining !== null) {
    status = 'valid'
    label = '有效'
    color = 'success'
  }
  return {
    status,
    label,
    color,
    issuedText: issuedAt !== null ? new Date(issuedAt * 1000).toLocaleString() : '',
    expiresText: expiresAt !== null ? new Date(expiresAt * 1000).toLocaleString() : '',
    remainingText: remaining !== null ? formatRemaining(remaining) : '',
  }
}

/** 列表行 AT 摘要（「AT 有效期」列用）。 */
export interface AtListSummary {
  /** 状态标签（有效 / 即将过期 / 已过期 / 无法解析 / 无 AT） */
  label: string
  color: string
  /** 短到期日（`2026-10-12`），无到期信息时为空串 */
  expiresShort: string
  /** 剩余时间人话（「7 天 8 小时」），无到期信息时为空串 */
  remainingText: string
  /** hover 全量信息（生成于 / 到期 / 剩余），供 Tooltip 展示 */
  tooltip: string
}

/**
 * 列表行 AT 摘要 —— 参考实现（chatgpt2api）在列表行内直接显示凭据状态，
 * 我们的列表此前只在详情弹窗里有 AT 生成/到期（用户反馈「没见到」）。
 * 与 `atLifecycleMeta` 同口径，输出列表用的紧凑形状。
 */
export function atListSummary(accessToken?: string): AtListSummary {
  const meta = atLifecycleMeta(accessToken)
  // 短日期用 epoch 确定性构造（不解析 locale 字符串 —— 不同 locale 下
  // toLocaleString 的格式不一样，解析会得出不稳定结果）。
  const claims = decodeJwtPayload(String(accessToken || '').trim())
  const expiresAt = Number(claims.exp) > 0 ? Number(claims.exp) : null
  let expiresShort = ''
  if (expiresAt !== null) {
    const d = new Date(expiresAt * 1000)
    const pad = (n: number) => String(n).padStart(2, '0')
    expiresShort = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
  }
  const parts: string[] = []
  if (meta.issuedText) parts.push(`生成于 ${meta.issuedText}`)
  if (meta.expiresText) parts.push(`到期 ${meta.expiresText}`)
  if (meta.remainingText) parts.push(meta.remainingText)
  return {
    label: meta.label,
    color: meta.color,
    expiresShort,
    remainingText: meta.remainingText,
    tooltip: parts.join(' · '),
  }
}
