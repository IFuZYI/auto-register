import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Button, Empty, Skeleton, App } from 'antd'
import {
  ArrowRightOutlined,
  CheckCircleFilled,
  PlusOutlined,
  ReloadOutlined,
} from '@ant-design/icons'
import { getPlatformColor, getPlatformLabel } from '@/lib/platforms'
import { accountStatusMeta } from '@/lib/accountFormat'
import { summarizeDashboardTasks } from '@/lib/dashboardTasks'
import { createDashboardDataLoader } from '@/lib/dashboardData'
import { readAttention } from '@/lib/attention'
import { AttentionBar } from '@/components/AttentionBar'
import { apiFetch } from '@/lib/utils'
import { PageHeader } from '@/components/PageHeader'
import { HelpTip } from '@/components/settings/SettingsPrimitives'

/** 状态色与表格的标签共用语义色，不只靠颜色区分，旁边始终有名称与数量。 */
function statusColor(status: string): string {
  switch (String(status || '').toLowerCase()) {
    case 'registered': return 'var(--success)'
    case 'expired':
    case 'limited': return 'var(--warning)'
    case 'invalid': return 'var(--danger)'
    case 'banned': return 'var(--danger)'
    default: return 'var(--text-muted)'
  }
}

function LoadFailed({ what, retry }: { what: string; retry: () => void }) {
  return (
    <div className="dash-error" role="alert">
      <strong>无法读取{what}</strong>
      <span>请检查服务连接后重试，当前不会显示为 0。</span>
      <Button size="small" onClick={retry}>重试</Button>
    </div>
  )
}

interface RowItem {
  key: string
  label: string
  value: number
  color: string
  hint?: string
}

function DistributionRows({ items, total, linkedPlatforms = false }: { items: RowItem[]; total: number; linkedPlatforms?: boolean }) {
  return (
    <>
      {items.map((item) => {
        const pct = total ? Math.round((item.value / total) * 100) : 0
        return (
          <div className="dash-row" key={item.key}>
            <div className="dash-row__top">
              {linkedPlatforms ? (
                <Link className="dash-row__label dash-row__label--link" to={`/accounts/${item.key}`}>
                  <i className="dash-dot" style={{ background: item.color }} />
                  {item.label}
                  {item.hint ? <HelpTip text={item.hint} /> : null}
                </Link>
              ) : (
                <span className="dash-row__label">
                  <i className="dash-dot" style={{ background: item.color }} />
                  {item.label}
                  {item.hint ? <HelpTip text={item.hint} /> : null}
                </span>
              )}
              <span className="dash-row__value">
                <b>{item.value.toLocaleString()}</b>
                <span>{pct}%</span>
              </span>
            </div>
            <div className="dash-row__track" aria-hidden="true">
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
  const navigate = useNavigate()
  const [loader] = useState(() => createDashboardDataLoader({
    fetchStats: signal => apiFetch('/accounts/stats', { signal }),
    fetchTasks: signal => apiFetch('/tasks', { signal }),
  }))
  const [data, setData] = useState(loader.getSnapshot)
  const [refreshing, setRefreshing] = useState(false)
  const requestGeneration = useRef(0)
  const { stats, statsLoaded, statsFailed, tasks, tasksLoaded, tasksFailed } = data
  const statsLoading = !statsLoaded
  const tasksLoading = !tasksLoaded

  const load = useCallback(async (announce = false) => {
    const request = ++requestGeneration.current
    if (announce) setRefreshing(true)
    const result = await loader.load(next => {
      if (request === requestGeneration.current) setData(next)
    })
    if (request !== requestGeneration.current) return
    setRefreshing(false)
    if (announce && result.applied) {
      if (result.statsError) message.error(`账号数据加载失败：${result.statsError}`)
      if (result.tasksError) message.error(`任务状态加载失败：${result.tasksError}`)
    }
  }, [loader, message])

  const taskSummary = useMemo(() => summarizeDashboardTasks(tasks), [tasks])
  const activeTasks = taskSummary.running + taskSummary.pending
  const hasActiveTasks = activeTasks > 0

  useEffect(() => {
    const initial = window.setTimeout(() => void load(), 0)
    // 有任务时及时看到进度；空闲时降频，后台标签页不继续轮询。
    //
    // **上一轮还没回来就不发新的。** `/api/accounts/stats` 实测要 3.5–6s：
    // 只要它还在路上就重发，新请求会 abort 掉旧的，那一半数据永远不落地，
    // 界面停在骨架屏（实测抓到 net::ERR_ABORTED）。让它自然完成。
    const timer = window.setInterval(() => {
      if (document.visibilityState !== 'visible') return
      if (loader.isBusy()) return
      void load()
    }, hasActiveTasks ? 15000 : 60000)
    return () => {
      window.clearTimeout(initial)
      window.clearInterval(timer)
    }
  }, [load, hasActiveTasks])

  useEffect(() => () => {
    requestGeneration.current += 1
    loader.dispose()
  }, [loader])

  const total = stats?.total ?? 0
  const registered = stats?.by_status?.registered ?? 0
  const recover = (stats?.by_status?.expired ?? 0) +
    (stats?.by_status?.invalid ?? 0) + (stats?.by_status?.limited ?? 0)
  const banned = stats?.by_status?.banned ?? 0
  const availability = total ? Math.round((registered / total) * 100) : 0
  const num = (value: number) => statsFailed || (statsLoading && stats === null)
    ? '—'
    : value.toLocaleString()

  const segments: RowItem[] = [
    { key: 'registered', label: '正常可用', value: registered, color: 'var(--success)' },
    {
      key: 'recover',
      label: '待恢复',
      value: recover,
      color: 'var(--warning)',
      hint: `过期 ${stats?.by_status?.expired ?? 0}，失效 ${stats?.by_status?.invalid ?? 0}，限流 ${stats?.by_status?.limited ?? 0}。这些账号可在账号管理中刷新凭据或等待额度恢复。`,
    },
    { key: 'banned', label: '已禁用', value: banned, color: 'var(--danger)' },
  ]
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
      label: status === 'registered' ? '正常可用' : accountStatusMeta(status).label,
      value: Number(count) || 0,
      color: statusColor(status),
    }))
    .sort((a, b) => b.value - a.value)

  // 「现在要紧吗」的一句话结论。数据未加载完成时传 loaded:false，
  // 让它说「正在读取」而不是报「一切正常」—— 后者是用户会信任的错误结论。
  const attention = readAttention({
    // 两个接口速度差一个数量级（任务 0.5s / 账号统计 2–4s）：分开传，
    // 让「有任务在跑」这类结论不被慢接口拖住。
    tasksLoaded,
    statsLoaded,
    total,
    byStatus: stats?.by_status ?? {},
    running: taskSummary.running,
    queued: taskSummary.pending,
    failed: taskSummary.failed,
  })

  return (
    <div className="dashboard-page">
      <PageHeader
        title="工作台"
        subtitle="查看账号可用性，跟进注册任务与面板对接。"
        actions={
          <>
            <Button icon={<ReloadOutlined spin={refreshing} />} onClick={() => void load(true)} loading={refreshing}>
              刷新数据
            </Button>
            <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/register')}>
              注册账号
            </Button>
          </>
        }
      />

      <AttentionBar reading={attention} />

      <div className="dashboard-focus">
        <section className="inventory" aria-labelledby="inventory-title" aria-busy={statsLoading && !stats}>
          <div className="inventory__head">
            <div className="inventory__title-row">
              <h2 className="inventory__label" id="inventory-title">账号可用性</h2>
              <span>共 {num(total)} 个账号</span>
            </div>
            <div className="inventory__legend">
              {barSegments.map((item) => (
                <div className={`inventory__legend-item inventory__legend-item--${item.key}`} key={item.key}>
                  <span className="inventory__legend-label">
                    <i className="dash-dot" style={{ background: item.color }} />
                    {item.label}
                    {item.hint ? <HelpTip text={item.hint} /> : null}
                  </span>
                  <b>{num(item.value)}</b>
                  <span className="inventory__legend-caption">
                    {item.key === 'registered' ? '按当前状态可使用' : item.key === 'recover' ? '需要恢复或等待额度' : item.key === 'banned' ? '已禁用的账号' : '其他状态'}
                  </span>
                </div>
              ))}
            </div>
          </div>
          {statsLoading || statsFailed ? (
            <div className="inventory__unavailable" role={statsFailed ? 'status' : undefined}>
              <span>{statsLoading ? '正在读取账号状态…' : '暂时无法计算可用比例，请重试。'}</span>
              {statsFailed && <Button size="small" onClick={() => void load(true)}>重试</Button>}
            </div>
          ) : (
            <>
              <div className="inventory__bar" aria-hidden="true">
                {barSegments.map((item) => (
                  <span key={item.key} style={{ flexGrow: item.value || 0, background: item.color }} />
                ))}
              </div>
              <div className="inventory__foot">
                <span><CheckCircleFilled /> 正常账号占比</span>
                <strong>{availability}%</strong>
                <span className="inventory__foot-note">按当前账号状态计算</span>
              </div>
            </>
          )}
        </section>

        <section className="task-summary" aria-labelledby="task-summary-title" aria-busy={tasksLoading && !tasks.length}>
          <div className="dash-section__head">
            <h2 id="task-summary-title">任务状态</h2>
            <span className={`live-status${hasActiveTasks ? ' is-active' : ''}`}>
              <i />{tasksFailed ? '状态未知' : hasActiveTasks ? '执行中' : '队列空闲'}
            </span>
          </div>
          {tasksLoading && !tasks.length ? (
            <Skeleton active paragraph={{ rows: 3 }} title={false} className="dashboard-skeleton" />
          ) : tasksFailed ? (
            <LoadFailed what="任务状态" retry={() => void load(true)} />
          ) : (
            <div className="task-summary__metrics">
              <Link to="/running-tasks" className="task-summary__metric">
                <span className="task-summary__metric-label"><i className="is-running" />进行中</span>
                <strong>{taskSummary.running}</strong>
                <span>查看实时进度</span>
              </Link>
              <Link to="/running-tasks" className="task-summary__metric">
                <span className="task-summary__metric-label"><i className="is-pending" />等待中</span>
                <strong>{taskSummary.pending}</strong>
                <span>查看任务队列</span>
              </Link>
              <Link to="/history" className={`task-summary__metric${taskSummary.failed ? ' has-failures' : ''}`}>
                <span className="task-summary__metric-label"><i className="is-failed" />失败</span>
                <strong>{taskSummary.failed}</strong>
                <span>查看注册结果</span>
              </Link>
            </div>
          )}
          <div className="task-summary__foot">
            <span>{tasksFailed ? '任务信息尚未更新，请重试。' : hasActiveTasks ? '进行中任务每 15 秒更新' : '查看进度、日志或停止任务'}</span>
            <Link to="/running-tasks" aria-label="查看全部任务"><ArrowRightOutlined /></Link>
          </div>
        </section>
      </div>

      <div className="dash-grid">
        <section className="dash-section" aria-labelledby="platform-title" aria-busy={statsLoading && !stats}>
          <div className="dash-section__head">
            <h2 id="platform-title">各平台账号</h2>
            <span className="dash-section__hint">点击平台管理账号</span>
          </div>
          {statsLoading && !stats ? (
            <Skeleton active paragraph={{ rows: 4 }} title={false} className="dashboard-skeleton" />
          ) : statsFailed ? (
            <LoadFailed what="账号分布" retry={() => void load(true)} />
          ) : platformItems.length ? (
            <DistributionRows items={platformItems} total={total} linkedPlatforms />
          ) : (
            <div className="dashboard-empty">
              <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="还没有账号，注册第一批账号开始使用。" />
              <Link to="/register">注册账号 <ArrowRightOutlined /></Link>
            </div>
          )}
        </section>

        <section className="dash-section" aria-labelledby="status-title" aria-busy={statsLoading && !stats}>
          <div className="dash-section__head">
            <h2 id="status-title">账号状态明细</h2>
            <span>{statsFailed ? '状态未知' : statsLoading ? '正在读取' : `${statusItems.length} 种状态`}</span>
          </div>
          {statsLoading && !stats ? (
            <Skeleton active paragraph={{ rows: 4 }} title={false} className="dashboard-skeleton" />
          ) : statsFailed ? (
            <LoadFailed what="账号状态" retry={() => void load(true)} />
          ) : statusItems.length ? (
            <DistributionRows items={statusItems} total={total} />
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="注册与检测结果会汇总到这里" />
          )}
        </section>
      </div>
    </div>
  )
}
