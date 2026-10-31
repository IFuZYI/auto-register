import { useState } from 'react'
import { Form, Input, Select, Switch } from 'antd'
import { EyeOutlined, EyeInvisibleOutlined } from '@ant-design/icons'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import SmsCountrySelect from '@/components/SmsCountrySelect'
import { SELECT_FIELDS } from '@/lib/configFields'
import { SectionHead, SettingRow } from '@/components/settings/SettingsPrimitives'

/**
 * 配置项字段的通用渲染器。
 *
 * 原本长在 Settings.tsx 里；「面板配置」页要复用同一套字段渲染，
 * 所以抽出来 —— 否则两页各写一份，下拉选项和密文显隐迟早走样。
 *
 * 排版取向（改造后）：**一行一个字段**，标签在左、控件在右，说明文字
 * 进 ⓘ 气泡。以前是「节标题 + 一大段常驻说明 + 竖排字段」，100 多个配置项
 * 铺开时解释文字占的版面比控件还多，改一个并发数要在三屏说明里找。
 */

export interface FieldConfig {
  key: string
  label: string
  placeholder?: string
  type?: 'select' | 'input' | 'boolean' | 'country' | 'country-multi'
  secret?: boolean
  /**
   * 说明文字：**只出现在 ⓘ 气泡里**，不占版面。
   *
   * 以前说明只写在节一级（`desc` 常驻正文），单个字段没有说明 —— 于是
   * 「这个开关到底影响什么」只能堆在节说明里，或者靠命名去猜。
   * 现在每个字段都可以有自己的说明，悬停/聚焦才展开。
   */
  hint?: string
  /**
   * 下拉候选值。不填时走 `SELECT_FIELDS`（`lib/configFields.ts` 的共享表）。
   *
   * 有些键只在这个页面出现（例如面板卡片里的 `chatgpt2api_import_mode`），
   * 塞进共享表会让「哪些键是全站选项」变得含混 —— 所以允许就地声明。
   */
  options?: { value: string; label: string }[]
  /**
   * 控件宽度档位。不填时按类型给（见 widthClass）——
   * 行式布局里除开关外一律同宽，右边缘才对得齐。
   */
  width?: 'sm' | 'md' | 'lg' | 'prose'
}

export interface SectionConfig {
  title: string
  /** 节说明：进节头的 ⓘ，不占正文版面。 */
  desc?: string
  fields: FieldConfig[]
}

/**
 * 控件宽度档位。
 *
 * 改造前这里按 key 猜宽度（timeout → 200px、url → 360px、其余 → 480px）。
 * 那是**竖排表单**时代的规则：字段各占一行、没有标签列，宽窄不一还算有层次。
 *
 * 换成行式布局后恰恰相反 —— 标签列已经固定，控件列的右边缘成为一条视觉轴，
 * 每个框宽一点点就是一道锯齿（实测同一页里下拉 325px / 密钥 430px /
 * 服务代码 480px，右下角参差）。所以现在**除开关外一律同宽**：
 * 开关没有边框，宽度由自身决定，占满 420px 只会把「开/关」推到大老远。
 */
function widthClass(field: FieldConfig): string {
  if (field.width) return `form-field--${field.width}`
  if (field.type === 'boolean') return 'form-field--auto'
  return 'form-field--set'
}

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
  const options = field.options ?? SELECT_FIELDS[field.key]
  const isBooleanField = field.type === 'boolean'
  const isCountryField = field.type === 'country' || field.type === 'country-multi'

  // 口令已配置时，气泡里补一句「留空 = 不修改」—— 这是最容易踩的坑：
  // 界面不回显明文，用户以为要重新粘一遍，空着保存反而把口令清了。
  const hint = field.secret && secretSet
    ? `已配置（出于安全不回显）。留空 = 不修改。${field.hint ? ' ' + field.hint : ''}`
    : field.hint

  return (
    <SettingRow label={field.label} hint={hint}>
      <Form.Item
        name={field.key}
        className={widthClass(field)}
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
    </SettingRow>
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
    <section className="set-section">
      <SectionHead title={section.title} hint={section.desc} />
      {section.fields.map((field) => (
        <ConfigField
          key={field.key}
          field={field}
          secretSet={Boolean(secretSetKeys?.has(field.key))}
        />
      ))}
    </section>
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
