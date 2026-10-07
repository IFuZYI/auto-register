import { useEffect, useState } from 'react'
import { Card, Row, Col, Tag, Button, Spin, Empty } from 'antd'
import {
  UserOutlined,
  CheckCircleOutlined,
  CloseCircleOutlined,
  StopOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { getPlatformTagColor, getPlatformLabel } from '@/lib/platforms'
import { accountStatusMeta } from '@/lib/accountFormat'
import { apiFetch } from '@/lib/utils'
import { PageHeader } from '@/components/PageHeader'
import { GRID_GAP } from '@/theme'

/** 平台分布条的配色：按平台名取稳定色相，没有映射时回落主题色。 */
const PLATFORM_BAR_COLORS: Record<string, string> = {
  chatgpt: '#10a37f',
  grok: '#1d9bf0',
}

interface StatCardProps {
  title: string
  value: number
  icon: React.ReactNode
  color: string
  index: number
}

function StatCard({ title, value, icon, color, index }: StatCardProps) {
  return (
    <div
      className="stat-card stagger-item"
      style={{ '--stagger-index': index } as React.CSSProperties}
    >
      <div style={{ padding: '22px 24px', position: 'relative', zIndex: 1 }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            gap: 12,
          }}
        >
          <div style={{ minWidth: 0 }}>
            <div
              style={{
                fontSize: 13,
                color: 'var(--text-muted)',
                fontWeight: 500,
                letterSpacing: 0,
                marginBottom: 10,
              }}
            >
              {title}
            </div>
            <div
              style={{
                fontSize: 34,
                fontWeight: 700,
                letterSpacing: '-0.04em',
                lineHeight: 1,
                fontVariantNumeric: 'tabular-nums',
                color: 'var(--text)',
              }}
            >
              {value}
            </div>
          </div>
          <div
            style={{
              width: 44,
              height: 44,
              flex: '0 0 44px',
              borderRadius: 13,
              display: 'grid',
              placeItems: 'center',
              fontSize: 20,
              color,
              background: `color-mix(in srgb, ${color} 14%, transparent)`,
            }}
          >
            {icon}
          </div>
        </div>
      </div>
    </div>
  )
}

export default function Dashboard() {
  const [stats, setStats] = useState<any>(null)
  const [loading, setLoading] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const data = await apiFetch('/accounts/stats')
      setStats(data)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const total = stats?.total ?? 0
  const statCards = [
    {
      title: '总账号数',
      value: total,
      icon: <UserOutlined />,
      color: 'var(--accent)',
    },
    {
      title: '正常',
      value: stats?.by_status?.registered ?? 0,
      icon: <CheckCircleOutlined />,
      color: 'var(--success)',
    },
    {
      title: '失效',
      // 过期 / 失效归这一档：它们同样是「这号还能救」——过期刷新可能救回、
      // 失效重登可能救回。禁用是「号没了」，单独一张卡（用户要求 2026-10-07：
      // 在失效右侧补位，同时四张卡 lg=6 恰好填满整行）。
      value: (stats?.by_status?.expired ?? 0)
        + (stats?.by_status?.invalid ?? 0),
      icon: <CloseCircleOutlined />,
      color: 'var(--danger)',
    },
    {
      title: '禁用',
      // 被封的账号（deleted or deactivated）—— 强判断，不因一次可用探测
      // 复活；与「失效」分开看量：处置方式不同（这号该弃，不重试）。
      // 紫色：红已归「失效」，橙的轻重感与「禁用更严重」倒挂。
      value: stats?.by_status?.banned ?? 0,
      icon: <StopOutlined />,
      color: 'var(--purple)',
    },
  ]

  const platformEntries = Object.entries(stats?.by_platform || {}) as [string, number][]
  const statusEntries = Object.entries(stats?.by_status || {}) as [string, number][]

  return (
    <div>
      <PageHeader
        title="仪表盘"
        subtitle="账号总览与分布"
        actions={
          <Button icon={<ReloadOutlined spin={loading} />} onClick={load} loading={loading}>
            刷新
          </Button>
        }
      />

      <Row gutter={[GRID_GAP, GRID_GAP]}>
        {statCards.map(({ title, value, icon, color }, i) => (
          <Col xs={24} sm={12} lg={6} key={title}>
            <StatCard title={title} value={value} icon={icon} color={color} index={i} />
          </Col>
        ))}
      </Row>

      <Row gutter={[GRID_GAP, GRID_GAP]} style={{ marginTop: GRID_GAP }}>
        <Col xs={24} lg={12}>
          <Card title="平台分布" styles={{ body: { paddingTop: 20 } }}>
            {loading ? (
              <div style={{ textAlign: 'center', padding: 48 }}>
                <Spin />
              </div>
            ) : platformEntries.length ? (
              platformEntries.map(([platform, count], i) => {
                const pct = total ? Math.round((count / total) * 100) : 0
                const color = PLATFORM_BAR_COLORS[platform] || 'var(--accent)'
                return (
                  <div
                    key={platform}
                    className="stagger-item"
                    style={
                      { marginBottom: 20, '--stagger-index': i } as React.CSSProperties
                    }
                  >
                    <div
                      style={{
                        display: 'flex',
                        justifyContent: 'space-between',
                        alignItems: 'center',
                        marginBottom: 8,
                        gap: 12,
                      }}
                    >
                      <Tag
                        color={getPlatformTagColor(platform)}
                        style={{ margin: 0, fontWeight: 500 }}
                      >
                        {getPlatformLabel(platform)}
                      </Tag>
                      <span
                        style={{
                          fontSize: 13,
                          color: 'var(--text-secondary)',
                          fontVariantNumeric: 'tabular-nums',
                        }}
                      >
                        {count}
                        <span style={{ color: 'var(--text-muted)' }}> · {pct}%</span>
                      </span>
                    </div>
                    {/* 自绘进度条：比 antd Progress 更细，两端圆角更贴合卡面 */}
                    <div
                      style={{
                        height: 6,
                        borderRadius: 3,
                        background: 'var(--bg-subtle)',
                        overflow: 'hidden',
                      }}
                    >
                      <div
                        style={{
                          width: `${pct}%`,
                          height: '100%',
                          borderRadius: 3,
                          background: color,
                          transition: 'width 0.6s var(--ease-out)',
                        }}
                      />
                    </div>
                  </div>
                )
              })
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <span style={{ fontSize: 13 }}>
                    暂无账号数据
                    <br />
                    <span style={{ color: 'var(--text-muted)' }}>注册成功后这里会显示各平台占比</span>
                  </span>
                }
                style={{ margin: '8px 0' }}
              />
            )}
          </Card>
        </Col>

        <Col xs={24} lg={12}>
          <Card title="状态分布" styles={{ body: { paddingTop: 12 } }}>
            {loading ? (
              <div style={{ textAlign: 'center', padding: 48 }}>
                <Spin />
              </div>
            ) : statusEntries.length ? (
              statusEntries.map(([status, count], i) => {
                const meta = accountStatusMeta(status)
                return (
                <div
                  key={status}
                  className="stagger-item"
                  style={
                    {
                      display: 'flex',
                      justifyContent: 'space-between',
                      alignItems: 'center',
                      padding: '12px 0',
                      borderBottom:
                        i === statusEntries.length - 1 ? 'none' : '1px solid var(--border)',
                      '--stagger-index': i,
                    } as React.CSSProperties
                  }
                >
                  <Tag color={meta.color} style={{ margin: 0 }}>
                    {meta.label}
                  </Tag>
                  <span
                    style={{
                      fontSize: 15,
                      fontWeight: 600,
                      fontVariantNumeric: 'tabular-nums',
                      color: 'var(--text)',
                    }}
                  >
                    {count}
                  </span>
                </div>
                )
              })
            ) : (
              <Empty
                image={Empty.PRESENTED_IMAGE_SIMPLE}
                description={
                  <span style={{ fontSize: 13 }}>
                    暂无状态数据
                    <br />
                    <span style={{ color: 'var(--text-muted)' }}>账号注册与检测结果会汇总到这里</span>
                  </span>
                }
                style={{ margin: '8px 0' }}
              />
            )}
          </Card>
        </Col>
      </Row>
    </div>
  )
}
