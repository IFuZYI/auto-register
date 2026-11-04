import { useCallback, useEffect, useState } from 'react'
import { Card, Row, Col, Tag, Button, Spin, Empty, App } from 'antd'
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
import { HelpTip } from '@/components/settings/SettingsPrimitives'

/** 平台分布条的配色：按平台名取稳定色相，没有映射时回落主题色。 */
const PLATFORM_BAR_COLORS: Record<string, string> = {
  chatgpt: '#10a37f',
  grok: '#1d9bf0',
}

interface StatCardProps {
  title: string
  /** 加载失败时传 '—'：0 会被读成「真的一个都没有」。 */
  value: number | string
  icon: React.ReactNode
  color: string
  index: number
  /** 这个数字由什么构成（进标题旁的 ⓘ）。合计型的卡片必须写清楚， */
  /*  否则会和下方明细里的同名项对不上（实测「失效 77 vs 69」）。 */
  hint?: string
}

function StatCard({ title, value, icon, color, index, hint }: StatCardProps) {
  return (
    <div
      className="stat-card stagger-item"
      style={{ '--stagger-index': index } as React.CSSProperties}
    >
      <div className="stat-card__body">
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
                display: 'flex',
                alignItems: 'center',
                gap: 5,
              }}
            >
              {title}
              <HelpTip text={hint} />
            </div>
            <div className="stat-card__value">
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

/**
 * 请求失败时占位：**不能**复用「暂无账号数据」那套文案。
 *
 * `/accounts/stats` 挂掉时 `stats` 是 null，所有 `?? 0` 都落到 0，分布区也走
 * 空分支 —— 屏幕上写着「暂无账号数据」，用户读到的是「我的账号没了」，而事实
 * 只是「这一次没拉到」。两件事必须分开说。
 */
function LoadFailed({ what }: { what: string }) {
  return (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description={
        <span style={{ fontSize: 13 }}>
          {what}加载失败
          <br />
          <span style={{ color: 'var(--text-muted)' }}>
            不是没有数据，是这次没拉到；点右上角「刷新」重试
          </span>
        </span>
      }
      style={{ margin: '8px 0' }}
    />
  )
}

export default function Dashboard() {
  const { message } = App.useApp()
  const [stats, setStats] = useState<any>(null)
  const [loading, setLoading] = useState(false)
  /** 本次加载是否抛错。只由 catch 置位，成功路径永远清掉 —— 空数组 ≠ 出错。 */
  const [loadFailed, setLoadFailed] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = await apiFetch('/accounts/stats')
      setStats(data)
      setLoadFailed(false)
    } catch (error) {
      // 原来这里只有 try/finally，没有 catch：异常静默冒泡，四张卡全显 0。
      const detail = error instanceof Error ? error.message : String(error)
      setLoadFailed(true)
      message.error(`总览数据加载失败：${detail}，下方数字不代表真实账号数`)
    } finally {
      setLoading(false)
    }
    // `message` 是 antd App 的 memo 化实例，身份稳定；进依赖是为了满足
    // exhaustive-deps，同时保证「刷新」按钮与 effect 用同一个 load。
  }, [message])

  useEffect(() => {
    load()
  }, [load])

  const total = stats?.total ?? 0
  // 出错时四张卡显示 '—' 而不是 0：0 是「账号数为零」这个合法值，用它表示错误
  // 等于把两种情况混成一个。
  const num = (v: number | undefined) => (loadFailed ? '—' : (v ?? 0))
  const statCards = [
    {
      title: '总账号数',
      value: num(total),
      icon: <UserOutlined />,
      color: 'var(--accent)',
    },
    {
      title: '正常',
      value: num(stats?.by_status?.registered),
      icon: <CheckCircleOutlined />,
      color: 'var(--success)',
    },
    {
      // 标题**不能叫「失效」**：这一档是「过期 + 失效」的合计，而下面的
      // 状态分布里「失效」是单独一项。实测 77（卡片）对 69（分布），
      // 同一个屏幕上两个不同的数都叫「失效」—— 用户只会认为数据坏了。
      // 改叫「待恢复」（这两类都能靠刷新/重登救回来），构成写进 ⓘ。
      title: '待恢复',
      hint: `过期 ${stats?.by_status?.expired ?? 0} + 失效 ${stats?.by_status?.invalid ?? 0}。这两类都能靠刷新 Token 或重登救回来，所以合成一档看量；下方的状态分布里仍分开列。`,
      value: loadFailed
        ? '—'
        : (stats?.by_status?.expired ?? 0) + (stats?.by_status?.invalid ?? 0),
      icon: <CloseCircleOutlined />,
      color: 'var(--danger)',
    },
    {
      title: '禁用',
      // 被封的账号（deleted or deactivated）—— 强判断，不因一次可用探测
      // 复活；与「失效」分开看量：处置方式不同（这号该弃，不重试）。
      // 紫色：红已归「失效」，橙的轻重感与「禁用更严重」倒挂。
      value: num(stats?.by_status?.banned),
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
        {statCards.map(({ title, value, icon, color, hint }, i) => (
          <Col xs={12} sm={12} lg={6} key={title}>
            <StatCard title={title} value={value} icon={icon} color={color} index={i} hint={hint} />
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
            ) : loadFailed ? (
              <LoadFailed what="平台分布" />
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
            ) : loadFailed ? (
              <LoadFailed what="状态分布" />
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
