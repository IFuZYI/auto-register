import { useEffect, useState } from 'react'
import { App, Button, Card, Form, Space, Tooltip, Typography } from 'antd'
import {
  ApiOutlined,
  ExportOutlined,
  GithubOutlined,
  ReloadOutlined,
  SaveOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import {
  ConfigSection,
  resolveFeatureEnabledConfig,
  type SectionConfig,
} from '@/components/settings/ConfigPanels'
import { parseBooleanConfigValue } from '@/lib/configValueParsers'
import {
  dropEmptySecrets,
  secretSetKeysFromConfig,
  stripSecretSetFlags,
} from '@/lib/secretConfig'
import { PANEL_ICONS } from '@/lib/panelIcons'
import { apiFetch } from '@/lib/utils'

/**
 * 面板配置（全局配置的一个 tab）。
 *
 * 原先拆在两个一级菜单里：「平台配置」管各面板的连接配置、「面板管理」管入口
 * 卡片。两页说的都是「面板」这一件事，用户要在两处来回找 —— 合并到一处。
 *
 * 上半页是入口卡片（来自 `GET /api/integrations/panels`），下半页是连接配置。
 * 卡片、地址字段、口令字段都由后端注册表下发，前端不硬编码面板清单；
 * 各面板的**额外**旋钮（自动维护阈值、分组 ID、上传类型…）在这里声明。
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
  /** 仅需要口令的面板才有（CPA 面板） */
  secret_key?: string
  secret_label?: string
  secret_placeholder?: string
  url_placeholder?: string
  secret_set?: boolean
}

/**
 * 各面板的连接配置。
 *
 * 地址与口令字段与注册表共用同一批配置键（`url_key` / `secret_key`）——
 * 这里写的是**面板项之外**的旋钮（开关、阈值、分组…），它们不属于
 * 「面板地址」这个抽象，注册表里没有。
 *
 * `anchor` 与卡片上的「去配置」按钮对应：卡片知道自己的 `key`，配置区知道
 * 自己的 `anchor`，两边靠它对上（注册表加面板时这里补一节即可）。
 */
interface PanelSection {
  /** 对应哪个面板卡片的「去配置」；不填表示这节没有对应卡片 */
  anchor?: string
  section: SectionConfig
}

const PANEL_SECTIONS: PanelSection[] = [
  {
    anchor: 'cpa',
    section: {
      title: 'CPA 面板（CLIProxyAPI）',
      desc: '地址与管理口令。注册完自动上传、状态同步也从这里读（同一个 auth-files 端点）；上传开关按平台分开。',
      fields: [
        { key: 'cpa_upload_chatgpt_enabled', label: '自动上传 ChatGPT 账号', type: 'boolean' },
        { key: 'cpa_upload_grok_enabled', label: '自动上传 Grok 账号', type: 'boolean' },
        { key: 'cpa_upload_proxy_enabled', label: '上传代理（账号绑定的代理）', type: 'boolean' },
        { key: 'cpa_api_url', label: 'API URL', placeholder: 'http://127.0.0.1:8317' },
        { key: 'cpa_api_key', label: '管理口令', secret: true, placeholder: '默认 cliproxyapi' },
      ],
    },
  },
  {
    anchor: 'sub2api',
    section: {
      title: 'Sub2API 面板',
      desc: '注册完成后自动上传到 Sub2API 管理后台',
      fields: [
        { key: 'sub2api_enabled', label: '启用自动上传', type: 'boolean' },
        { key: 'sub2api_api_url', label: 'API URL', placeholder: 'https://your-sub2api.example.com' },
        { key: 'sub2api_api_key', label: 'API Key', secret: true },
        { key: 'sub2api_group_ids', label: '分组 ID', placeholder: '多个分组用英文逗号分隔，例如 2,4,8' },
      ],
    },
  },
  {
    anchor: 'grok2api',
    section: {
      title: 'grok2api',
      desc: 'Grok 账号池与 API 网关。注册出 SSO 后自动上传 Web 账号并开启 NSFW；Console / Build 凭据由你在 grok2api 中手动转换。',
      fields: [
        { key: 'grok2api_enabled', label: '启用自动上传', type: 'boolean' },
        { key: 'grok2api_base_url', label: 'API URL', placeholder: 'http://127.0.0.1:8000' },
        { key: 'grok2api_username', label: '管理员用户名', placeholder: 'admin' },
        { key: 'grok2api_password', label: '管理员密码', secret: true },
      ],
    },
  },
  {
    anchor: 'chatgpt2api',
    section: {
      title: 'chatgpt2api',
      desc: 'ChatGPT 网页号池（只认 access_token，与 CPA 的 codex 凭据互不相干）。',
      fields: [
        { key: 'chatgpt2api_enabled', label: '启用自动上传', type: 'boolean' },
        { key: 'chatgpt2api_auto_sync_enabled', label: '凭证自动维护（本地较新自动推送更新）', type: 'boolean' },
        { key: 'chatgpt2api_upload_proxy_enabled', label: '上传代理（账号绑定的代理）', type: 'boolean' },
        { key: 'chatgpt2api_api_url', label: 'API URL', placeholder: 'http://127.0.0.1:8000' },
        { key: 'chatgpt2api_api_key', label: '管理密钥', secret: true },
      ],
    },
  },
  {
    anchor: 'chatgpt-auto-maintenance',
    section: {
      title: 'ChatGPT Token 自动维护',
      desc:
        '定时扫描 ChatGPT 账号：AT 临期（剩余 ≤ 24h）或已过期时，在到期前随机一个时刻自动刷新（至少提前 1 小时，不设固定更新时刻）；' +
        '失败自动退避重试，连续失败 3 次或已封禁的账号不再自动尝试。手动刷新 Token 与注册任务不受影响。',
      fields: [
        { key: 'chatgpt_auto_refresh_enabled', label: '自动刷新临期/过期 Token', type: 'boolean' },
      ],
    },
  },
]

/** 库里存 "0"/"1" 字符串的开关：读时归一成布尔，写时归一回去。 */
const BOOLEAN_KEYS = [
  'cpa_upload_chatgpt_enabled',
  'cpa_upload_grok_enabled',
  'cpa_upload_proxy_enabled',
  'sub2api_enabled',
  'grok2api_enabled',
  'chatgpt2api_enabled',
  'chatgpt2api_upload_proxy_enabled',
  'chatgpt2api_auto_sync_enabled',
  'chatgpt_auto_refresh_enabled',
] as const

/**
 * 本页出现的所有口令字段名。
 *
 * 两个来源：注册表项声明的（`secret_key`，如 CPA 的管理口令）与本节声明的
 * （`secret: true`，如 grok2api 的管理员密码）。**必须收齐** —— 漏一个，
 * 那个口令就会被 `GET /api/config` 的明文回填进表单、保存时再原样提交，
 * 等于把明文塞进 DOM 又绕一圈（实测 grok2api_password 就是这样）。
 */
function secretFieldKeys(panels: PanelItem[]): string[] {
  const keys = new Set<string>()
  for (const panel of panels) {
    if (panel.secret_key) keys.add(panel.secret_key)
  }
  for (const { section } of PANEL_SECTIONS) {
    for (const field of section.fields) {
      if (field.secret) keys.add(field.key)
    }
  }
  return [...keys]
}

function PanelCard({
  panel,
  onOpenGithub,
  onConfigure,
}: {
  panel: PanelItem
  onOpenGithub: (url: string) => void
  onConfigure: () => void
}) {
  const configured = panel.configured
  const hasGithub = Boolean(String(panel.github || '').trim())

  return (
    <Card
      styles={{ body: { padding: 20, display: 'flex', flexDirection: 'column', gap: 14, height: '100%' } }}
      style={{ height: '100%' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div
          style={{
            width: 38,
            height: 38,
            borderRadius: 11,
            display: 'grid',
            placeItems: 'center',
            fontSize: 18,
            background: configured ? 'var(--accent-soft)' : 'var(--bg-subtle)',
            color: configured ? 'var(--accent)' : 'var(--text-muted)',
            flex: '0 0 auto',
          }}
        >
          {PANEL_ICONS[panel.key] ?? <ApiOutlined />}
        </div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontWeight: 600 }}>{panel.label}</div>
          <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>{panel.desc}</div>
        </div>
        {/* GitHub 主页直达。没有公开仓库的项目（自建系统）不渲染按钮 ——
            摆一个点了没反应的图标比不摆更糟。 */}
        {hasGithub ? (
          <Tooltip title="打开 GitHub 项目主页">
            <Button
              type="text"
              size="small"
              icon={<GithubOutlined />}
              aria-label={`打开 ${panel.label} 的 GitHub 主页`}
              onClick={() => onOpenGithub(panel.github)}
              style={{ color: 'var(--text-muted)' }}
            />
          </Tooltip>
        ) : null}
      </div>

      <div style={{ flex: 1, minWidth: 0 }}>
        {configured ? (
          <Typography.Text copyable style={{ fontSize: 12 }} ellipsis>
            {panel.url}
          </Typography.Text>
        ) : (
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            未配置
          </Typography.Text>
        )}
      </div>

      <Space>
        {configured ? (
          <Button
            icon={<ExportOutlined />}
            onClick={() => window.open(panel.url, '_blank', 'noopener')}
          >
            打开面板
          </Button>
        ) : (
          // 未配置时把视线带到下面的连接配置区，而不是跳去别的页面 ——
          // 配置就在本页，跳走反而要用户自己找回来。
          <Button icon={<SettingOutlined />} onClick={onConfigure}>
            去配置
          </Button>
        )}
      </Space>
    </Card>
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
      config.cpa_upload_grok_enabled = resolveFeatureEnabledConfig(
        config.cpa_upload_grok_enabled,
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
      // 其余布尔开关（上传代理 ×2 + 自动维护 ×2）：库里存 "0"/"1" 字符串，
      // 必须归一成布尔再回填 —— antd Switch 把非空字符串一律视为真，
      // 直接回填 "0" 会把关闭的开关渲染成「开启」（实测复现）。
      // 默认值一律 false：它们没有「已配置即开启」的语义。
      for (const key of BOOLEAN_KEYS) {
        config[key] = resolveFeatureEnabledConfig(config[key], false)
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

  const scrollToConfig = (panel: PanelItem) => {
    document
      .getElementById(`panel-config-${panel.key}`)
      ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  return (
    <Form form={form} layout="vertical">
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          gap: 12,
          flexWrap: 'wrap',
          marginBottom: 12,
        }}
      >
        <Typography.Text type="secondary" style={{ fontSize: 13 }}>
          面板是独立的远程服务 —— 这里配置它们的地址，已配置的可以直接打开
        </Typography.Text>
        <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
          刷新
        </Button>
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fill, minmax(248px, 1fr))',
          gap: 16,
          marginBottom: 'var(--section-gap)',
        }}
      >
        {panels.map((panel) => (
          <PanelCard
            key={panel.key}
            panel={panel}
            onOpenGithub={(url) => window.open(url, '_blank', 'noopener')}
            onConfigure={() => scrollToConfig(panel)}
          />
        ))}
      </div>

      {PANEL_SECTIONS.map(({ anchor, section }) => (
        <div key={section.title} id={anchor ? `panel-config-${anchor}` : undefined}>
          <ConfigSection section={section} secretSetKeys={secretSetKeys} />
        </div>
      ))}

      <Button type="primary" icon={<SaveOutlined />} onClick={save} loading={saving} block>
        保存面板配置
      </Button>
    </Form>
  )
}
