/**
 * 「邮箱导入」下面还分三个视图：Outlook / Hotmail / MailAPI URL 走同一张微软号池表。
 * 视图选择存在配置项 `mail_import_source` 里，`mail_provider` 只记到号池粒度，
 * 所以不能拿它反推视图——那样选什么都会退回 Outlook。取号时后端还要按视图筛
 * account_type，所以提交注册任务必须把它一起带上。
 *
 * 历史：曾有第四个视图 `applemail`（小苹果 / AppleMail，appleemail.top）——
 * 它不是 iCloud 邮箱，只是名字带 Apple 的第三方临时邮箱服务，已弃用删除。
 * 旧配置里若存过它，`normalizeMailImportSource` 兜底收敛到 Outlook。
 */
import { Form } from 'antd'
import type { FormInstance } from 'antd'

export type MailImportSource = 'outlook' | 'hotmail' | 'mailapi'

export const MAIL_IMPORT_SOURCES: MailImportSource[] = ['outlook', 'hotmail', 'mailapi']

export const MAIL_IMPORT_SOURCE_OPTIONS: { value: MailImportSource; label: string }[] = [
  { value: 'outlook', label: 'Outlook（微软号池）' },
  { value: 'hotmail', label: 'Hotmail（微软号池）' },
  { value: 'mailapi', label: 'MailAPI URL（邮箱----mailapi_url）' },
]

/** 后端把这些 mail_provider 值显示成「邮箱导入」 */
export const MAIL_IMPORT_PROVIDERS = ['microsoft', 'outlook']

/** 旧库里存过的值 → 现有视图。microsoft 是旧的号池粒度名字。 */
const LEGACY_SOURCE_ALIASES: Record<string, MailImportSource> = {
  microsoft: 'outlook',
}

export function isMailImportProvider(mailProvider: string): boolean {
  const value = String(mailProvider || '').trim().toLowerCase()
  // 这里不再认 applemail：那个视图（以及它背后的第三方临时邮箱渠道）已删除，
  // 后端 `get_all()` 会把老库里的 applemail 收敛成空串，前端根本收不到这个值。
  // 留着它只会让「已删除的渠道」看起来还活着。
  return MAIL_IMPORT_PROVIDERS.includes(value)
}

export function normalizeMailImportSource(value: unknown, mailProvider: unknown = ''): MailImportSource {
  const raw = String(value ?? '').trim().toLowerCase()
  const aliased = LEGACY_SOURCE_ALIASES[raw] ?? raw
  if ((MAIL_IMPORT_SOURCES as string[]).includes(aliased)) {
    return aliased as MailImportSource
  }
  // mailProvider 参数保留是为了兼容旧调用签名（历史上用它把 applemail 反推成视图）
  void mailProvider
  return 'outlook'
}

/**
 * 读设置页表单里的 `mail_import_source`，空串表示配置还没回来。
 *
 * 设置页没有这个字段的 Form.Item——视图选择器画在「邮箱导入」卡片头上，值只由
 * setFieldsValue 写进 store。而 useWatch 默认盯的是注册过的字段（rc-field-form 里
 * 是 getFieldsValue()），不带 preserve 就永远读到 undefined：库里明明存着 mailapi，
 * 界面照样退回 Outlook。
 */
export function useStoredMailImportSource(form: FormInstance): string {
  const watched = Form.useWatch('mail_import_source', { form, preserve: true })
  return String(watched ?? '').trim().toLowerCase()
}

export function resolveEffectiveMailProvider(mailProvider: string, mailImportSource: unknown): string {
  if (mailProvider !== 'mail_import') return mailProvider
  void mailImportSource
  // 「邮箱导入」在下拉里是一个 UI 值，后端真正认识的是 microsoft。
  // applemail 视图已弃用，所有视图现在都落在微软号池上。
  return 'microsoft'
}
