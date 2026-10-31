import { useEffect, useState } from 'react'
import { App, Button, Form, Typography } from 'antd'
import {
  ApiOutlined,
  ExportOutlined,
  GithubOutlined,
  ReloadOutlined,
  SaveOutlined,
} from '@ant-design/icons'
import {
  ConfigField,
  resolveFeatureEnabledConfig,
  type FieldConfig,
} from '@/components/settings/ConfigPanels'
import { HelpTip } from '@/components/settings/SettingsPrimitives'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import {
  dropEmptySecrets,
  secretSetKeysFromConfig,
  stripSecretSetFlags,
} from '@/lib/secretConfig'
import { PANEL_ICONS } from '@/lib/panelIcons'
import { apiFetch } from '@/lib/utils'

/**
 * 平台面板 —— 一个平台一张卡片，卡片里就是这个平台的配置。
 *
 * 改造前是「上面一排入口卡片 + 下面一列连接配置表单」，两边靠 `scrollIntoView`
 * 对应：卡片说「未配置」，按钮叫「去配置」，点了滚到下面某节。问题在于
 * **同一个平台的连接参数被写了两遍位置**（身份在卡上、旋钮在下面），
 * 用户要先认卡片、再滚下去找对应的那一节。
 *
 * 现在一张卡片就是全部：卡头给身份与状态，卡体给旋钮，卡脚给去处。
 * 页面上只有一个保存按钮（四种面板的配置都在同一份 config 里）。
 */

interface PanelItem {
  key: string
  label: string
  desc: string
  url_key: string
  /** 项目主页；空串表示没有公开仓库（自建/私有部署），不渲染图标按钮 */
  github: string
  url: string
  configured: boolean
  /** 仅需要口令的面板才有 */
  secret_key?: string
  secret_label?: string
  secret_placeholder?: string
  url_placeholder?: string
  secret_set?: boolean
}

/** 卡头那句定位。注册表的 `desc` 更长，进 ⓘ。 */
const PANEL_TAGLINE: Record<string, string> = {
  cpa: 'ChatGPT 账号的 auth-file 出口',
  sub2api: '账号转 OAuth 后推送到管理后台',
  chatgpt2api: 'ChatGPT 网页号池',
  grok2api: 'Grok 账号池与 API 网关',
}

/**
 * 各面板的旋钮。
 *
 * 顺序即卡片里的行序：**先开关，后连接参数**。用户扫一眼卡片先要知道
 * 「有没有在自动上传」，地址是配好了才需要改的东西。
 */
const PANEL_FIELDS: Record<string, FieldConfig[]> = {
  cpa: [
    {
      key: 'cpa_upload_chatgpt_enabled',
      label: '自动上传',
      type: 'boolean',
      hint: '注册成功后自动把 ChatGPT 账号推到 CPA。Grok 自 2026-10-27 起不再支持 CPA，只保留 Grok2API。',
    },
    {
      key: 'cpa_upload_proxy_enabled',
      label: '上传代理',
      type: 'boolean',
      hint: '把账号绑定的代理一并写进 auth 文件（顶层 proxy_url），让 CPA 用同一条出口访问上游。账号没有绑定代理时留空、不影响上传。',
    },
    { key: 'cpa_api_url', label: '地址', placeholder: 'http://127.0.0.1:8317' },
    {
      key: 'cpa_api_key',
      label: '管理口令',
      secret: true,
      placeholder: '默认 cliproxyapi',
      hint: 'CLIProxyAPI 管理端的 Bearer 口令。上传与状态同步走同一个 auth-files 端点。',
    },
  ],
  sub2api: [
    {
      key: 'sub2api_enabled',
      label: '自动上传',
      type: 'boolean',
      hint: '注册成功后自动上传到 Sub2API 管理后台。',
    },
    {
      key: 'sub2api_upload_proxy_enabled',
      label: '上传代理',
      type: 'boolean',
      hint: 'Sub2API 的账号只认 proxy_id，所以会先在面板里按「协议 + 主机 + 端口 + 用户名」找一条代理记录，找不到就建一条，再把这个账号绑上去。绑定失败不影响账号上传。',
    },
    { key: 'sub2api_api_url', label: '地址', placeholder: 'https://your-sub2api.example.com' },
    { key: 'sub2api_api_key', label: 'API Key', secret: true },
    {
      key: 'sub2api_group_ids',
      label: '分组 ID',
      placeholder: '2,4,8',
      hint: '多个分组用英文逗号分隔。留空按面板的默认分组（2）。',
    },
  ],
  chatgpt2api: [
    {
      key: 'chatgpt2api_enabled',
      label: '自动上传',
      type: 'boolean',
      hint: '注册成功后自动上传到 ChatGPT2API 号池。',
    },
    {
      key: 'chatgpt2api_import_mode',
      label: '导入内容',
      hint:
        '仅 AT/RT：只给号池喂 access_token + refresh_token，不交出密码。' +
        '完全账户：再加邮箱、密码与 2FA 密钥 —— 对端（≥ 2.2.0）能在 RT 失效后用密码自动重登，' +
        '不交密码就只能人工救。默认完全账户，与升级前行为一致。',
    },
    {
      key: 'chatgpt2api_upload_proxy_enabled',
      label: '上传代理',
      type: 'boolean',
      hint: '把这个账号绑定的代理随凭据一起交给 ChatGPT2API（导入对象的 proxy 字段），让号池用同一条出口。',
    },
    { key: 'chatgpt2api_api_url', label: '地址', placeholder: 'http://127.0.0.1:8000' },
    { key: 'chatgpt2api_api_key', label: '管理密钥', secret: true },
  ],
  grok2api: [
    {
      key: 'grok2api_enabled',
      label: '自动上传',
      type: 'boolean',
      hint: '注册出 SSO 后自动上传 Web 账号并开启 NSFW。Console / Build 凭据由你在 Grok2API 里手动转换。',
    },
    {
      key: 'grok2api_upload_proxy_enabled',
      label: '上传代理',
      type: 'boolean',
      hint: 'Grok2API 的账号没有代理字段，出口由 egress 节点提供。开启后会按「主机 + 端口」建（或复用）一个出口节点，再把账号绑上去。绑定失败不影响账号上传。',
    },
    { key: 'grok2api_base_url', label: '地址', placeholder: 'http://127.0.0.1:8000' },
    { key: 'grok2api_username', label: '用户名', placeholder: 'admin' },
    { key: 'grok2api_password', label: '密码', secret: true },
  ],
}

/** 「导入内容」这类枚举字段的候选值。 */
const FIELD_OPTIONS: Record<string, { value: string; label: string }[]> = {
  // 选项文字保持短：卡片里的控件列只有 200px 上下，长标签会把下拉撑成
  // 「完全账户（账号密码 + AT/RT + 2FA）」这样一长条，与旁边的开关、
  // 地址框右边缘全错开。内容写进 ⓘ 里。
  chatgpt2api_import_mode: [
    { value: 'full', label: '完全账户' },
    { value: 'atrt', label: '仅 AT/RT' },
  ],
}

/** 库里存 "0"/"1" 字符串的开关：读时归一成布尔，写时归一回去。 */
const BOOLEAN_KEYS = [
  'cpa_upload_chatgpt_enabled',
  'cpa_upload_proxy_enabled',
  'sub2api_enabled',
  'sub2api_upload_proxy_enabled',
  'grok2api_enabled',
  'grok2api_upload_proxy_enabled',
  'chatgpt2api_enabled',
  'chatgpt2api_upload_proxy_enabled',
  'chatgpt2api_auto_sync_enabled',
  'chatgpt_auto_refresh_enabled',
  'cpa_auto_sync_enabled',
  'sub2api_auto_sync_enabled',
  'grok2api_auto_sync_enabled',
] as const

/** 枚举字段：不是开关也不是自由文本，回填时要有个兜底值。 */
const ENUM_DEFAULTS: Record<string, string> = {
  chatgpt2api_import_mode: 'full',
}

/**
 * 本页出现的所有口令字段名。
 *
 * 两个来源：注册表项声明的（`secret_key`，如 CPA 的管理口令）与本节声明的
 * （`secret: true`，如 Grok2API 的管理员密码）。**必须收齐** —— 漏一个，
 * 那个口令就会被 `GET /api/config` 的明文回填进表单、保存时再原样提交，
 * 等于把明文塞进 DOM 又绕一圈（实测 grok2api_password 就是这样）。
 */
function secretFieldKeys(panels: PanelItem[]): string[] {
  const keys = new Set<string>()
  for (const panel of panels) {
    if (panel.secret_key) keys.add(panel.secret_key)
  }
  for (const fields of Object.values(PANEL_FIELDS)) {
    for (const field of fields) {
      if (field.secret) keys.add(field.key)
    }
  }
  return [...keys]
}

function PanelCard({
  panel,
  secretSetKeys,
  onOpenGithub,
}: {
  panel: PanelItem
  secretSetKeys: ReadonlySet<string>
  onOpenGithub: (url: string) => void
}) {
  const [expanded, setExpanded] = useState(false)
  const configured = panel.configured
  const hasGithub = Boolean(String(panel.github || '').trim())
  const fields = PANEL_FIELDS[panel.key] ?? []
  const tagline = PANEL_TAGLINE[panel.key] ?? ''

  return (
    <section className={`panel-card${expanded ? ' is-expanded' : ''}`}>
      <header className="panel-card__head">
        <div className={`panel-card__icon${configured ? ' is-on' : ''}`}>
          {PANEL_ICONS[panel.key] ?? <ApiOutlined />}
        </div>
        <div className="panel-card__id">
          <div className="panel-card__name">
            {panel.label}
            <HelpTip text={panel.desc} />
          </div>
          <div className="panel-card__desc">{tagline}</div>
        </div>
        <span className={`panel-card__state${configured ? ' is-on' : ''}`}>
          {configured ? '已连接' : '未配置'}
        </span>
      </header>

      {expanded ? (
        <div className="panel-card__body">
          {fields.map((field) => (
            <ConfigField
              key={field.key}
              field={
                FIELD_OPTIONS[field.key]
                  ? { ...field, type: 'select', options: FIELD_OPTIONS[field.key] }
                  : field
              }
              secretSet={secretSetKeys.has(field.key)}
            />
          ))}
        </div>
      ) : (
        <div className="panel-card__body panel-card__body--idle">
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            {configured && panel.url ? panel.url : '还没有配置连接参数'}
          </Typography.Text>
        </div>
      )}

      <footer className="panel-card__foot">
        {configured ? (
          <Button
            size="small"
            icon={<ExportOutlined />}
            onClick={() => window.open(panel.url, '_blank', 'noopener')}
          >
            打开面板
          </Button>
        ) : null}
        <Button size="small" type="text" onClick={() => setExpanded((v) => !v)}>
          {expanded ? '收起' : '配置'}
        </Button>
        {hasGithub ? (
          <Button
            size="small"
            type="text"
            icon={<GithubOutlined />}
            aria-label={`打开 ${panel.label} 的 GitHub 主页`}
            onClick={() => onOpenGithub(panel.github)}
            style={{ marginLeft: 'auto', color: 'var(--text-muted)' }}
          />
        ) : null}
      </footer>
    </section>
  )
}

export function PanelConfigPanel() {
  const { message } = App.useApp()
  const [form] = Form.useForm()
  const [panels, setPanels] = useState<PanelItem[]>([])
  // 已设置口令的键（服务端 `<key>_set`）：给输入框显示「已配置」提示用。
  const [secretSetKeys, setSecretSetKeys] = useState<Set<string>>(new Set())
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const [data, config] = await Promise.all([
        apiFetch('/integrations/panels') as Promise<{ items?: PanelItem[] }>,
        apiFetch('/config') as Promise<Record<string, unknown>>,
      ])
      const items = Array.isArray(data.items) ? data.items : []
      setPanels(items)

      // 开关类：历史数据里可能是空串。填了 URL + Key 的按已启用处理，
      // 否则首次打开会把已经配好的上传目标显示成关闭。
      // ⚠️ 口令已不回明文（`GET /api/config` 只回 `<key>_set`），所以判据
      // 必须用 `_set` 标记，不能再用 `config.cpa_api_key`（那永远是空串，
      // 会把已配置的面板误判成未配置）。
      const cpaReady = Boolean(String(config.cpa_api_url ?? '').trim() && config.cpa_api_key_set)
      // ChatGPT / Grok 分开开关（用户要求）。老配置里只有 `cpa_enabled`：
      // 那时它是「两边都传」，所以两个平台键都按它回落，保持行为不变。
      const legacyCpa = resolveFeatureEnabledConfig(config.cpa_enabled, cpaReady)
      config.cpa_upload_chatgpt_enabled = resolveFeatureEnabledConfig(
        config.cpa_upload_chatgpt_enabled,
        legacyCpa,
      )
      config.sub2api_enabled = resolveFeatureEnabledConfig(
        config.sub2api_enabled,
        Boolean(String(config.sub2api_api_url ?? '').trim() && config.sub2api_api_key_set),
      )
      // grok2api 的「已配置」判据：地址 + 用户名 + 密码（三个都要，
      // 与客户端 `Grok2ApiClient.configured` 一致）
      config.grok2api_enabled = resolveFeatureEnabledConfig(
        config.grok2api_enabled,
        Boolean(
          String(config.grok2api_base_url ?? '').trim()
          && String(config.grok2api_username ?? '').trim()
          && config.grok2api_password_set,
        ),
      )
      // chatgpt2api 的「已配置」判据：地址 + 密钥（两个都要）
      config.chatgpt2api_enabled = resolveFeatureEnabledConfig(
        config.chatgpt2api_enabled,
        Boolean(String(config.chatgpt2api_api_url ?? '').trim() && config.chatgpt2api_api_key_set),
      )
      // 其余布尔开关（上传代理 ×4 + 自动同步 ×4）：库里存 "0"/"1" 字符串，
      // 必须归一成布尔再回填 —— antd Switch 把非空字符串一律视为真，
      // 直接回填 "0" 会把关闭的开关渲染成「开启」（实测复现）。
      // 默认值一律 false：它们没有「已配置即开启」的语义。
      for (const key of BOOLEAN_KEYS) {
        config[key] = resolveFeatureEnabledConfig(config[key], false)
      }
      // 枚举字段：库里的空串会让 Select 显示空白，回落成默认档
      for (const [key, fallback] of Object.entries(ENUM_DEFAULTS)) {
        const value = String(config[key] ?? '').trim()
        if (!value) config[key] = fallback
      }
      // 口令字段留空 = 「不修改」：接口本就不回明文，这里再清一遍是双保险
      // （服务端也会拦空串，见 api/config.py 的 SECRET_CONFIG_KEYS 处理）。
      // 两个来源的口令都要清（注册表声明的 + 本节声明的）。
      for (const key of secretFieldKeys(items)) config[key] = ''
      setSecretSetKeys(secretSetKeysFromConfig(config))
      form.setFieldsValue(config)
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : '加载面板配置失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const save = async () => {
    setSaving(true)
    try {
      // 只提交本页确实读到的字段：`getFieldsValue()` 不带未注册字段，
      // 用 `true` 会把整份 config（110+ 键）提交上去。
      // `<key>_set` 是服务端下发的只读标记，不是配置项 —— 一并摘掉。
      const payload = stripSecretSetFlags(form.getFieldsValue() as Record<string, unknown>)

      for (const key of BOOLEAN_KEYS) {
        if (key in payload) payload[key] = parseBooleanConfigValue(payload[key])
      }
      // 口令留空 = 不改：空串会把已存的口令覆盖掉。两个来源的口令都要兜住
      // （注册表声明的 + 本节声明的），否则那一栏的空值会直接写库。
      // （服务端也拦空串，见 api/config.py 的 SECRET_CONFIG_KEYS 处理。）
      dropEmptySecrets(payload, secretFieldKeys(panels))

      await apiFetch('/config', { method: 'PUT', body: JSON.stringify({ data: payload }) })
      message.success('面板配置已保存')
      await load()
    } catch (e: unknown) {
      message.error(e instanceof Error ? e.message : '保存失败')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Form form={form} layout="vertical">
      {/* 这里**不再有标题**：页面顶部的「面板配置 ⓘ」已经说明了这一页管什么，
          再来一个「平台面板」就是同一个意思说两遍（实测两行几乎一样的字
          挨在一起，看着像层级没对齐）。只留一个右对齐的刷新。 */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', marginBottom: 12 }}>
        <Button
          size="small"
          type="text"
          icon={<ReloadOutlined />}
          loading={loading}
          onClick={load}
        >
          刷新
        </Button>
      </div>

      <div className="panel-grid">
        {panels.map((panel) => (
          <PanelCard
            key={panel.key}
            panel={panel}
            secretSetKeys={secretSetKeys}
            onOpenGithub={(url) => window.open(url, '_blank', 'noopener')}
          />
        ))}
      </div>

      <Button
        type="primary"
        icon={<SaveOutlined />}
        onClick={save}
        loading={saving}
        style={{ marginTop: 20 }}
      >
        保存面板配置
      </Button>
    </Form>
  )
}

export type { PanelItem }
