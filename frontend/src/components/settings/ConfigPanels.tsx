import { useState } from 'react'
import { Card, Form, Input, Select, Switch } from 'antd'
import { EyeOutlined, EyeInvisibleOutlined } from '@ant-design/icons'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import SmsCountrySelect from '@/components/SmsCountrySelect'
import { SELECT_FIELDS } from '@/lib/configFields'

/**
 * 配置项字段的通用渲染器。
 *
 * 原本长在 Settings.tsx 里；「面板配置」页要复用同一套字段渲染，
 * 所以抽出来 —— 否则两页各写一份，下拉选项和密文显隐迟早走样。
 */

export interface FieldConfig {
  key: string
  label: string
  placeholder?: string
  type?: 'select' | 'input' | 'boolean' | 'country' | 'country-multi'
  secret?: boolean
  /**
   * 控件宽度档位。不填时按 key 猜（见 inferWidthClass）——
   * 配置项有 100 多个，逐个标注既啰嗦又容易漏。
   */
  width?: 'sm' | 'md' | 'lg' | 'prose'
}

export interface SectionConfig {
  title: string
  desc?: string
  fields: FieldConfig[]
}

/**
 * 按配置键猜控件宽度档位。
 *
 * 之前所有配置框都跟随容器拉满（实测「检查间隔」这种填 30 的字段有 924px），
 * 一个一位数占满整行既浪费空间也不易扫读。这里按语义兜底：
 * 数字/开关 → 窄，URL/密钥/路径 → 中，其余留全宽。
 */
const NARROW_KEY_HINTS = ['timeout', 'interval', 'count', 'retry', 'port', 'level', 'limit', 'delay', 'batch', 'concurrency', 'times', 'size', 'days', 'hours']
const WIDE_KEY_HINTS = ['url', 'key', 'token', 'secret', 'path', 'dir', 'endpoint', 'proxy', 'host', 'base_url', 'password', 'account', 'email']

function inferWidthClass(field: FieldConfig): string {
  if (field.width) return `form-field--${field.width}`
  const key = field.key.toLowerCase()
  // 开关只有一个 44px 的控件，不该继承容器全宽 ——
  // 之前它没有宽度类，实测「启用自动上传」占 924px，标签与开关之间空一大片
  if (field.type === 'boolean') return 'form-field--sm'
  if (NARROW_KEY_HINTS.some((h) => key.includes(h))) return 'form-field--sm'
  if (WIDE_KEY_HINTS.some((h) => key.includes(h))) return 'form-field--lg'
  if (field.type === 'select' || field.type === 'country' || field.type === 'country-multi') {
    return 'form-field--md'
  }
  return 'form-field--lg'
}

const FIELD_HELP_TEXT: Record<string, string> = {}

export function ConfigField({
  field,
  secretSet = false,
}: {
  field: FieldConfig
  /** 服务端下发的 `<key>_set` 标记：口令已存过。由上层从 config 传入 —— */
  /*  `_set` 没有对应的 Form.Item，`useWatch` 观察不到未注册字段（实测）。 */
  secretSet?: boolean
}) {
  const [showSecret, setShowSecret] = useState(false)
  const options = SELECT_FIELDS[field.key]
  const isBooleanField = field.type === 'boolean'
  const isCountryField = field.type === 'country' || field.type === 'country-multi'

  return (
    <Form.Item
      label={field.label}
      name={field.key}
      extra={
        field.secret && secretSet
          ? '已配置（出于安全不回显）；留空 = 不修改'
          : FIELD_HELP_TEXT[field.key]
      }
      className={inferWidthClass(field)}
      valuePropName={isBooleanField ? 'checked' : undefined}
    >
      {isCountryField ? (
        <SmsCountrySelect
          multiple={field.type === 'country-multi'}
          placeholder={field.placeholder}
        />
      ) : options ? (
        <Select options={options} style={{ width: '100%' }} />
      ) : isBooleanField ? (
        <Switch checkedChildren="开启" unCheckedChildren="关闭" />
      ) : field.secret ? (
        // `visible` 是「是否明文显示」：默认必须为 false（打码），
        // 写成 !showSecret 会让所有密钥一进页面就是明文。
        <Input.Password
          placeholder={field.placeholder}
          visibilityToggle={{
            visible: showSecret,
            onVisibleChange: setShowSecret,
          }}
          iconRender={(visible) => (visible ? <EyeOutlined /> : <EyeInvisibleOutlined />)}
        />
      ) : (
        <Input placeholder={field.placeholder} />
      )}
    </Form.Item>
  )
}

export function ConfigSection({
  section,
  secretSetKeys,
}: {
  section: SectionConfig
  /**
   * 已设置口令的键集合（来自 `GET /api/config` 的 `<key>_set`）。
   *
   * 由页面传入而不是在字段里 `useWatch`：`_set` 没有对应的 Form.Item，
   * antd 的 `useWatch`/`getFieldsValue()` 都看不到未注册字段（实测），
   * 只有页面自己拿着接口返回值才知道谁已配置。
   */
  secretSetKeys?: ReadonlySet<string>
}) {
  return (
    <Card title={section.title} style={{ marginBottom: 'var(--section-gap)' }}>
      {/* 说明放在正文顶部而不是卡片头部右侧：
          长句挤在标题旁边会被压成一两行小字，窄屏更是一团。 */}
      {section.desc && (
        <p
          style={{
            margin: '0 0 20px',
            fontSize: 13,
            lineHeight: 1.6,
            color: 'var(--text-muted)',
            maxWidth: 'var(--w-prose)',
          }}
        >
          {section.desc}
        </p>
      )}
      {section.fields.map((field) => (
        <ConfigField
          key={field.key}
          field={field}
          secretSet={Boolean(secretSetKeys?.has(field.key))}
        />
      ))}
    </Card>
  )
}

/**
 * 把配置里的开关值归一成布尔。
 * 库里存的是 `"0"` / `"1"` 字符串，历史数据里也混有 `"true"` / `""`，
 * 空值按 `fallbackEnabled` 处理（例如已填了 URL 和 Key 就默认开启）。
 */
export function resolveFeatureEnabledConfig(value: unknown, fallbackEnabled: boolean): boolean {
  const normalized = String(value ?? '').trim()
  if (!normalized) return fallbackEnabled
  return parseBooleanConfigValue(normalized)
}
