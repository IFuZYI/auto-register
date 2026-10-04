import { useEffect, useState } from 'react'

import { apiFetch } from '@/lib/utils'

export interface SmsCountryOption {
  value: string
  label: string
  name: string
  openai_sms_whitelisted?: boolean
}

/** 接码平台的国家 ID 存的是数字，配置里也只存数字，UI 上才换成中文名。 */
export function parseCountryIdList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item).trim()).filter(Boolean)
  }
  return String(value ?? '')
    .split(/[,，;；\s]+/)
    .map((item) => item.trim())
    .filter(Boolean)
}

export function formatCountryIdList(value: unknown): string {
  return parseCountryIdList(value).join(',')
}

/**
 * 拉一次国家清单给下拉用。
 *
 * 表里有 190 多个国家，泰国排在最前 —— OpenAI 只对它走纯短信，其余国家多半
 * 会被要求 WhatsApp 验证，把它埋在字母序中间等于让人踩坑。
 */
export function useSmsCountryOptions(enabled = true): SmsCountryOption[] {
  const [options, setOptions] = useState<SmsCountryOption[]>([])

  useEffect(() => {
    if (!enabled || options.length > 0) return
    let cancelled = false

    apiFetch('/sms/country-options')
      .then((data: { items?: SmsCountryOption[] }) => {
        if (cancelled) return
        const items = [...(data.items || [])]
        items.sort((a, b) => {
          if (Boolean(a.openai_sms_whitelisted) !== Boolean(b.openai_sms_whitelisted)) {
            return a.openai_sms_whitelisted ? -1 : 1
          }
          return a.name.localeCompare(b.name, 'zh-Hans-CN')
        })
        setOptions(items)
      })
      .catch(() => {
        // 拉不到就退回手填数字，不值得为此弹错
      })

    return () => {
      cancelled = true
    }
  }, [enabled, options.length])

  return options
}

/** 已存的值不在清单里时（平台改了编号之类），补一个占位项，免得下拉把它吞掉。 */
export function withUnknownCountries(
  options: SmsCountryOption[],
  selected: string[],
): SmsCountryOption[] {
  const known = new Set(options.map((item) => item.value))
  const extras = selected
    .filter((value) => value && !known.has(value))
    .map((value) => ({ value, name: value, label: `未知国家 (${value})` }))
  return extras.length > 0 ? [...extras, ...options] : options
}
