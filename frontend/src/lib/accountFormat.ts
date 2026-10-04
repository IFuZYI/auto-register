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
