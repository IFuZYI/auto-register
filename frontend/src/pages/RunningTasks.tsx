import { useCallback, useEffect, useState } from 'react'
import {
  App,
  Alert,
  Badge,
  Button,
  Card,
  Col,
  Drawer,
  Dropdown,
  Empty,
  Popconfirm,
  Progress,
  Row,
  Space,
  Tag,
  Typography,
} from 'antd'
import {
  DeleteOutlined,
  FileTextOutlined,
  PlayCircleOutlined,
  LoadingOutlined,
  ReloadOutlined,
  ClearOutlined,
  DownOutlined,
} from '@ant-design/icons'
import { getPlatformTagColor, getPlatformLabel } from '@/lib/platforms'
import { visibleFinishedTasks } from '@/lib/finishedTasks'
import { pollIntervalMs, shouldTickSeconds } from '@/lib/pollBackoff'
import { taskKindMeta } from '@/lib/taskKinds'
import { CLEANUP_ENDPOINTS, RETENTION_OPTIONS, retentionLabel, retentionOptionFromKey } from '@/lib/logCleanup'
import { apiFetch } from '@/lib/utils'
import { TaskLogPanel } from '@/components/TaskLogPanel'
import type { TaskKind } from '@/components/TaskLogPanel'
import { PageHeader } from '@/components/PageHeader'
import { Link } from 'react-router-dom'

const { Text } = Typography

interface TaskSnapshot {
  id: string
  platform: string
  source: string
  status: 'pending' | 'running' | 'done' | 'failed' | 'stopped'
  total: number
  progress: string
  success: number
  registered: number
  skipped: number
  errors: string[]
  created_at: number | string | null
  updated_at: number | string | null
  control: { stop_requested: boolean }
}

// `color` 用 antd Badge 的联合类型而不是 `string`：写成 string 时调用处
// 得 `as any`，eslint 的 no-explicit-any 会报错（实测两处）。
type BadgeStatus = 'default' | 'processing' | 'success' | 'error' | 'warning'
const STATUS_CONFIG: Record<string, { color: BadgeStatus; label: string; icon?: React.ReactNode }> = {
  pending: { color: 'default', label: '等待中', icon: <LoadingOutlined /> },
  running: { color: 'processing', label: '运行中', icon: <LoadingOutlined /> },
  done: { color: 'success', label: '已完成' },
  failed: { color: 'error', label: '失败' },
  stopped: { color: 'warning', label: '已停止' },
}

function toUnixSeconds(value: unknown): number | null {
  if (value === null || value === undefined) return null
  if (typeof value === 'string') {
    const trimmed = value.trim()
    if (!trimmed) return null
    const maybeNum = Number(trimmed)
    if (Number.isFinite(maybeNum)) {
      return maybeNum > 1_000_000_000_000 ? maybeNum / 1000 : maybeNum
    }
    const parsed = Date.parse(trimmed)
    if (Number.isFinite(parsed)) return parsed / 1000
    return null
  }
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  // 兼容毫秒时间戳
  return value > 1_000_000_000_000 ? value / 1000 : value
}

function formatDuration(startTs: unknown, endTs?: unknown): string {
  const start = toUnixSeconds(startTs)
  const end = toUnixSeconds(endTs) ?? (Date.now() / 1000)
  if (start === null || !Number.isFinite(end)) return '-'
  const seconds = Math.max(0, Math.floor(end - start))
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return `${h}h ${m}m`
}

export default function RunningTasks() {
  const [tasks, setTasks] = useState<TaskSnapshot[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [hasLoaded, setHasLoaded] = useState(false)
  const [logTaskId, setLogTaskId] = useState<string | null>(null)
  const [now, setNow] = useState(() => Date.now() / 1000)
  const [cleaning, setCleaning] = useState(false)
  // 已完成任务的渲染上限：实测 253 张卡片会生成 12830 个 DOM 节点，
  // 首屏主线程长任务达 6.3 秒（正常帧间隔 16ms）。默认只渲染最近 20 条，
  // 其余按需展开 —— 与日志一样，历史数据不该在首屏全量渲染。
  // 可展开**也可收起**：展开后若不能收，一次点击就让每次 2.5 秒轮询都
  // 重渲染全部卡片（实测帧间隔回到 7 秒且不再恢复）。
  const [showAllFinished, setShowAllFinished] = useState(false)
  const { modal, message: msg } = App.useApp()

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const data = (await apiFetch('/tasks')) as TaskSnapshot[]
      // sort: running first, then pending, then finished (newest first)
      const order = { running: 0, pending: 1, done: 2, failed: 3, stopped: 4 }
      const sorted = [...(data || [])].sort((a, b) => {
        const oa = order[a.status] ?? 9
        const ob = order[b.status] ?? 9
        if (oa !== ob) return oa - ob
        const ta = toUnixSeconds(a.created_at) ?? 0
        const tb = toUnixSeconds(b.created_at) ?? 0
        return tb - ta
      })
      setTasks(sorted)
      setLoadError('')
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      setLoadError(detail)
      msg.error({ content: `任务列表加载失败：${detail}。请刷新重试。`, key: 'task-list-load-error' })
    } finally {
      setLoading(false)
      setHasLoaded(true)
    }
  }, [msg])

  // 轮询间隔跟着「有没有运行中任务」走：实测 715 个任务全结束时页面仍每
  // 2.5 秒拉一次大响应，60 秒内 24 次请求而内容没变（见 lib/pollBackoff.ts）。
  const hasActiveForPolling = tasks.some(
    (t) => t.status === 'running' || t.status === 'pending',
  )

  useEffect(() => {
    load()
    const poll = setInterval(() => {
      load()
      if (shouldTickSeconds(hasActiveForPolling)) setNow(Date.now() / 1000)
    }, pollIntervalMs(hasActiveForPolling))
    // 每秒刷新「已运行时长」：只在有运行中任务时开 —— 没有秒级变化的数字时
    // 每秒 setState 只是让整棵树空转重渲染。
    //
    // 任务刚从 running 变 done 时（`hasActiveForPolling` 由 true 转 false），
    // effect 重建、立刻 fetch，但 tick 不再重建 —— 最后一次「已运行时长」
    // 会停在旧值直到下一次轮询（最多一个 idle 间隔 15 秒）。这里在切换时
    // 补一次 setNow，让终态时长立刻正确。
    const tick = shouldTickSeconds(hasActiveForPolling)
      ? setInterval(() => setNow(Date.now() / 1000), 1000)
      : null
    if (!hasActiveForPolling) setNow(Date.now() / 1000)
    return () => {
      clearInterval(poll)
      if (tick !== null) clearInterval(tick)
    }
  }, [load, hasActiveForPolling])

  const isActive = (t: TaskSnapshot) => t.status === 'running' || t.status === 'pending'
  const activeTasks = tasks.filter(isActive)
  const finishedTasks = tasks.filter((t) => !isActive(t))
  // 折叠规则放在 `lib/finishedTasks.ts` 里（真实执行、有测试）：
  // 直接 `slice(0, 20)` 会把**刚失败**的任务挤到后面藏起来 ——
  // 后端排序是「先状态档位再时间」，failed 永远排在 done 之后。
  const visibleFinished = visibleFinishedTasks(finishedTasks, showAllFinished)
  const logTask = logTaskId ? tasks.find((task) => task.id === logTaskId) : null

  const handleDelete = async (taskId: string) => {
    try {
      await apiFetch(`/tasks/${taskId}`, { method: 'DELETE' })
      if (logTaskId === taskId) setLogTaskId(null)
      setTasks((prev) => prev.filter((t) => t.id !== taskId))
      msg.success('任务已删除')
    } catch (error) {
      const detail = error instanceof Error ? error.message : '删除失败'
      msg.error(detail)
    }
  }

  const handleCleanup = (days: number) => {
    modal.confirm({
      title: `清理${retentionLabel(days)}的任务记录？`,
      content: '仅清理已结束的任务；进行中的任务与保留期内的记录不受影响。',
      okText: '清理',
      cancelText: '取消',
      okButtonProps: { danger: true },
      onOk: async () => {
        setCleaning(true)
        try {
          const result = (await apiFetch(CLEANUP_ENDPOINTS.runs, {
            method: 'POST',
            body: JSON.stringify({ older_than_days: days }),
          })) as { deleted: number }
          msg.success(`已清理 ${result.deleted} 条任务记录（保留 ${retentionLabel(days)}）`)
        } catch (error) {
          msg.error(error instanceof Error ? error.message : '清理失败')
        } finally {
          setCleaning(false)
        }
        // 刷新放在 try 之外：清理已经成功（上面已提示），刷新失败不该再报「清理失败」。
        await load().catch(() => {})
      },
    })
  }

  const renderTask = (task: TaskSnapshot) => {
    const cfg = STATUS_CONFIG[task.status] || { color: 'default', label: task.status }
    const failed = task.errors?.length ?? 0
    const totalRaw = Number(task.total)
    const doneRaw = Number(task.registered)
    const success = Number.isFinite(Number(task.success)) ? Number(task.success) : 0
    const skipped = Number.isFinite(Number(task.skipped)) ? Number(task.skipped) : 0
    const total = Number.isFinite(totalRaw) && totalRaw > 0 ? Math.floor(totalRaw) : 0
    const done = Number.isFinite(doneRaw) && doneRaw > 0 ? Math.floor(doneRaw) : 0
    const pct = total > 0 ? Math.max(0, Math.min(100, Math.round((done / total) * 100))) : 0

    const duration = isActive(task)
      ? formatDuration(task.created_at, now)
      : formatDuration(task.created_at, task.updated_at)

    return (
      <Card
        key={task.id}
        size="small"
        className="app-card"
        style={{ marginBottom: 12 }}
        styles={{ body: { padding: '14px 18px' } }}
      >
        <Row gutter={[12, 8]} align="middle" wrap>
          {/* Task ID + platform + kind */}
          <Col flex="240px">
            <Space direction="vertical" size={2}>
              <Text code style={{ fontSize: 11 }}>
                {task.id}
              </Text>
              <Space size={4}>
                <Tag color={getPlatformTagColor(task.platform)} style={{ margin: 0 }}>
                  {getPlatformLabel(task.platform)}
                </Tag>
                {(() => {
                  const kind = taskKindMeta(task.source)
                  return (
                    <Tag color={kind.color} style={{ margin: 0 }}>
                      {kind.label}
                    </Tag>
                  )
                })()}
              </Space>
            </Space>
          </Col>

          {/* Status */}
          <Col flex="90px">
            <Badge status={cfg.color} text={cfg.label} />
          </Col>

          {/* Duration */}
          <Col flex="70px">
            <Text type="secondary" style={{ fontSize: 12 }}>
              ⏱ {duration}
            </Text>
          </Col>

          {/* Progress bar */}
          <Col flex="1" style={{ minWidth: 160 }}>
            <Space direction="vertical" size={2} style={{ width: '100%' }}>
                <Progress
                  percent={pct}
                  size="small"
                status={
                  task.status === 'failed'
                    ? 'exception'
                    : task.status === 'done'
                      ? 'success'
                      : task.status === 'stopped'
                        ? 'exception'
                        : 'active'
                }
                format={() => `${done}/${total}`}
              />
              <Space size={8}>
                <Text style={{ fontSize: 11, color: 'var(--success)' }}>
                  ✓ 成功 {success}
                </Text>
                {failed > 0 && (
                  <Text style={{ fontSize: 11, color: 'var(--danger)' }}>
                    ✗ 失败 {failed}
                  </Text>
                )}
                {skipped > 0 && (
                  <Text style={{ fontSize: 11, color: 'var(--warning)' }}>
                    → 跳过 {skipped}
                  </Text>
                )}
              </Space>
            </Space>
          </Col>

          {/* Log button */}
          <Col>
            <Space>
              <Button
                size="small"
                icon={<FileTextOutlined />}
                onClick={() => setLogTaskId(task.id)}
              >
                查看日志
              </Button>
              {!isActive(task) && (
                <Popconfirm
                  title="删除这条任务记录？"
                  description="仅删除任务记录和日志，不影响已注册的账号。删除后无法恢复。"
                  okText="删除"
                  cancelText="取消"
                  okButtonProps={{ danger: true }}
                  onConfirm={() => handleDelete(task.id)}
                >
                  <Button size="small" danger icon={<DeleteOutlined />}>
                    删除
                  </Button>
                </Popconfirm>
              )}
            </Space>
          </Col>
        </Row>
      </Card>
    )
  }

  /**
   * 已完成任务的**紧凑行**。
   *
   * 改造前 464 条已完成任务也是一条一张卡（实测 90~100px/张）：一屏 5~6 条，
   * 翻到底要 80 多屏。而「已完成」这一段的用途是**查**（哪个任务跑过、结果
   * 怎样），不是盯进度 —— 一张卡的信息量（进度条、两行元信息）在这里全是浪费。
   *
   * 进行中的任务仍然用完整卡片：那里进度条和实时时长是有用的。
   */
  const renderCompactTask = (task: TaskSnapshot) => {
    const cfg = STATUS_CONFIG[task.status] || { color: 'default', label: task.status }
    const failed = task.errors?.length ?? 0
    const totalRaw = Number(task.total)
    const doneRaw = Number(task.registered)
    const success = Number.isFinite(Number(task.success)) ? Number(task.success) : 0
    const skipped = Number.isFinite(Number(task.skipped)) ? Number(task.skipped) : 0
    const total = Number.isFinite(totalRaw) && totalRaw > 0 ? Math.floor(totalRaw) : 0
    const done = Number.isFinite(doneRaw) && doneRaw > 0 ? Math.floor(doneRaw) : 0
    const duration = formatDuration(task.created_at, task.updated_at)
    const kind = taskKindMeta(task.source)

    return (
      <div key={task.id} className="task-row">
        <span className="task-row__id" title={task.id}>{task.id}</span>
        <span className="task-row__tags">
          <Tag color={getPlatformTagColor(task.platform)} style={{ margin: 0 }}>
            {getPlatformLabel(task.platform)}
          </Tag>
          <Tag color={kind.color} style={{ margin: 0 }}>{kind.label}</Tag>
        </span>
        <span className="task-row__status">
          <Badge status={cfg.color} text={cfg.label} />
        </span>
        <span className="task-row__meta">⏱ {duration}</span>
        <span className="task-row__meta">
          {done}/{total}
          <span style={{ color: 'var(--success)', marginLeft: 8 }}>✓ {success}</span>
          {failed > 0 ? <span style={{ color: 'var(--danger)', marginLeft: 6 }}>✗ {failed}</span> : null}
          {skipped > 0 ? <span style={{ color: 'var(--warning)', marginLeft: 6 }}>→ {skipped}</span> : null}
        </span>
        <span className="task-row__actions">
          <Button type="link" size="small" style={{ paddingInline: 4 }} onClick={() => setLogTaskId(task.id)}>
            查看日志
          </Button>
          <Popconfirm
            title="确认删除该任务记录？"
            okText="删除"
            cancelText="取消"
            okButtonProps={{ danger: true }}
            onConfirm={() => handleDelete(task.id)}
          >
            <Button type="link" size="small" danger style={{ paddingInline: 4 }}>删除</Button>
          </Popconfirm>
        </span>
      </div>
    )
  }

  return (
    <div>
      <PageHeader
        title="任务中心"
        subtitle={
          !hasLoaded
            ? '正在读取任务状态…'
            : loadError
            ? '任务状态未能更新，以下显示的是最近一次成功读取的内容。'
            : activeTasks.length > 0
            ? `${activeTasks.length} 个任务待处理或进行中，共 ${tasks.length} 个任务。`
            : `当前没有进行中任务，共 ${tasks.length} 条任务记录。`
        }
        actions={
          <>
            <Dropdown
              disabled={cleaning}
              menu={{
                items: RETENTION_OPTIONS.map((option) => ({
                  key: String(option.days),
                  label: `清理 ${option.label}`,
                })),
                onClick: ({ key }) => {
                  const option = retentionOptionFromKey(key)
                  if (option) handleCleanup(option.days)
                },
              }}
            >
              <Button icon={<ClearOutlined />} loading={cleaning}>
                清理任务记录 <DownOutlined />
              </Button>
            </Dropdown>
            <Button icon={<ReloadOutlined />} loading={loading} onClick={load}>
              刷新任务
            </Button>
          </>
        }
      />

      {loadError ? (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="无法读取最新任务状态"
          description={`${loadError}。${tasks.length ? '当前列表可能不是最新状态，' : ''}请检查服务连接后重试。`}
          action={<Button size="small" onClick={() => void load()} loading={loading}>重试</Button>}
        />
      ) : null}

      {/* Active tasks */}
      {activeTasks.length > 0 && (
        <div style={{ marginBottom: 28 }}>
          <Text
            strong
            className="task-section-title"
            style={{ color: 'var(--accent-text)' }}
          >
            进行中（{activeTasks.length}）
          </Text>
          {activeTasks.map(renderTask)}
        </div>
      )}

      {/* Finished tasks */}
      {finishedTasks.length > 0 && (
        <div>
          <Text className="task-section-title" style={{ color: 'var(--text-secondary)' }}>
            已结束（{finishedTasks.length}）
          </Text>
          <div className="task-history-list">
            {visibleFinished.map(renderCompactTask)}
          </div>
          {finishedTasks.length > visibleFinished.length && (
            <Button
              block
              type="dashed"
              className="task-history-list__more"
              onClick={() => setShowAllFinished((prev) => !prev)}
            >
              {showAllFinished
                ? '收起'
                : `展开其余 ${finishedTasks.length - visibleFinished.length} 条已完成任务`}
            </Button>
          )}
        </div>
      )}

      {tasks.length === 0 && hasLoaded && !loading && !loadError && (
        <div className="task-empty">
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="还没有任务。开始一次注册后，可在这里查看进度与日志。"
          />
          <div style={{ textAlign: 'center' }}>
            <Link to="/register" className="task-empty__action"><Button type="primary" icon={<PlayCircleOutlined />}>开始注册</Button></Link>
          </div>
        </div>
      )}

      {/* Log drawer */}
      <Drawer
        title={
          <Space>
            <FileTextOutlined />
            <span>任务日志</span>
            {logTaskId && (
              <Text code style={{ fontSize: 11 }}>
                {logTaskId}
              </Text>
            )}
          </Space>
        }
        open={!!logTaskId}
        onClose={() => setLogTaskId(null)}
        width={720}
        destroyOnClose
        styles={{ body: { padding: 16 } }}
      >
        {logTaskId && <TaskLogPanel taskId={logTaskId} kind={(logTask?.source || 'register') as TaskKind} />}
      </Drawer>
    </div>
  )
}
