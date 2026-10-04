import { apiFetch } from '@/lib/utils'

export type ICloudRegion = 'global' | 'china'

export interface ICloudCredentialState {
  has_session_cookies?: boolean
  has_dsid?: boolean
  has_hme_service_url?: boolean
  has_mail_gateway?: boolean
  has_web_auth_token?: boolean
  has_imap_credentials?: boolean
  credentials_unreadable?: boolean
}

export interface ICloudQuota {
  limit: number
  used: number
  remaining: number
  reset_at: string | null
}

export interface ICloudAccount {
  id: number
  email: string
  display_name: string
  region: ICloudRegion
  status: string
  enabled: boolean
  alias_count: number
  sync_error: string
  last_sync_at: string | null
  created_at: string | null
  credential_state: ICloudCredentialState
  quota: ICloudQuota
}

export interface ICloudAlias {
  id: number
  account_id: number
  account_email: string
  address: string
  label: string
  note: string
  status: string
  /** 号池状态：unpooled（未入池）/ available（未使用）/ in_use（使用中）/ used（已使用） */
  pool_status: 'unpooled' | 'available' | 'in_use' | 'used'
  /**
   * 这个别名被**哪些平台**消耗过（`,grok,chatgpt,` 形式，空串 = 没有）。
   *
   * 邮箱只对用掉它的那个平台一次性 —— 同一个地址注册过 ChatGPT 之后
   * 还能注册 Grok，所以 `used` 不等于「对谁都不可用」：界面用它说明
   * 「为什么这个 used 号还能被某个平台领到」。
   */
  used_platforms: string
  /**
   * `accounts` 表里的权威注册证据（跨库查出来，已排序）。
   *
   * 与 `used_platforms` 的区别：那是池子自己的记账，任务中途崩掉会缺项；
   * 这个是「真的注册过」的证据。界面按它显示「已注册平台」并做平台筛选。
   */
  registered_platforms: string[]
  provider_id: string
  /** 免登录查看最新邮件的凭证，为空说明是后端补 token 之前的旧数据 */
  share_token: string
  created_at: string | null
}

export interface ICloudTrustedPhone {
  id: number
  number: string
  push_mode: string
}

/** 验证码投递方式：受信任设备推送、短信，或需要先选择手机号。 */
export type ICloudLoginDelivery = 'trusted_devices' | 'sms' | 'sms_selection_required' | ''

export interface ICloudLoginState {
  login_id: string
  status: 'verification_required' | 'completed'
  expires_at: number
  delivery: ICloudLoginDelivery
  trusted_phone_numbers: ICloudTrustedPhone[]
  email: string
  display_name: string
  region: ICloudRegion
  account?: ICloudAccount
}

export interface ICloudMailAddress {
  email: string
  name: string
}

export interface ICloudMessage {
  id: string
  mailbox: string
  subject: string
  snippet: string
  text_body: string
  html_body: string
  from: ICloudMailAddress
  to: ICloudMailAddress[]
  received_at: string
  alias_address: string
  is_read: boolean
  has_attachments: boolean
}

export interface ICloudLoginPayload {
  email: string
  password: string
  display_name?: string
  region?: ICloudRegion
  imap_host?: string
  imap_port?: number
  imap_username?: string
  imap_password?: string
}

export interface ICloudCookieImportPayload {
  email?: string
  display_name?: string
  region?: ICloudRegion
  cookie_header?: string
  cookies_json?: unknown
  imap_host?: string
  imap_port?: number
  imap_username?: string
  imap_password?: string
}

function post<T>(path: string, body?: unknown): Promise<T> {
  return apiFetch(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
  })
}

export async function listICloudAccounts(): Promise<ICloudAccount[]> {
  const data = await apiFetch('/icloud/accounts')
  return data?.items ?? []
}

export function startICloudLogin(payload: ICloudLoginPayload): Promise<ICloudLoginState> {
  return post('/icloud/login-sessions', payload)
}

export function verifyICloudLogin(loginId: string, code: string): Promise<ICloudLoginState> {
  return post(`/icloud/login-sessions/${loginId}/verify`, { code })
}

export function resendICloudLoginCode(loginId: string): Promise<ICloudLoginState> {
  return post(`/icloud/login-sessions/${loginId}/resend`)
}

export function sendICloudLoginSMS(
  loginId: string,
  phoneId: number,
  mode = '',
): Promise<ICloudLoginState> {
  return post(`/icloud/login-sessions/${loginId}/sms`, { phone_id: phoneId, mode })
}

export function cancelICloudLogin(loginId: string): Promise<{ ok: boolean }> {
  return apiFetch(`/icloud/login-sessions/${loginId}`, { method: 'DELETE' })
}

export function importICloudCookie(payload: ICloudCookieImportPayload): Promise<ICloudAccount> {
  return post('/icloud/accounts/import-cookie', payload)
}

export function setICloudAccountEnabled(id: number, enabled: boolean): Promise<ICloudAccount> {
  return apiFetch(`/icloud/accounts/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ enabled }),
  })
}

export function deleteICloudAccount(id: number): Promise<{ ok: boolean }> {
  return apiFetch(`/icloud/accounts/${id}`, { method: 'DELETE' })
}

export function syncICloudAccount(id: number): Promise<{
  fetched: number
  created: number
  updated: number
}> {
  return post(`/icloud/accounts/${id}/sync`)
}

export interface ICloudAliasPool {
  /** 生成/同步下来但还没被勾选「导入邮箱池」的地址；注册取号会跳过它们。 */
  unpooled: number
  available: number
  in_use: number
  used: number
  total: number
}

/** 空号池（接口没回 pool 时的兜底，也是页面 useState 的初值）。
 *
 * 单点定义：类型以后加字段（比如 failed）时，兜底与初值一起变，
 * 不会漏掉一处而渲染出 undefined 计数。 */
export const EMPTY_ALIAS_POOL: ICloudAliasPool = {
  unpooled: 0,
  available: 0,
  in_use: 0,
  used: 0,
  total: 0,
}

/** 批量入池 / 出池的结果。skipped 是「不在预期状态、因此没动」的那些。 */
export interface ICloudAliasPoolChangeResult {
  changed: number[]
  skipped: number[]
  pool_status: ICloudAlias['pool_status']
  pool: ICloudAliasPool
}

export async function listICloudAliases(
  accountId?: number,
): Promise<{ items: ICloudAlias[]; pool: ICloudAliasPool }> {
  const query = accountId ? `?account_id=${accountId}` : ''
  const data = await apiFetch(`/icloud/aliases${query}`)
  return {
    items: data?.items ?? [],
    pool: data?.pool ?? EMPTY_ALIAS_POOL,
  }
}

export function setICloudAliasPoolStatus(
  id: number,
  poolStatus: ICloudAlias['pool_status'],
): Promise<ICloudAlias> {
  return post(`/icloud/aliases/${id}/pool-status`, { pool_status: poolStatus })
}

/** 把选中的隐私邮箱导入号池（未入池 → 未使用），注册取号才会领到它们。 */
export function importICloudAliasesToPool(
  ids: number[],
): Promise<ICloudAliasPoolChangeResult> {
  return post<ICloudAliasPoolChangeResult>('/icloud/aliases/import-to-pool', { ids })
}

/** 把选中的隐私邮箱移出号池（未使用 → 未入池）。导入的反向操作。 */
export function unpoolICloudAliases(ids: number[]): Promise<ICloudAliasPoolChangeResult> {
  return post<ICloudAliasPoolChangeResult>('/icloud/aliases/unpool', { ids })
}

export async function generateICloudAliases(payload: {
  account_id: number
  label?: string
  note?: string
  count?: number
}): Promise<ICloudAlias[]> {
  const data = await post<{ items: ICloudAlias[] }>('/icloud/aliases', payload)
  return data?.items ?? []
}

export function deleteICloudAlias(id: number, remote = true): Promise<{ ok: boolean }> {
  return apiFetch(`/icloud/aliases/${id}?remote=${remote}`, { method: 'DELETE' })
}

/**
 * 停用 / 重新激活隐私邮箱。与删除的区别：停用是可逆的，地址仍保留在 Apple 那边，
 * 只是不再转发邮件；删除不可逆。
 */
export function setICloudAliasActive(id: number, active: boolean): Promise<ICloudAlias> {
  return post<ICloudAlias>(`/icloud/aliases/${id}/${active ? 'reactivate' : 'deactivate'}`, {})
}

export interface ICloudBatchDeleteResult {
  ok: boolean
  deleted: number[]
  failed: { alias_id: number; code: string; message: string }[]
}

export function batchDeleteICloudAliases(
  ids: number[],
  remote = true,
): Promise<ICloudBatchDeleteResult> {
  return post<ICloudBatchDeleteResult>('/icloud/aliases/batch-delete', { ids, remote })
}

/**
 * limit 是主号收件箱的倒序扫描窗口，不是"返回几封"——主号收件箱里混着所有
 * 隐私邮箱的信，窗口开太窄会把这个地址的最新一封漏在窗口外，所以取服务端默认的 50。
 */
export async function listICloudAliasMessages(
  aliasId: number,
  limit = 50,
): Promise<ICloudMessage[]> {
  const data = await apiFetch(`/icloud/aliases/${aliasId}/messages?limit=${limit}`)
  return data?.items ?? []
}
