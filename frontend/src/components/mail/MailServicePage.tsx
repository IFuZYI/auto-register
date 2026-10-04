/**
 * 邮箱服务二级页面的公共骨架。
 *
 * 三个二级页面（Outlook 本地 / 临时邮箱 / 远程 icloud-hme）的结构是一样的：
 * 加载 /config → 渲染若干个 ConfigSection → 保存。差异只在「渲染哪些
 * section、要不要额外的导入面板」。所以把加载与保存收在这里，页面只负责
 * 声明自己的内容。
 *
 * 不在其列的两个：
 * - 「默认邮箱服务」曾是一个独立二级页面，现已并回「全局配置 > 邮箱」——
 *   它是全局默认值，不该跟各渠道的连接参数并列。
 * - 「iCloud 本地」直接渲染 iCloud 控制台（主号 / 别名管理），不加载也不
 *   保存 config，所以不走这个骨架。
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { App, Button, Form, Space, Typography } from 'antd'
import type { FormInstance } from 'antd'
import { ReloadOutlined, SaveOutlined } from '@ant-design/icons'
import { ConfigSection, type SectionConfig } from '@/components/settings/ConfigPanels'
import { PageHeader } from '@/components/PageHeader'
import { apiFetch } from '@/lib/utils'
import { parseCountryIdList } from '@/lib/smsCountries'
import {
  dropEmptySecrets,
  secretSetKeysFromConfig,
  stripSecretSetFlags,
} from '@/lib/secretConfig'

/**
 * 本页可能出现的口令字段（与服务端 `api/config.py` 的 `SECRET_CONFIG_KEYS` 对应）。
 * 邮箱渠道的 API Key / 密码类字段都收在这里，留空 = 不修改。
 */
const SECRET_KEYS = [
  'sms_api_key',
  'yescaptcha_key',
  'twocaptcha_key',
  'contribution_key',
  'custom_contribution_token',
] as const

/**
 * `MailImportPanel` 会读写的配置键。
 *
 * 面板在 `children` 里渲染、不在 `sections` 里，但它确实要读表单里的这两个
 * 字段（当前 provider 与邮箱导入视图）。不登记的话加载时不会灌进表单，
 * 面板就会拿着空值去请求快照。
 */
const IMPORT_PANEL_KEYS = [
  'mail_provider',
  'mail_import_source',
] as const

interface MailServicePageProps {
  title: string
  subtitle: string
  /** 要渲染的配置区块 */
  sections: SectionConfig[]
  /**
   * 缺省值：配置里没有这个键时表单显示什么。
   *
   * 只是显示层的兜底（后端另有自己的默认值）。以前这些兜底写在「全局配置」页的
   * 加载逻辑里；邮箱 tab 拆成一级菜单后跟着各自的字段搬到对应页面，否则用户会
   * 看到空输入框，以为配置丢了。
   */
  defaults?: Record<string, unknown>
  /**
   * 区块之后的自定义内容（导入面板等）。
   *
   * 函数形式能拿到页面共用的 `form` 与已加载的 `config`。`config` 是必需的：
   * 有些判断要看「本页没有的字段」——比如临时邮箱页要根据全局的 `mail_provider`
   * 决定要不要显示 CF Worker 域名池，而那个字段配在「默认邮箱服务」页。
   */
  children?: ReactNode | ((form: FormInstance, config: Record<string, unknown>) => ReactNode)
  /**
   * 本页拥有、但不在 `sections` 里的配置键。
   *
   * 保存时按「sections 的字段 + 这里登记的键」过滤，只提交本页真正拥有的字段。
   * 页面自己画的控件（cfworker 域名池的清单、邮箱导入面板的 provider 选择）不在
   * sections 里，不登记就会在保存时被过滤掉 —— 用户改了却存不上。
   */
  extraKeys?: readonly string[]
  /**
   * 保存前的字段归一化：改 `values` 即可（原地修改）。
   * 返回错误信息则中止保存并提示。
   *
   * 用途：某些字段在表单里是数组/布尔，落库前要转成字符串（cfworker 域名清单
   * 存 JSON、开关存 "true"/"false"）。这类转换与页面内容强相关，放在页面上比
   * 塞进通用骨架里清楚。
   */
  normalize?: (values: Record<string, unknown>) => string | null | void
}

export function MailServicePage({
  title,
  subtitle,
  sections,
  defaults,
  children,
  extraKeys,
  normalize,
}: MailServicePageProps) {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [loadFailed, setLoadFailed] = useState(false)
  const [config, setConfig] = useState<Record<string, unknown>>({})
  // 已设置口令的键（服务端 `<key>_set`）：给输入框显示「已配置」提示用。
  const [secretSetKeys, setSecretSetKeys] = useState<Set<string>>(new Set())

  // 用 ref 持有 props，让加载逻辑的依赖里不出现 `sections` / `defaults` / `extraKeys`。
  //
  // 页面传进来的这几个都是内联字面量，每次 render 都是新引用。若把它们写进
  // `useCallback` 依赖，父组件任何一次重渲染（点侧栏折叠、父级 setState）都会
  // 让 `loadConfig` 换身份 → effect 重跑 → 重新拉配置并 `setFieldsValue` 覆盖
  // 表单 —— **用户在输入框里还没保存的改动会被静默擦掉**。实测：在临时邮箱页
  // 填好 API URL，点一下侧栏折叠，输入框弹回原值。
  const propsRef = useRef({ sections, defaults, extraKeys })
  propsRef.current = { sections, defaults, extraKeys }

  const loadConfig = useCallback(() => {
    const { sections, defaults, extraKeys } = propsRef.current
    setLoadFailed(false)
    apiFetch('/config').then((data) => {
      const values = data as Record<string, unknown>
      // 留一份未经表单转换的原始值给 children 用（form 里只有本页注册过的字段）
      setConfig({ ...values })
      setSecretSetKeys(secretSetKeysFromConfig(values))

      // 只把本页拥有的字段灌进表单。
      //
      // 不能 `setFieldsValue(整份 config)`：antd 会把每个键都注册进 form store，
      // 于是 `getFieldsValue(true)` 拿到的是整份配置（实测 110 个键）。除了把别页
      // 的字段一起提交上去，更要命的是这些键里有数组（如 `cfworker_domains`），
      // 而 `configs.value` 是字符串列 —— 提交数组会让后端 500，整次保存失败。
      //
      // 这里过滤一次，保存时就不用再猜哪些字段是本页的。
      const owned = new Set<string>(extraKeys ?? [])
      for (const section of sections) {
        for (const field of section.fields) owned.add(field.key)
      }
      // 导入面板要读这几个键（在 children 里渲染，不在 sections 里）
      for (const key of IMPORT_PANEL_KEYS) owned.add(key)

      const scoped: Record<string, unknown> = {}
      for (const key of owned) {
        if (key in values) scoped[key] = values[key]
      }

      for (const [key, fallback] of Object.entries(defaults ?? {})) {
        if (!String(scoped[key] ?? '').trim()) {
          scoped[key] = fallback
        }
      }
      if ('sms_allowed_countries' in scoped) {
        scoped.sms_allowed_countries = parseCountryIdList(scoped.sms_allowed_countries)
      }
      form.setFieldsValue(scoped)
      setLoaded(true)
    }).catch((err) => {
      // 读配置失败时不能放行保存：表单还是空的，提交上去等于把已有配置清空。
      // 但也得给条出路 —— 按钮旁边会变成「重新加载」，不让用户卡死在这一页。
      console.error('加载配置失败', err)
      setLoadFailed(true)
    })
  }, [form])

  useEffect(() => {
    loadConfig()
  }, [loadConfig])

  const save = async () => {
    // 配置还没读回来就别让保存 —— 空表单提交上去等于把已有配置清成空值。
    // 数据丢失比晚几秒保存严重得多。
    if (!loaded) {
      message.warning('配置还在加载，请稍候')
      return
    }

    const values = await form.validateFields()
    // 用 getFieldsValue(true) 而不是 validateFields 的返回值：不在校验结果里的
    // 字段（本页没有，但导入面板写的 `mail_import_source` 可能如此）保存时也得
    // 带上，否则会把用户的选择清掉。
    //
    // 必须拷一层再改：`getFieldsValue(true)` 返回的是表单 store 本身
    // （rc-field-form 的实现如此），在它上面归一化会把 store 写坏。
    // `<key>_set` 是服务端下发的只读标记，不是配置项 —— 一并摘掉。
    const all = stripSecretSetFlags({ ...form.getFieldsValue(true) } as Record<string, unknown>)
    Object.assign(all, values)

    const problem = normalize?.(all)
    if (problem) {
      message.error(problem)
      return
    }

    setSaving(true)
    try {
      if ('sms_allowed_countries' in all) {
        all.sms_allowed_countries = parseCountryIdList(all.sms_allowed_countries).join(',')
      }
      // 口令留空 = 不修改（邮箱服务页有 API Key / 密码类字段）。
      dropEmptySecrets(all, SECRET_KEYS)

      await apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data: all }) })
      message.success('保存成功')
      setSaved(true)
      setTimeout(() => setSaved(false), 2000)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div style={{ maxWidth: 'var(--w-page)' }}>
      <PageHeader title={title} subtitle={subtitle} />
      <Form form={form} layout="vertical">
        {sections.map((section) => (
          <ConfigSection
            key={section.title}
            section={section}
            secretSetKeys={secretSetKeys}
          />
        ))}
        {typeof children === 'function' ? children(form, config) : children}
        {loadFailed ? (
          <Space>
            <Button danger icon={<ReloadOutlined />} onClick={loadConfig}>
              重新加载
            </Button>
            <Typography.Text type="secondary">
              配置没读出来，重新加载后再保存（避免用空表单覆盖已有配置）
            </Typography.Text>
          </Space>
        ) : (
          <Button
            type="primary"
            icon={<SaveOutlined />}
            onClick={save}
            loading={saving}
            disabled={!loaded}
            style={{ marginTop: 8 }}
          >
            {saved ? '已保存 ✓' : '保存配置'}
          </Button>
        )}
      </Form>
    </div>
  )
}

export default MailServicePage
