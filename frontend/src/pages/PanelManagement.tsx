import { useEffect, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { App, Button, Card, Space, Tooltip, Typography } from 'antd'
import {
  ApiOutlined,
  ExportOutlined,
  GithubOutlined,
  ReloadOutlined,
  SettingOutlined,
} from '@ant-design/icons'
import { PageHeader } from '@/components/PageHeader'
import { PanelComparisonPanel } from '@/components/settings/PanelComparisonPanel'
import { PANEL_ICONS } from '@/lib/panelIcons'
import { apiFetch } from '@/lib/utils'

/**
 * 面板管理：**当前所选面板**的入口 + 本地 ↔ 远程对比。
 *
 * 选中面板走侧栏二级菜单（`/panel-management/:panelKey`）—— 二级项由
 * `/api/integrations/panels` 下发，注册表加面板侧栏自动多一项。本页不再铺一排
 * 可选卡片：侧栏已经能选，卡片再选一遍是重复的第二套选择器（用户要求去掉）。
 * 因此这里只渲染**当前选中面板的一张卡**：地址、打开/去配置、GitHub 直达。
 *
 * 没选面板时（`/panel-management`）自动落到第一个面板，页面不留空白 ——
 * 直接粘一级链接或从菜单点一级项进来都能看到内容。
 *
 * 连接配置不在本页渲染 —— 同一份配置在两个页面各渲染一次会让用户以为要分别填，
 * 改了一个另一个不生效（实测踩过）。
 *
 * 历史上这里还有一套「本地插件」管理（安装 / 启动 / 停止 / 卸载 CLIProxyAPI 源码），
 * 已按用户要求整块删除：本应用不再 clone 源码、不再编译、不再管本机进程。
 */

/** 后端 `/api/integrations/panels` 返回的面板项 */
interface PanelItem {
  key: string
  label: string
  desc: string
  url_key: string
  /** 项目主页；空串表示没有公开仓库（自建/私有部署），不渲染图标按钮 */
  github: string
  url: string
  configured: boolean
  /** 这个面板对应的平台（批量动作用） */
  platform?: string
  /** 上传动作 id（如 `upload_cpa`）；空串/缺省表示没有上传动作 */
  upload_action?: string
  /** 拉远端状态的动作 id（如 `sync_cliproxyapi_status`）；空串表示没有 */
  sync_action?: string
  /** 多平台面板：涉及哪些平台（CPA 是 chatgpt + grok） */
  platforms?: string[]
  /** 多平台面板：平台 → 上传动作 id */
  upload_actions?: Record<string, string>
  /** 多平台面板：平台 → 同步动作 id */
  sync_actions?: Record<string, string>
}

/** 面板配置页（全局配置下的一个 tab）。 */
const PANEL_CONFIG_PATH = '/settings?tab=panel'

/** 当前选中面板的单张卡片：地址 + 打开/去配置 + GitHub 直达。 */
function SelectedPanelCard({
  panel,
  onConfigure,
  onOpenGithub,
}: {
  panel: PanelItem
  onConfigure: () => void
  onOpenGithub: (url: string) => void
}) {
  const configured = panel.configured
  const hasGithub = Boolean(String(panel.github || '').trim())

  return (
    <Card
      styles={{ body: { padding: 20, display: 'flex', flexDirection: 'column', gap: 14 } }}
      style={{ borderLeft: '3px solid var(--accent)' }}
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
        {/* GitHub 主页直达。没有公开仓库的项目（自建系统）不渲染按钮 —— 摆一个
            点了没反应的图标比不摆更糟。 */}
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

      <div style={{ minWidth: 0 }}>
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
          <Button icon={<SettingOutlined />} onClick={onConfigure}>
            去配置
          </Button>
        )}
      </Space>
    </Card>
  )
}

export default function PanelManagement() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  // 选中的面板由 URL 决定（`/panel-management/:panelKey`），不是组件内部状态 ——
  // 侧栏二级项、浏览器前进后退、直接粘链接都要能对上同一个面板。
  const { panelKey } = useParams<{ panelKey: string }>()
  const [panels, setPanels] = useState<PanelItem[]>([])
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const data = await apiFetch('/integrations/panels') as { items?: PanelItem[] }
      setPanels(Array.isArray(data.items) ? data.items : [])
    } catch (e: unknown) {
      const detail = e instanceof Error ? e.message : '加载面板配置失败'
      message.error(detail)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  // 没带 panelKey 时落到第一个面板：侧栏一级项与「面板管理」入口都用一级路径，
  // 不兜底的话进来是空页。`replace` 保证回退不会卡在一级路径上反复跳。
  const selectedPanel = panels.find((panel) => panel.key === panelKey) ?? panels[0]

  useEffect(() => {
    if (!panelKey && panels.length > 0) {
      navigate(`/panel-management/${panels[0].key}`, { replace: true })
    }
  }, [panelKey, panels, navigate])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--section-gap)' }}>
      <PageHeader
        title="面板管理"
        subtitle="用左侧「面板管理」下的子项切换面板；连接配置在「全局配置 → 面板配置」"
        actions={
          <Space>
            <Button icon={<SettingOutlined />} onClick={() => navigate(PANEL_CONFIG_PATH)}>
              面板配置
            </Button>
            <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
              刷新
            </Button>
          </Space>
        }
      />

      {selectedPanel ? (
        <SelectedPanelCard
          panel={selectedPanel}
          onConfigure={() => navigate(PANEL_CONFIG_PATH)}
          onOpenGithub={(url) => window.open(url, '_blank', 'noopener')}
        />
      ) : null}

      {selectedPanel ? (
        <PanelComparisonPanel
          panelKey={selectedPanel.key}
          panelLabel={selectedPanel.label}
          platform={selectedPanel.platform || ''}
          platformActions={{
            upload: selectedPanel.upload_actions,
            sync: selectedPanel.sync_actions,
          }}
          uploadAction={selectedPanel.upload_action || ''}
          syncAction={selectedPanel.sync_action || ''}
        />
      ) : null}
    </div>
  )
}
