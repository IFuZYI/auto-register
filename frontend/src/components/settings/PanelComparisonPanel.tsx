import { useCallback, useEffect, useMemo, useState } from 'react'
import { App, Button, Empty, Popconfirm, Segmented, Space, Spin, Table, Tag, Tooltip, Typography } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  CloudDownloadOutlined,
  CloudUploadOutlined,
  SyncOutlined,
} from '@ant-design/icons'
import { apiFetch } from '@/lib/utils'
import { formatLocalTime, localTimezoneLabel } from '@/lib/time'
import {
  countByPlatform,
  filterRowsByPlatform,
  selectPullIds,
  selectPushIds,
  shouldShowPlatformFilter,
  summarizeRows,
} from '@/lib/panelComparison'

/**
 * 选中面板后的本地管理面板：本地账号 ↔ 远端账号的对比 + 面板操作。
 *
 * 数据来自 `GET /api/integrations/panels/{key}/comparison`（带缓存，
 * `refresh=1` 绕过）；对比口径与方向规则见 `docs/API_REFERENCE.md` 的
 * 「集成服务」一节。动作走 `POST /api/actions/{platform}/{action_id}/batch`
 * （同步状态）与 `POST /api/integrations/panels/{key}/{sync,push}`
 * （更新本地/远程凭证），账号 ID 由对比行的 `local_id` 提供。
 */

interface ComparisonRow {
  email: string
  /** 这一行属于哪个平台（CPA 同时托管 ChatGPT 与 Grok） */
  platform: string
  state: string
  label: string
  local_id: number | null
  local_status: string
  local_updated_at: string
  local_updated_hour: string
  local_has_refresh_token: boolean
  remote_id: string
  remote_status: string
  remote_updated_at: string
  remote_updated_at_raw: string
  remote_updated_hour: string
  remote_extra: Record<string, unknown>
  differences: string[]
  /** 凭证比对的差异字段（access_token / refresh_token …） */
  credential_differences: string[]
  /** 凭证实际比了几个字段（0 = 比不了） */
  credential_compared: number
  /** 谁更新（按小时）：local_newer / remote_newer / time_synced / '' */
  time_relation: string
  /** time_relation 的依据：credential（凭证签发时间）/ record（记录时间）/ '' */
  time_basis: string
  /** 本地 AT 生成时间（JWT iat）—— 「本地更新时间」列显示它，空则回落记录时间 */
  local_credential_issued_at: string
  /** 远端 AT 生成时间（JWT iat）—— 「远端更新时间」列显示它，空则回落记录时间 */
  remote_credential_issued_at: string
}

interface ComparisonPayload {
  panel: string
  fetched_at: string
  cached: boolean
  summary: Record<string, number>
  /** 状态名 → 中文标签（后端 `services/panel_comparison.py` 的 STATE_LABELS） */
  labels: Record<string, string>
  rows: ComparisonRow[]
  remote_error: string
  local_count: number
  remote_count: number
}

/** 状态 → 颜色。红/黄表示需要动作，绿表示已同步。 */
const STATE_COLORS: Record<string, string> = {
  local_only: 'warning',
  remote_only: 'processing',
  credential_diff: 'error',
  unknown_credential: 'default',
  synced: 'success',
  unknown_time: 'default',
}

/** 平台名 → 展示文案（CPA 这类多平台面板的对比行要标出平台）。 */
const PLATFORM_LABELS: Record<string, string> = {
  chatgpt: 'ChatGPT',
  grok: 'Grok',
}

/**
 * 状态筛选条的展示顺序（标签文案来自后端 `labels`）。
 *
 * 不含 `unknown_time` —— 它永远不会是行状态（时间只是辅助信息），
 * 摆成筛选项会恒为 0。
 */
const STATE_ORDER = [
  'local_only',
  'remote_only',
  'credential_diff',
  'unknown_credential',
  'synced',
]

/** 时间串 → 浏览器本地时区显示（远端时区可能不同，后端已归一）。 */
function shortTime(value: string): string {
  return formatLocalTime(value)
}

/** 批量动作单次上限（与后端 `_resolve_batch_accounts` 的 1000 一致）。 */
const BATCH_ACTION_LIMIT = 1000

/**
 * 可键盘操作的状态筛选标签。
 *
 * antd 的 `Tag.CheckableTag` 只绑了 `onClick` —— 渲染出的 `<span>` 既没有
 * `tabIndex` 也没有 `role`，键盘用户完全用不了这排筛选（Tab 遍历会整组跳过，
 * Enter/Space 也没有任何反应）。`CheckableTag` 会把 restProps 透传到 span 上，
 * 所以这里补上 `role` / `tabIndex` 与 Enter/Space 处理即可，不用换组件。
 *
 * 类型上要放宽：`CheckableTagProps` 没声明 HTML 属性（虽然运行时会透传），
 * 直接用会报 "Property 'role' does not exist"。
 */
const CheckableTag = Tag.CheckableTag as React.ComponentType<
  React.ComponentProps<typeof Tag.CheckableTag> & React.HTMLAttributes<HTMLSpanElement>
>

function FilterTag({
  label,
  checked,
  onSelect,
}: {
  label: string
  checked: boolean
  onSelect: () => void
}) {
  return (
    <CheckableTag
      checked={checked}
      onChange={onSelect}
      role="button"
      tabIndex={0}
      aria-pressed={checked}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          onSelect()
        }
      }}
    >
      {label}
    </CheckableTag>
  )
}

/**
 * 表格列定义。
 *
 * 放模块级而不是组件里：这些 render 只用模块级的 `shortTime` / `STATE_COLORS`
 * 与 antd 组件，不读任何 props/state —— 摆在组件体内会让每次渲染都重建整份
 * 列数组与所有闭包（对比面板本身有 7 个筛选项 + 分页，一次交互就多轮渲染）。
 */
const COLUMNS: ColumnsType<ComparisonRow> = [
  {
    title: '平台',
    dataIndex: 'platform',
    width: 90,
    render: (value: string) =>
      value ? (
        // 用已有的 accent / purple token，不引新色板：
        // antd 的 `geekblue` 在暗色下是 rgb(82,115,224)，压在自己的浅底上
        // 只有 4.16:1（低于 AA 4.5，对比度门禁实测抓到）。token 是
        // 全站统一调过对比度的那套（见 `index.css` 的 tag 覆盖段）。
        <Tag color={value === 'grok' ? 'purple' : 'processing'} style={{ marginInlineEnd: 0 }}>
          {PLATFORM_LABELS[value] || value}
        </Tag>
      ) : (
        <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
      ),
  },
  {
    title: '邮箱',
    dataIndex: 'email',
    width: 240,
    render: (value: string, row) => (
      <Space direction="vertical" size={0}>
        <Typography.Text ellipsis style={{ maxWidth: 220 }}>{value}</Typography.Text>
        <Space size={4}>
          {row.local_id ? (
            <Tooltip title={`本地账号 id=${row.local_id}，状态 ${row.local_status || '未知'}`}>
              <Tag color={row.local_status === 'banned' ? 'error' : 'default'} style={{ marginInlineEnd: 0 }}>
                本地
              </Tag>
            </Tooltip>
          ) : null}
          {row.remote_id ? (
            <Tooltip title={`远端标识 ${row.remote_id}${row.remote_status ? `，状态 ${row.remote_status}` : ''}`}>
              <Tag color="blue" style={{ marginInlineEnd: 0 }}>远端</Tag>
            </Tooltip>
          ) : null}
          {row.local_id && !row.local_has_refresh_token ? (
            <Tooltip title="本地没有 refresh_token：远端刷新时可能换不到新凭证">
              <Tag color="warning" style={{ marginInlineEnd: 0 }}>无RT</Tag>
            </Tooltip>
          ) : null}
        </Space>
      </Space>
    ),
  },
  {
    title: '对比',
    dataIndex: 'state',
    width: 130,
    render: (value: string, row) => (
      <Tooltip
        title={
          row.state === 'synced'
            ? `凭证一致（比对了 ${row.credential_compared} 个字段）`
            : row.state === 'credential_diff'
              ? `不同的凭证字段：${(row.credential_differences || []).join('、') || '—'}`
              : row.state === 'unknown_credential'
                ? '远端接口不返回凭证（AT/RT），无法比对'
                : row.state === 'unknown_time'
                  ? '至少一边没有可用的更新时间'
                  : `按小时比较（不管分秒）：本地 ${row.local_updated_hour || '—'} / 远端 ${row.remote_updated_hour || '—'}`
        }
      >
        <Tag color={STATE_COLORS[value] || 'default'}>{row.label}</Tag>
      </Tooltip>
    ),
  },
  {
    // 显示 **AT 生成时间**（JWT iat）—— 记录更新时间会被「同步远端状态」等
    // 回写操作 touch 成噪声，AT 生成时间才是「凭证什么时候生成的」真实口径
    // （与方向判定的 time_relation 同一来源）。解不出 iat 时回落记录时间。
    title: '本地更新时间',
    dataIndex: 'local_updated_at',
    width: 150,
    render: (value: string, row) => {
      const issued = row.local_credential_issued_at || ''
      const shown = issued || value
      return (
        <Tooltip
          title={
            issued
              ? `本地 AT 生成时间（JWT iat）；按浏览器本地时区（${localTimezoneLabel()}）显示`
              : `本地没有可解析的 AT 生成时间，显示记录更新时间；按浏览器本地时区（${localTimezoneLabel()}）显示${
                  row.local_updated_hour ? `；小时档位 ${row.local_updated_hour}` : ''
                }`
          }
        >
          <span style={{ fontSize: 12 }}>{shortTime(shown)}</span>
        </Tooltip>
      )
    },
  },
  {
    // 同上：优先远端 AT 生成时间，解不出时回落远端记录更新时间。
    title: '远端更新时间',
    dataIndex: 'remote_updated_at',
    width: 150,
    render: (value: string, row) => {
      const issued = row.remote_credential_issued_at || ''
      const shown = issued || value
      return (
        <Tooltip
          title={
            issued
              ? `远端 AT 生成时间（JWT iat）；按浏览器本地时区（${localTimezoneLabel()}）显示`
              : `远端没有可解析的 AT 生成时间，显示记录更新时间（远端面板服务器时区已归一）；按浏览器本地时区（${localTimezoneLabel()}）显示${
                  row.remote_updated_at_raw ? `；远端原始值：${row.remote_updated_at_raw}` : ''
                }`
          }
        >
          <span style={{ fontSize: 12 }}>{shortTime(shown)}</span>
        </Tooltip>
      )
    },
  },
  {
    title: '远端信息',
    key: 'remote_extra',
    width: 220,
    render: (_: unknown, row) => {
      const extra = row.remote_extra || {}
      const plan = String(extra.plan_type || '')
      const until = String(extra.chatgpt_subscription_active_until || '')
      const disabled = Boolean(extra.disabled)
      const bits = [
        plan ? `套餐 ${plan}` : '',
        until ? `订阅至 ${shortTime(until)}` : '',
        disabled ? '已禁用' : '',
        row.remote_status ? `状态 ${row.remote_status}` : '',
      ].filter(Boolean)
      if (!bits.length) return <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
      return <span style={{ fontSize: 12 }}>{bits.join(' · ')}</span>
    },
  },
  {
    title: '凭证',
    key: 'credentials',
    width: 120,
    render: (_: unknown, row) => {
      // 凭证比对的结果直说：比了几个字段、哪几个不同
      if (row.state === 'credential_diff') {
        return (
          <Tooltip title={`不同的字段：${(row.credential_differences || []).join('、')}`}>
            <Tag color="error">{(row.credential_differences || []).length} 项不同</Tag>
          </Tooltip>
        )
      }
      if (row.state === 'synced') {
        return (
          <Tooltip title={`比对了 ${row.credential_compared} 个凭证字段，全部相同`}>
            <span style={{ color: 'var(--success, #389e0d)', fontSize: 12 }}>
              {row.credential_compared} 项相同
            </span>
          </Tooltip>
        )
      }
      if (row.state === 'unknown_credential') {
        return (
          <Tooltip title="远端接口不返回凭证（AT/RT），无法比对 —— 这不等于已同步">
            <Tag>比不了</Tag>
          </Tooltip>
        )
      }
      return <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
    },
  },
  {
    title: '时间',
    dataIndex: 'time_relation',
    width: 110,
    render: (value: string, row) => {
      const labels: Record<string, string> = {
        local_newer: '本地较新',
        remote_newer: '远端较新',
        time_synced: '同小时',
      }
      const text = labels[value] || ''
      if (!text) return <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
      const basisTip =
        row.time_basis === 'credential'
          ? '按凭证签发时间（JWT iat）比较 —— 记录时间会被状态回写等操作顶成噪声'
          : '按记录更新时间比较（凭证解不出签发时间时的兜底）'
      return (
        <Tooltip title={basisTip}>
          <span style={{ fontSize: 12 }}>{text}</span>
        </Tooltip>
      )
    },
  },
  {
    title: '差异',
    dataIndex: 'differences',
    width: 130,
    render: (value: string[]) =>
      value?.length ? (
        <Tooltip title={value.join('、')}>
          <Tag color="warning">{value.length} 项不一致</Tag>
        </Tooltip>
      ) : (
        <span style={{ color: 'var(--text-muted)', fontSize: 12 }}>—</span>
      ),
  },
]

export function PanelComparisonPanel({
  panelKey,
  panelLabel,
  platform = '',
  platforms = [],
  platformActions = {},
  syncAction = '',
}: {
  panelKey: string
  panelLabel: string
  /** 这个面板对应的平台（来自注册表），批量动作要用 */
  platform?: string
  /**
   * 面板涉及的**全部**平台（CPA 是 `['chatgpt', 'grok']`）。
   *
   * 与 `platform` 的区别：`platform` 是兼容字段（老消费方读它，取第一个平台），
   * 多平台面板要靠这个列表才知道该渲染平台选择器 —— 只看 `platform` 的话
   * CPA 永远只显示「chatgpt」一个平台，平台选择器就不会出现。
   */
  platforms?: string[]
  /**
   * 平台 → 「同步远端状态」动作 id 的映射（CPA 这类多平台面板用）。
   *
   * 面板同时服务多个平台时（CPA 托管 ChatGPT + Grok），同一个动作在两个平台上
   * 是两份实现、两条接口 —— 批量时必须按**行自己的平台**分发，不能全用第一个。
   */
  platformActions?: { sync?: Record<string, string> }
  /** 拉远端状态的动作 id（如 `sync_cliproxyapi_status`）；空串表示没有 */
  syncAction?: string
}) {
  const { message } = App.useApp()
  const [payload, setPayload] = useState<ComparisonPayload | null>(null)
  const [loading, setLoading] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [stateFilter, setStateFilter] = useState('')
  /**
   * 平台筛选（`''` = 全部）。只对多平台面板有意义（见 `shouldShowPlatformFilter`）。
   *
   * 它**同时**作用在三处：表格行、筛选条计数、批量动作的 id 集合 —— 只筛表格
   * 的话，用户看着 32 个 Grok 行点「上传」，实际会把 ChatGPT 的一起传上去。
   */
  const [platformFilter, setPlatformFilter] = useState('')
  // 操作进行中的标记（`sync-remote` / `push-to-remote` / `sync-from-remote`）
  const [running, setRunning] = useState('')

  const load = useCallback(
    async (refresh: boolean) => {
      if (refresh) setSyncing(true)
      else setLoading(true)
      try {
        const query = refresh ? '?refresh=1' : ''
        const data = (await apiFetch(
          `/integrations/panels/${panelKey}/comparison${query}`,
        )) as ComparisonPayload
        setPayload(data)
        if (refresh) message.success('已同步到最新')
      } catch (e: unknown) {
        message.error(e instanceof Error ? e.message : '加载对比数据失败')
      } finally {
        setLoading(false)
        setSyncing(false)
      }
    },
    [panelKey, message],
  )

  useEffect(() => {
    setPayload(null)
    setStateFilter('')
    setPlatformFilter('')
    void load(false)
  }, [load])

  /** 平台筛选后的行 —— 表格、筛选条计数、批量动作都读它（只筛表格会「看到的
   * 和传出去的不是同一批」）。 */
  const platformRows = useMemo(
    () => filterRowsByPlatform(payload?.rows || [], platformFilter),
    [payload, platformFilter],
  )

  const rows = useMemo(() => {
    if (!stateFilter) return platformRows
    return platformRows.filter((row) => row.state === stateFilter)
  }, [platformRows, stateFilter])

  // 计数在前端重算：平台筛选后统计条的数字必须跟着变（读后端全量值会「筛了
  // Grok 还显示 181」）。
  const summary = useMemo(() => summarizeRows(platformRows), [platformRows])
  const platformCounts = useMemo(() => countByPlatform(payload?.rows || []), [payload])
  const stateLabels = payload?.labels || {}
  const fetchTime = shortTime(payload?.fetched_at || '')
  /** 远端读取失败：此时所有本地账号都退化成 `local_only`，推送方向判定失效
   * （会把全部本地账号重传）—— 推送按钮据此禁用。 */
  const remoteUnavailable = Boolean(payload?.remote_error)

  /** 更新远程凭证的目标（推送方向，见 lib 的 `selectPushIds`）。 */
  const pushIds = useMemo(
    () => selectPushIds(platformRows),
    [platformRows],
  )
  /** 更新本地凭证的目标（拉回方向，与 `pushIds` 互斥）。 */
  const pullIds = useMemo(
    () => selectPullIds(platformRows),
    [platformRows],
  )

  /** 跑「同步远端状态」批量动作：按**每个账号自己的平台**分发接口
   * （CPA 同时托管 ChatGPT + Grok，用错平台的接口会报「账号不存在」）。 */
  const runBatch = useCallback(
    async (
      fallbackActionId: string,
      actionLabel: string,
      accountIds: number[],
      key: string,
    ) => {
      if (!accountIds.length) {
        message.info(`没有需要${actionLabel}的账号`)
        return
      }
      const actionFor = (plat: string) =>
        platformActions.sync?.[plat] || (plat === platform ? fallbackActionId : '')
      const idsByPlatform = new Map<string, number[]>()
      for (const row of payload?.rows || []) {
        if (!row.local_id || !accountIds.includes(row.local_id)) continue
        const plat = row.platform || platform
        if (!actionFor(plat)) continue
        const list = idsByPlatform.get(plat) || []
        list.push(row.local_id)
        idsByPlatform.set(plat, list)
      }
      if (!idsByPlatform.size) return

      setRunning(key)
      const toastKey = `panel-action:${key}`
      message.loading({ content: `${actionLabel}进行中（${accountIds.length} 个）...`, key: toastKey, duration: 0 })
      try {
        let total = 0, success = 0, failed = 0
        for (const [plat, ids] of idsByPlatform) {
          const actionId = actionFor(plat)
          // 后端单次上限 1000（`api/actions.py` 的 `_resolve_batch_accounts`）
          const chunks: number[][] = []
          for (let i = 0; i < ids.length; i += BATCH_ACTION_LIMIT) {
            chunks.push(ids.slice(i, i + BATCH_ACTION_LIMIT))
          }
          for (const chunk of chunks) {
            const result = (await apiFetch(`/actions/${plat}/${actionId}/batch`, {
              method: 'POST',
              body: JSON.stringify({ account_ids: chunk, params: {} }),
            })) as { total: number; success: number; failed: number }
            total += result.total
            success += result.success
            failed += result.failed
          }
        }
        if (!failed) {
          message.success({ content: `${actionLabel}完成：成功 ${success} / ${total}`, key: toastKey })
        } else if (!success) {
          message.error({ content: `${actionLabel}失败：成功 0 / ${total}`, key: toastKey })
        } else {
          message.warning({ content: `${actionLabel}部分完成：成功 ${success} / ${total}`, key: toastKey })
        }
        await load(true)
      } catch (e: unknown) {
        message.error({ content: `${actionLabel}失败：${e instanceof Error ? e.message : e}`, key: toastKey })
      } finally {
        setRunning('')
      }
    },
    [platform, platformActions, payload, message, load],
  )

  /**
   * 「同步到最新」：把远端较新的凭证拉回本地。
   *
   * 与 `runBatch` 不同 —— 这不是按账号发平台动作，而是**整面板**的一次调用
   * （`POST /integrations/panels/{key}/sync`）：方向判定（谁较新）与写库都在
   * 后端做，前端只展示汇总。原因：凭证比对的结果（哪些行 remote_newer）
   * 后端手里才有完整上下文，逐账号发动作会 N 次拉远端。
   */
  /**
   * 「更新本地凭证」：把远端较新的凭证拉回本地。
   *
   * 面板级调用（`POST .../sync`）：方向判定与写库都在后端做，前端只展示
   * 汇总。`platform` 带当前筛选 —— 后端按它只拉该平台的行。
   */
  const runRemoteSync = useCallback(async () => {
    setRunning('sync-from-remote')
    const toastKey = 'panel-sync-from-remote'
    message.loading({ content: '更新本地凭证中（拉取远端较新的凭证）...', key: toastKey, duration: 0 })
    try {
      const query = platformFilter ? `?platform=${encodeURIComponent(platformFilter)}` : ''
      const result = (await apiFetch(`/integrations/panels/${panelKey}/sync${query}`, {
        method: 'POST',
      })) as { pulled: number; skipped: number; total: number; remote_error?: string }
      if (result.remote_error) {
        message.error({ content: `更新本地失败：${result.remote_error}`, key: toastKey })
      } else if (result.pulled) {
        message.success({
          content: `已从远端拉回 ${result.pulled} 个账号的凭证（其余 ${result.skipped} 个无需更新）`,
          key: toastKey,
        })
      } else {
        message.info({
          content: `没有需要更新的账号（${result.skipped} 个无需更新）`,
          key: toastKey,
        })
      }
      await load(true)
    } catch (e: unknown) {
      message.error({ content: `更新本地失败：${e instanceof Error ? e.message : e}`, key: toastKey })
    } finally {
      setRunning('')
    }
  }, [panelKey, platformFilter, message, load])

  /** 更新远程凭证：未上传 + 本地较新的推上去（面板级 `POST .../push`，方向
   * 判定在后端）。`delete_old=true`：新建式面板推成功后清理旧记录。 */
  const runRemotePush = useCallback(async () => {
    setRunning('push-to-remote')
    const toastKey = 'panel-push-to-remote'
    message.loading({ content: '更新远程凭证中（推送本地较新的凭证）...', key: toastKey, duration: 0 })
    try {
      const params = new URLSearchParams({ delete_old: 'true' })
      if (platformFilter) params.set('platform', platformFilter)
      const result = (await apiFetch(`/integrations/panels/${panelKey}/push?${params.toString()}`, {
        method: 'POST',
      })) as { pushed: number; deleted: number; failed: number; skipped: number; total: number; remote_error?: string }
      if (result.remote_error) {
        message.error({ content: `更新远程失败：${result.remote_error}`, key: toastKey })
      } else if (result.pushed) {
        const deleted = result.deleted ? `，已清理 ${result.deleted} 条旧记录` : ''
        const failed = result.failed ? `，失败 ${result.failed} 个` : ''
        const content = `已推送 ${result.pushed} 个账号到远端${deleted}${failed}`
        if (result.failed) {
          message.warning({ content, key: toastKey })
        } else {
          message.success({ content, key: toastKey })
        }
      } else if (result.failed) {
        // 推了但全失败（配置缺失/网络错误）—— 不能报「无需更新」。
        message.error({ content: `更新远程失败：${result.failed} 个账号推送失败`, key: toastKey })
      } else {
        message.info({
          content: `没有需要推送的账号（${result.skipped} 个无需更新）`,
          key: toastKey,
        })
      }
      await load(true)
    } catch (e: unknown) {
      message.error({ content: `更新远程失败：${e instanceof Error ? e.message : e}`, key: toastKey })
    } finally {
      setRunning('')
    }
  }, [panelKey, platformFilter, message, load])

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 10 }}>
        <Space wrap>
          <Typography.Text strong>{panelLabel} · 本地 ↔ 远程对比</Typography.Text>
          {fetchTime ? (
            <Tooltip title={`数据拉取时间：${payload?.fetched_at}${payload?.cached ? '（来自缓存）' : ''}`}>
              <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                同步时间 {fetchTime}
              </Typography.Text>
            </Tooltip>
          ) : null}
          <Typography.Text type="secondary" style={{ fontSize: 12 }}>
            本地 {payload?.local_count ?? 0} · 远端 {payload?.remote_count ?? 0}
          </Typography.Text>
          {/* 时区标注：时间列统一按浏览器本地时区显示（后端已归一）。 */}
          <Tooltip title="所有时间列均按浏览器本地时区显示（后端已把本地 UTC 与远端面板时间归一后比较）">
            <Typography.Text type="secondary" style={{ fontSize: 12 }}>
              时区 {localTimezoneLabel()}
            </Typography.Text>
          </Tooltip>
        </Space>
        {/* 按钮组要允许换行：窄屏下按钮一排超宽，不 wrap 会被容器裁掉。 */}
        <Space wrap>
          {syncAction || Object.keys(platformActions.sync || {}).length ? (
            <Tooltip title="读远端状态回写本地（含封禁/失效判定）">
              <Button
                icon={<SyncOutlined />}
                loading={running === 'sync-remote'}
                disabled={Boolean(running)}
                onClick={() => {
                  // 当前平台筛选下的全部本地账号都要同步 —— 不是「只处理某一档」
                  // 的动作。读 `platformRows`：筛了 Grok 就只同步 Grok 的账号。
                  const ids = platformRows
                    .filter((row) => row.local_id)
                    .map((row) => row.local_id as number)
                  void runBatch(syncAction, '同步远端状态', ids, 'sync-remote')
                }}
                data-hermes-action="sync-remote-status"
              >
                同步远端状态
              </Button>
            </Tooltip>
          ) : null}
          {/* 更新远程凭证：未上传 + 本地较新的推上去（推送方向）。 */}
          <Tooltip title="推送「未上传 + 本地较新」的凭证；远端较新的用「更新本地凭证」">
            <Popconfirm
              title={
                pushIds.length
                  ? `把 ${pushIds.length} 个账号的凭证推送到 ${panelLabel}？`
                  : '按对比结果推送：只处理「未上传」与「本地较新」的账号'
              }
              description="未上传的补传；本地较新的覆盖远端。同小时/无法判定时间的不动。"
              okText="更新"
              cancelText="取消"
              disabled={Boolean(running) || remoteUnavailable}
              onConfirm={() => void runRemotePush()}
            >
              <Button
                type="primary"
                icon={<CloudUploadOutlined />}
                loading={running === 'push-to-remote'}
                disabled={Boolean(running) || remoteUnavailable}
                data-hermes-action="push-to-remote"
              >
                更新远程凭证{pushIds.length ? ` (${pushIds.length})` : ''}
              </Button>
            </Popconfirm>
          </Tooltip>
          {/* 更新本地凭证：远端较新的拉回来（拉回方向）。 */}
          <Tooltip title="拉回「远端较新」的凭证（AT/RT）—— 覆盖本地凭证字段">
            <Popconfirm
              title={
                pullIds.length
                  ? `把 ${pullIds.length} 个「远端较新」账号的凭证拉回本地？`
                  : '按对比结果同步：只拉回「远端较新」的账号'
              }
              description="会覆盖本地账号的 access_token / refresh_token 等凭证字段（其它字段保留）。"
              okText="更新"
              cancelText="取消"
              disabled={Boolean(running) || remoteUnavailable}
              onConfirm={() => void runRemoteSync()}
            >
              <Button
                icon={<CloudDownloadOutlined />}
                loading={running === 'sync-from-remote'}
                disabled={Boolean(running) || remoteUnavailable}
                data-hermes-action="sync-from-remote"
              >
                更新本地凭证{pullIds.length ? ` (${pullIds.length})` : ''}
              </Button>
            </Popconfirm>
          </Tooltip>
          <Tooltip title="重新拉取账号清单（绕缓存，不改任何账号）">
            <Button
              icon={<SyncOutlined />}
              loading={syncing}
              onClick={() => void load(true)}
              data-hermes-action="sync-latest"
            >
              重新拉取对比
            </Button>
          </Tooltip>
        </Space>
      </div>

      {payload?.remote_error ? (
        <div
          style={{
            padding: '8px 12px',
            borderRadius: 8,
            fontSize: 12,
            background: 'var(--bg-subtle)',
            color: 'var(--text-muted)',
          }}
        >
          远端读取失败：{payload.remote_error} —— 下方只显示本地账号
        </div>
      ) : null}

      {/* 平台选择（只对多平台面板出现：CPA 同时托管 ChatGPT 与 Grok）。
          单平台面板不渲染 —— 只有「全部」一个选项的选择器是纯噪声。
          选中的平台**同时**作用于表格、下面的状态筛选计数与批量动作按钮。 */}
      {shouldShowPlatformFilter(platforms) ? (
        <Space wrap size={8}>
          <Segmented
            value={platformFilter}
            onChange={(value) => setPlatformFilter(String(value))}
            options={[
              { value: '', label: `全部 ${platformCounts.total ?? 0}` },
              ...platforms.map((item) => ({
                value: item,
                label: `${PLATFORM_LABELS[item] || item} ${platformCounts[item] ?? 0}`,
              })),
            ]}
            aria-label="按平台筛选"
          />
        </Space>
      ) : null}

      <Space wrap size={6}>
        <FilterTag
          label={`全部 ${summary.total ?? 0}`}
          checked={stateFilter === ''}
          onSelect={() => setStateFilter('')}
        />
        {STATE_ORDER.map((state) => (
          <FilterTag
            key={state}
            label={`${stateLabels[state] || state} ${summary[state] ?? 0}`}
            checked={stateFilter === state}
            onSelect={() => setStateFilter(state)}
          />
        ))}
      </Space>

      <Spin spinning={loading}>
        {rows.length ? (
          <Table
            // key 必须带平台：同一个邮箱在 CPA 上可能同时有 codex 与 xai 两条
            // （邮箱池按平台消耗），选「全部」时两行的 email 相同 —— 只用
            // email 当 key 会被 React 认成同一个节点（渲染错行 + key 冲突警告）。
            rowKey={(row) => `${row.platform || ''}|${row.email}`}
            columns={COLUMNS}
            dataSource={rows}
            size="small"
            pagination={{ pageSize: 20, showSizeChanger: true, pageSizeOptions: ['20', '50', '100'] }}
            // scroll.x 必须 ≥ 各列宽度之和，否则 antd 会等比压缩所有列 ——
            // 实测 1000 < 列宽和时，唯一的无宽度列（远端信息）被挤到 30px，
            // 表头 4 个字竖排成 94px 高、整行被撑到 62px。
            // 各列：平台 90 + 邮箱 240 + 对比 130 + 本地 150 + 远端 150 +
            // 远端信息 220 + 凭证 120 + 时间 110 + 差异 130 = 1340。
            // 改任一列宽时把这个数一起改。
            scroll={{ x: 1340 }}
          />
        ) : (
          <Empty
            description={
              payload
                ? '没有符合条件的账号'
                : '用左侧「面板管理」下的子项切换面板查看对比'
            }
          />
        )}
      </Spin>
    </div>
  )
}

export default PanelComparisonPanel
