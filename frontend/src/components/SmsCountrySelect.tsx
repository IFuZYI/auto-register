import { Select } from 'antd'

import { parseCountryIdList, useSmsCountryOptions, withUnknownCountries } from '@/lib/smsCountries'

interface SmsCountrySelectProps {
  multiple?: boolean
  placeholder?: string
  /** 单选时是配置里存的国家 ID 字符串，多选时是 ID 数组 */
  value?: string | string[]
  onChange?: (next: string | string[]) => void
}

/** 接码国家下拉：显示中文名，存的还是平台那套数字 ID。 */
export function SmsCountrySelect({
  multiple = false,
  placeholder,
  value,
  onChange,
}: SmsCountrySelectProps) {
  const options = useSmsCountryOptions()
  const selected = parseCountryIdList(value)
  const merged = withUnknownCountries(options, selected).map((item) => ({
    value: item.value,
    label: item.openai_sms_whitelisted ? `${item.label} · OpenAI 纯短信` : item.label,
  }))

  if (multiple) {
    return (
      <Select
        mode="multiple"
        allowClear
        showSearch
        optionFilterProp="label"
        maxTagCount="responsive"
        placeholder={placeholder}
        options={merged}
        value={selected}
        onChange={(next) => onChange?.(next as string[])}
        style={{ width: '100%' }}
      />
    )
  }

  return (
    <Select
      allowClear
      showSearch
      optionFilterProp="label"
      placeholder={placeholder}
      options={merged}
      value={selected[0]}
      onChange={(next) => onChange?.(String(next ?? ''))}
      style={{ width: '100%' }}
    />
  )
}

export default SmsCountrySelect
