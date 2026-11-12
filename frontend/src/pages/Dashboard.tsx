import { useCallback, useEffect, useState } from 'react'
import { Button, Empty, Skeleton, App } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { getPlatformColor, getPlatformLabel } from '@/lib/platforms'
import { accountStatusMeta } from '@/lib/accountFormat'
import { apiFetch } from '@/lib/utils'
import { PageHeader } from '@/components/PageHeader'
import { HelpTip } from '@/components/settings/SettingsPrimitives'

/** 状态色：与表格里的 Tag 同一套语义，但这里用点 + 细条，不用药丸。 */
function statusColor(status: string): string {
  switch (String(status || '').toLowerCase()) {
    case 'registered':
      return 'var(--success)'
    case 'expired':
    case 'limited':
      return 'var(--warning)'
    case 'invalid':
      return 'var(--danger)'
    case 'banned':
      return 'var(--purple)'
    default:
      return 'var(--text-muted)'
  }
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

interface RowItem {
  key: string
  label: string
  value: number
  color: string
  hint?: string
}

function DistributionRows({ items, total }: { items: RowItem[]; total: number }) {
  return (
    <>
      {items.map((item) => {
        const pct = total ? Math.round((item.value / total) * 100) : 0
        return (
          <div className="dash-row" key={item.key}>
            <div className="dash-row__top">
              <span className="dash-row__label">
                <i className="dash-dot" style={{ background: item.color }} />
                {item.label}
                {item.hint ? <HelpTip text={item.hint} /> : null}
              </span>
              <span className="dash-row__value">
                {item.value}
                <span>{pct}%</span>
              </span>
            </div>
            <div className="dash-row__track">
              <span style={{ width: `${pct}%`, background: item.color }} />
            </div>
          </div>
        )
      })}
    </>
  )
}

export default function Dashboard() {
  const { message } = App.useApp()
  const [stats, setStats] = useState<any>(null)
  // 初值就是 true：effect 要等 paint 之后才跑，false 会让首帧先亮一个 0。
  const [loading, setLoading] = useState(true)
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
  }, [message])

  useEffect(() => {
    load()
  }, [load])

  const total = stats?.total ?? 0
  // 首屏还在加载时不要先亮一个 0：0 是「真的没有账号」，加载中是「还不知道」。
  const num = (v: number | undefined) =>
    loadFailed || (loading && stats === null) ? '—' : (v ?? 0)

  // 三档状态：正常 / 待恢复（过期+失效）/ 禁用。与下方「状态分布」的明细
  // 口径一致：合计项写明构成，别让两个同名数字对不上。
  const segments: RowItem[] = [
    {
      key: 'registered',
      label: '正常',
      value: stats?.by_status?.registered ?? 0,
      color: 'var(--success)',
    },
    {
      key: 'recover',
      label: '待恢复',
      // 限流也是「等额度恢复/刷新」这一档，不能漏（漏了条带就加不出总数）
      value:
        (stats?.by_status?.expired ?? 0) +
        (stats?.by_status?.invalid ?? 0) +
        (stats?.by_status?.limited ?? 0),
      color: 'var(--warning)',
      hint: `过期 ${stats?.by_status?.expired ?? 0} + 失效 ${stats?.by_status?.invalid ?? 0} + 限流 ${stats?.by_status?.limited ?? 0}。这些都能靠刷新 Token 或等额度恢复救回来，所以合成一档看量；下方的状态分布里仍分开列。`,
    },
    {
      key: 'banned',
      label: '禁用',
      value: stats?.by_status?.banned ?? 0,
      color: 'var(--purple)',
    },
  ]

  // 段和必须等于总数：出现没列进三档的状态时补一段「其他」，
  // 否则 100% 宽的条带只代表部分账号（图例也加不出上面的总数）。
  const covered = segments.reduce((sum, item) => sum + item.value, 0)
  const remainder = Math.max(total - covered, 0)
  const barSegments = remainder
    ? [...segments, { key: 'other', label: '其他', value: remainder, color: 'var(--text-muted)' }]
    : segments

  const platformItems: RowItem[] = Object.entries(stats?.by_platform || {})
    .map(([platform, count]) => ({
      key: platform,
      label: getPlatformLabel(platform),
      value: Number(count) || 0,
      color: getPlatformColor(platform),
    }))
    .sort((a, b) => b.value - a.value)

  const statusItems: RowItem[] = Object.entries(stats?.by_status || {})
    .map(([status, count]) => ({
      key: status,
      label: accountStatusMeta(status).label,
      value: Number(count) || 0,
      color: statusColor(status),
    }))
    .sort((a, b) => b.value - a.value)

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

      <section className="inventory" aria-label="账号总览">
        <div className="inventory__head">
          <div>
            <div className="inventory__label">总账号数</div>
            <div className="inventory__value">{num(total)}</div>
          </div>
          <div className="inventory__legend">
            {barSegments.map((item) => (
              <span className="inventory__legend-item" key={item.key}>
                <i className="dash-dot" style={{ background: item.color }} />
                {item.label}
                {item.hint ? <HelpTip text={item.hint} /> : null}
                <b>{num(item.value)}</b>
              </span>
            ))}
          </div>
        </div>
        <div className="inventory__bar" aria-hidden>
          {barSegments.map((item) => (
            <span
              key={item.key}
              style={{
                flexGrow: item.value || 0,
                background: item.color,
                opacity: item.value ? 1 : 0.25,
              }}
            />
          ))}
        </div>
      </section>

      <div className="dash-grid">
        <section className="dash-section" aria-label="平台分布">
          <div className="dash-section__head">
            <h2>平台分布</h2>
            <span>{platformItems.length} 个平台</span>
          </div>
          {loading && !stats ? (
            <Skeleton active paragraph={{ rows: 4 }} title={false} style={{ marginTop: 18 }} />
          ) : loadFailed ? (
            <LoadFailed what="平台分布" />
          ) : platformItems.length ? (
            <DistributionRows items={platformItems} total={total} />
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
        </section>

        <section className="dash-section" aria-label="状态分布">
          <div className="dash-section__head">
            <h2>状态分布</h2>
            <span>{statusItems.length} 种状态</span>
          </div>
          {loading && !stats ? (
            <Skeleton active paragraph={{ rows: 4 }} title={false} style={{ marginTop: 18 }} />
          ) : loadFailed ? (
            <LoadFailed what="状态分布" />
          ) : statusItems.length ? (
            <DistributionRows items={statusItems} total={total} />
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
        </section>
      </div>
    </div>
  )
}
